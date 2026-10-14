"""历史会话管理路由。

- ``GET /api/v1/sessions``：列出历史会话（按更新时间倒序）。支持
  ``workspace_prefix`` 按用户维度过滤（各用户会话位于独立的
  ``user-<phone>/`` 工作区目录下），供 TUI/前端会话切换面板使用；
- ``GET /api/v1/sessions/{session_id}``：按 session_id 查询单个会话详情
  （含 recent_turns）。
"""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from congclaw.api.deps import resolve_workspace
from congclaw.core.paths import legacy_workspace_prefixes
from congclaw.core.session import alist_sessions
from congclaw.db import SessionModel, session_scope
from congclaw.domain.business_store import DEFAULT_DEMO_PHONE

router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])


@router.get("")
async def list_sessions(
    limit: int = 100,
    workspace_prefix: str | None = None,
    phone: str | None = None,
) -> dict:
    """列出历史会话摘要（按 updated_at 倒序）。

    ``phone`` 为主过滤键：按 ``session.phone`` 显式归属只返回当前用户的会话，
    从而支持多用户隔离。默认演示用户额外纳入无归属（``phone=''``）的历史会话，
    使多用户改造前的旧会话不丢失，这类会话在返回结果中带 ``legacy: true``。

    未传 ``phone`` 时退回 ``workspace_prefix`` 前缀过滤（``user-<phone>/`` 目录）。
    """
    phones: list[str] | None = None
    if phone:
        phones = [phone]
        if phone == DEFAULT_DEMO_PHONE:
            phones.append("")

    own_prefix: str | None = None
    extra_prefixes: list[str] = []
    if not phones:
        if workspace_prefix:
            own_prefix = str(resolve_workspace(workspace_prefix)) + os.sep
        if str(phone or "") == DEFAULT_DEMO_PHONE:
            # 遗留工作区名本身就是 ``workspace-<时间戳>``，故这里不补分隔符
            extra_prefixes = legacy_workspace_prefixes()

    items = await alist_sessions(
        limit=limit,
        workspace_prefix=own_prefix,
        extra_prefixes=extra_prefixes,
        phones=phones,
    )
    for item in items:
        # 无归属（phone 为空）的会话属于多用户改造前的历史资产
        item["legacy"] = not str(item.get("phone") or "")
    return {"total": len(items), "sessions": items}


@router.get("/{session_id}")
async def get_session(session_id: str) -> dict:
    """按 session_id 查询单个会话详情。"""
    async with session_scope() as db_session:
        result = await db_session.execute(
            select(SessionModel).where(SessionModel.session_id == session_id)
        )
        row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"会话不存在：{session_id}")
    import json as _json

    try:
        recent_turns = _json.loads(row.recent_turns) if row.recent_turns else []
    except (_json.JSONDecodeError, TypeError):
        recent_turns = []
    return {
        "session_id": row.session_id,
        "workspace": row.workspace,
        "phone": row.phone or "",
        "turn_index": int(row.turn_index or 0),
        "last_route": row.last_route or "",
        "last_task": row.last_task or "",
        "last_final_answer": row.last_final_answer or "",
        "summary": row.summary or "",
        "recent_turns": recent_turns,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
