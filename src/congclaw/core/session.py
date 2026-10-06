from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import func, or_, select

from congclaw.core.checkpoint import workspace_manifest
from congclaw.db import SessionModel, session_scope
from congclaw.db.engine import init_db


SESSION_ROOT = Path(".congclaw") / "session"
SESSION_FILE = "session.json"
SESSION_SUMMARY_FILE = "SESSION_SUMMARY.md"
MAX_RECENT_TURNS = 18
MAX_TURN_CONTENT = 1800
MAX_SESSION_SUMMARY = 5000
MAX_SESSION_CONTEXT = 7000


def session_dir(workspace: Path) -> Path:
    return workspace / SESSION_ROOT


def session_file(workspace: Path) -> Path:
    """会话文件路径（阶段 1 起仅作事件元信息展示，不再作为存储来源）。"""
    return session_dir(workspace) / SESSION_FILE


def session_summary_file(workspace: Path) -> Path:
    return workspace / SESSION_SUMMARY_FILE


async def aload_or_create_session(workspace: Path) -> dict[str, Any]:
    """阶段 4：会话加载/创建的原生 async 版本（HTTP/SSE 链路直接 await）。"""
    workspace.mkdir(parents=True, exist_ok=True)
    workspace_key = str(workspace)
    row = await _load_session_row(workspace_key)
    if row is not None:
        session = _row_to_session(row, workspace)
    else:
        session = _normalize_session({}, workspace)
        await _upsert_session_row(workspace_key, session)
    # 保留人读摘要文件，便于事件展示与调试（小文件写，卸载到线程避免阻塞循环）
    await asyncio.to_thread(_write_summary_file, workspace, session)
    return session


def load_or_create_session(workspace: Path) -> dict[str, Any]:
    return _run(aload_or_create_session(workspace))


async def alist_sessions(
    limit: int = 100,
    workspace_prefix: str | None = None,
    extra_prefixes: list[str] | None = None,
) -> list[dict[str, Any]]:
    """列出历史会话（按更新时间倒序）。

    ``workspace_prefix`` 用于按用户维度过滤：各用户的会话存放在独立的
    ``user-<phone>/`` 目录下，传入该目录的绝对路径前缀即可只返回该用户的会话。

    ``extra_prefixes`` 为额外的前缀集合（OR 语义），用于让默认用户同时看到
    多用户改造前遗留在 ``workspaces/workspace-*`` 下的未归属历史会话。
    """
    await _ensure_db()
    stmt = select(SessionModel)
    prefixes = [prefix for prefix in [workspace_prefix, *(extra_prefixes or [])] if prefix]
    if prefixes:
        stmt = stmt.where(or_(*[_prefix_condition(prefix) for prefix in prefixes]))
    stmt = stmt.order_by(SessionModel.updated_at.desc()).limit(limit)
    async with session_scope() as session:
        result = await session.execute(stmt)
        rows = result.scalars().all()
    return [_row_to_session_summary(row) for row in rows]


def _prefix_condition(prefix: str):
    """构造「workspace 以 prefix 开头」的精确条件。

    用 ``substr`` 比较而非 ``LIKE``：Windows 路径中的反斜杠与 ``%``/``_``
    通配符会干扰 ``LIKE`` 的匹配语义。
    """
    return func.substr(SessionModel.workspace, 1, len(prefix)) == prefix


def list_sessions(
    limit: int = 100,
    workspace_prefix: str | None = None,
    extra_prefixes: list[str] | None = None,
) -> list[dict[str, Any]]:
    return _run(alist_sessions(limit, workspace_prefix, extra_prefixes))


def append_user_turn(session: dict[str, Any], content: str) -> int:
    turn = int(session.get("turn_index", 0)) + 1
    session["turn_index"] = turn
    session["last_task"] = content
    _append_turn(
        session,
        {
            "turn": turn,
            "role": "user",
            "route": "",
            "content": trim_text(content, MAX_TURN_CONTENT),
            "timestamp": utc_now(),
        },
    )
    return turn


