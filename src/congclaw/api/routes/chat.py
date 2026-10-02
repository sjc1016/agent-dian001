"""对话主入口路由。

``POST /api/v1/chat`` 接收用户消息，调用 graph 流式生成事件，
以 SSE（``text/event-stream``）格式逐帧推送给客户端。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from congclaw.api.deps import resolve_workspace
from congclaw.core.agent import event_to_sse, stream_session_events_async

router = APIRouter(prefix="/api/v1", tags=["chat"])


class ChatRequest(BaseModel):
    """对话请求体。"""

    message: str = Field(..., description="用户输入的消息文本")
    workspace: str | None = Field(None, description="会话工作区路径，不填则使用默认工作区")
    session_id: str | None = Field(None, description="会话 ID（预留）")
    phone: str | None = Field(None, description="会话绑定号码，不填使用演示号码 13800138000")
    approval_mode: str = Field("inline", description="写操作审批策略：inline（人工确认）/ auto（自动批准）/ deny（拒绝）")
    max_attempts: int = Field(3, ge=1, le=10, description="最大重试次数")


@router.post("/chat")
async def chat(request: ChatRequest) -> StreamingResponse:
    """多轮对话主入口，SSE 流式返回 graph 事件。"""
    workspace = resolve_workspace(request.workspace)

    async def event_stream():
        try:
            async for event in stream_session_events_async(
                request.message,
                session_workspace=workspace,
                max_attempts=request.max_attempts,
                approval_mode=request.approval_mode,
                phone=request.phone,
            ):
                yield event_to_sse(event)
        except Exception as exc:  # noqa: BLE001
            # graph 内部异常（如 LLM 未配置、工具调用失败）通过 SSE 错误帧返回，
            # 避免直接断开连接导致客户端读流异常。
            yield event_to_sse(
                {"type": "error", "error": f"{type(exc).__name__}: {exc}"}
            )
        # 结束标记
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
