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


async def init_db(db_path: Path | None = None) -> Path:
    """幂等创建数据库文件并执行 schema.sql，返回数据库路径。

    使用底层 aiosqlite 的 ``executescript`` 执行多语句 DDL；
    ``CREATE TABLE IF NOT EXISTS`` 保证可重复调用。
    """
    path = Path(db_path or resolve_db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    schema_sql = SCHEMA_FILE.read_text(encoding="utf-8")

    async with aiosqlite.connect(path) as connection:
        await connection.execute("PRAGMA journal_mode=WAL")
        await connection.execute("PRAGMA foreign_keys=ON")
        await connection.executescript(schema_sql)
        # 阶段 2：旧库迁移——session 表补齐跨轮计数列（CREATE IF NOT EXISTS 不会改已存在的表）
        cursor = await connection.execute("PRAGMA table_info(session)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "clarify_count" not in columns:
            await connection.execute("ALTER TABLE session ADD COLUMN clarify_count INTEGER NOT NULL DEFAULT 0")
        if "unknown_count" not in columns:
            await connection.execute("ALTER TABLE session ADD COLUMN unknown_count INTEGER NOT NULL DEFAULT 0")
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