def append_assistant_turn(
    session: dict[str, Any],
    *,
    turn: int,
    route: str,
    content: str,
    summary: str = "",
) -> None:
    session["last_route"] = route
    session["last_final_answer"] = trim_text(content, MAX_TURN_CONTENT)
    _append_turn(
        session,
        {
            "turn": turn,
            "role": "assistant",
            "route": route,
            "content": trim_text(content, MAX_TURN_CONTENT),
            "summary": trim_text(summary or content, 700),
            "timestamp": utc_now(),
        },
    )


async def asave_session(workspace: Path, session: dict[str, Any]) -> dict[str, Any]:
    """阶段 4：会话保存的原生 async 版本。"""
    workspace.mkdir(parents=True, exist_ok=True)
    session = _normalize_session(session, workspace)
    _compact_session(session)
    session["updated_at"] = utc_now()
    await _upsert_session_row(str(workspace), session)
    await asyncio.to_thread(_write_summary_file, workspace, session)
    return session


def save_session(workspace: Path, session: dict[str, Any]) -> dict[str, Any]:
    return _run(asave_session(workspace, session))


def build_session_context(workspace: Path, session: dict[str, Any] | None = None) -> str:
    session = _normalize_session(session or load_or_create_session(workspace), workspace)
    manifest = workspace_manifest(workspace, limit=40)
    files = [
        item.get("path", "")
        for item in manifest
        if item.get("type") == "file" and not str(item.get("path", "")).startswith(".congclaw/session/")
    ][:30]
    recent_turns = [
        {
            "turn": turn.get("turn"),
            "role": turn.get("role", ""),
            "route": turn.get("route", ""),
            "content": trim_text(str(turn.get("content", "")), 600),
            "summary": trim_text(str(turn.get("summary", "")), 300),
        }
        for turn in session.get("recent_turns", [])[-10:]
    ]
    payload = {
        "session_id": session.get("session_id", ""),
        "turn_index": session.get("turn_index", 0),
        "workspace": str(workspace),
        "summary": trim_text(str(session.get("summary", "")), 1800),
        "last_route": session.get("last_route", ""),
        "last_task": trim_text(str(session.get("last_task", "")), 600),
        "last_final_answer": trim_text(str(session.get("last_final_answer", "")), 900),
        "recent_turns": recent_turns,
        "recent_files": files,
    }
    return trim_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), MAX_SESSION_CONTEXT)


def session_started_event(workspace: Path, session: dict[str, Any], *, resumed: bool = False) -> dict[str, Any]:
    return {
        "type": "session_started",
        "session_id": session.get("session_id", ""),
        "workspace": str(workspace),
        "turn_index": session.get("turn_index", 0),
        "resumed": resumed,
        "session_file": str(session_file(workspace)),
        "summary_file": str(session_summary_file(workspace)),
    }


def session_turn_started_event(workspace: Path, session: dict[str, Any], *, turn: int, task: str) -> dict[str, Any]:
    return {
        "type": "session_turn_started",
        "session_id": session.get("session_id", ""),
        "workspace": str(workspace),
        "turn": turn,
        "task": trim_text(task, 900),
    }


def session_turn_saved_event(workspace: Path, session: dict[str, Any], *, turn: int, route: str) -> dict[str, Any]:
    return {
        "type": "session_turn_saved",
        "session_id": session.get("session_id", ""),
        "workspace": str(workspace),
        "turn": turn,
        "route": route,
        "turn_count": len(session.get("recent_turns", [])),
        "summary_file": str(session_summary_file(workspace)),
    }


