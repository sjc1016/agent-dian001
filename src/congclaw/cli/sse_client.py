"""HTTP/SSE 客户端工具（CLI 与 TUI 共用）。

阶段 1 起，CLI/TUI 不再本地运行 graph，而是通过 HTTP 调用
``POST /api/v1/chat`` 并解析 SSE 流。
"""

from __future__ import annotations

import json
from typing import Any, Iterator

import httpx

DEFAULT_API_URL = "http://127.0.0.1:8000"


def stream_chat_events(
    message: str,
    *,
    api_url: str = DEFAULT_API_URL,
    workspace: str | None = None,
    max_attempts: int = 3,
    timeout: float = 600.0,
) -> Iterator[dict[str, Any]]:
    """调用 ``/api/v1/chat`` 并逐条 yield 解析后的事件字典。

    以阻塞方式迭代 SSE 流，适合 CLI / TUI 的同步渲染场景。
    """
    url = api_url.rstrip("/") + "/api/v1/chat"
    payload: dict[str, Any] = {"message": message, "max_attempts": max_attempts}
    if workspace:
        payload["workspace"] = workspace

    with httpx.stream("POST", url, json=payload, timeout=timeout) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if not line:
                continue
            if line.startswith("data: "):
                data = line[6:]
                if data == "[DONE]":
                    break
                try:
                    yield json.loads(data)
                except json.JSONDecodeError:
                    # 忽略无法解析的帧
                    continue


def fetch_sessions(api_url: str = DEFAULT_API_URL, limit: int = 100) -> list[dict[str, Any]]:
    """调用 ``GET /api/v1/sessions`` 获取历史会话列表。"""
    url = api_url.rstrip("/") + "/api/v1/sessions"
    with httpx.Client(timeout=30.0) as client:
        response = client.get(url, params={"limit": limit})
        response.raise_for_status()
        data = response.json()
    return list(data.get("sessions", []))
