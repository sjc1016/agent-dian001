"""电信模拟业务的 SQLite 异步仓储（接口形态对齐真实 BOSS/CRM）。

与 :mod:`congclaw.rag.store` 一致采用 aiosqlite 短连接：演示场景读多写少，
短连接天然规避"连接跨事件循环"问题（agent 子图每轮在独立 asyncio 循环里驱动）。

数据来源：``db/schema.sql`` 中的 account / package_catalog / user_package /
fault_ticket / pending_approval 五张表与演示种子。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import aiosqlite

from congclaw.db.baseline import DEFAULT_PACKAGE_ID, provision_account_baseline
from congclaw.db.engine import init_db, resolve_db_path
from congclaw.skills.base import SkillError

# 演示环境的默认用户号码（用户未显式提供手机号时使用）
DEFAULT_DEMO_PHONE = "13800138000"

FAULT_TYPES = ("宽带故障", "手机网络故障", "通话故障", "IPTV故障", "其他")
TICKET_INITIAL_STATUS = "已受理"
TICKET_INITIAL_NOTE = "工单已受理，等待客服代表联系并安排维修。"


def _connect() -> aiosqlite.core.Connection:
    return aiosqlite.connect(resolve_db_path())


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def _ensure() -> None:
    await init_db()


def _money(value: Any) -> float:
    return round(float(value or 0.0), 2)


# ---------------------------------------------------------------------------
# 账户 / 套餐（只读）
# ---------------------------------------------------------------------------


async def get_account(phone: str) -> dict[str, Any]:
    """查询账户余额与本月实时话费；账户不存在抛 SkillError。"""
    await _ensure()
    async with _connect() as connection:
        connection.row_factory = aiosqlite.Row
        cursor = await connection.execute(
            "SELECT phone, owner_name, balance, real_time_fee, bill_cycle, updated_at"
            " FROM account WHERE phone = ?",
            (phone,),
        )
        row = await cursor.fetchone()
    if row is None:
        raise SkillError(f"未查询到号码 {phone} 的账户信息，请确认号码是否正确。", code="account_not_found")
    data = dict(row)
    data["balance"] = _money(data["balance"])
    data["real_time_fee"] = _money(data["real_time_fee"])
    return data


async def list_accounts(*, limit: int = 50) -> list[dict[str, Any]]:
    """列出全部在网账户（号码 + 用户名 + 机主姓名），供前端用户切换使用。

    用户身份即电信号码：同一号码在 ``user_profile`` 中拥有独立的长期记忆，
    在 ``session`` 表中拥有独立的工作区与会话记录。返回值不包含口令哈希。
    """
    await _ensure()
    async with _connect() as connection:
        connection.row_factory = aiosqlite.Row
        cursor = await connection.execute(
            "SELECT phone, username, owner_name FROM account ORDER BY phone LIMIT ?",
            (int(limit),),
        )
        rows = await cursor.fetchall()
    return [
        {
            "phone": row["phone"],
            "username": row["username"] or "",
            "owner_name": row["owner_name"] or "",
        }
        for row in rows
    ]


async def provision_account(
    phone: str, *, owner_name: str = "", package_id: str = DEFAULT_PACKAGE_ID
) -> dict[str, Any]:
    """为新开户或基线缺失的号码补齐业务数据（幂等，只补缺失）。

    补齐 ``user_package``（默认档位套餐）与 ``user_profile``（长期记忆锚点），
    使该号码与演示账号一样可正常使用套餐查询、套餐办理等全部业务操作。
    注册流程已在同一事务内直接调用底层函数，本入口供迁移与补偿场景使用。
    """
    await _ensure()
    async with _connect() as connection:
        result = await provision_account_baseline(
            connection, phone, owner_name=owner_name, package_id=package_id
        )
        await connection.commit()
    return result


async def get_user_package_detail(phone: str) -> dict[str, Any]:
    """查询用户当前套餐 + 余量（user_package 关联 package_catalog 计算）。"""
    await _ensure()
    async with _connect() as connection:
        connection.row_factory = aiosqlite.Row
        cursor = await connection.execute(
            """
            SELECT up.phone, up.package_id, up.package_name, up.data_used_gb,
                   up.voice_used_min, up.effective_date,
                   pc.monthly_fee, pc.data_quota_gb, pc.voice_minutes,
                   pc.broadband_mbps, pc.description
              FROM user_package up
              LEFT JOIN package_catalog pc ON pc.package_id = up.package_id
             WHERE up.phone = ?
            """,
            (phone,),
        )
        row = await cursor.fetchone()
    if row is None:
        raise SkillError(f"未查询到号码 {phone} 的在网套餐信息。", code="package_not_found")
    data = dict(row)
    quota = float(data.get("data_quota_gb") or 0.0)
    used = float(data.get("data_used_gb") or 0.0)
    voice_quota = int(data.get("voice_minutes") or 0)
    voice_used = int(data.get("voice_used_min") or 0)
    data["data_used_gb"] = _money(used)
    data["data_quota_gb"] = _money(quota)
    data["data_remaining_gb"] = _money(max(0.0, quota - used))
    data["voice_remaining_min"] = max(0, voice_quota - voice_used)
    data["monthly_fee"] = _money(data.get("monthly_fee"))
    return data


async def list_packages(keyword: str = "", *, active_only: bool = True) -> list[dict[str, Any]]:
    """可办理套餐目录；keyword 在套餐名/档位描述上做模糊匹配。"""
    await _ensure()
    sql = (
        "SELECT package_id, name, monthly_fee, data_quota_gb, voice_minutes,"
        " broadband_mbps, description, active FROM package_catalog"
    )
    params: list[Any] = []
    if active_only:
        sql += " WHERE active = 1"
    if keyword:
        sql += " WHERE active = 1" if not active_only else " AND"
        sql += " name LIKE ? OR description LIKE ? OR package_id LIKE ?"
        like = f"%{keyword.strip()}%"
        params.extend([like, like, like])
    sql += " ORDER BY monthly_fee"
    async with _connect() as connection:
        connection.row_factory = aiosqlite.Row
        cursor = await connection.execute(sql, params)
        rows = await cursor.fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["monthly_fee"] = _money(item["monthly_fee"])
        item["data_quota_gb"] = _money(item["data_quota_gb"])
        result.append(item)
    return result


async def resolve_package(target: str) -> dict[str, Any]:
    """按 package_id 精确、名称包含（唯一命中）解析目标套餐。"""
    target = (target or "").strip()
    if not target:
        raise SkillError("缺少目标套餐信息（套餐名称或套餐编号）。", code="missing_package")
    async with _connect() as connection:
        connection.row_factory = aiosqlite.Row
        cursor = await connection.execute(
            "SELECT package_id, name, monthly_fee, data_quota_gb, voice_minutes,"
            " broadband_mbps, description, active FROM package_catalog"
            " WHERE active = 1 AND package_id = ?",
            (target,),
        )
        row = await cursor.fetchone()
        if row is None:
            cursor = await connection.execute(
                "SELECT package_id, name, monthly_fee, data_quota_gb, voice_minutes,"
                " broadband_mbps, description, active FROM package_catalog"
                " WHERE active = 1 AND name LIKE ? ORDER BY monthly_fee",
                (f"%{target}%",),
            )
            matches = await cursor.fetchall()
            if len(matches) == 1:
                row = matches[0]
            elif len(matches) > 1:
                names = "、".join(item["name"] for item in matches)
                raise SkillError(f"匹配到多个套餐：{names}，请告知具体档位名称。", code="ambiguous_package")
    if row is None:
        raise SkillError(f"未找到可办理的套餐「{target}」，可先查询可办理套餐列表。", code="package_not_found")
    item = dict(row)
    item["monthly_fee"] = _money(item["monthly_fee"])
    item["data_quota_gb"] = _money(item["data_quota_gb"])
    return item


# ---------------------------------------------------------------------------
# 故障工单（写 + 读）
# ---------------------------------------------------------------------------


def new_ticket_id(now: datetime | None = None) -> str:
    moment = now or datetime.now()
    return f"FT{moment.strftime('%Y%m%d%H%M%S')}{uuid4().hex[:4]}"


async def create_fault_ticket(
    *,
    phone: str,
    fault_type: str,
    description: str,
    address: str = "",
    contact: str = "",
) -> dict[str, Any]:
    """建故障报修工单，返回工单详情。"""
    await _ensure()
    # 号码必须是在网账户（模拟真实 BOSS 校验）
    account = await get_account(phone)
    if fault_type not in FAULT_TYPES:
        raise SkillError(f"故障类型仅支持：{'、'.join(FAULT_TYPES)}", code="invalid_fault_type")
    if not description.strip():
        raise SkillError("请描述故障现象（如断网时间、光猫指示灯情况）。", code="missing_description")
    ticket_id = new_ticket_id()
    now = utc_now()
    async with _connect() as connection:
        await connection.execute(
            "INSERT INTO fault_ticket"
            " (ticket_id, phone, fault_type, description, address, contact,"
            "  status, status_note, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ticket_id,
                phone,
                fault_type,
                description.strip(),
                address.strip(),
                (contact or phone).strip(),
                TICKET_INITIAL_STATUS,
                TICKET_INITIAL_NOTE,
                now,
                now,
            ),
        )
        await connection.commit()
    return {
        "ticket_id": ticket_id,
        "phone": phone,
        "owner_name": account.get("owner_name", ""),
        "fault_type": fault_type,
        "description": description.strip(),
        "address": address.strip(),
        "contact": (contact or phone).strip(),
        "status": TICKET_INITIAL_STATUS,
        "status_note": TICKET_INITIAL_NOTE,
        "created_at": now,
    }


async def get_fault_ticket(ticket_id: str) -> dict[str, Any]:
    await _ensure()
    async with _connect() as connection:
        connection.row_factory = aiosqlite.Row
        cursor = await connection.execute(
            "SELECT ticket_id, phone, fault_type, description, address, contact,"
            " status, status_note, created_at, updated_at"
            " FROM fault_ticket WHERE ticket_id = ?",
            (ticket_id.strip(),),
        )
        row = await cursor.fetchone()
    if row is None:
        raise SkillError(f"未查询到工单 {ticket_id}，请核对工单号。", code="ticket_not_found")
    return dict(row)


async def list_fault_tickets(phone: str, *, limit: int = 5) -> list[dict[str, Any]]:
    """按号码查最近工单（建单后立即查状态走此路径）。"""
    await _ensure()
    async with _connect() as connection:
        connection.row_factory = aiosqlite.Row
        cursor = await connection.execute(
            "SELECT ticket_id, phone, fault_type, description, address, contact,"
            " status, status_note, created_at, updated_at"
            " FROM fault_ticket WHERE phone = ? ORDER BY created_at DESC LIMIT ?",
            (phone, int(limit)),
        )
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# 套餐变更（写，需人工确认后才会调用本函数）
# ---------------------------------------------------------------------------


async def change_package(phone: str, target: str) -> dict[str, Any]:
    """执行套餐变更：校验目标套餐与当前套餐后更新 user_package（演示为次月生效）。"""
    await _ensure()
    # 变更前先确认号码是在网账户，以便与「套餐缺失」区分出明确错误
    account = await get_account(phone)
    try:
        current = await get_user_package_detail(phone)
    except SkillError as error:
        if error.code != "package_not_found":
            raise
        # 该号码尚未建立套餐基线（历史数据缺失或早期注册账号）：
        # 先按默认档位开户，再继续本次变更，避免「办不了套餐」
        await provision_account(phone, owner_name=str(account.get("owner_name") or ""))
        current = await get_user_package_detail(phone)
    target_pkg = await resolve_package(target)
    if target_pkg["package_id"] == current["package_id"]:
        raise SkillError(
            f"号码 {phone} 当前已是「{current['package_name']}」，无需重复变更。",
            code="same_package",
        )

    effective_date = _next_month_first_day()
    now = utc_now()
    async with _connect() as connection:
        await connection.execute(
            "UPDATE user_package SET package_id = ?, package_name = ?,"
            " data_used_gb = 0.0, voice_used_min = 0, effective_date = ?, updated_at = ?"
            " WHERE phone = ?",
            (
                target_pkg["package_id"],
                target_pkg["name"],
                effective_date,
                now,
                phone,
            ),
        )
        await connection.commit()
    return {
        "phone": phone,
        "old_package_id": current["package_id"],
        "old_package_name": current["package_name"],
        "old_monthly_fee": _money(current.get("monthly_fee")),
        "new_package_id": target_pkg["package_id"],
        "new_package_name": target_pkg["name"],
        "new_monthly_fee": target_pkg["monthly_fee"],
        "new_data_quota_gb": target_pkg["data_quota_gb"],
        "new_voice_minutes": int(target_pkg["voice_minutes"]),
        "broadband_mbps": int(target_pkg["broadband_mbps"]),
        "effective_date": effective_date,
        "status": "办理成功",
    }


def _next_month_first_day(now: datetime | None = None) -> str:
    moment = now or datetime.now()
    year = moment.year + (1 if moment.month == 12 else 0)
    month = 1 if moment.month == 12 else moment.month + 1
    return f"{year:04d}-{month:02d}-01"


# ---------------------------------------------------------------------------
# P4-16：跨轮人工确认存储
# ---------------------------------------------------------------------------


async def save_pending_approval(
    workspace: str,
    *,
    approval_id: str,
    skill_name: str,
    args: dict[str, Any],
    summary: str,
) -> dict[str, Any]:
    """落一条待确认记录；同一会话此前若有未决记录，先置 cancelled（避免悬挂）。"""
    await _ensure()
    now = utc_now()
    async with _connect() as connection:
        await connection.execute(
            "UPDATE pending_approval SET status = 'cancelled', updated_at = ?"
            " WHERE workspace = ? AND status = 'pending'",
            (now, workspace),
        )
        await connection.execute(
            "INSERT INTO pending_approval"
            " (approval_id, workspace, skill_name, args_json, summary, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
            (
                approval_id,
                workspace,
                skill_name,
                json.dumps(args, ensure_ascii=False, default=str),
                summary,
                now,
                now,
            ),
        )
        await connection.commit()
    return {
        "approval_id": approval_id,
        "workspace": workspace,
        "skill_name": skill_name,
        "args": dict(args),
        "summary": summary,
        "status": "pending",
        "created_at": now,
    }


async def get_pending_approval(workspace: str) -> dict[str, Any] | None:
    """取会话当前未决的人工确认（无则 None）。"""
    await _ensure()
    async with _connect() as connection:
        connection.row_factory = aiosqlite.Row
        cursor = await connection.execute(
            "SELECT approval_id, workspace, skill_name, args_json, summary, status, created_at, updated_at"
            " FROM pending_approval WHERE workspace = ? AND status = 'pending'"
            " ORDER BY created_at DESC LIMIT 1",
            (workspace,),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    data = dict(row)
    try:
        data["args"] = json.loads(data.pop("args_json") or "{}")
    except json.JSONDecodeError:
        data["args"] = {}
    if not isinstance(data["args"], dict):
        data["args"] = {}
    return data


async def update_pending_approval(approval_id: str, status: str) -> bool:
    """更新确认单状态（approved/executed/cancelled/failed）。"""
    await _ensure()
    async with _connect() as connection:
        cursor = await connection.execute(
            "UPDATE pending_approval SET status = ?, updated_at = ? WHERE approval_id = ?",
            (status, utc_now(), approval_id),
        )
        await connection.commit()
        return int(cursor.rowcount or 0) > 0
