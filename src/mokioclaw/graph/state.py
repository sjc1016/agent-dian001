from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph import add_messages

from mokioclaw.core.state import RuntimeState


class TodoItem(TypedDict):
    id: str
    content: str
    status: str
    note: str


class VerificationResult(TypedDict):
    command: str
    ok: bool
    exit_code: int | None
    stdout: str
    stderr: str


class SourceItem(TypedDict, total=False):
    title: str
    url: str
    content: str
    score: float


class AgentHandoff(TypedDict, total=False):
    from_agent: str
    to_agent: str
    instruction: str
    result: str


class VerificationCheck(TypedDict, total=False):
    name: str
    passed: bool
    detail: str


class CompressionEvent(TypedDict, total=False):
    before_tokens: int
    after_tokens: int
    removed_messages: int
    summary: str
    next_node: str


class LayeredMemory(TypedDict, total=False):
    rules: dict[str, Any]
    working_memory: dict[str, Any]
    history_summary_store: dict[str, Any]


class MokioGraphState(TypedDict, total=False):
    task: str
    runtime: RuntimeState
    messages: Annotated[list[BaseMessage], add_messages]
    plan_summary: str
    todos: list[TodoItem]
    acceptance_criteria: list[str]
    verification_commands: list[str]
    verification_results: list[VerificationResult]
    passed: bool
    attempts: int
    max_attempts: int
    final_answer: str
    intent_route: str
    intent_reason: str
    intent_confidence: float
    # 阶段 2：意图识别与任务调度
    intent_category: str  # rag_query / agent_service / clarify / irrelevant / unknown
    clarify_count: int  # 连续追问轮数（跨轮持久化，明确业务后清零）
    clarify_question: str
    pending_slots: list[str]  # 待确认槽位（追问澄清用）
    fallback_reason: str  # irrelevant_request / clarify_exceeded / unknown_streak
    unknown_count: int  # 连续 unknown 轮数（达到阈值强制兜底）
    chat_response: str
    session_id: str
    session_turn: int
    session_context: str
    # 阶段 5：多轮记忆与查询重写
    rewritten_task: str  # query_rewrite 产出的独立完整问句（无指代时与 task 相同）
    rewrite_changed: bool  # 本轮重写是否实际改写
    rewrite_reason: str
    recent_turns: list[dict[str, Any]]  # 短期会话窗口（由会话持久化注入）
    user_profile: dict[str, Any]  # 长期用户摘要（跨会话，按手机号装载）
    memory_context: str  # 预渲染的记忆层文本（下传给 RAG/Agent 子图）
    # 阶段 4：业务 Agent 子图入参（workspace 隔离会话与待审批单；phone 为会话绑定号码）
    workspace: str
    phone: str
    approval_mode: str  # inline / auto / deny
    pending_approval: dict[str, Any] | None
    approval_resolution: str  # confirmed / cancelled / unknown
    tool_traces: list[dict[str, Any]]  # Skill 调用轨迹（P6 评测消费）
    last_actor_summary: str
    research_notes: str
    sources: list[SourceItem]
    agent_handoffs: list[AgentHandoff]
    code_agent_summary: str
    verifier_summary: str
    verification_checks: list[VerificationCheck]
    context_summary: str
    context_token_count: int
    context_token_limit: int
    context_should_compress: bool
    context_next_node: str
    compression_events: list[CompressionEvent]
    memory_snapshot: LayeredMemory
    history_summary: str
    last_error: str
    metadata: dict[str, Any]
