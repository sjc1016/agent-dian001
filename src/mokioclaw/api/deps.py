"""FastAPI 依赖注入。

提供 DB 会话、配置、工作区等共享依赖，避免在每个路由中重复构造。
阶段 1 仅引入会话级 DB session 与配置读取；模型预热、Skill 注册等
重型依赖将在后续阶段通过 lifespan 单例化后在此注入。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, AsyncIterator

from dotenv import load_dotenv
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from mokioclaw.core.paths import find_project_root
from mokioclaw.db.engine import get_engine, session_scope


def get_settings() -> dict[str, str]:
    """读取运行时配置（环境变量）。"""
    load_dotenv()
    return {
        "api_base_url": os.getenv("API_BASE_URL", "http://127.0.0.1:8000"),
        "db_path": os.getenv("DB_PATH", "data/telecom_cs.db"),
    }


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """提供请求级别的异步 DB 会话。"""
    async with session_scope() as session:
        yield session


def resolve_workspace(workspace: str | None = None) -> Path:
    """解析会话工作区路径。

    若未指定，则使用项目根目录下的默认工作区；阶段 1 保持与旧 CLI
    一致的 workspace 定位策略，后续阶段可改为按 session_id 路由。
    """
    if workspace:
        path = Path(workspace).expanduser()
    else:
        from mokioclaw.core.paths import default_workspace

        path = default_workspace()
    if not path.is_absolute():
        path = find_project_root() / path
    return path


SettingsDep = Annotated[dict[str, str], Depends(get_settings)]
DbSessionDep = Annotated[AsyncSession, Depends(get_db_session)]
