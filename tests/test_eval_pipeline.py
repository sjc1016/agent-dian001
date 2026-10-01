"""阶段 6：自动化评测流水线单测。

覆盖 collector / normalizer / rule_checks / llm_judge（退化路径）/ report / runner。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mokioclaw.eval.collector import SessionTrace, collect_from_events, collect_from_trace_dir
from mokioclaw.eval.llm_judge import JudgeScores, _fallback_scores, judge_answer
from mokioclaw.eval.normalizer import EvalSample, normalize
from mokioclaw.eval.report import EvalReport, SampleEvalResult, render_markdown, write_report
from mokioclaw.eval.rule_checks import run_rule_checks


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
    assert "86.50" in trace.final_answer


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

    monkeypatch.setattr("mokioclaw.providers.openai_provider.create_model", _boom)

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
    result = SampleEvalResult(sample=sample, rule_report=rule_report)
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

    monkeypatch.setattr("mokioclaw.graph.nodes.create_model", lambda: FakeIntentModel())

    # Agent 思考 → 调 query_balance → 反思 pass → 答复
    from mokioclaw.prompts.agent import AGENT_REFLECT_PROMPT, AGENT_RESPOND_PROMPT

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

    monkeypatch.setattr("mokioclaw.agent.nodes.create_model", lambda: FakeAgentModel())
    # LLM-Judge 用退化路径（无 API key）
    monkeypatch.setenv("API_KEY", "")

    from mokioclaw.db import dispose_engine
    from mokioclaw.eval.runner import run_single_sample

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