def build_session_summary_markdown(workspace: Path, session: dict[str, Any]) -> str:
    lines = [
        "# CongClaw Session Summary",
        "",
        f"- session_id: {session.get('session_id', '')}",
        f"- workspace: {workspace}",
        f"- turns: {session.get('turn_index', 0)}",
        f"- updated_at: {session.get('updated_at', '')}",
        f"- last_route: {session.get('last_route', '') or '(none)'}",
        "",
        "## Summary",
        "",
        str(session.get("summary", "") or "(no compressed summary yet)"),
        "",
        "## Recent Turns",
        "",
    ]
    for turn in session.get("recent_turns", [])[-MAX_RECENT_TURNS:]:
        role = turn.get("role", "")
        route = f" / {turn.get('route')}" if turn.get("route") else ""
        lines.append(f"- turn {turn.get('turn')}: {role}{route}: {trim_text(str(turn.get('content', '')), 260)}")
    return "\n".join(lines).rstrip() + "\n"


def trim_text(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_session(raw: dict[str, Any], workspace: Path) -> dict[str, Any]:
    now = utc_now()
    session = dict(raw) if isinstance(raw, dict) else {}
    session.setdefault("version", 1)
    session.setdefault("session_id", f"session-{uuid4().hex[:8]}")
    session.setdefault("workspace", str(workspace))
    session.setdefault("created_at", now)
    session.setdefault("updated_at", now)
    session.setdefault("turn_index", 0)
    session.setdefault("summary", "")
    session.setdefault("recent_turns", [])
    session.setdefault("last_route", "")
    session.setdefault("last_task", "")
    session.setdefault("last_final_answer", "")
    session.setdefault("pending_slots", [])
    session.setdefault("clarify_count", 0)
    session.setdefault("unknown_count", 0)
    if not isinstance(session.get("recent_turns"), list):
        session["recent_turns"] = []
    if not isinstance(session.get("pending_slots"), list):
        # 阶段 1 曾以 dict 兼容存储；阶段 2 统一为字符串列表
        session["pending_slots"] = []
    session["clarify_count"] = int(session.get("clarify_count") or 0)
    session["unknown_count"] = int(session.get("unknown_count") or 0)
    session["workspace"] = str(workspace)
    session["turn_index"] = int(session.get("turn_index") or 0)
    session["summary"] = trim_text(str(session.get("summary", "")), MAX_SESSION_SUMMARY)
    return session


def _append_turn(session: dict[str, Any], turn: dict[str, Any]) -> None:
    turns = list(session.get("recent_turns", []))
    turns.append(turn)
    session["recent_turns"] = turns


def _compact_session(session: dict[str, Any]) -> None:
    turns = list(session.get("recent_turns", []))
    if len(turns) <= MAX_RECENT_TURNS:
        return
    older = turns[: -MAX_RECENT_TURNS]
    kept = turns[-MAX_RECENT_TURNS:]
    additions = []
    for turn in older:
        role = turn.get("role", "")
        route = f"/{turn.get('route')}" if turn.get("route") else ""
        summary = turn.get("summary") or turn.get("content", "")
        additions.append(f"turn {turn.get('turn')}: {role}{route}: {trim_text(str(summary), 260)}")
    existing = str(session.get("summary", ""))
    session["summary"] = trim_text("\n".join(part for part in [existing, *additions] if part), MAX_SESSION_SUMMARY)
    session["recent_turns"] = kept


# ---------------------------------------------------------------------------
# SQLite 持久化辅助：阶段 4 起 ``aload_or_create_session`` / ``asave_session``
# 为原生 async 主实现（HTTP/SSE 链路直接 await，全链路无工作线程桥接）；
# 同步公共函数仅作为 CLI/历史调用方的薄适配层（asyncio.run 驱动同一协程）。
# ---------------------------------------------------------------------------


def _run(coro):
    """在同步上下文中执行协程（同步适配层使用）。"""
    return asyncio.run(coro)


async def _ensure_db() -> None:
    """确保数据库已初始化（幂等）。"""
    await init_db()


async def _load_session_row(workspace: str) -> SessionModel | None:
    await _ensure_db()
    async with session_scope() as session:
        result = await session.execute(
            select(SessionModel).where(SessionModel.workspace == workspace)
        )
        return result.scalar_one_or_none()


async def _upsert_session_row(workspace: str, data: dict[str, Any]) -> None:
    await _ensure_db()
    async with session_scope() as session:
        existing = (
            await session.execute(
                select(SessionModel).where(SessionModel.workspace == workspace)
            )
        ).scalar_one_or_none()
        if existing is None:
            row = SessionModel(
                session_id=str(data.get("session_id", "")),
                workspace=workspace,
                turn_index=int(data.get("turn_index", 0)),
                last_route=str(data.get("last_route", "")),
                last_task=str(data.get("last_task", "")),
                last_final_answer=str(data.get("last_final_answer", "")),
                summary=str(data.get("summary", "")),
                recent_turns=json.dumps(data.get("recent_turns", []), ensure_ascii=False, default=str),
                pending_slots=json.dumps(data.get("pending_slots", []), ensure_ascii=False, default=str),
                clarify_count=int(data.get("clarify_count", 0) or 0),
                unknown_count=int(data.get("unknown_count", 0) or 0),
                created_at=str(data.get("created_at", utc_now())),
                updated_at=str(data.get("updated_at", utc_now())),
            )
            session.add(row)
        else:
            existing.session_id = str(data.get("session_id", existing.session_id))
            existing.turn_index = int(data.get("turn_index", existing.turn_index))
            existing.last_route = str(data.get("last_route", existing.last_route))
            existing.last_task = str(data.get("last_task", existing.last_task))
            existing.last_final_answer = str(data.get("last_final_answer", existing.last_final_answer))
            existing.summary = str(data.get("summary", existing.summary))
            existing.recent_turns = json.dumps(data.get("recent_turns", []), ensure_ascii=False, default=str)
            existing.pending_slots = json.dumps(data.get("pending_slots", []), ensure_ascii=False, default=str)
            existing.clarify_count = int(data.get("clarify_count", 0) or 0)
            existing.unknown_count = int(data.get("unknown_count", 0) or 0)
            existing.updated_at = str(data.get("updated_at", utc_now()))


def _row_to_session_summary(row: SessionModel) -> dict[str, Any]:
    """会话列表项的轻量摘要（不含 recent_turns 全文，避免列表响应过大）。"""
    return {
        "session_id": row.session_id,
        "workspace": row.workspace,
        "turn_index": int(row.turn_index or 0),
        "last_route": row.last_route or "",
        "last_task": trim_text(str(row.last_task or ""), 120),
        "last_final_answer": trim_text(str(row.last_final_answer or ""), 160),
        "summary": trim_text(str(row.summary or ""), 300),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _row_to_session(row: SessionModel, workspace: Path) -> dict[str, Any]:
    try:
        recent_turns = json.loads(row.recent_turns) if row.recent_turns else []
    except (json.JSONDecodeError, TypeError):
        recent_turns = []
    try:
        pending_slots = json.loads(row.pending_slots) if row.pending_slots else []
    except (json.JSONDecodeError, TypeError):
        pending_slots = []
    if not isinstance(pending_slots, list):
        # 阶段 1 曾以 dict 兼容存储；阶段 2 统一为字符串列表
        pending_slots = []
    return {
        "version": 1,
        "session_id": row.session_id,
        "workspace": str(workspace),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "turn_index": row.turn_index,
        "summary": row.summary,
        "recent_turns": recent_turns,
        "last_route": row.last_route,
        "last_task": row.last_task,
        "last_final_answer": row.last_final_answer,
        "pending_slots": pending_slots,
        "clarify_count": int(row.clarify_count or 0),
        "unknown_count": int(row.unknown_count or 0),
    }


def _write_summary_file(workspace: Path, session: dict[str, Any]) -> None:
    """保留人读的会话摘要文件（非存储来源，仅用于事件展示与调试）。"""
    try:
        path = session_summary_file(workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(build_session_summary_markdown(workspace, session), encoding="utf-8")
    except OSError:
        pass
