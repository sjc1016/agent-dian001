"""P6-9：链路级 trace 结构断言。

与 :mod:`congclaw.eval.rule_checks` 的分工：

- ``rule_checks`` 判定样本**终态**（分流 / 参数 / 兜底 / 追问轮次），决定 pass_rate；
- ``trace_checks`` 判定链路**中途**的结构正确性（顺序、配对、枚举、终态自洽），
  单独输出、不并入 pass_rate。

依据的原则是「能断言的就断言」：输出离散、客观、可枚举的环节用硬断言，
判错即实现缺陷。连续、主观、分布类的量（命中数、重试率、耗时等）留给指标层。

当前覆盖 RAG 子图链路与 Agent 子图链路。数据来源为
:class:`congclaw.eval.collector.SessionTrace` 的 ``events`` 原始事件流。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from congclaw.eval.collector import SessionTrace

# 每轮固定两路召回（BM25 + dense）
RAG_RECALL_CHANNELS = ("bm25", "dense")
RAG_FANOUT = len(RAG_RECALL_CHANNELS)
# 每轮各触发一次的下游环节
RAG_PASS_STAGES = ("rag_fusion", "rag_rerank", "rag_gate")
# 证据门决策枚举（见 rag/nodes.py::evidence_gate_node）
RAG_GATE_DECISIONS = ("parent_lookup", "rewrite", "fallback")
# 单轮内允许推进的相位；rewrite 会开启新一轮，回到召回相位
_RAG_PHASE = {
    "rag_start": 0,
    "rag_retrieve": 1,
    "rag_fusion": 2,
    "rag_rerank": 3,
    "rag_gate": 4,
    "rag_parent_lookup": 5,
    "rag_answer": 5,
    "rag_fallback": 5,
}
# 对应 rag.config.MAX_RAG_ATTEMPTS：放宽重写最多 1 次
MAX_RAG_REWRITE = 1
# 反思决策枚举（见 agent/nodes.py::reflect_node）
AGENT_REFLECT_DECISIONS = ("pass", "retry", "fallback")
# Agent 链路的终态事件
AGENT_TERMINALS = ("agent_answer", "agent_fallback")


@dataclass
class TraceAssertion:
    """一条链路结构断言。"""

    name: str
    title: str
    passed: bool
    detail: str = ""


@dataclass
class TraceCheckReport:
    """一条轨迹的链路断言结果。"""

    trace_id: str
    #: 该轨迹是否适用本组断言（非 RAG 轨迹为 False，报告应标注「不适用」）
    applicable: bool
    assertions: list[TraceAssertion] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(item.passed for item in self.assertions)

    @property
    def failures(self) -> list[TraceAssertion]:
        return [item for item in self.assertions if not item.passed]


def run_trace_checks(trace: SessionTrace) -> TraceCheckReport:
    """对一条会话轨迹执行链路结构断言。

    只有链路在本次轨迹中真实出现（或路由声称该走这条路）时才纳入断言，
    两条链路都不涉及则 ``applicable=False``。
    """
    assertions: list[TraceAssertion] = []

    rag_events = _events_of(trace, "rag_")
    if rag_events or trace.route == "rag_answer":
        assertions.extend(_rag_assertions(trace, rag_events))

    agent_events = _events_of(trace, "agent_", "skill_")
    if agent_events or trace.route == "agent_loop":
        assertions.extend(_agent_assertions(trace, agent_events))

    return TraceCheckReport(
        trace_id=trace.trace_id,
        applicable=bool(assertions),
        assertions=assertions,
    )


def _events_of(trace: SessionTrace, *prefixes: str) -> list[dict[str, Any]]:
    """按落盘顺序取出指定前缀的子图事件。"""
    return [
        event
        for event in trace.events
        if isinstance(event, dict)
        and str(event.get("type") or "").startswith(prefixes)
    ]


def _index_of(types: list[str], event_type: str, *, after: int = -1) -> int:
    """返回 ``after`` 之后首次出现 ``event_type`` 的下标，找不到返回 -1。"""
    for index in range(after + 1, len(types)):
        if types[index] == event_type:
            return index
    return -1


def _rag_assertions(trace: SessionTrace, events: list[dict[str, Any]]) -> list[TraceAssertion]:
    types = [str(event.get("type") or "") for event in events]
    counts = Counter(types)
    decisions = [
        str(event.get("decision") or "") for event in events if event.get("type") == "rag_gate"
    ]
    channels = [
        str(event.get("channel") or "") for event in events if event.get("type") == "rag_retrieve"
    ]
    passes = counts.get("rag_fusion", 0)

    return [
        _check_trigger(trace, counts),
        _check_lifecycle(trace, types, counts),
        _check_dual_recall(channels),
        _check_recall_pairing(channels),
        _check_stage_alignment(counts, passes),
        _check_stage_order(types),
        _check_gate_decisions(decisions),
        _check_rewrite(types),
        _check_terminal(decisions, counts),
    ]


def _check_trigger(trace: SessionTrace, counts: Counter[str]) -> TraceAssertion:
    """路由与子图触发必须一致：走 RAG 才进子图，其他路由不得进。"""
    started = counts.get("rag_start", 0) > 0
    if trace.route == "rag_answer":
        passed = started
        detail = "" if started else f"route={trace.route} 但未发出 rag_start"
    else:
        passed = not started
        detail = "" if passed else f"route={trace.route or '未知'} 不应触发 RAG 子图"
    return TraceAssertion("rag_trigger", "RAG 子图触发与路由一致", passed, detail)


def _check_lifecycle(
    trace: SessionTrace, types: list[str], counts: Counter[str]
) -> TraceAssertion:
    """入口到出口必须闭环，且顺序正确；子图异常中断单独报出。"""
    if not counts.get("rag_start", 0):
        return TraceAssertion("rag_lifecycle", "rag_start → rag_finished 闭环", True)
    if counts.get("rag_error", 0):
        return TraceAssertion(
            "rag_lifecycle", "rag_start → rag_finished 闭环", False, "子图抛出异常中断（rag_error）"
        )
    if not counts.get("rag_finished", 0):
        return TraceAssertion(
            "rag_lifecycle", "rag_start → rag_finished 闭环", False, "缺少 rag_finished，链路未闭环"
        )
    passed = _index_of(types, "rag_start") < _index_of(types, "rag_finished")
    return TraceAssertion(
        "rag_lifecycle",
        "rag_start → rag_finished 闭环",
        passed,
        "" if passed else "rag_finished 早于 rag_start",
    )


def _check_dual_recall(channels: list[str]) -> TraceAssertion:
    """首轮必须双路并行召回。"""
    first_pair = channels[:RAG_FANOUT]
    passed = sorted(first_pair) == sorted(RAG_RECALL_CHANNELS)
    return TraceAssertion(
        "rag_dual_recall",
        "首轮双路并行召回（bm25 + dense）",
        passed,
        "" if passed else f"首轮召回通道={first_pair or '无'}",
    )


def _check_recall_pairing(channels: list[str]) -> TraceAssertion:
    """两路召回必须齐备且次数相等，避免单路丢失或通道值非法。"""
    tally = Counter(channels)
    unknown = sorted({item for item in channels if item not in RAG_RECALL_CHANNELS})
    if unknown:
        return TraceAssertion(
            "rag_recall_pairing", "两路召回成对且通道齐备", False, f"未知召回通道={unknown}"
        )
    missing = sorted(set(RAG_RECALL_CHANNELS) - set(tally))
    if missing:
        return TraceAssertion(
            "rag_recall_pairing", "两路召回成对且通道齐备", False, f"缺失召回通道={missing}"
        )
    passed = len(set(tally.values())) <= 1
    return TraceAssertion(
        "rag_recall_pairing",
        "两路召回成对且通道齐备",
        passed,
        "" if passed else f"两路召回次数不配对: {dict(tally)}",
    )


def _check_stage_alignment(counts: Counter[str], passes: int) -> TraceAssertion:
    """每轮环节事件数固定为 召回×2 : 融合×1 : 精排×1 : 门控×1。"""
    expected = {"rag_retrieve": RAG_FANOUT * passes}
    expected.update({name: passes for name in RAG_PASS_STAGES})
    actual = {name: counts.get(name, 0) for name in expected}
    passed = actual == expected
    return TraceAssertion(
        "rag_stage_alignment",
        "环节事件数比例 2:1:1:1（召回×2 → 融合/精排/门控各 1）",
        passed,
        "" if passed else f"期望={expected} 实际={actual}",
    )


def _check_stage_order(types: list[str]) -> TraceAssertion:
    """同一轮内相位只能前进：start → 召回 → 融合 → 精排 → 门控 → 回溯/生成。"""
    phase = -1
    for index, event_type in enumerate(types):
        if event_type == "rag_rewrite":
            phase = 0  # 重写开启新一轮，回到召回前的相位
            continue
        rank = _RAG_PHASE.get(event_type)
        if rank is None:
            continue
        if rank < phase:
            return TraceAssertion(
                "rag_stage_order",
                "同轮环节顺序不可逆",
                False,
                f"第 {index + 1} 个事件 {event_type} 逆序（当前已推进到相位 {phase}）",
            )
        phase = rank
    return TraceAssertion("rag_stage_order", "同轮环节顺序不可逆", True)


def _check_gate_decisions(decisions: list[str]) -> TraceAssertion:
    """证据门决策必须落在枚举内。"""
    invalid = sorted({item for item in decisions if item not in RAG_GATE_DECISIONS})
    return TraceAssertion(
        "rag_gate_decision",
        "证据门决策取值合法",
        not invalid,
        "" if not invalid else f"非法决策={invalid}，允许值={list(RAG_GATE_DECISIONS)}",
    )


def _check_rewrite(types: list[str]) -> TraceAssertion:
    """重写至多一次，且重写后必须重新召回并再次门控。"""
    rewrites = [index for index, item in enumerate(types) if item == "rag_rewrite"]
    problems: list[str] = []
    if len(rewrites) > MAX_RAG_REWRITE:
        problems.append(f"重写 {len(rewrites)} 次，超出上限 {MAX_RAG_REWRITE}")
    for index in rewrites:
        if _index_of(types, "rag_retrieve", after=index) < 0:
            problems.append("重写后未重新召回")
        if _index_of(types, "rag_gate", after=index) < 0:
            problems.append("重写后未重新门控")
    return TraceAssertion(
        "rag_rewrite_rerun",
        f"重写后重新召回并再次门控（至多 {MAX_RAG_REWRITE} 次）",
        not problems,
        "；".join(problems),
    )


def _check_terminal(decisions: list[str], counts: Counter[str]) -> TraceAssertion:
    """终态必须与末次门控决策一致，且答案与兜底互斥。"""
    has_answer = counts.get("rag_answer", 0) > 0
    has_fallback = counts.get("rag_fallback", 0) > 0
    last_decision = decisions[-1] if decisions else ""
    problems: list[str] = []
    if has_answer and has_fallback:
        problems.append("同时出现 rag_answer 与 rag_fallback")
    if last_decision == "parent_lookup" and not has_answer:
        problems.append("末次门控为 parent_lookup 但未生成答案")
    if last_decision == "fallback" and not has_fallback:
        problems.append("末次门控为 fallback 但未走兜底")
    if last_decision == "rewrite":
        problems.append("末次门控仍为 rewrite，链路未收敛")
    if has_answer and not counts.get("rag_parent_lookup", 0):
        problems.append("生成答案前未做父分片回溯")
    return TraceAssertion(
        "rag_terminal_consistency",
        "终态与末次门控决策一致",
        not problems,
        "；".join(problems),
    )


# ---------------------------------------------------------------------------
# Agent 子图链路
# ---------------------------------------------------------------------------


def _agent_assertions(
    trace: SessionTrace, events: list[dict[str, Any]]
) -> list[TraceAssertion]:
    counts = Counter(str(event.get("type") or "") for event in events)
    return [
        _check_agent_trigger(trace, counts),
        _check_call_pairing(events),
        _check_agent_terminal(counts),
        _check_confirm_isolation(events),
        _check_reflect(events),
    ]


def _check_agent_trigger(trace: SessionTrace, counts: Counter[str]) -> TraceAssertion:
    """路由与子图触发必须一致：走 Agent 才进子图，其他路由不得进。"""
    started = counts.get("agent_start", 0) > 0
    if trace.route == "agent_loop":
        passed = started
        detail = "" if started else f"route={trace.route} 但未发出 agent_start"
    else:
        passed = not started
        detail = "" if passed else f"route={trace.route or '未知'} 不应触发 Agent 子图"
    return TraceAssertion("agent_trigger", "Agent 子图触发与路由一致", passed, detail)


def _check_call_pairing(events: list[dict[str, Any]]) -> TraceAssertion:
    """每次 skill_call 必须产出配对的 skill_result，且不得有孤儿结果。"""
    calls = [event for event in events if event.get("type") == "skill_call"]
    results = [event for event in events if event.get("type") == "skill_result"]
    if not calls and not results:
        return TraceAssertion("agent_call_pairing", "skill_call ↔ skill_result 一一配对", True)

    call_ids = [str(event.get("call_id") or "") for event in calls]
    result_ids = [str(event.get("call_id") or "") for event in results]
    if not all(call_ids) or not all(result_ids):
        return TraceAssertion(
            "agent_call_pairing",
            "skill_call ↔ skill_result 一一配对",
            False,
            "存在缺失 call_id 的调用或结果事件，无法配对",
        )

    call_set, result_set = set(call_ids), set(result_ids)
    problems: list[str] = []
    missing = sorted(call_set - result_set)
    orphan = sorted(result_set - call_set)
    if missing:
        problems.append(f"调用未产出结果 call_id={missing}")
    if orphan:
        problems.append(f"结果缺少调用 call_id={orphan}")
    return TraceAssertion(
        "agent_call_pairing",
        "skill_call ↔ skill_result 一一配对",
        not problems,
        "；".join(problems),
    )


def _check_agent_terminal(counts: Counter[str]) -> TraceAssertion:
    """Agent 链路必须收敛到答复或兜底，且两者互斥。"""
    terminals = [name for name in AGENT_TERMINALS if counts.get(name, 0)]
    problems: list[str] = []
    if len(terminals) > 1:
        problems.append("同时出现 " + " 与 ".join(terminals))
    if counts.get("agent_start", 0) and not terminals:
        problems.append("链路结束但无终态事件（agent_answer / agent_fallback）")
    return TraceAssertion(
        "agent_terminal", "终态为答复或兜底且互斥", not problems, "；".join(problems)
    )


def _check_confirm_isolation(events: list[dict[str, Any]]) -> TraceAssertion:
    """写操作弹出确认卡片时，同一回合不得执行该 Skill（inline 模式先确认后执行）。"""
    confirmations = [
        event for event in events if event.get("type") == "agent_confirm_required"
    ]
    if not confirmations:
        return TraceAssertion("agent_confirm_isolation", "确认卡片与执行互斥", True)

    problems: list[str] = []
    missing_fields = [
        index
        for index, event in enumerate(confirmations, start=1)
        if not event.get("skill_name") or not event.get("approval_id")
    ]
    if missing_fields:
        problems.append(f"第 {missing_fields} 张确认卡片缺少 skill_name / approval_id")

    confirmed = {str(event.get("skill_name") or "") for event in confirmations}
    executed = {
        str(event.get("name") or "")
        for event in events
        if event.get("type") == "skill_call"
    }
    leaked = sorted(confirmed & executed)
    if leaked:
        problems.append(f"已弹确认卡片却仍执行 {leaked}")
    return TraceAssertion(
        "agent_confirm_isolation", "确认卡片与执行互斥", not problems, "；".join(problems)
    )


def _check_reflect(events: list[dict[str, Any]]) -> TraceAssertion:
    """反思决策必须落在枚举内，且与工具结果、后续链路自洽。

    判定窗口按轮切分：只看该次反思之前、上一次反思之后的工具结果，
    避免把上一轮的失败误算到本轮。
    """
    positions = [
        index for index, event in enumerate(events) if event.get("type") == "agent_reflect"
    ]
    if not positions:
        return TraceAssertion("agent_reflect_consistency", "反思决策与工具结果自洽", True)

    problems: list[str] = []
    for order, index in enumerate(positions):
        decision = str(events[index].get("decision") or "")
        window_start = positions[order - 1] + 1 if order else 0
        failed_tools = [
            str(event.get("name") or "")
            for event in events[window_start:index]
            if event.get("type") == "skill_result" and not event.get("ok")
        ]
        remaining = [str(event.get("type") or "") for event in events[index + 1 :]]
        if decision not in AGENT_REFLECT_DECISIONS:
            problems.append(f"非法反思决策 {decision!r}")
        elif decision == "pass" and failed_tools:
            problems.append(f"存在失败工具 {failed_tools} 却放行（pass）")
        elif decision == "retry" and "skill_call" not in remaining:
            problems.append("retry 后未重新调用工具")
        elif decision == "fallback" and "agent_fallback" not in remaining:
            problems.append("fallback 决策后未走兜底")
    return TraceAssertion(
        "agent_reflect_consistency",
        "反思决策与工具结果自洽",
        not problems,
        "；".join(problems),
    )
