"""P6-3：把 trace 轨迹归一化为标准评测样本。

结合标注数据集的期望值与 :class:`collector.SessionTrace` 的实际轨迹，
产出 :class:`EvalSample`，供 rule_checks 与 llm_judge 消费。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from congclaw.eval.collector import SessionTrace


@dataclass
class EvalSample:
    """单条评测样本（期望 + 实际 + 轨迹回链）。"""

    sample_id: str
    input: str
    # 期望
    expected_category: str
    expected_route: str
    expected_tools: list[str]
    expected_fallback: str  # none / irrelevant_request / clarify_exceeded / unknown_streak
    # 实际
    actual_category: str
    actual_route: str
    actual_tools: list[str]
    actual_answer: str
    actual_fallback: str  # none 或兜底原因
    # 轨迹
    clarify_rounds: int
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    rag_hit_count: int = 0
    reflect_decision: str = ""
    trace_id: str = ""
    events_file: str = ""
    notes: str = ""


def normalize(trace: SessionTrace, labeled: dict[str, Any]) -> EvalSample:
    """把一条 trace 与标注样本合并为 EvalSample。"""
    actual_tools = [str(call.get("name") or "") for call in trace.skill_calls]
    actual_fallback = "none"
    if trace.fallback:
        actual_fallback = str(trace.fallback.get("reason") or "irrelevant_request")
    elif trace.agent_fallback:
        actual_fallback = str(trace.agent_fallback.get("reason") or "agent_fallback")
    elif trace.rag_fallback:
        actual_fallback = "rag_no_evidence"

    rag_hit_count = 0
    if trace.rag_rerank:
        rag_hit_count = int(trace.rag_rerank.get("count") or 0)

    reflect_decision = ""
    if trace.agent_reflect:
        reflect_decision = str(trace.agent_reflect.get("decision") or "")

    return EvalSample(
        sample_id=str(labeled.get("id") or ""),
        input=str(labeled.get("input") or ""),
        expected_category=str(labeled.get("expected_category") or ""),
        expected_route=str(labeled.get("expected_route") or ""),
        expected_tools=list(labeled.get("expected_tools") or []),
        expected_fallback=str(labeled.get("expected_fallback") or "none"),
        actual_category=trace.category,
        actual_route=trace.route,
        actual_tools=actual_tools,
        actual_answer=trace.final_answer,
        actual_fallback=actual_fallback,
        clarify_rounds=len(trace.clarify_questions),
        tool_calls=trace.skill_calls,
        tool_results=trace.skill_results,
        rag_hit_count=rag_hit_count,
        reflect_decision=reflect_decision,
        trace_id=trace.trace_id,
        events_file=trace.events_file,
        notes=str(labeled.get("notes") or ""),
    )
