"""账号鉴权仓储：登录、注册与账号检索。

沿用 :mod:`congclaw.skills.business_store` 的 aiosqlite 短连接风格：演示场景
读多写少，短连接天然规避「连接跨异步循环」问题。

登录账号支持两种标识：
- **手机号**：``account.phone``，同时是业务数据与会话隔离的维度；
- **用户名**：``account.username``，注册时可为中文/字母/数字/下划线。

安全约定：本模块对外只返回 ``{phone, username, owner_name}`` 这类公开字段，
任何情况下都不会把 ``password_hash`` 交给调用方。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import aiosqlite

from congclaw.core.security import (
    MIN_PASSWORD_LENGTH,
    hash_password,
    verify_password,
)
from congclaw.db.baseline import DEFAULT_PACKAGE_ID, provision_account_baseline
from congclaw.db.engine import init_db, resolve_db_path
from congclaw.skills.business_store import utc_now

# 手机号：11 位、1 开头、第二位 3-9
PHONE_PATTERN = re.compile(r"^1[3-9]\d{9}$")
# 用户名：中文 / 字母 / 数字 / 下划线，2-20 位
USERNAME_PATTERN = re.compile(r"^[\u4e00-\u9fa5A-Za-z0-9_]{2,20}$")


class AuthError(Exception):
    """鉴权/注册领域错误，由 API 层转换为对应的 HTTP 状态码。"""

    def __init__(self, message: str, *, code: str = "auth_error", status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


def _connect() -> aiosqlite.core.Connection:
    return aiosqlite.connect(resolve_db_path())


def _public_user(row: Any) -> dict[str, Any]:
    """裁剪账号行，只保留可对外暴露的字段。"""
    return {
        "phone": row["phone"],
        "username": row["username"] or "",
        "owner_name": row["owner_name"] or "",
    }


async def search_accounts(keyword: str = "", *, limit: int = 8) -> list[dict[str, Any]]:
    """按关键词检索已注册账号，供登录框的搜索提示使用。

    匹配范围为手机号 / 用户名 / 机主姓名；关键词为空时返回最近注册的若干账号。
    """
    await init_db()
    word = (keyword or "").strip()
    sql = (
        "SELECT phone, username, owner_name FROM account"
        if not word
        else "SELECT phone, username, owner_name FROM account"
        " WHERE phone LIKE ? OR username LIKE ? OR owner_name LIKE ?"
    )
    params: list[Any] = [] if not word else [f"%{word}%"] * 3
    sql += " ORDER BY updated_at DESC, phone LIMIT ?"
    params.append(int(limit))
    async with _connect() as connection:
        connection.row_factory = aiosqlite.Row
        cursor = await connection.execute(sql, params)
        rows = await cursor.fetchall()
    return [_public_user(row) for row in rows]


async def _find_by_identifier(connection: aiosqlite.core.Connection, account: str) -> Any:
    """按「手机号或用户名」定位账号行（精确匹配）。"""
    connection.row_factory = aiosqlite.Row
    cursor = await connection.execute(
        "SELECT phone, username, owner_name, password_hash FROM account"
        " WHERE phone = ? OR username = ? LIMIT 1",
        (account, account),
    )
    return await cursor.fetchone()


async def login(account: str, password: str) -> dict[str, Any]:
    """校验账号口令，成功返回公开用户信息。

    账号不存在与口令错误返回同一提示，避免通过错误文案枚举账号。
    """
    await init_db()
    identifier = (account or "").strip()
    if not identifier or not password:
        raise AuthError("请输入登录账号和密码。", code="missing_credential")

    async with _connect() as connection:
        row = await _find_by_identifier(connection, identifier)

    # 口令校验放在连接外执行：PBKDF2 计算量较大，不必占用数据库连接
    if row is None or not verify_password(password, row["password_hash"] or ""):
        raise AuthError("账号或密码不正确，请重新输入。", code="invalid_credential", status_code=401)
    return _public_user(row)


async def register(
    *,
    phone: str,
    username: str = "",
    password: str,
    owner_name: str = "",
) -> dict[str, Any]:
    """注册新账号并同步写入数据库。

    校验通过后在 ``account`` 表插入一行（即「新注册用户同步数据库」），
    并在**同一事务内**补齐开户基线数据：一条 ``user_package``（默认档位套餐）
    与一条 ``user_profile``（长期记忆锚点）。

    这样新注册用户与演示账号处于同样的数据起点，可直接使用全部业务操作
    （余额查询、套餐查询、套餐办理、故障报修与工单查询），并独立拥有
    自己的会话工作区与跨会话记忆。
    """
    await init_db()
    phone = (phone or "").strip()
    name = (username or "").strip() or (owner_name or "").strip() or phone
    display_name = (owner_name or "").strip() or name

    if not PHONE_PATTERN.match(phone):
        raise AuthError("请输入正确的 11 位手机号。", code="invalid_phone")
    if not USERNAME_PATTERN.match(name):
        raise AuthError("用户名需为 2-20 位中文、字母、数字或下划线。", code="invalid_username")
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise AuthError(
            f"密码至少 {MIN_PASSWORD_LENGTH} 位，请重新设置。", code="invalid_password"
        )

    now = utc_now()
    bill_cycle = datetime.now().strftime("%Y-%m")
    async with _connect() as connection:
        connection.row_factory = aiosqlite.Row
        cursor = await connection.execute(
            "SELECT phone, username FROM account WHERE phone = ? OR username = ? LIMIT 1",
            (phone, name),
        )
        exists = await cursor.fetchone()
        if exists is not None:
            if exists["phone"] == phone:
                raise AuthError("该手机号已注册，请直接登录。", code="phone_taken", status_code=409)
            raise AuthError("该用户名已被使用，请更换。", code="username_taken", status_code=409)

        await connection.execute(
            "INSERT INTO account"
            " (phone, owner_name, username, password_hash, balance, real_time_fee,"
            "  bill_cycle, updated_at)"
            " VALUES (?, ?, ?, ?, 0.0, 0.0, ?, ?)",
            (phone, display_name, name, hash_password(password), bill_cycle, now),
        )
        # 开户基线：套餐 + 长期记忆锚点，与账号写入同一事务，避免半开户状态
        baseline = await provision_account_baseline(
            connection, phone, owner_name=display_name, package_id=DEFAULT_PACKAGE_ID
        )
        await connection.commit()

    return {
        "phone": phone,
        "username": name,
        "owner_name": display_name,
        "provisioned": baseline,
    }


async def get_public_user(phone: str) -> dict[str, Any] | None:
    """按号码查询公开用户信息（不存在返回 None）。"""
    await init_db()
    async with _connect() as connection:
        connection.row_factory = aiosqlite.Row
        cursor = await connection.execute(
            "SELECT phone, username, owner_name FROM account WHERE phone = ? LIMIT 1",
            ((phone or "").strip(),),
        )
        row = await cursor.fetchone()
    return _public_user(row) if row is not None else None
