"""阶段 5：跨会话长期用户摘要仓储（SQLite ``user_profile`` 表）。

- 按 **手机号** 维度持久化长期记忆（一个用户可跨多个会话 workspace）；
- 会话每回合结束后调用 :func:`aconsolidate_user_profile` 做**增量压缩**：
  以 ``turn_count`` 为水位，只把新轮次并入摘要，优先用 LLM 压缩，
  LLM 不可用/返回非法时退化为确定性规则合并（保证无模型环境也可跨会话记忆）；
- 新会话开始时 :func:`aget_user_profile` 装载，注入主图长期记忆层（P5-8）。

与 ``skills/business_store.py``、``rag/store.py`` 一致采用 aiosqlite 短连接，
规避"连接跨事件循环"问题。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

import aiosqlite
from langchain_core.messages import HumanMessage, SystemMessage

from mokioclaw.db.engine import init_db, resolve_db_path
from mokioclaw.prompts.memory import PROFILE_COMPRESSION_PROMPT
from mokioclaw.providers.openai_provider import create_model

MAX_TOPICS = 8
MAX_OPEN_TICKETS = 10
MAX_SUMMARY_CHARS = 1200
MAX_PROFILE_TURN_CHARS = 600

# 路由 → 兜底主题词（无工具轨迹/LLM 不可用时使用）
_ROUTE_TOPICS = {
    "rag_answer": "套餐资费与业务规则咨询",
    "agent_loop": "业务查询或办理",
    "clarify": "需求澄清",
    "fallback": "",
    "workflow": "",
}

_SKILL_TOPICS = {
    "query_balance": "话费余额查询",
    "query_package": "当前套餐与余量查询",
    "list_packages": "可办理套餐咨询",
    "report_fault": "故障报修",
    "query_fault_status": "工单进度查询",
    "change_package": "套餐变更办理",
}

_PACKAGE_PATTERNS = (
    r"5G畅享\s*\d{2,3}\s*元档",
    r"全屋WiFi融合\s*\d{2,3}\s*元档",
    r"\d{2,3}\s*元档",
    r"\b[PB]\d{3}\b",
)

_TICKET_DONE_MARKERS = ("已完成", "已解决", "已取消", "已关闭")


def _connect() -> aiosqlite.core.Connection:
    return aiosqlite.connect(resolve_db_path())


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_profile(phone: str) -> dict[str, Any]:
    return {
        "phone": phone,
        "owner_name": "",
        "summary": "",
        "topics": [],
        "open_tickets": [],
        "preferred_package": "",
        "turn_count": 0,
        "last_session_workspace": "",
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }


async def aget_user_profile(phone: str) -> dict[str, Any] | None:
    """按号码读取长期用户摘要；不存在返回 None。"""
    if not phone:
        return None
    await init_db()
    async with _connect() as connection:
        connection.row_factory = aiosqlite.Row
        cursor = await connection.execute(
            "SELECT phone, owner_name, summary, topics, open_tickets, preferred_package,"
            " turn_count, last_session_workspace, created_at, updated_at"
            " FROM user_profile WHERE phone = ?",
            (phone,),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return _row_to_profile(dict(row))


async def aupsert_user_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """插入或更新一条用户摘要（按 phone 主键 upsert）。"""
    phone = str(profile.get("phone") or "")
    if not phone:
        raise ValueError("user_profile.phone is required")
    normalized = _normalize_profile(profile)
    await init_db()
    async with _connect() as connection:
        cursor = await connection.execute(
            "SELECT phone FROM user_profile WHERE phone = ?", (phone,)
        )
        exists = await cursor.fetchone()
        if exists is None:
            await connection.execute(
                "INSERT INTO user_profile (phone, owner_name, summary, topics, open_tickets,"
                " preferred_package, turn_count, last_session_workspace, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    phone,
                    normalized["owner_name"],
                    normalized["summary"],
                    json.dumps(normalized["topics"], ensure_ascii=False),
                    json.dumps(normalized["open_tickets"], ensure_ascii=False),
                    normalized["preferred_package"],
                    int(normalized["turn_count"]),
                    normalized["last_session_workspace"],
                    normalized["created_at"],
                    normalized["updated_at"],
                ),
            )
        else:
            await connection.execute(
                "UPDATE user_profile SET owner_name = ?, summary = ?, topics = ?,"
                " open_tickets = ?, preferred_package = ?, turn_count = ?,"
                " last_session_workspace = ?, updated_at = ? WHERE phone = ?",
                (
                    normalized["owner_name"],
                    normalized["summary"],
                    json.dumps(normalized["topics"], ensure_ascii=False),
                    json.dumps(normalized["open_tickets"], ensure_ascii=False),
                    normalized["preferred_package"],
                    int(normalized["turn_count"]),
                    normalized["last_session_workspace"],
                    normalized["updated_at"],
                    phone,
                ),
            )
        await connection.commit()
    return normalized


async def aconsolidate_user_profile(
    phone: str,
    *,
    workspace: str,
    session: dict[str, Any],
    route: str,
    response: str,
    tool_traces: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """增量压缩：把水位之后的新轮次并入长期摘要并落库（P5-7）。

    LLM 可用时由压缩模型合并；任何异常/非法 JSON 都退化为确定性规则合并，
    保证无外部模型环境（如测试、离线）下跨会话记忆仍可用。
    """
    existing = await aget_user_profile(phone) or default_profile(phone)
    watermark = int(existing.get("turn_count", 0) or 0)
    turn_index = int(session.get("turn_index", 0) or 0)
    if turn_index <= watermark:
        return existing  # 没有新增轮次，幂等返回

    new_turns = [
        turn
        for turn in session.get("recent_turns", [])
        if int(turn.get("turn", 0) or 0) > watermark
    ]
    # 极端情况下（旧摘要压缩裁掉了更早的轮次），至少纳入本轮
    if not new_turns:
        new_turns = [
            {"turn": turn_index, "role": "user", "content": str(session.get("last_task", ""))},
            {"turn": turn_index, "role": "assistant", "route": route, "content": response},
        ]

    merged: dict[str, Any] | None = None
    error = ""
    try:
        response_msg = await create_model().ainvoke(
            [
                SystemMessage(content=PROFILE_COMPRESSION_PROMPT),
                HumanMessage(
                    content=_compression_payload(existing, new_turns, route, tool_traces or [])
                ),
            ]
        )
        parsed = _extract_json(str(getattr(response_msg, "content", "") or ""))
        if parsed is not None:
            merged = _merge_from_llm(existing, parsed, new_turns, tool_traces or [])
    except Exception as exc:  # noqa: BLE001 —— 模型不可用走确定性兜底，不阻断会话
        error = f"{type(exc).__name__}: {exc}"

    if merged is None:
        merged = _rule_based_merge(existing, new_turns, route, tool_traces or [])
        merged["compression"] = "rule_based"
        if error:
            merged["compression_error"] = error
    else:
        merged["compression"] = "llm"

    merged["phone"] = phone
    merged["turn_count"] = turn_index
    merged["last_session_workspace"] = str(workspace)
    merged["updated_at"] = utc_now()
    merged.setdefault("created_at", existing.get("created_at") or utc_now())
    normalized = await aupsert_user_profile(merged)
    normalized["compression"] = merged["compression"]
    return normalized


# ---------------------------------------------------------------------------
# 压缩合并实现
# ---------------------------------------------------------------------------


def _merge_from_llm(
    existing: dict[str, Any],
    parsed: dict[str, Any],
    new_turns: list[dict[str, Any]],
    tool_traces: list[dict[str, Any]],
) -> dict[str, Any]:
    topics = _clean_str_list(parsed.get("topics"))[:MAX_TOPICS]
    tickets = _normalize_tickets(parsed.get("open_tickets"))
    preferred = str(parsed.get("preferred_package") or "").strip()
    summary = str(parsed.get("summary") or "").strip()

    # LLM 漏抽时用规则补齐（工具轨迹是强信号）
    rule = _rule_based_merge(existing, new_turns, "", tool_traces)
    if not preferred:
        preferred = rule["preferred_package"]
    if not tickets:
        tickets = rule["open_tickets"]
    topics = _dedupe_extend(rule["topics"][:2], topics)[:MAX_TOPICS]
    if not summary:
        summary = rule["summary"]

    return {
        "owner_name": str(existing.get("owner_name") or ""),
        "topics": topics,
        "open_tickets": tickets[:MAX_OPEN_TICKETS],
        "preferred_package": preferred or rule["preferred_package"],
        "summary": _clip(summary, MAX_SUMMARY_CHARS),
    }


def _rule_based_merge(
    existing: dict[str, Any],
    new_turns: list[dict[str, Any]],
    route: str,
    tool_traces: list[dict[str, Any]],
) -> dict[str, Any]:
    """无 LLM 时的确定性合并：工具轨迹 + 路由 + 关键词抽取。"""
    skills = [
        str(event.get("name") or "")
        for event in tool_traces
        if event.get("type") == "skill_call"
    ]
    text_blob = " ".join(
        str(turn.get("content") or "") for turn in new_turns if turn.get("role") == "user"
    )

    new_topics: list[str] = []
    for skill in skills:
        label = _SKILL_TOPICS.get(skill)
        if label:
            new_topics.append(label)
    if not new_topics:
        label = _ROUTE_TOPICS.get(route, "")
        if label:
            package = _extract_package(text_blob)
            new_topics.append(f"{package}相关咨询" if package and "咨询" in label else label)

    package = _extract_package_from_traces(tool_traces) or _extract_package(text_blob)

    tickets = _tickets_from_traces(tool_traces)
    tickets = _merge_tickets(_as_list(existing.get("open_tickets")), tickets)

    topics = _dedupe_extend(new_topics, _as_list(existing.get("topics")))[:MAX_TOPICS]

    summary_parts = [part for part in (str(existing.get("summary") or "").strip(),) if part]
    if new_topics:
        summary_parts.append("近期咨询/办理：" + "、".join(new_topics) + "。")
    if tickets:
        open_desc = "、".join(
            f"{t.get('fault_type') or '故障'}工单{t.get('ticket_id')}（{t.get('status') or '处理中'}）"
            for t in tickets
        )
        summary_parts.append("在办工单：" + open_desc + "。")
    if package:
        summary_parts.append(f"关注套餐：{package}。")
    summary = _clip(" ".join(summary_parts), MAX_SUMMARY_CHARS)

    return {
        "owner_name": str(existing.get("owner_name") or ""),
        "topics": topics,
        "open_tickets": tickets,
        "preferred_package": package or str(existing.get("preferred_package") or ""),
        "summary": summary,
    }


def _compression_payload(
    existing: dict[str, Any],
    new_turns: list[dict[str, Any]],
    route: str,
    tool_traces: list[dict[str, Any]],
) -> str:
    payload = {
        "用户号码": existing.get("phone", ""),
        "现有长期摘要": {
            "topics": existing.get("topics", []),
            "open_tickets": existing.get("open_tickets", []),
            "preferred_package": existing.get("preferred_package", ""),
            "summary": existing.get("summary", ""),
        },
        "本会话新增轮次": [
            {
                "turn": turn.get("turn"),
                "role": turn.get("role", ""),
                "route": turn.get("route", route if turn.get("role") == "assistant" else ""),
                "content": _clip(str(turn.get("content") or turn.get("summary") or ""), MAX_PROFILE_TURN_CHARS),
            }
            for turn in new_turns
        ],
        "本轮业务工具调用": [
            {
                "name": event.get("name", ""),
                "ok": event.get("ok"),
                "preview": _clip(str(event.get("preview") or ""), 400),
            }
            for event in tool_traces
            if event.get("type") == "skill_result"
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _extract_package(text: str) -> str:
    for pattern in _PACKAGE_PATTERNS:
        match = re.search(pattern, text or "")
        if match:
            return re.sub(r"\s+", "", match.group(0))
    return ""


def _extract_package_from_traces(tool_traces: list[dict[str, Any]]) -> str:
    for event in tool_traces:
        if event.get("type") != "skill_call":
            continue
        args = event.get("args") or {}
        if isinstance(args, dict):
            candidate = (
                args.get("target_package")
                or args.get("package_name")
                or args.get("keyword")
                or ""
            )
            package = _extract_package(str(candidate))
            if package:
                return package
    return ""


def _tickets_from_traces(tool_traces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tickets: list[dict[str, Any]] = []
    for event in tool_traces:
        if event.get("type") != "skill_result" or event.get("name") != "report_fault":
            continue
        if not event.get("ok"):
            continue
        data = _maybe_json(event.get("preview"))
        if not isinstance(data, dict):
            continue
        ticket_id = str(data.get("ticket_id") or data.get("ticket") or "").strip()
        if ticket_id:
            tickets.append(
                {
                    "ticket_id": ticket_id,
                    "fault_type": str(data.get("fault_type") or "故障报修"),
                    "status": str(data.get("status") or "已受理"),
                }
            )
    return tickets


def _merge_tickets(existing: list[Any], new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for raw in [*existing, *new]:
        if not isinstance(raw, dict):
            continue
        ticket_id = str(raw.get("ticket_id") or "").strip()
        if not ticket_id:
            continue
        item = {
            "ticket_id": ticket_id,
            "fault_type": str(raw.get("fault_type") or "故障报修"),
            "status": str(raw.get("status") or "处理中"),
        }
        if item["status"] in _TICKET_DONE_MARKERS:
            merged.pop(ticket_id, None)
            continue
        merged[ticket_id] = item
    return list(merged.values())[:MAX_OPEN_TICKETS]


def _normalize_tickets(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    tickets = []
    for item in raw:
        if isinstance(item, dict) and item.get("ticket_id"):
            tickets.append(
                {
                    "ticket_id": str(item.get("ticket_id")),
                    "fault_type": str(item.get("fault_type") or "故障报修"),
                    "status": str(item.get("status") or "处理中"),
                }
            )
    return _merge_tickets([], tickets)


def _dedupe_extend(head: list[str], tail: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in [*head, *tail]:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _clean_str_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _as_list(raw: Any) -> list[Any]:
    return raw if isinstance(raw, list) else []


def _maybe_json(text: Any) -> Any:
    if not isinstance(text, str):
        return text
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def _normalize_profile(raw: dict[str, Any]) -> dict[str, Any]:
    now = utc_now()
    profile = default_profile(str(raw.get("phone") or ""))
    profile.update(
        {
            "owner_name": str(raw.get("owner_name") or ""),
            "summary": _clip(str(raw.get("summary") or ""), MAX_SUMMARY_CHARS),
            "topics": _dedupe_extend(_clean_str_list(raw.get("topics")), [])[:MAX_TOPICS],
            "open_tickets": _merge_tickets([], _normalize_tickets(raw.get("open_tickets"))),
            "preferred_package": str(raw.get("preferred_package") or ""),
            "turn_count": int(raw.get("turn_count", 0) or 0),
            "last_session_workspace": str(raw.get("last_session_workspace") or ""),
            "created_at": str(raw.get("created_at") or now),
            "updated_at": str(raw.get("updated_at") or now),
        }
    )
    return profile


def _row_to_profile(row: dict[str, Any]) -> dict[str, Any]:
    return _normalize_profile(
        {
            "phone": row.get("phone", ""),
            "owner_name": row.get("owner_name", ""),
            "summary": row.get("summary", ""),
            "topics": _maybe_json(row.get("topics")) or [],
            "open_tickets": _maybe_json(row.get("open_tickets")) or [],
            "preferred_package": row.get("preferred_package", ""),
            "turn_count": int(row.get("turn_count", 0) or 0),
            "last_session_workspace": row.get("last_session_workspace", ""),
            "created_at": row.get("created_at", ""),
            "updated_at": row.get("updated_at", ""),
        }
    )


def _clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _extract_json(text: str) -> dict[str, Any] | None:
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    raw = fenced.group(1) if fenced else text
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        parsed = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
