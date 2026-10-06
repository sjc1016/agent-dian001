"""阶段 6：自动化评测流水线单测。

覆盖 collector / normalizer / rule_checks / llm_judge（退化路径）/ report / runner。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from congclaw.eval.collector import SessionTrace, collect_from_events, collect_from_trace_dir
from congclaw.eval.llm_judge import JudgeScores, _fallback_scores, judge_answer
from congclaw.eval.normalizer import EvalSample, normalize
from congclaw.eval.report import EvalReport, SampleEvalResult, render_markdown, write_report
from congclaw.eval.rule_checks import run_rule_checks
from congclaw.eval.trace_checks import TraceAssertion, TraceCheckReport, run_trace_checks
from congclaw.eval.trace_metrics import compute_metrics


def _make_events() -> list[dict]:
    """构造一条 agent_service 查余额的事件流。"""
    return [
        {"type": "session_started", "session_id": "s1"},
        {"type": "profile_loaded", "phone": "13800138000", "exists": False},
        {"type": "session_turn_started", "turn": 1, "task": "查余额"},
        {"type": "query_rewrite", "changed": False, "original": "查余额", "rewritten": "查余额"},
        {
            "type": "intent_decision",
            "route": "agent_loop",
            "category": "agent_service",
            "reason": "查询话费余额",
            "confidence": 0.9,
        },
        {"type": "agent_start", "query": "查余额"},
        {
            "type": "agent_thinking",
            "reasoning": "需要查询余额",
            "calls": [{"name": "query_balance", "args": {}, "call_id": "c1"}],
        },
        {"type": "skill_call", "name": "query_balance", "call_id": "c1", "args": {}},
        {"type": "skill_result", "name": "query_balance", "call_id": "c1", "ok": True, "preview": "86.5"},
        {"type": "agent_reflect", "decision": "pass", "reason": "ok", "checks": []},
        {"type": "agent_answer", "mode": "generated", "preview": "您的余额为 86.50 元。"},
        {"type": "session_turn_saved", "turn": 1, "route": "agent_loop"},
    ]


LABELED_BALANCE = {
    "id": "E01",
    "input": "查一下我话费余额",
    "expected_category": "agent_service",
    "expected_route": "agent_loop",
    "expected_tools": ["query_balance"],
    "expected_fallback": "none",
    "notes": "查余额",
}


# ---------------------------------------------------------------------------
# collector
# ---------------------------------------------------------------------------


def test_collect_from_events_parses_agent_trace() -> None:
    trace = collect_from_events(_make_events(), trace_id="t1")
    assert trace.trace_id == "t1"
    assert trace.category == "agent_service"
    assert trace.route == "agent_loop"
    assert len(trace.skill_calls) == 1
    assert trace.skill_calls[0]["name"] == "query_balance"
    assert trace.agent_reflect is not None
    assert trace.agent_reflect["decision"] == "pass"
    assert trace.agent_start is not None
    assert trace.session_turn_saved is not None
    assert "86.50" in trace.final_answer


def test_collect_from_events_indexes_rag_lifecycle() -> None:
    """RAG 链路中段事件（重写/启动/融合/回溯/结束）都要被索引。"""
    events = [
        {"type": "query_rewrite", "changed": True, "original": "它的月费是多少", "rewritten": "5G畅享199元档月费"},
        {"type": "intent_decision", "category": "rag_query", "route": "rag_answer", "confidence": 0.9},
        {"type": "rag_start", "query": "5G畅享套餐包含多少流量"},
        {"type": "rag_retrieve", "channel": "bm25", "count": 5},
        {"type": "rag_retrieve", "channel": "dense", "count": 5},
        {"type": "rag_fusion", "count": 7},
        {"type": "rag_rerank", "count": 3},
        {"type": "rag_gate", "decision": "parent_lookup", "reason": "evidence_hit"},
        {"type": "rag_parent_lookup", "parent_count": 2},
        {"type": "rag_answer", "answer_preview": "199 元档含 100GB 流量[1]。"},
        {"type": "rag_finished", "answer_chars": 24},
        {"type": "session_turn_saved", "turn": 1, "route": "rag_answer"},
    ]
    trace = collect_from_events(events)

    assert trace.query_rewrite is not None
    assert trace.query_rewrite["changed"] is True
    assert trace.rag_start is not None
    assert {e["channel"] for e in trace.rag_retrieves} == {"bm25", "dense"}
    assert trace.rag_fusion is not None
    assert trace.rag_parent_lookup is not None
    assert trace.rag_parent_lookup["parent_count"] == 2
    assert trace.rag_finished is not None
    assert trace.session_turn_saved is not None


def test_collect_from_events_indexes_rag_rewrite_branch() -> None:
    """重写分支：rag_rewrite 被索引，单例事件按「取最后一次」覆盖。"""
    events = [
        {"type": "rag_start", "query": "那个多少钱"},
        {"type": "rag_retrieve", "channel": "bm25", "count": 0},
        {"type": "rag_fusion", "count": 0},
        {"type": "rag_rerank", "count": 0},
        {"type": "rag_gate", "decision": "rewrite", "reason": "no_evidence"},
        {"type": "rag_rewrite", "original_query": "那个多少钱", "rewritten_query": "199元5G套餐多少钱"},
        {"type": "rag_retrieve", "channel": "bm25", "count": 3},
        {"type": "rag_fusion", "count": 3},
        {"type": "rag_rerank", "count": 2},
        {"type": "rag_gate", "decision": "parent_lookup", "reason": "evidence_hit"},
        {"type": "rag_parent_lookup", "parent_count": 1},
        {"type": "rag_answer", "answer_preview": "199 元档月费 199 元[1]。"},
        {"type": "rag_finished", "answer_chars": 20},
    ]
    trace = collect_from_events(events)

    assert trace.rag_rewrite is not None
    assert trace.rag_rewrite["rewritten_query"] == "199元5G套餐多少钱"
    assert len(trace.rag_retrieves) == 2
    assert trace.rag_fusion["count"] == 3
    assert trace.rag_gate["decision"] == "parent_lookup"


def test_collect_from_trace_dir_reads_jsonl(tmp_path: Path) -> None:
    trace_dir = tmp_path / "trace-001"
    trace_dir.mkdir()
    events = [
        {"seq": 1, "type": "intent_decision", "payload": {"type": "intent_decision", "category": "rag_query", "route": "rag_answer"}},
        {"seq": 2, "type": "rag_answer", "payload": {"type": "rag_answer", "answer_preview": "5G套餐含100GB"}},
    ]
    with (trace_dir / "events.jsonl").open("w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    trace = collect_from_trace_dir(trace_dir)
    assert trace.category == "rag_query"
    assert trace.route == "rag_answer"
    assert "100GB" in trace.final_answer


def test_collect_from_events_irrelevant_fallback() -> None:
    events = [
        {"type": "intent_decision", "category": "irrelevant", "route": "fallback", "confidence": 0.8},
        {"type": "fallback_reply", "reason": "irrelevant_request", "reply": "抱歉我是电信客服"},
    ]
    trace = collect_from_events(events)
    assert trace.category == "irrelevant"
    assert trace.fallback is not None
    assert "电信客服" in trace.final_answer


# ---------------------------------------------------------------------------
# normalizer
# ---------------------------------------------------------------------------


def test_normalize_agent_service_sample() -> None:
    trace = collect_from_events(_make_events())
    sample = normalize(trace, LABELED_BALANCE)
    assert sample.sample_id == "E01"
    assert sample.actual_category == "agent_service"
    assert sample.actual_route == "agent_loop"
    assert sample.actual_tools == ["query_balance"]
    assert sample.actual_fallback == "none"
    assert sample.clarify_rounds == 0


# ---------------------------------------------------------------------------
# rule_checks
# ---------------------------------------------------------------------------


def test_rule_checks_pass_for_correct_balance_sample() -> None:
    trace = collect_from_events(_make_events())
    sample = normalize(trace, LABELED_BALANCE)
    report = run_rule_checks(sample)
    assert report.passed
    names = {r.name for r in report.results}
    assert names == {"routing", "tool_params", "fallback", "clarify_rounds"}


def test_rule_checks_detect_wrong_routing() -> None:
    trace = collect_from_events(_make_events())
    labeled = dict(LABELED_BALANCE)
    labeled["expected_category"] = "rag_query"
    labeled["expected_route"] = "rag_answer"
    sample = normalize(trace, labeled)
    report = run_rule_checks(sample)
    assert not report.passed
    routing = next(r for r in report.results if r.name == "routing")
    assert not routing.passed


def test_rule_checks_detect_missing_expected_tool() -> None:
    events = [
        {"type": "intent_decision", "category": "agent_service", "route": "agent_loop"},
        {"type": "agent_answer", "mode": "direct", "preview": "好的"},
    ]
    trace = collect_from_events(events)
    labeled = dict(LABELED_BALANCE)
    sample = normalize(trace, labeled)
    report = run_rule_checks(sample)
    tool = next(r for r in report.results if r.name == "tool_params")
    assert not tool.passed
    assert "query_balance" in tool.detail


def test_rule_checks_irrelevant_should_fallback() -> None:
    events = [
        {"type": "intent_decision", "category": "irrelevant", "route": "fallback"},
        {"type": "fallback_reply", "reason": "irrelevant_request", "reply": "抱歉"},
    ]
    trace = collect_from_events(events)
    labeled = {
        "id": "E28",
        "input": "写首诗",
        "expected_category": "irrelevant",
        "expected_route": "fallback",
        "expected_tools": [],
        "expected_fallback": "irrelevant_request",
    }
    sample = normalize(trace, labeled)
    report = run_rule_checks(sample)
    fb = next(r for r in report.results if r.name == "fallback")
    assert fb.passed


def test_rule_checks_clarify_rounds_within_limit() -> None:
    events = [
        {"type": "intent_decision", "category": "clarify", "route": "clarify"},
        {"type": "clarify_question", "question": "请问您要办理什么业务？", "clarify_count": 1},
        {"type": "clarify_question", "question": "能再说具体点吗？", "clarify_count": 2},
    ]
    trace = collect_from_events(events)
    labeled = {
        "id": "E25",
        "input": "我想换个",
        "expected_category": "clarify",
        "expected_route": "clarify",
        "expected_tools": [],
        "expected_fallback": "none",
    }
    sample = normalize(trace, labeled)
    report = run_rule_checks(sample)
    clarify = next(r for r in report.results if r.name == "clarify_rounds")
    assert clarify.passed
    assert sample.clarify_rounds == 2


def test_rule_checks_clarify_rounds_over_limit_fails() -> None:
    sample = EvalSample(
        sample_id="E26",
        input="模糊",
        expected_category="clarify",
        expected_route="clarify",
        expected_tools=[],
        expected_fallback="none",
        actual_category="clarify",
        actual_route="clarify",
        actual_tools=[],
        actual_answer="能具体点吗",
        actual_fallback="none",
        clarify_rounds=6,
    )
    report = run_rule_checks(sample)
    clarify = next(r for r in report.results if r.name == "clarify_rounds")
    assert not clarify.passed
    assert not report.passed


def test_rule_checks_business_sample_unexpected_fallback_fails() -> None:
    """业务类样本（expected_fallback=none）意外触发兜底应判失败。"""
    sample = EvalSample(
        sample_id="E02",
        input="查余额",
        expected_category="agent_service",
        expected_route="agent_loop",
        expected_tools=["query_balance"],
        expected_fallback="none",
        actual_category="agent_service",
        actual_route="agent_loop",
        actual_tools=["query_balance"],
        actual_answer="",
        actual_fallback="agent_fallback",
        clarify_rounds=0,
        tool_calls=[{"name": "query_balance", "args": {}}],
    )
    report = run_rule_checks(sample)
    fb = next(r for r in report.results if r.name == "fallback")
    assert not fb.passed
    assert "unexpected fallback" in fb.detail


def test_rule_checks_tool_param_invalid_enum_detected() -> None:
    """枚举值非法被注册表参数声明捕获（report_fault.fault_type 有 enum）。"""
    sample = EvalSample(
        sample_id="E11",
        input="报修",
        expected_category="agent_service",
        expected_route="agent_loop",
        expected_tools=["report_fault"],
        expected_fallback="none",
        actual_category="agent_service",
        actual_route="agent_loop",
        actual_tools=["report_fault"],
        actual_answer="",
        actual_fallback="none",
        clarify_rounds=0,
        tool_calls=[{
            "name": "report_fault",
            "args": {"fault_type": "外星信号", "description": "断网"},
        }],
    )
    report = run_rule_checks(sample)
    tool = next(r for r in report.results if r.name == "tool_params")
    assert not tool.passed
    assert "fault_type" in tool.detail


def test_rule_checks_tool_param_missing_required_detected() -> None:
    sample = EvalSample(
        sample_id="E12",
        input="报修",
        expected_category="agent_service",
        expected_route="agent_loop",
        expected_tools=["report_fault"],
        expected_fallback="none",
        actual_category="agent_service",
        actual_route="agent_loop",
        actual_tools=["report_fault"],
        actual_answer="",
        actual_fallback="none",
        clarify_rounds=0,
        tool_calls=[{"name": "report_fault", "args": {"fault_type": "宽带故障"}}],
    )
    report = run_rule_checks(sample)
    tool = next(r for r in report.results if r.name == "tool_params")
    assert not tool.passed
    assert "description" in tool.detail


# ---------------------------------------------------------------------------
# trace_checks：RAG 链路结构断言
# ---------------------------------------------------------------------------


def _rag_hit_events() -> list[dict]:
    """命中路径：双路召回 → 融合 → 精排 → parent_lookup → 生成。"""
    return [
        {"type": "query_rewrite", "changed": False, "original": "套餐流量", "rewritten": "套餐流量"},
        {"type": "intent_decision", "route": "rag_answer", "category": "rag_query", "confidence": 0.92},
        {"type": "rag_start", "query": "5G畅享套餐包含多少流量"},
        {"type": "rag_retrieve", "channel": "bm25", "count": 5, "attempt": 0},
        {"type": "rag_retrieve", "channel": "dense", "count": 5, "attempt": 0},
        {"type": "rag_fusion", "bm25_count": 5, "dense_count": 5, "fused_count": 7},
        {"type": "rag_rerank", "count": 3},
        {"type": "rag_gate", "decision": "parent_lookup", "reason": "evidence_hit", "attempts": 0},
        {"type": "rag_parent_lookup", "parent_count": 2},
        {"type": "rag_answer", "sources_count": 2, "answer_preview": "199 元档含 100GB 流量[1]。"},
        {"type": "rag_finished", "has_answer": True, "sources_count": 2, "rag_attempts": 0},
    ]


def _rag_rewrite_events() -> list[dict]:
    """重写路径：首轮无证据 → rewrite → 重新召回并命中。"""
    return [
        {"type": "intent_decision", "route": "rag_answer", "category": "rag_query", "confidence": 0.8},
        {"type": "rag_start", "query": "那个多少钱"},
        {"type": "rag_retrieve", "channel": "bm25", "count": 0, "attempt": 0},
        {"type": "rag_retrieve", "channel": "dense", "count": 0, "attempt": 0},
        {"type": "rag_fusion", "bm25_count": 0, "dense_count": 0, "fused_count": 0},
        {"type": "rag_rerank", "count": 0},
        {"type": "rag_gate", "decision": "rewrite", "reason": "no_evidence", "attempts": 0},
        {"type": "rag_rewrite", "original_query": "那个多少钱", "rewritten_query": "199元5G套餐多少钱", "attempts": 1},
        {"type": "rag_retrieve", "channel": "bm25", "count": 3, "attempt": 1},
        {"type": "rag_retrieve", "channel": "dense", "count": 2, "attempt": 1},
        {"type": "rag_fusion", "bm25_count": 3, "dense_count": 2, "fused_count": 4},
        {"type": "rag_rerank", "count": 2},
        {"type": "rag_gate", "decision": "parent_lookup", "reason": "evidence_hit", "attempts": 1},
        {"type": "rag_parent_lookup", "parent_count": 1},
        {"type": "rag_answer", "sources_count": 1, "answer_preview": "199 元[1]。"},
        {"type": "rag_finished", "has_answer": True, "sources_count": 1, "rag_attempts": 1},
    ]


def _rag_fallback_events() -> list[dict]:
    """兜底路径：重写后仍无证据 → fallback。"""
    events = _rag_rewrite_events()
    terminal = [
        {"type": "rag_gate", "decision": "fallback", "reason": "no_evidence_after_rewrite", "attempts": 1},
        {"type": "rag_fallback", "reason": "rag_no_evidence", "attempts": 1},
        {"type": "rag_finished", "has_answer": False, "sources_count": 0, "rag_attempts": 1},
    ]
    return events[:12] + terminal


def _assertion(report: TraceCheckReport, name: str) -> TraceAssertion:
    return next(item for item in report.assertions if item.name == name)


def test_trace_checks_pass_on_hit_path() -> None:
    report = run_trace_checks(collect_from_events(_rag_hit_events()))
    assert report.applicable
    assert report.passed, report.failures


def test_trace_checks_pass_on_rewrite_path() -> None:
    report = run_trace_checks(collect_from_events(_rag_rewrite_events()))
    assert report.applicable
    assert report.passed, report.failures


def test_trace_checks_pass_on_fallback_path() -> None:
    report = run_trace_checks(collect_from_events(_rag_fallback_events()))
    assert report.applicable
    assert report.passed, report.failures


def test_trace_checks_skip_plain_trace() -> None:
    """既不走 RAG 也不走 Agent（如 clarify）的轨迹不适用链路断言。"""
    events = [
        {"type": "intent_decision", "route": "clarify", "category": "clarify", "confidence": 0.4},
        {"type": "clarify_question", "question": "请问您是想查询话费还是办理套餐？"},
    ]
    report = run_trace_checks(collect_from_events(events))
    assert not report.applicable
    assert report.assertions == []


def test_trace_checks_pass_on_agent_path() -> None:
    """agent_service 轨迹只跑 Agent 侧断言，不掺入 RAG 断言。"""
    report = run_trace_checks(collect_from_events(_make_events()))
    assert report.applicable
    assert {item.name for item in report.assertions} == {
        "agent_trigger",
        "agent_call_pairing",
        "agent_terminal",
        "agent_confirm_isolation",
        "agent_reflect_consistency",
    }
    assert report.passed, report.failures


def test_trace_checks_detect_agent_routing_mismatch() -> None:
    events = _make_events()
    events[4] = {"type": "intent_decision", "route": "clarify", "category": "clarify"}
    report = run_trace_checks(collect_from_events(events))
    assert not _assertion(report, "agent_trigger").passed


def test_trace_checks_detect_unpaired_skill_result() -> None:
    events = [e for e in _make_events() if e.get("type") != "skill_result"]
    report = run_trace_checks(collect_from_events(events))
    pairing = _assertion(report, "agent_call_pairing")
    assert not pairing.passed
    assert "未产出结果" in pairing.detail


def test_trace_checks_detect_agent_answer_and_fallback_conflict() -> None:
    events = _make_events()
    events.insert(11, {"type": "agent_fallback", "reason": "attempts_exceeded"})
    report = run_trace_checks(collect_from_events(events))
    assert not _assertion(report, "agent_terminal").passed


def test_trace_checks_detect_confirm_card_leaked_execution() -> None:
    """写操作既弹了确认卡片又执行了，属于审批短路失效。"""
    events = _make_events()
    events.insert(
        6,
        {"type": "agent_confirm_required", "approval_id": "ap-1", "skill_name": "query_balance"},
    )
    report = run_trace_checks(collect_from_events(events))
    isolation = _assertion(report, "agent_confirm_isolation")
    assert not isolation.passed
    assert "仍执行" in isolation.detail


def test_trace_checks_detect_reflect_pass_with_failed_tool() -> None:
    events = _make_events()
    events[8] = {"type": "skill_result", "name": "query_balance", "call_id": "c1", "ok": False}
    report = run_trace_checks(collect_from_events(events))
    consistency = _assertion(report, "agent_reflect_consistency")
    assert not consistency.passed
    assert "却放行" in consistency.detail


def test_trace_checks_detect_retry_without_reinvoke() -> None:
    events = _make_events()
    events[9] = {"type": "agent_reflect", "decision": "retry", "reason": "try again"}
    report = run_trace_checks(collect_from_events(events))
    consistency = _assertion(report, "agent_reflect_consistency")
    assert not consistency.passed
    assert "未重新调用" in consistency.detail


def test_trace_checks_detect_agent_fallback_without_fallback_event() -> None:
    events = _make_events()
    events[9] = {"type": "agent_reflect", "decision": "fallback", "reason": "hard failure"}
    report = run_trace_checks(collect_from_events(events))
    consistency = _assertion(report, "agent_reflect_consistency")
    assert not consistency.passed
    assert "未走兜底" in consistency.detail


def test_trace_checks_detect_routing_mismatch() -> None:
    events = _rag_hit_events()
    events[1] = {"type": "intent_decision", "route": "agent_loop", "category": "agent_service"}
    report = run_trace_checks(collect_from_events(events))
    assert not _assertion(report, "rag_trigger").passed
    assert not report.passed


def test_trace_checks_detect_missing_recall_channel() -> None:
    events = [e for e in _rag_hit_events() if e.get("channel") != "dense"]
    report = run_trace_checks(collect_from_events(events))
    assert not _assertion(report, "rag_dual_recall").passed
    assert not _assertion(report, "rag_recall_pairing").passed
    assert not _assertion(report, "rag_stage_alignment").passed


def test_trace_checks_detect_stage_disorder() -> None:
    events = _rag_hit_events()
    fusion = events.pop(5)
    events.insert(2, fusion)
    report = run_trace_checks(collect_from_events(events))
    assert not _assertion(report, "rag_stage_order").passed
    assert "逆序" in _assertion(report, "rag_stage_order").detail


def test_trace_checks_detect_invalid_gate_decision() -> None:
    events = _rag_hit_events()
    events[7] = {"type": "rag_gate", "decision": "直接回答", "attempts": 0}
    report = run_trace_checks(collect_from_events(events))
    assert not _assertion(report, "rag_gate_decision").passed


def test_trace_checks_detect_rewrite_without_rerun() -> None:
    events = _rag_rewrite_events()
    gate_index = next(i for i, e in enumerate(events) if e.get("decision") == "rewrite")
    events.insert(gate_index + 1, {"type": "rag_rewrite", "attempts": 1})
    report = run_trace_checks(collect_from_events(events))
    rewrite = _assertion(report, "rag_rewrite_rerun")
    assert not rewrite.passed
    assert "超出上限" in rewrite.detail


def test_trace_checks_detect_answer_and_fallback_conflict() -> None:
    events = _rag_hit_events()
    events.insert(9, {"type": "rag_fallback", "reason": "rag_no_evidence", "attempts": 0})
    report = run_trace_checks(collect_from_events(events))
    assert not _assertion(report, "rag_terminal_consistency").passed


# ---------------------------------------------------------------------------
# llm_judge（退化路径，不依赖真实 LLM）
# ---------------------------------------------------------------------------


def test_judge_answer_fallback_when_llm_unavailable(monkeypatch) -> None:
    import asyncio

    trace = collect_from_events(_make_events())
    sample = normalize(trace, LABELED_BALANCE)

    # 强制 provider 层 create_model 抛错，触发退化（judge_answer 内部从 provider 导入）
    def _boom(*args, **kwargs):
        raise RuntimeError("no api key")

    monkeypatch.setattr("congclaw.providers.openai_provider.create_model", _boom)

    scores = asyncio.run(judge_answer(sample))
    assert scores.fallback is True
    assert 1 <= scores.answer_quality <= 5
    assert 1 <= scores.reasoning <= 5
    assert 1 <= scores.compliance <= 5


def test_fallback_scores_for_irrelevant() -> None:
    sample = EvalSample(
        sample_id="E28",
        input="写首诗",
        expected_category="irrelevant",
        expected_route="fallback",
        expected_tools=[],
        expected_fallback="irrelevant_request",
        actual_category="irrelevant",
        actual_route="fallback",
        actual_tools=[],
        actual_answer="抱歉我是电信客服",
        actual_fallback="irrelevant_request",
        clarify_rounds=0,
    )
    scores = _fallback_scores(sample)
    assert scores.compliance >= 3  # 无关请求正确兜底


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def test_render_markdown_contains_pass_rate_and_scores() -> None:
    trace = collect_from_events(_make_events())
    sample = normalize(trace, LABELED_BALANCE)
    rule_report = run_rule_checks(sample)
    result = SampleEvalResult(
        sample=sample,
        rule_report=rule_report,
        judge_scores=JudgeScores(answer_quality=4, reasoning=4, compliance=5),
    )
    report = EvalReport(
        report_id="eval-test",
        created_at="2026-01-01T00:00:00Z",
        total=1,
        passed=1,
        results=[result],
    )
    md = render_markdown(report)
    assert "电信客服智能体评测报告" in md
    assert "100.0%" in md
    assert "答案质量" in md
    assert "E01" in md


def test_write_report_creates_md_and_json(tmp_path: Path) -> None:
    trace = collect_from_events(_make_events())
    sample = normalize(trace, LABELED_BALANCE)
    rule_report = run_rule_checks(sample)
    trace_report = run_trace_checks(trace)
    result = SampleEvalResult(
        sample=sample, rule_report=rule_report, trace_report=trace_report
    )
    report = EvalReport(
        report_id="eval-write",
        created_at="2026-01-01T00:00:00Z",
        total=1,
        passed=1,
        results=[result],
    )
    out = tmp_path / "reports"
    md_path = write_report(report, reports_dir=out)
    assert md_path.exists()
    assert (out / "eval-write.json").exists()
    data = json.loads((out / "eval-write.json").read_text(encoding="utf-8"))
    assert data["report_id"] == "eval-write"
    assert data["pass_rate"] == 100.0
    assert data["trace_summary"] == {"total": 1, "applicable": 1, "passed": 1, "failed": 0}
    assert data["samples"][0]["trace"]["passed"] is True


def test_report_trace_section_is_independent_of_pass_rate() -> None:
    """链路断言失败只体现在链路校验小节，不拉低规则通过率。"""
    trace = collect_from_events(_make_events())
    sample = normalize(trace, LABELED_BALANCE)
    rule_report = run_rule_checks(sample)
    assert rule_report.passed

    broken = [e for e in _make_events() if e.get("type") != "skill_result"]
    trace_report = run_trace_checks(collect_from_events(broken))
    assert not trace_report.passed

    report = EvalReport(
        report_id="eval-trace",
        created_at="2026-01-01T00:00:00Z",
        total=1,
        passed=1,  # 规则口径仍然通过
        results=[
            SampleEvalResult(
                sample=sample, rule_report=rule_report, trace_report=trace_report
            )
        ],
    )
    md = render_markdown(report)
    assert report.pass_rate == 100.0
    assert "100.0%" in md
    assert "## 链路校验（trace 结构断言，不计入通过率）" in md
    assert "`agent_call_pairing`" in md
    assert report.trace_summary() == {"total": 1, "applicable": 1, "passed": 0, "failed": 1}
    assert report.trace_assertion_failures() == {"agent_call_pairing": 1}


def test_render_markdown_without_trace_report() -> None:
    trace = collect_from_events(_make_events())
    sample = normalize(trace, LABELED_BALANCE)
    result = SampleEvalResult(sample=sample, rule_report=run_rule_checks(sample))
    report = EvalReport(
        report_id="eval-notrace",
        created_at="2026-01-01T00:00:00Z",
        total=1,
        passed=1,
        results=[result],
    )
    md = render_markdown(report)
    assert report.trace_summary()["applicable"] == 0
    assert "没有可校验的 RAG / Agent 链路轨迹" in md


# ---------------------------------------------------------------------------
# runner（端到端，模型打桩）
# ---------------------------------------------------------------------------


def test_runner_single_sample_with_stubbed_model(tmp_path: Path, monkeypatch) -> None:
    """用打桩模型跑单条样本，验证完整流水线产出结果。"""
    import asyncio
    import json as _json

    from langchain_core.messages import AIMessage

    # 意图：agent_service
    intent_payload = _json.dumps(
        {"category": "agent_service", "confidence": 0.9, "reason": "e2e", "missing_slots": []},
        ensure_ascii=False,
    )

    class FakeIntentModel:
        async def ainvoke(self, messages, **kwargs):
            return AIMessage(content=intent_payload)

    monkeypatch.setattr("congclaw.graph.nodes.create_model", lambda: FakeIntentModel())

    # Agent 思考 → 调 query_balance → 反思 pass → 答复
    from congclaw.prompts.agent import AGENT_REFLECT_PROMPT, AGENT_RESPOND_PROMPT

    class FakeAgentModel:
        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages, **kwargs):
            system = str(messages[0].content) if messages else ""
            if system.startswith(AGENT_REFLECT_PROMPT[:16]):
                return AIMessage(
                    content=_json.dumps({"decision": "pass", "reason": "ok", "checks": []})
                )
            if system.startswith(AGENT_RESPOND_PROMPT[:16]):
                return AIMessage(content="您的余额为 86.50 元。")
            return AIMessage(
                content="",
                tool_calls=[{"id": "c1", "name": "query_balance", "args": {}}],
            )

    monkeypatch.setattr("congclaw.agent.nodes.create_model", lambda: FakeAgentModel())
    # LLM-Judge 用退化路径（无 API key）
    monkeypatch.setenv("API_KEY", "")

    from congclaw.db import dispose_engine
    from congclaw.eval.runner import run_single_sample

    ws = tmp_path / "ws-e01"
    try:
        result = asyncio.run(run_single_sample(LABELED_BALANCE, workspace=ws, use_llm_judge=True))
    finally:
        # 释放 DB 引擎单例，避免污染后续测试的 session 存储
        asyncio.run(dispose_engine())

    assert result.sample.sample_id == "E01"
    assert result.rule_report.passed
    assert result.sample.actual_category == "agent_service"
    assert "query_balance" in result.sample.actual_tools
    assert result.judge_scores is not None
    assert result.judge_scores.fallback is True  # 无 API key 退化
    # 真实事件流也要能通过 Agent 链路断言
    assert result.trace_report is not None
    assert result.trace_report.applicable
    assert result.trace_report.passed, result.trace_report.failures
    # 真实事件流同样能产出指标；耗时类指标来自落盘 trace
    assert result.metrics is not None
    assert result.metrics.values["agent_tool_calls"] == 1.0
    assert result.metrics.values["agent_tool_success_rate"] == 1.0
    assert "e2e_ms" in result.metrics.values


def test_prune_eval_workspaces_keeps_recent(tmp_path: Path) -> None:
    """评测临时工作区只保留最近 keep 个，更早的自动清理，且不误删无关目录。"""
    import os

    from congclaw.eval.runner import prune_eval_workspaces

    for i in range(5):
        run_dir = tmp_path / f"cong-eval-{i}"
        run_dir.mkdir()
        (run_dir / "events.jsonl").write_text("{}", encoding="utf-8")
        os.utime(run_dir, (1000 + i, 1000 + i))  # 制造新旧顺序

    unrelated = tmp_path / "not-eval-output"
    unrelated.mkdir()

    removed = prune_eval_workspaces(keep=3, temp_root=tmp_path)

    assert sorted(p.name for p in removed) == ["cong-eval-0", "cong-eval-1"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "cong-eval-2",
        "cong-eval-3",
        "cong-eval-4",
        "not-eval-output",
    ]
    assert unrelated.exists()


def test_prune_eval_workspaces_noop_within_keep(tmp_path: Path) -> None:
    """未超过保留数量时不做任何删除。"""
    from congclaw.eval.runner import prune_eval_workspaces

    (tmp_path / "cong-eval-only").mkdir()
    assert prune_eval_workspaces(keep=3, temp_root=tmp_path) == []
    assert (tmp_path / "cong-eval-only").exists()
    # 目录不存在时也不报错
    assert prune_eval_workspaces(keep=3, temp_root=tmp_path / "missing") == []


# ---------------------------------------------------------------------------
# trace 指标（观测值）
# ---------------------------------------------------------------------------


def test_compute_metrics_rag_sample_without_timestamps() -> None:
    """内存事件流（无 elapsed_ms）也能算出非耗时指标。"""
    metrics = compute_metrics(collect_from_events(_rag_hit_events()))
    values = metrics.values

    assert values["rag_recall_bm25"] == 5.0
    assert values["rag_recall_dense"] == 5.0
    assert values["rag_recall_empty_channels"] == 0.0
    assert values["rag_fusion_keep_rate"] == 0.7  # 7 / (5 + 5)
    assert values["rag_parent_count"] == 2.0
    assert values["rag_source_count"] == 2.0
    assert values["intent_confidence"] == 0.92
    # 没有落盘 trace 就没有时间戳，耗时类指标直接缺席而不是记 0
    assert "rag_duration_ms" not in values
    assert "intent_ms" not in values
    assert "agent_tool_calls" not in values


def test_compute_metrics_agent_sample() -> None:
    metrics = compute_metrics(collect_from_events(_make_events()))
    values = metrics.values

    assert values["agent_tool_calls"] == 1.0
    assert values["agent_tool_success_rate"] == 1.0
    assert values["agent_calls_per_round"] == 1.0
    assert values["agent_first_reflect_pass"] == 1.0
    assert values["agent_reflect_retries"] == 0.0
    # 该样本没有 RAG 事件，RAG 指标整体缺席
    assert not any(name.startswith("rag_") for name in values)


def test_compute_metrics_rewrite_effectiveness() -> None:
    """重写有效性：重写后召回到=1，仍为空=0。"""
    hit = compute_metrics(collect_from_events(_rag_rewrite_events()))
    assert hit.values["rag_rewrite_effective"] == 1.0

    empty_after_rewrite = [
        {"type": "rag_start", "query": "那个多少钱"},
        {"type": "rag_retrieve", "channel": "bm25", "count": 0, "attempt": 0},
        {"type": "rag_retrieve", "channel": "dense", "count": 0, "attempt": 0},
        {"type": "rag_gate", "decision": "rewrite", "attempts": 0},
        {"type": "rag_rewrite", "original_query": "那个多少钱", "rewritten_query": "改写", "attempts": 1},
        {"type": "rag_retrieve", "channel": "bm25", "count": 0, "attempt": 1},
        {"type": "rag_retrieve", "channel": "dense", "count": 0, "attempt": 1},
        {"type": "rag_gate", "decision": "fallback", "attempts": 1},
        {"type": "rag_fallback", "reason": "rag_no_evidence", "attempts": 1},
        {"type": "rag_finished", "has_answer": False},
    ]
    missed = compute_metrics(collect_from_events(empty_after_rewrite))
    assert missed.values["rag_rewrite_effective"] == 0.0
    assert missed.values["rag_recall_empty_channels"] == 2.0


def test_compute_metrics_reads_elapsed_ms_from_trace_file(tmp_path: Path) -> None:
    """落盘 trace 带 elapsed_ms，可算出耗时类指标与门控裕度。"""
    records = [
        {"seq": 1, "elapsed_ms": 20, "type": "session_turn_started", "payload": {"turn": 1}},
        {
            "seq": 2,
            "elapsed_ms": 150,
            "type": "intent_decision",
            "payload": {"route": "rag_answer", "category": "rag_query", "confidence": 0.9},
        },
        {"seq": 3, "elapsed_ms": 1200, "type": "rag_start", "payload": {"query": "套餐流量"}},
        {"seq": 4, "elapsed_ms": 1210, "type": "rag_retrieve",
         "payload": {"channel": "bm25", "count": 5, "attempt": 0}},
        {"seq": 5, "elapsed_ms": 1400, "type": "rag_retrieve",
         "payload": {"channel": "dense", "count": 0, "attempt": 0}},
        {"seq": 6, "elapsed_ms": 1500, "type": "rag_fusion",
         "payload": {"bm25_count": 5, "dense_count": 0, "fused_count": 4}},
        {"seq": 7, "elapsed_ms": 1800, "type": "rag_rerank",
         "payload": {"count": 2, "top": [{"rerank_prob": 0.8}, {"rerank_prob": 0.3}]}},
        {"seq": 8, "elapsed_ms": 1820, "type": "rag_gate",
         "payload": {"decision": "parent_lookup", "hit_count": 2,
                     "top_rerank_prob": 0.8, "gate_prob": 0.53}},
        {"seq": 9, "elapsed_ms": 1900, "type": "rag_parent_lookup",
         "payload": {"parent_count": 2,
                     "evidence": [{"matched_children": ["c1", "c2"]}, {"matched_children": ["c3"]}]}},
        {"seq": 10, "elapsed_ms": 3000, "type": "rag_answer", "payload": {"sources_count": 2}},
        {"seq": 11, "elapsed_ms": 3050, "type": "rag_finished", "payload": {"has_answer": True}},
        {"seq": 12, "elapsed_ms": 3100, "type": "session_turn_saved", "payload": {"turn": 1}},
    ]
    for record in records:
        record["timestamp"] = "2026-01-01T00:00:00Z"
    trace_dir = tmp_path / "trace-20260101-000000-abc123"
    trace_dir.mkdir()
    (trace_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records), encoding="utf-8"
    )

    trace = collect_from_trace_dir(trace_dir)
    values = compute_metrics(trace).values

    assert values["intent_ms"] == 130.0  # 150 - 20
    assert values["e2e_ms"] == 3080.0  # 3100 - 20
    assert values["rag_duration_ms"] == 1850.0  # 3050 - 1200
    assert values["rag_recall_dense"] == 0.0
    assert values["rag_recall_empty_channels"] == 1.0
    assert values["rag_fusion_keep_rate"] == 0.8  # 4 / 5
    assert values["rag_top_rerank_prob"] == 0.8
    assert values["rag_gate_margin"] == 0.27  # 0.8 - 0.53
    assert values["rag_gate_hit_count"] == 2.0
    assert values["rag_children_per_parent"] == 1.5  # 3 个子片段 / 2 个父分片


def test_aggregate_metrics_mean_min_max_and_coverage() -> None:
    """聚合给均值/极值/样本数；不适用的样本不计入 n。"""
    from congclaw.eval.trace_metrics import TraceMetrics, aggregate_metrics

    summary = aggregate_metrics(
        [
            TraceMetrics({"e2e_ms": 100.0, "rag_source_count": 2.0}),
            TraceMetrics({"e2e_ms": 300.0}),
        ]
    )
    assert summary["e2e_ms"] == {"mean": 200.0, "min": 100.0, "max": 300.0, "n": 2}
    assert summary["rag_source_count"] == {
        "mean": 2.0,
        "min": 2.0,
        "max": 2.0,
        "n": 1,
    }


def test_report_renders_metrics_section(tmp_path: Path) -> None:
    """链路指标在报告里单开一节，且不进入 pass_rate。"""
    from congclaw.eval.trace_metrics import TraceMetrics

    trace = collect_from_events(_make_events())
    sample = normalize(trace, LABELED_BALANCE)
    result = SampleEvalResult(
        sample=sample,
        rule_report=run_rule_checks(sample),
        metrics=TraceMetrics({"e2e_ms": 100.0, "agent_tool_calls": 1.0}),
    )
    report = EvalReport(
        report_id="eval-metrics",
        created_at="2026-01-01T00:00:00Z",
        total=1,
        passed=1,
        results=[result],
    )

    md = render_markdown(report)
    assert "## 链路指标（观测值，不做阈值判定、不计入通过率）" in md
    assert "端到端耗时(ms)" in md
    assert report.pass_rate == 100.0

    out = tmp_path / "reports"
    write_report(report, reports_dir=out)
    data = json.loads((out / "eval-metrics.json").read_text(encoding="utf-8"))
    assert data["trace_metrics"]["e2e_ms"]["mean"] == 100.0
    assert data["samples"][0]["metrics"]["agent_tool_calls"] == 1.0
