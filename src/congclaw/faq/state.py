"""阶段 7：常见问答沉淀子图独立状态（与主图 CongGraphState 解耦）。

主图把「本轮完整问答过程」传入子图，子图回传「是否沉淀 + 条目内容 + 落库编号」；
判重候选、抽取结果等中间数据留在子图状态里，并通过 custom 事件写入 trace。
"""

from __future__ import annotations

from typing import Any, TypedDict


class FaqSedimentState(TypedDict, total=False):
    # 输入（本轮回合上下文，来自主图 state）
    task: str  # 用户原始问句
    rewritten_task: str  # 指代消解后的独立问句
    route: str  # rag_answer / agent_loop
    category: str  # 意图类别
    confidence: float  # 意图置信度
    final_answer: str  # 本轮最终答复
    sources: list[dict[str, Any]]  # RAG 检索来源
    tool_traces: list[dict[str, Any]]  # Skill 调用轨迹
    recent_turns: list[dict[str, Any]]  # 短期会话窗口（回溯审批轮的真实诉求）
    session_id: str
    session_turn: int
    # 规则门控
    question: str  # 用于沉淀的问句（审批轮回溯上一轮业务诉求后的结果）
    gate_passed: bool
    gate_reason: str
    # 判重召回
    candidates: list[dict[str, Any]]
    # LLM 抽取与合并决策
    should_sediment: bool
    action: str  # create / merge / skip
    merge_into: str  # action=merge 时的目标条目编号
    entry: dict[str, Any]
    reason: str
    # 落库
    faq_id: str
    persisted: bool
    error: str
