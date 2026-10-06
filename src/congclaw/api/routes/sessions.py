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
from congclaw.skills.business_store import DEFAULT_DEMO_PHONE

router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])


@router.get("")
async def list_sessions(
    limit: int = 100,
    workspace_prefix: str | None = None,
    phone: str | None = None,
) -> dict:
    """列出历史会话摘要（按 updated_at 倒序）。

    ``workspace_prefix`` 可为相对路径（如 ``.congclaw/workspaces/user-13800138000``），
    由 :func:`resolve_workspace` 解析为绝对路径后按前缀匹配，从而只返回该用户的会话。

    ``phone`` 用于识别默认演示用户：多用户改造前创建的会话没有 ``user-<phone>``
    归属目录，统一划归默认用户，使其历史记录不因引入用户维度而丢失。
    这类会话在返回结果中带 ``legacy: true`` 标记。
    """
    own_prefix: str | None = None
    if workspace_prefix:
        resolved = resolve_workspace(workspace_prefix)
        # 追加路径分隔符，避免 user-138 / user-1380 这类前缀误匹配
        own_prefix = str(resolved) + os.sep

    extra_prefixes: list[str] = []
    if str(phone or "") == DEFAULT_DEMO_PHONE:
        # 遗留工作区名本身就是 ``workspace-<时间戳>``，故这里不补分隔符
        extra_prefixes = legacy_workspace_prefixes()

    items = await alist_sessions(
        limit=limit, workspace_prefix=own_prefix, extra_prefixes=extra_prefixes
    )
    if own_prefix:
        for item in items:
            item["legacy"] = not str(item.get("workspace", "")).startswith(own_prefix)
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
