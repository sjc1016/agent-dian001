"""历史会话管理路由。

- ``GET /api/v1/sessions``：列出全部历史会话（按更新时间倒序），供 TUI/前端
  会话切换面板使用；
- ``GET /api/v1/sessions/{session_id}``：按 session_id 查询单个会话详情
  （含 recent_turns）。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from congclaw.core.session import alist_sessions
from congclaw.db import SessionModel, session_scope

router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])


@router.get("")
async def list_sessions(limit: int = 100) -> dict:
    """列出全部历史会话摘要（按 updated_at 倒序）。"""
    items = await alist_sessions(limit=limit)
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
        "turn_index": int(row.turn_index or 0),
        "last_route": row.last_route or "",
        "last_task": row.last_task or "",
        "last_final_answer": row.last_final_answer or "",
        "summary": row.summary or "",
        "recent_turns": recent_turns,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
