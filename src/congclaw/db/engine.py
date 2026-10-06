"""SQLite 异步引擎与建库逻辑。

路径优先级：``DB_PATH`` 环境变量（.env）> 默认 ``data/telecom_cs.db``。
相对路径统一解析到项目根目录（含 ``pyproject.toml`` 或 ``.git`` 的目录）。
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import aiosqlite
from dotenv import load_dotenv
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from congclaw.core.paths import find_project_root
from congclaw.core.security import DEFAULT_PASSWORD, hash_password
from congclaw.db.baseline import backfill_account_baselines

DEFAULT_DB_PATH = Path("data") / "telecom_cs.db"
SCHEMA_FILE = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = "0"

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def resolve_db_path() -> Path:
    """解析 SQLite 数据库文件的绝对路径。"""
    load_dotenv()
    raw = os.getenv("DB_PATH", "").strip()
    path = Path(raw) if raw else DEFAULT_DB_PATH
    if not path.is_absolute():
        path = find_project_root() / path
    return path


def database_url(db_path: Path | None = None) -> str:
    """生成 aiosqlite 异步连接串。"""
    path = Path(db_path or resolve_db_path())
    return "sqlite+aiosqlite:///" + path.resolve().as_posix()


def _attach_pragmas(engine: AsyncEngine) -> None:
    """在每个底层连接建立时设置 SQLite PRAGMA。"""

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            # WAL 是数据库级持久属性，重复设置也安全
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()


def get_engine(db_path: Path | None = None) -> AsyncEngine:
    """获取进程内缓存的异步引擎（首次调用时创建）。"""
    global _engine, _session_factory
    if _engine is None:
        engine = create_async_engine(database_url(db_path), future=True)
        _attach_pragmas(engine)
        _engine = engine
        _session_factory = async_sessionmaker(engine, expire_on_commit=False)
    return _engine


# 阶段 6：登录鉴权。account 表补齐 username / password_hash 列。
ACCOUNT_AUTH_COLUMNS = (
    ("username", "TEXT NOT NULL DEFAULT ''"),
    ("password_hash", "TEXT NOT NULL DEFAULT ''"),
)


async def _account_columns(connection: aiosqlite.Connection) -> set[str]:
    """读取 account 表现有列名；表不存在时返回空集合。"""
    cursor = await connection.execute("PRAGMA table_info(account)")
    return {row[1] for row in await cursor.fetchall()}


async def _add_account_auth_columns(connection: aiosqlite.Connection) -> None:
    """为已存在的 account 表补齐鉴权列（须在建表脚本之前调用）。

    schema.sql 中演示种子的 INSERT 已引用 ``username`` 列，存量库若缺列必须先
    ALTER 补上，否则 ``executescript`` 会以
    「table account has no column named username」中断建库。
    表尚不存在（空集合）时直接返回，交由 schema.sql 建表。
    """
    columns = await _account_columns(connection)
    if not columns:
        return
    for name, ddl in ACCOUNT_AUTH_COLUMNS:
        if name not in columns:
            await connection.execute(f"ALTER TABLE account ADD COLUMN {name} {ddl}")


async def _migrate_account_auth(connection: aiosqlite.Connection) -> None:
    """回填账号鉴权数据（幂等，可重复执行）。

    1. 存量账号回填 ``username = owner_name``，重复用户名按号码后缀去重；
    2. 所有空口令账号回填默认口令 ``123456``（逐条独立加盐哈希）；
    3. 建立用户名的唯一索引，保证「用户名 → 唯一账号」。
    """
    await _add_account_auth_columns(connection)

    # 存量账号：用户名默认取机主姓名
    await connection.execute(
        "UPDATE account SET username = owner_name WHERE username = '' AND owner_name <> ''"
    )

    # 用户名去重：同名账号仅保留号码最小者，其余追加号码后缀，避免唯一索引创建失败
    cursor = await connection.execute(
        "SELECT username FROM account WHERE username <> ''"
        " GROUP BY username HAVING COUNT(*) > 1"
    )
    duplicates = [row[0] for row in await cursor.fetchall()]
    for name in duplicates:
        cursor = await connection.execute(
            "SELECT phone FROM account WHERE username = ? ORDER BY phone", (name,)
        )
        for (phone,) in (await cursor.fetchall())[1:]:
            await connection.execute(
                "UPDATE account SET username = ? WHERE phone = ?",
                (f"{name}_{str(phone)[-4:]}", phone),
            )

    # 空口令账号回填默认口令（每条单独加盐，避免共用同一哈希）
    cursor = await connection.execute("SELECT phone FROM account WHERE password_hash = ''")
    for (phone,) in await cursor.fetchall():
        await connection.execute(
            "UPDATE account SET password_hash = ? WHERE phone = ?",
            (hash_password(DEFAULT_PASSWORD), phone),
        )

    await connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_account_username"
        " ON account(username) WHERE username <> ''"
    )


async def init_db(db_path: Path | None = None) -> Path:
    """幂等创建数据库文件并执行 schema.sql，返回数据库路径。

    使用底层 aiosqlite 的 ``executescript`` 执行多语句 DDL；
    ``CREATE TABLE IF NOT EXISTS`` 保证可重复调用，随后执行列级迁移补齐存量库缺失的列。
    """
    path = Path(db_path or resolve_db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    schema_sql = SCHEMA_FILE.read_text(encoding="utf-8")

    async with aiosqlite.connect(path) as connection:
        await connection.execute("PRAGMA journal_mode=WAL")
        await connection.execute("PRAGMA foreign_keys=ON")
        # 建表脚本内已有引用新列的种子 INSERT，存量库必须先补列
        await _add_account_auth_columns(connection)
        await connection.executescript(schema_sql)
        # 阶段 2：旧库迁移——session 表补齐跨轮计数列（CREATE IF NOT EXISTS 不会改已存在的表）
        cursor = await connection.execute("PRAGMA table_info(session)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "clarify_count" not in columns:
            await connection.execute("ALTER TABLE session ADD COLUMN clarify_count INTEGER NOT NULL DEFAULT 0")
        if "unknown_count" not in columns:
            await connection.execute("ALTER TABLE session ADD COLUMN unknown_count INTEGER NOT NULL DEFAULT 0")
        # 阶段 6：登录鉴权所需的账号口令回填与用户名唯一索引
        await _migrate_account_auth(connection)
        # 阶段 6：开户基线——为缺少 user_package / user_profile 的存量账号补齐，
        # 保证新注册与历史账号都能使用套餐查询、套餐办理等全部业务操作
        await backfill_account_baselines(connection)
        await connection.commit()

    return path


@asynccontextmanager
async def session_scope(db_path: Path | None = None) -> AsyncIterator[AsyncSession]:
    """事务型异步会话上下文：正常退出提交，异常回滚。"""
    if _session_factory is None:
        get_engine(db_path)
    assert _session_factory is not None
    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """释放缓存的引擎连接池（主要用于测试与重启）。"""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
