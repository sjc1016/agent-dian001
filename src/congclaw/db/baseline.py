"""新开户业务基线数据（套餐 + 长期记忆锚点）。

一张电信号码除 ``account``（账户余额/话费）外，还依赖两张按号码维度的业务表：

- ``user_package``：当前在网套餐与余量。套餐查询（``query_package``）与
  套餐办理（``change_package``）都以该行存在为前提，缺行会导致业务直接失败；
- ``user_profile``：跨会话长期记忆锚点（阶段 5）。

新注册账号与部分历史账号可能只有 ``account`` 行而缺少上述数据，本模块提供统一的
「开户基线」补齐逻辑：一律使用 ``INSERT OR IGNORE``，**只补缺失、绝不覆盖已有数据**。

放在 ``db`` 层是为了让建库迁移（:mod:`congclaw.db.engine`）与业务写入
（:mod:`congclaw.skills.business_store`）共用同一套 SQL，避免模块循环导入。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import aiosqlite

# 新开户默认套餐：目录中最基础的 5G 档位（见 schema.sql 种子数据）
DEFAULT_PACKAGE_ID = "P129"
DEFAULT_PACKAGE_NAME = "5G畅享129元档"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _current_bill_cycle() -> str:
    return datetime.now().strftime("%Y-%m")


async def _resolve_package_name(connection: aiosqlite.Connection, package_id: str) -> str:
    """取套餐名；目录中查不到时回退到默认档位名称。"""
    cursor = await connection.execute(
        "SELECT name FROM package_catalog WHERE package_id = ? LIMIT 1", (package_id,)
    )
    row = await cursor.fetchone()
    if row is None or not row[0]:
        return DEFAULT_PACKAGE_NAME
    return str(row[0])


async def _has_row(connection: aiosqlite.Connection, table: str, phone: str) -> bool:
    cursor = await connection.execute(
        f"SELECT 1 FROM {table} WHERE phone = ? LIMIT 1", (phone,)
    )
    return await cursor.fetchone() is not None


async def provision_account_baseline(
    connection: aiosqlite.Connection,
    phone: str,
    *,
    owner_name: str = "",
    package_id: str = DEFAULT_PACKAGE_ID,
) -> dict[str, Any]:
    """为号码补齐开户基线数据（幂等，只补缺失）。

    需调用方传入已打开的 aiosqlite 连接并自行 ``commit``，以便与注册事务合并。
    返回本次实际补齐了哪些数据，供调用方记录：
    ``{"package_created": bool, "profile_created": bool, "bill_cycle_filled": bool}``
    """
    phone = (phone or "").strip()
    if not phone:
        raise ValueError("provision_account_baseline 需要有效的 phone")

    now = _utc_now()
    name = (owner_name or "").strip()

    # 1) 当前套餐：缺失时按默认档位开户，保证套餐查询/办理可用
    package_created = False
    if not await _has_row(connection, "user_package", phone):
        package_name = await _resolve_package_name(connection, package_id)
        cursor = await connection.execute(
            "INSERT OR IGNORE INTO user_package"
            " (phone, package_id, package_name, data_used_gb, voice_used_min,"
            "  effective_date, updated_at)"
            " VALUES (?, ?, ?, 0.0, 0, ?, ?)",
            (phone, package_id, package_name, datetime.now().strftime("%Y-%m-%d"), now),
        )
        package_created = bool(cursor.rowcount)

    # 2) 长期记忆锚点：缺失时补空档案，供跨会话摘要按 phone 装载
    profile_created = False
    if not await _has_row(connection, "user_profile", phone):
        cursor = await connection.execute(
            "INSERT OR IGNORE INTO user_profile"
            " (phone, owner_name, summary, topics, open_tickets, preferred_package,"
            "  turn_count, last_session_workspace, created_at, updated_at)"
            " VALUES (?, ?, '', '[]', '[]', '', 0, '', ?, ?)",
            (phone, name, now, now),
        )
        profile_created = bool(cursor.rowcount)

    # 3) 账户账期：历史账号可能为空，补齐为本月，避免账单相关展示缺值
    bill_cycle_filled = False
    cursor = await connection.execute(
        "UPDATE account SET bill_cycle = ? WHERE phone = ? AND bill_cycle = ''",
        (_current_bill_cycle(), phone),
    )
    bill_cycle_filled = int(cursor.rowcount or 0) > 0

    return {
        "package_created": package_created,
        "profile_created": profile_created,
        "bill_cycle_filled": bill_cycle_filled,
    }


async def backfill_account_baselines(
    connection: aiosqlite.Connection, *, package_id: str = DEFAULT_PACKAGE_ID
) -> dict[str, int]:
    """为所有缺少开户基线数据的存量账号补齐（建库迁移时调用，幂等）。"""
    cursor = await connection.execute("SELECT phone, owner_name FROM account ORDER BY phone")
    accounts = list(await cursor.fetchall())

    packages_created = 0
    profiles_created = 0
    for phone, owner_name in accounts:
        result = await provision_account_baseline(
            connection, str(phone), owner_name=str(owner_name or ""), package_id=package_id
        )
        packages_created += int(bool(result["package_created"]))
        profiles_created += int(bool(result["profile_created"]))

    return {
        "accounts": len(accounts),
        "packages_created": packages_created,
        "profiles_created": profiles_created,
    }
