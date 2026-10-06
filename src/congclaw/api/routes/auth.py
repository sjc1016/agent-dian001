"""账号鉴权路由。

前端「切换用户」不再是简单切换，而是先经过账号口令校验：

- ``POST /api/v1/auth/login``：手机号或用户名 + 密码登录；
- ``POST /api/v1/auth/register``：注册新账号（写库并返回新用户）；
- ``GET  /api/v1/auth/accounts``：按关键词检索已注册账号，供登录框搜索提示。

登录成功后前端仍以 ``phone`` 作为用户身份：它决定业务数据（账户/套餐）、
长期记忆（``user_profile``）与会话工作区（``user-<phone>/``）。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from congclaw.skills.auth_store import (
    AuthError,
    get_public_user,
    login as login_account,
    register as register_account,
    search_accounts,
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginRequest(BaseModel):
    """登录入参：account 支持手机号或用户名。"""

    account: str = Field(..., description="登录账号：手机号或用户名", min_length=1)
    password: str = Field(..., description="登录密码", min_length=1)


class RegisterRequest(BaseModel):
    """注册入参：注册成功后账号即写入数据库。"""

    phone: str = Field(..., description="手机号（11 位，作为用户唯一标识）")
    password: str = Field(..., description="登录密码，至少 6 位")
    username: str = Field("", description="用户名，留空则默认取机主姓名或手机号")
    owner_name: str = Field("", description="机主姓名，留空则与用户名一致")


@router.post("/login")
async def login(payload: LoginRequest) -> dict:
    """校验账号口令；通过后返回公开用户信息（不含口令哈希）。"""
    try:
        user = await login_account(payload.account, payload.password)
    except AuthError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    return {"ok": True, "user": user}


@router.post("/register")
async def register(payload: RegisterRequest) -> dict:
    """注册新账号：写入 account（并建立 user_profile 记忆锚点）。"""
    try:
        user = await register_account(
            phone=payload.phone,
            username=payload.username,
            password=payload.password,
            owner_name=payload.owner_name,
        )
    except AuthError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    return {"ok": True, "created": True, "user": user}


@router.get("/accounts")
async def list_candidate_accounts(keyword: str = "", limit: int = 8) -> dict:
    """按关键词检索已注册账号（手机号/用户名/机主姓名），用于登录框搜索提示。"""
    accounts = await search_accounts(keyword, limit=max(1, min(int(limit), 50)))
    return {"total": len(accounts), "accounts": accounts}


@router.get("/users/{phone}")
async def get_user(phone: str) -> dict:
    """按号码查询公开用户信息（供前端校验登录态是否仍有效）。"""
    user = await get_public_user(phone)
    if user is None:
        raise HTTPException(status_code=404, detail=f"账号不存在：{phone}")
    return {"ok": True, "user": user}
