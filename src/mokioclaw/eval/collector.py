"""P6-2：从 trace 的 events.jsonl 采集会话轨迹。

trace 文件由 :class:`mokioclaw.core.trace.SessionTraceRecorder` 写出，
每行格式为::

    {"seq": N, "timestamp": "...", "elapsed_ms": 123, "type": "intent_decision", "payload": {...}}

collector 把原始事件流还原为结构化的 :class:`SessionTrace`，
供 normalizer / rule_checks / llm_judge 消费。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SessionTrace:
    """一条会话的结构化轨迹。"""

    trace_id: str
    events_file: str
    events: list[dict[str, Any]] = field(default_factory=list)
    # 关键事件按类型索引（取最后一次，评测以最终状态为准）
    intent_decision: dict[str, Any] | None = None
    clarify_questions: list[dict[str, Any]] = field(default_factory=list)
    fallback: dict[str, Any] | None = None
    rag_retrieves: list[dict[str, Any]] = field(default_factory=list)
    rag_rerank: dict[str, Any] | None = None
    rag_gate: dict[str, Any] | None = None
    rag_answer: dict[str, Any] | None = None
    rag_fallback: dict[str, Any] | None = None
    agent_thinking: dict[str, Any] | None = None
    skill_calls: list[dict[str, Any]] = field(default_factory=list)
    skill_results: list[dict[str, Any]] = field(default_factory=list)
    agent_reflect: dict[str, Any] | None = None
    agent_answer: dict[str, Any] | None = None
    agent_fallback: dict[str, Any] | None = None
    confirm_required: dict[str, Any] | None = None
    final_answer: str = ""

    @property
    def route(self) -> str:
        if self.intent_decision:
            return str(self.intent_decision.get("route") or "")
        return ""

    @property
    def category(self) -> str:
        if self.intent_decision:
            return str(self.intent_decision.get("category") or "")
        return ""


def collect_from_trace_dir(trace_dir: str | Path) -> SessionTrace:
    """从 trace 目录读取 events.jsonl 并解析为 SessionTrace。

    trace_dir 形如 ``{workspace}/.mokioclaw/traces/{trace_id}/``。
    """
    trace_path = Path(trace_dir)
    events_file = trace_path / "events.jsonl"
    if not events_file.exists():
        raise FileNotFoundError(f"trace events file not found: {events_file}")

    trace = SessionTrace(trace_id=trace_path.name, events_file=str(events_file))
    with events_file.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            trace.events.append(record)
            _classify(trace, record)

    # 从事件中抽取最终答复文本
    trace.final_answer = _extract_final_answer(trace)
    return trace


def collect_from_events_file(events_file: str | Path) -> SessionTrace:
    """直接从 events.jsonl 文件路径采集。"""
    return collect_from_trace_dir(Path(events_file).parent)


def collect_from_events(events: list[dict[str, Any]], *, trace_id: str = "in-memory") -> SessionTrace:
    """从内存中的事件列表构建 SessionTrace（供评测运行器直接消费流事件）。

    events 为 graph 流式输出的原始事件（custom_event 的 event 字段或 graph_event）。
    """
    trace = SessionTrace(trace_id=trace_id, events_file="")
    for event in events:
        if not isinstance(event, dict):
            continue
        # 兼容外层包装 {"type": "custom_event", "event": {...}}
        inner = event.get("event") if event.get("type") == "custom_event" else event
        if not isinstance(inner, dict):
            continue
        trace.events.append(inner)
        _classify(trace, {"type": inner.get("type", ""), "payload": inner})
    trace.final_answer = _extract_final_answer(trace)
    return trace


def find_latest_trace_dir(workspace: str | Path) -> Path | None:
    """在 workspace 下找到最新的 trace 目录（按修改时间）。"""
    trace_root = Path(workspace) / ".mokioclaw" / "traces"
    if not trace_root.exists():
        return None
    candidates = [p for p in trace_root.iterdir() if p.is_dir()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _classify(trace: SessionTrace, record: dict[str, Any]) -> None:
    event_type = str(record.get("type", ""))
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return

    if event_type == "intent_decision":
        trace.intent_decision = payload
    elif event_type == "clarify_question":
        trace.clarify_questions.append(payload)
    elif event_type == "fallback_reply":
        trace.fallback = payload
    elif event_type == "rag_retrieve":
        trace.rag_retrieves.append(payload)
    elif event_type == "rag_rerank":
        trace.rag_rerank = payload
    elif event_type == "rag_gate":
        trace.rag_gate = payload
    elif event_type == "rag_answer":
        trace.rag_answer = payload
    elif event_type == "rag_fallback":
        trace.rag_fallback = payload
    elif event_type == "agent_thinking":
        trace.agent_thinking = payload
    elif event_type == "skill_call":
        trace.skill_calls.append(payload)
    elif event_type == "skill_result":
        trace.skill_results.append(payload)
    elif event_type == "agent_reflect":
        trace.agent_reflect = payload
    elif event_type == "agent_answer":
        trace.agent_answer = payload
    elif event_type == "agent_fallback":
        trace.agent_fallback = payload
    elif event_type == "agent_confirm_required":
        trace.confirm_required = payload


def _extract_final_answer(trace: SessionTrace) -> str:
    """按优先级抽取最终用户可见答复。"""
    # 兜底回复
    if trace.fallback:
        return str(trace.fallback.get("reply") or "")
    if trace.agent_fallback:
        from mokioclaw.prompts.agent import AGENT_FALLBACK_REPLY

        return AGENT_FALLBACK_REPLY
    # Agent 直接/生成答复
    if trace.agent_answer:
        preview = str(trace.agent_answer.get("preview") or "")
        return preview
    # RAG 答案预览
    if trace.rag_answer:
        return str(trace.rag_answer.get("answer_preview") or "")
    # 追问话术
    if trace.clarify_questions:
        return str(trace.clarify_questions[-1].get("question") or "")
    return ""
