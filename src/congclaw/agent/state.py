"""Agent 深度推理子图独立状态（与主图 CongGraphState 解耦）。

主图只把"用户问句 + 会话上下文 + 默认号码 + 审批模式 + 上轮未决确认"传入，
子图把"最终答复 + 兜底原因"接回；工具调用中间结果全部留在子图状态，
并通过 custom 事件写入 trace（P6 评测轨迹直接消费）。
"""

from __future__ import annotations

from typing import Any, TypedDict


class ToolCall(TypedDict, total=False):
    id: str
    name: str
    args: dict[str, Any]
    approved: bool  # 跨轮人工确认已通过（仅 requires_confirmation 写操作）


class ToolResult(TypedDict, total=False):
    call_id: str
    name: str
    ok: bool
    data: dict[str, Any]
    error: str
    error_code: str


class VerificationCheck(TypedDict, total=False):
    name: str
    passed: bool
    detail: str


class ConfirmationRequest(TypedDict, total=False):
    """写操作人工确认卡片（P4-16）。"""

    approval_id: str
    skill_name: str
    args: dict[str, Any]
    summary: str


class AgentSubState(TypedDict, total=False):
    # ---- 输入 ----
    query: str
    session_context: str  # 旧版记忆层文本（兼容保留）
    memory_context: str  # 阶段 5：主图预渲染的四层记忆文本（短期窗口+长期摘要）
    phone: str
    workspace: str
    approval_mode: str  # inline / auto / deny
    pending_approval: dict[str, Any] | None  # 主图注入的上轮未决办理
    approval_resolution: str  # confirmed / cancelled / unknown

    # ---- approval_entry ----
    entry_decision: str  # think / act / cancel

    # ---- think 思考 ----
    reasoning: str
    tool_calls: list[ToolCall]
    direct_answer: str  # 模型判断无需工具（追问/寒暄）时直接输出

    # ---- act 工具编排 ----
    tool_results: list[ToolResult]
    confirmation_request: ConfirmationRequest | None
    last_approved_call: ToolCall | None

    # ---- reflect 反思校验 ----
    verify_decision: str  # pass / retry / fallback
    verify_reason: str
    verify_hint: str
    verification_checks: list[VerificationCheck]
    attempts: int
    max_attempts: int

    # ---- 输出 ----
    answer: str
    chat_response: str  # 与 answer 同义，对齐主图节点输出契约
    final_answer: str
    fallback_reason: str

    # ---- 测试注入 ----
    _registry: Any  # 测试通过 state 注入替身注册中心（生产取进程单例）
