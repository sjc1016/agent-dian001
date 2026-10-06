"""用户（电信客户）路由。

用户身份即电信号码：每位客户在 ``account``/``user_package`` 中拥有自己的
业务数据，在 ``user_profile`` 中拥有独立的跨会话长期记忆，其会话则存放在
``.congclaw/workspaces/user-<phone>/`` 目录下互相隔离。

- ``GET /api/v1/users``：列出全部演示客户（号码 + 机主姓名），
  供前端用户切换器渲染候选项。
"""

from __future__ import annotations

from fastapi import APIRouter

from congclaw.skills.business_store import DEFAULT_DEMO_PHONE, list_accounts

router = APIRouter(prefix="/api/v1/users", tags=["users"])


@router.get("")
async def list_users(limit: int = 50) -> dict:
    """列出全部在网客户；数据库为空时回退到默认演示号码，保证前端始终可用。"""
    users = await list_accounts(limit=limit)
    if not users:
        users = [{"phone": DEFAULT_DEMO_PHONE, "owner_name": "默认用户"}]
    return {"total": len(users), "users": users, "default_phone": DEFAULT_DEMO_PHONE}
