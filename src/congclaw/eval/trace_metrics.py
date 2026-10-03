"""P6-10：链路级数值指标（观测值，不做阈值判定）。

与 :mod:`congclaw.eval.trace_checks` 的分工：

- ``trace_checks`` 判定**离散、可枚举**的结构正确性（顺序 / 配对 / 枚举 / 终态自洽），输出 ✅/❌；
- ``trace_metrics`` 量化**连续、分布类**的量（召回条数、门控裕度、成功率、耗时），输出数值。

两者都不并入 ``pass_rate``。指标只给观测值（均值 / 最小 / 最大 / 样本数），不设阈值，
便于在改动前后、不同模型之间做横向对比；阈值判定等积累出基线后再谈。

数据来源优先 ``events.jsonl``：只有落盘记录带 ``elapsed_ms``，耗时类指标依赖它；
回退到内存事件流（评测中间态、单测）时只产出非耗时指标。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from congclaw.eval.collector import SessionTrace

#: 指标展示名与渲染顺序（按 全局 → RAG → Agent 分组）
METRIC_LABELS: dict[str, str] = {
    "intent_confidence": "意图置信度",
    "intent_ms": "意图判定耗时(ms)",
    "e2e_ms": "端到端耗时(ms)",
    "rag_recall_bm25": "首轮 BM25 召回条数",
    "rag_recall_dense": "首轮 dense 召回条数",
    "rag_recall_empty_channels": "首轮空召回路数",
    "rag_fusion_keep_rate": "RRF 融合保留率",
    "rag_top_rerank_prob": "精排头部相关概率",
    "rag_gate_margin": "证据门裕度(头部概率-阈值)",
    "rag_gate_hit_count": "门控命中条数",
    "rag_rewrite_effective": "重写有效性(1=重写后召回)",
    "rag_parent_count": "父分片回填数",
    "rag_children_per_parent": "每父分片命中子片段数",
    "rag_source_count": "答案引用条数",
    "rag_duration_ms": "RAG 子图耗时(ms)",
    "agent_tool_calls": "工具执行次数",
    "agent_tool_success_rate": "工具成功率",
    "agent_calls_per_round": "每轮思考调用数",
    "agent_first_reflect_pass": "首次反思放行(1/0)",
    "agent_reflect_retries": "反思重试次数",
    "agent_reflect_checks_pass_rate": "反思校验项通过率",
    "agent_duration_ms": "Agent 子图耗时(ms)",
}

# 事件名（与 rag/nodes.py、agent/nodes.py 的埋点一致）
_TURN_START = "session_turn_started"
_TURN_SAVED = "session_turn_saved"
_INTENT = "intent_decision"


@dataclass
class TraceMetrics:
    """一条轨迹的指标观测值（指标名 → 数值，缺省即不适用）。"""

    values: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, float]:
        return dict(self.values)


def compute_metrics(trace: SessionTrace) -> TraceMetrics:
    """计算一条轨迹的链路指标；缺数据的指标不输出（而非记 0）。"""
    events = _iter_events(trace)
    values: dict[str, float] = {}

    _fill(values, _global_metrics(trace, events))
    if any(name.startswith("rag_") for name, _, _ in events):
        _fill(values, _rag_metrics(events))
    if any(name.startswith(("agent_", "skill_")) for name, _, _ in events):
        _fill(values, _agent_metrics(events))

    return TraceMetrics(values=values)


def aggregate_metrics(metrics: Iterable[TraceMetrics]) -> dict[str, dict[str, float]]:
    """按指标名聚合：均值 / 最小 / 最大 / 样本数。

    样本数 ``n`` 是该指标真正适用的样本数（链路里没走到这一步的样本不计入），
    因此不同指标的 n 可以不同，读数时需一并看。
    """
    buckets: dict[str, list[float]] = {}
    for item in metrics:
        for name, value in item.values.items():
            buckets.setdefault(name, []).append(float(value))

    ordered = [name for name in METRIC_LABELS if name in buckets]
    ordered.extend(name for name in buckets if name not in METRIC_LABELS)  # 未登记的新指标也不丢
    return {
        name: {
            "mean": round(sum(buckets[name]) / len(buckets[name]), 2),
            "min": round(min(buckets[name]), 2),
            "max": round(max(buckets[name]), 2),
            "n": len(buckets[name]),
        }
        for name in ordered
    }


# ---------------------------------------------------------------------------
# 全局（意图 / 端到端）
# ---------------------------------------------------------------------------


def _global_metrics(
    trace: SessionTrace, events: list[tuple[str, dict[str, Any], float | None]]
) -> dict[str, float]:
    values: dict[str, float] = {}
    if isinstance(trace.intent_decision, dict):
        _put(values, "intent_confidence", _number(trace.intent_decision.get("confidence")))

    started = _elapsed(events, _TURN_START)
    _put(values, "intent_ms", _delta(_elapsed(events, _INTENT), started))
    _put(values, "e2e_ms", _delta(_elapsed(events, _TURN_SAVED), started))
    return values


# ---------------------------------------------------------------------------
# RAG 子图（检索 → 融合 → 精排 → 门控 → 回溯 → 生成）
# ---------------------------------------------------------------------------


def _rag_metrics(
    events: list[tuple[str, dict[str, Any], float | None]]
) -> dict[str, float]:
    values: dict[str, float] = {}

    retrieves = [(payload, ms) for name, payload, ms in events if name == "rag_retrieve"]
    for channel, key in (("bm25", "rag_recall_bm25"), ("dense", "rag_recall_dense")):
        counts = [
            _number(payload.get("count"))
            for payload, _ in retrieves
            if payload.get("channel") == channel and _is_first_round(payload)
        ]
        _put(values, key, counts[0] if counts else None)

    first_round = [payload for payload, _ in retrieves if _is_first_round(payload)]
    if first_round:
        empty = sum(1 for payload in first_round if (_number(payload.get("count")) or 0.0) == 0)
        _put(values, "rag_recall_empty_channels", float(empty))

    fusions = [payload for name, payload, _ in events if name == "rag_fusion"]
    if fusions:
        last = fusions[-1]
        candidate = (_number(last.get("bm25_count")) or 0.0) + (_number(last.get("dense_count")) or 0.0)
        fused = _number(last.get("fused_count"))
        if candidate > 0 and fused is not None:
            _put(values, "rag_fusion_keep_rate", fused / candidate)

    reranks = [payload for name, payload, _ in events if name == "rag_rerank"]
    if reranks:
        top = reranks[-1].get("top")
        if isinstance(top, list) and top and isinstance(top[0], dict):
            _put(values, "rag_top_rerank_prob", _number(top[0].get("rerank_prob")))

    gates = [payload for name, payload, _ in events if name == "rag_gate"]
    if gates:
        last = gates[-1]
        prob = _number(last.get("top_rerank_prob"))
        threshold = _number(last.get("gate_prob"))
        if prob is not None and threshold is not None:
            # 正值＝过门有余量，越接近 0 越"擦线过门"，负值＝不该过门
            _put(values, "rag_gate_margin", prob - threshold)
        _put(values, "rag_gate_hit_count", _number(last.get("hit_count")))

    rewrite_positions = [
        index for index, (name, _, _) in enumerate(events) if name == "rag_rewrite"
    ]
    if rewrite_positions:
        recalled = any(
            name == "rag_retrieve" and (_number(payload.get("count")) or 0.0) > 0
            for name, payload, _ in events[rewrite_positions[-1] + 1 :]
        )
        _put(values, "rag_rewrite_effective", 1.0 if recalled else 0.0)

    lookups = [payload for name, payload, _ in events if name == "rag_parent_lookup"]
    if lookups:
        last = lookups[-1]
        parents = _number(last.get("parent_count")) or 0.0
        _put(values, "rag_parent_count", parents)
        evidence = last.get("evidence")
        if parents and isinstance(evidence, list):
            children = sum(
                len(item.get("matched_children") or [])
                for item in evidence
                if isinstance(item, dict)
            )
            _put(values, "rag_children_per_parent", children / parents)

    answers = [payload for name, payload, _ in events if name == "rag_answer"]
    if answers:
        _put(values, "rag_source_count", _number(answers[-1].get("sources_count")))

    _put(
        values,
        "rag_duration_ms",
        _delta(_elapsed(events, "rag_finished"), _elapsed(events, "rag_start")),
    )
    return values


# ---------------------------------------------------------------------------
# Agent 子图（思考 → 调用 → 反思）
# ---------------------------------------------------------------------------


def _agent_metrics(
    events: list[tuple[str, dict[str, Any], float | None]]
) -> dict[str, float]:
    values: dict[str, float] = {}

    results = [payload for name, payload, _ in events if name == "skill_result"]
    if results:
        ok = sum(1 for payload in results if payload.get("ok"))
        _put(values, "agent_tool_calls", float(len(results)))
        _put(values, "agent_tool_success_rate", ok / len(results))

    calls = [payload for name, payload, _ in events if name == "skill_call"]
    thinkings = [payload for name, payload, _ in events if name == "agent_thinking"]
    if calls and thinkings:
        _put(values, "agent_calls_per_round", len(calls) / len(thinkings))

    reflects = [payload for name, payload, _ in events if name == "agent_reflect"]
    if reflects:
        _put(
            values,
            "agent_first_reflect_pass",
            1.0 if reflects[0].get("decision") == "pass" else 0.0,
        )
        _put(
            values,
            "agent_reflect_retries",
            float(sum(1 for payload in reflects if payload.get("decision") == "retry")),
        )
        checks = reflects[0].get("checks")
        if isinstance(checks, list) and checks:
            passed = sum(1 for item in checks if isinstance(item, dict) and item.get("passed"))
            _put(values, "agent_reflect_checks_pass_rate", passed / len(checks))

    end = _elapsed(events, "agent_finished") or _elapsed(events, "agent_answer")
    _put(values, "agent_duration_ms", _delta(end, _elapsed(events, "agent_start")))
    return values


# ---------------------------------------------------------------------------
# 事件归一化与取值
# ---------------------------------------------------------------------------


def _iter_events(trace: SessionTrace) -> list[tuple[str, dict[str, Any], float | None]]:
    """归一化为 ``(事件类型, payload, elapsed_ms)``。

    优先读 ``events.jsonl``：落盘记录统一为 ``{seq, elapsed_ms, type, payload}``，
    耗时指标依赖其中的 ``elapsed_ms``；没有文件时回退内存事件流（无耗时）。
    """
    records: list[dict[str, Any]] = []
    path = str(getattr(trace, "events_file", "") or "")
    if path and Path(path).exists():
        try:
            with Path(path).open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        parsed = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(parsed, dict):
                        records.append(parsed)
        except OSError:
            records = []

    if not records:
        records = [event for event in trace.events if isinstance(event, dict)]

    normalized: list[tuple[str, dict[str, Any], float | None]] = []
    for record in records:
        payload = record.get("payload")
        if isinstance(payload, dict) and "seq" in record:  # 落盘记录
            normalized.append(
                (str(record.get("type") or ""), payload, _number(record.get("elapsed_ms")))
            )
        else:  # 内存事件：payload 就是事件本身
            normalized.append((str(record.get("type") or ""), record, None))
    return normalized


def _elapsed(
    events: list[tuple[str, dict[str, Any], float | None]], event_type: str
) -> float | None:
    """取某类事件最后一次出现时的累计耗时（ms）。"""
    hits = [ms for name, _, ms in events if name == event_type and ms is not None]
    return hits[-1] if hits else None


def _delta(end: float | None, start: float | None) -> float | None:
    if end is None or start is None:
        return None
    return max(end - start, 0.0)


def _is_first_round(payload: dict[str, Any]) -> bool:
    """首轮召回：``attempt`` 为 0，或事件未带该字段（内存事件流）。"""
    attempt = _number(payload.get("attempt"))
    return attempt in (None, 0.0)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _put(values: dict[str, float], name: str, value: float | None) -> None:
    if value is not None:
        values[name] = round(float(value), 4)


def _fill(target: dict[str, float], extra: dict[str, float]) -> None:
    target.update(extra)
