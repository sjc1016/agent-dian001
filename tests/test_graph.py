from __future__ import annotations

import asyncio
import json
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES, add_messages

from mokioclaw.core.state import RuntimeState
from mokioclaw.graph.memory import build_layered_memory, persist_history_summary, read_history_summary
from mokioclaw.graph.nodes import (
    agent_loop_node,
    clarify_node,
    context_compressor_node,
    context_compressor_route,
    context_monitor_node,
    context_monitor_route,
    estimate_context_tokens,
    fallback_node,
    final_node,
    get_context_token_limit,
    intent_route_fn,
    intent_router_node,
    planner_node,
    rag_answer_node,
    verifier_node,
    verifier_route,
)
from mokioclaw.graph.workflow import build_workflow


def test_model_verifier_passes_from_json(monkeypatch, tmp_path: Path) -> None:
    class FakeModel:
        def invoke(self, messages):
            return AIMessage(
                content=json.dumps(
                    {
                        "passed": True,
                        "reason": "HTML file satisfies the request.",
                        "checks": [{"name": "html", "passed": True, "detail": "ok"}],
                        "recommended_next_instruction": "",
                    }
                )
            )

    monkeypatch.setattr("mokioclaw.graph.nodes.create_model", lambda: FakeModel())
    state = {
        "runtime": RuntimeState(workspace=tmp_path),
        "task": "demo",
        "todos": [{"id": "todo-1", "content": "verify", "status": "in_progress", "note": ""}],
        "attempts": 0,
        "max_attempts": 3,
    }

    result = verifier_node(state)

    assert result["passed"] is True
    assert result["attempts"] == 1
    assert result["todos"][0]["status"] == "completed"
    assert result["verification_checks"][0]["name"] == "html"
    assert verifier_route({**state, **result}) == "final"


def test_model_verifier_invalid_json_fails_and_routes_back(monkeypatch, tmp_path: Path) -> None:
    class FakeModel:
        async def ainvoke(self, messages, **kwargs):
            return self.invoke(messages)

        def invoke(self, messages):
            return AIMessage(content="not json")

    monkeypatch.setattr("mokioclaw.graph.nodes.create_model", lambda: FakeModel())
    state = {
        "runtime": RuntimeState(workspace=tmp_path),
        "task": "demo",
        "attempts": 0,
        "max_attempts": 3,
    }

    result = verifier_node(state)

    assert result["passed"] is False
    assert "valid JSON" in result["last_error"]
    assert verifier_route({**state, **result}) == "planner"


def test_verifier_routes_to_final_at_max_attempts() -> None:
    assert verifier_route({"passed": False, "attempts": 3, "max_attempts": 3}) == "final"


def test_final_node_reports_multiagent_status() -> None:
    result = final_node(
        {
            "passed": True,
            "plan_summary": "demo plan",
            "todos": [{"content": "write page", "status": "completed"}],
            "verification_checks": [{"name": "html", "passed": True, "detail": "ok"}],
            "sources": [{"title": "source", "url": "https://example.com"}],
            "code_agent_summary": "done",
            "verifier_summary": "looks good",
        }
    )

    assert "PASSED" in result["final_answer"]
    assert "demo plan" in result["final_answer"]
    assert "https://example.com" in result["final_answer"]


def test_workflow_compiles_without_fixed_actor_node() -> None:
    workflow = build_workflow()

    assert workflow is not None


def test_intent_router_routes_rag_query_with_model_json(monkeypatch) -> None:
    class FakeModel:
        async def ainvoke(self, messages, **kwargs):
            return self.invoke(messages)

        def invoke(self, messages):
            return AIMessage(content='{"category":"rag_query","confidence":0.9,"reason":"资费咨询","missing_slots":[]}')

    monkeypatch.setattr("mokioclaw.graph.nodes.create_model", lambda: FakeModel())

    result = asyncio.run(intent_router_node({"task": "5G 融合套餐每月多少钱", "clarify_count": 3, "unknown_count": 2}))

    assert result["intent_category"] == "rag_query"
    assert result["intent_route"] == "rag_answer"
    assert result["intent_confidence"] == 0.9
    # 明确业务后跨轮计数清零（连续语义）
    assert result["clarify_count"] == 0
    assert result["unknown_count"] == 0
    assert intent_route_fn(result) == "rag_answer"


def test_intent_router_routes_agent_service_with_model_json(monkeypatch) -> None:
    class FakeModel:
        async def ainvoke(self, messages, **kwargs):
            return self.invoke(messages)

        def invoke(self, messages):
            return AIMessage(content='{"category":"agent_service","confidence":0.85,"reason":"办理诉求","missing_slots":[]}')

    monkeypatch.setattr("mokioclaw.graph.nodes.create_model", lambda: FakeModel())

    result = asyncio.run(intent_router_node({"task": "帮我开通国际漫游"}))

    assert result["intent_category"] == "agent_service"
    assert result["intent_route"] == "agent_loop"
    assert intent_route_fn(result) == "agent_loop"


def test_intent_router_routes_clarify_and_keeps_pending_slots(monkeypatch) -> None:
    class FakeModel:
        async def ainvoke(self, messages, **kwargs):
            return self.invoke(messages)

        def invoke(self, messages):
            return AIMessage(content='{"category":"clarify","confidence":0.7,"reason":"信息不全","missing_slots":[]}')

    monkeypatch.setattr("mokioclaw.graph.nodes.create_model", lambda: FakeModel())

    result = asyncio.run(intent_router_node({"task": "帮我办一下那个", "pending_slots": ["号码"], "clarify_count": 1}))

    assert result["intent_category"] == "clarify"
    assert result["intent_route"] == "clarify"
    # 追问延续：模型未给槽位时沿用旧槽位；轮数由 clarify_node 递增，router 不动
    assert result["pending_slots"] == ["号码"]
    assert result["clarify_count"] == 1


def test_intent_router_routes_irrelevant_to_fallback(monkeypatch) -> None:
    class FakeModel:
        async def ainvoke(self, messages, **kwargs):
            return self.invoke(messages)

        def invoke(self, messages):
            return AIMessage(content='{"category":"irrelevant","confidence":0.95,"reason":"超出客服范围","missing_slots":[]}')

    monkeypatch.setattr("mokioclaw.graph.nodes.create_model", lambda: FakeModel())

    result = asyncio.run(intent_router_node({"task": "帮我写一首诗"}))

    assert result["intent_category"] == "irrelevant"
    assert result["intent_route"] == "fallback"
    assert result["fallback_reason"] == "irrelevant_request"
    assert intent_route_fn(result) == "fallback"


def test_intent_router_low_confidence_treated_as_unknown(monkeypatch) -> None:
    class FakeModel:
        async def ainvoke(self, messages, **kwargs):
            return self.invoke(messages)

        def invoke(self, messages):
            return AIMessage(content='{"category":"clarify","confidence":0.3,"reason":"拿不准","missing_slots":[]}')

    monkeypatch.setattr("mokioclaw.graph.nodes.create_model", lambda: FakeModel())

    result = asyncio.run(intent_router_node({"task": "那个东西怎么样了"}))

    assert result["intent_category"] == "unknown"
    assert "low confidence" in result["intent_reason"]
    assert result["unknown_count"] == 1
    assert intent_route_fn(result) == "clarify"


def test_intent_router_invalid_json_defaults_to_unknown(monkeypatch) -> None:
    class FakeModel:
        async def ainvoke(self, messages, **kwargs):
            return self.invoke(messages)

        def invoke(self, messages):
            return AIMessage(content="not json")

    monkeypatch.setattr("mokioclaw.graph.nodes.create_model", lambda: FakeModel())

    result = asyncio.run(intent_router_node({"task": "嗯嗯"}))

    assert result["intent_category"] == "unknown"
    assert result["unknown_count"] == 1
    assert intent_route_fn(result) == "clarify"


def test_intent_router_unknown_streak_forces_fallback(monkeypatch) -> None:
    class FakeModel:
        async def ainvoke(self, messages, **kwargs):
            return self.invoke(messages)

        def invoke(self, messages):
            return AIMessage(content='{"category":"unknown","confidence":0.2,"reason":"无法判断","missing_slots":[]}')

    monkeypatch.setattr("mokioclaw.graph.nodes.create_model", lambda: FakeModel())

    result = asyncio.run(intent_router_node({"task": "？？", "unknown_count": 1}))

    assert result["intent_route"] == "fallback"
    assert result["fallback_reason"] == "unknown_streak"
    assert intent_route_fn(result) == "fallback"


def test_intent_router_clarify_exceeded_forces_fallback(monkeypatch) -> None:
    class FakeModel:
        async def ainvoke(self, messages, **kwargs):
            return self.invoke(messages)

        def invoke(self, messages):
            return AIMessage(content='{"category":"clarify","confidence":0.7,"reason":"仍不明确","missing_slots":[]}')

    monkeypatch.setattr("mokioclaw.graph.nodes.create_model", lambda: FakeModel())

    result = asyncio.run(intent_router_node({"task": "还是那个事", "clarify_count": 5}))

    assert result["intent_route"] == "fallback"
    assert result["fallback_reason"] == "clarify_exceeded"
    assert intent_route_fn(result) == "fallback"


def test_clarify_node_asks_question_and_increments_count(monkeypatch) -> None:
    class FakeModel:
        async def ainvoke(self, messages, **kwargs):
            return self.invoke(messages)

        def invoke(self, messages):
            return AIMessage(content="请问您想查询话费还是办理套餐呢？")

    monkeypatch.setattr("mokioclaw.graph.nodes.create_model", lambda: FakeModel())

    result = asyncio.run(clarify_node({"task": "帮我办一下", "clarify_count": 2, "pending_slots": ["号码"]}))

    assert result["clarify_count"] == 3
    assert result["clarify_question"] == "请问您想查询话费还是办理套餐呢？"
    assert result["final_answer"] == result["clarify_question"]


def test_clarify_node_uses_default_question_when_model_fails(monkeypatch) -> None:
    class FakeModel:
        async def ainvoke(self, messages, **kwargs):
            return self.invoke(messages)

        def invoke(self, messages):
            raise RuntimeError("llm down")

    monkeypatch.setattr("mokioclaw.graph.nodes.create_model", lambda: FakeModel())

    result = asyncio.run(clarify_node({"task": "那个", "clarify_count": 0}))

    assert result["clarify_count"] == 1
    assert result["final_answer"]


def test_fallback_node_replies_with_template() -> None:
    result = fallback_node({"task": "写首诗", "fallback_reason": "irrelevant_request"})

    assert result["fallback_reason"] == "irrelevant_request"
    assert result["chat_response"] == result["final_answer"]
    assert result["final_answer"]


def test_fallback_node_infers_reason_from_counts() -> None:
    result = fallback_node({"task": "嗯", "clarify_count": 5})

    assert result["fallback_reason"] == "clarify_exceeded"
    assert result["final_answer"]


def test_rag_answer_node_drives_rag_subgraph(monkeypatch) -> None:
    """P3-21：rag_answer 不再是占位话术，而是在节点内驱动 RAG 子图。"""

    async def empty_search(query, top_k):
        return []

    async def empty_rerank(query, hits):
        return []

    async def empty_parents(ordered_ids):
        return []

    class FakeRagModel:
        async def ainvoke(self, messages, **kwargs):
            return self.invoke(messages)

        def invoke(self, messages):
            return AIMessage(content="规范的检索问句")

    monkeypatch.setattr("mokioclaw.rag.nodes.bm25_search", empty_search)
    monkeypatch.setattr("mokioclaw.rag.nodes.dense_search", empty_search)
    monkeypatch.setattr("mokioclaw.rag.nodes.rerank_hits", empty_rerank)
    monkeypatch.setattr("mokioclaw.rag.nodes.build_parent_evidence", empty_parents)
    monkeypatch.setattr("mokioclaw.rag.nodes.create_model", lambda: FakeRagModel())

    result = asyncio.run(rag_answer_node({"task": "今天天气怎么样"}))

    assert result["chat_response"] == result["final_answer"]
    # 两路召回均空、重写一次后仍无证据 → 子图兜底原因透传到主图 metadata
    assert result["final_answer"]
    assert result["metadata"]["rag_fallback_reason"] == "rag_no_evidence"


def test_agent_loop_node_drives_agent_subgraph_with_direct_reply(monkeypatch, tmp_path: Path) -> None:
    """阶段 4：agent_loop 不再是占位话术，而是挂载并驱动 Agent 深度推理子图。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "agent-node-test.db"))
    monkeypatch.setenv("SKILL_WATCH", "0")

    class FakeAgentModel:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages, **kwargs):
            return AIMessage(content="好的，这是 Agent 子图的直接回复。")

        async def ainvoke(self, messages, **kwargs):
            return AIMessage(content="好的，这是 Agent 子图的直接回复。")

    monkeypatch.setattr("mokioclaw.agent.nodes.create_model", lambda: FakeAgentModel())

    result = asyncio.run(
        agent_loop_node(
            {"task": "停机保号", "workspace": str(tmp_path / "ws"), "max_attempts": 3}
        )
    )

    assert "Agent 子图的直接回复" in result["final_answer"]
    assert result["chat_response"] == result["final_answer"]


def test_context_token_limit_defaults_and_env(monkeypatch) -> None:
    monkeypatch.setattr("mokioclaw.graph.nodes.load_dotenv", lambda: None)
    monkeypatch.delenv("MOKIO_CONTEXT_TOKEN_LIMIT", raising=False)
    assert get_context_token_limit() == 400000

    monkeypatch.setenv("MOKIO_CONTEXT_TOKEN_LIMIT", "1234")
    assert get_context_token_limit() == 1234


def test_estimate_context_tokens_uses_model_counter(monkeypatch, tmp_path: Path) -> None:
    class FakeModel:
        def get_num_tokens_from_messages(self, messages):
            return 42 + len(messages)

    monkeypatch.setattr("mokioclaw.graph.nodes.create_model", lambda: FakeModel())
    result = estimate_context_tokens(
        {
            "runtime": RuntimeState(workspace=tmp_path),
            "task": "demo",
            "messages": [HumanMessage(content="hello")],
        }
    )

    assert result == 44


def test_context_monitor_does_not_compress_below_limit(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MOKIO_CONTEXT_TOKEN_LIMIT", "100")
    monkeypatch.setattr("mokioclaw.graph.nodes.estimate_context_tokens", lambda state: 10)
    result = context_monitor_node(
        {
            "runtime": RuntimeState(workspace=tmp_path),
            "task": "demo",
            "context_next_node": "verifier",
        }
    )

    assert result["context_should_compress"] is False
    assert result["context_next_node"] == "verifier"
    assert context_monitor_route(result) == "verifier"


def test_context_monitor_compresses_at_limit(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MOKIO_CONTEXT_TOKEN_LIMIT", "100")
    monkeypatch.setattr("mokioclaw.graph.nodes.estimate_context_tokens", lambda state: 100)
    result = context_monitor_node(
        {
            "runtime": RuntimeState(workspace=tmp_path),
            "task": "demo",
            "context_next_node": "planner",
        }
    )

    assert result["context_should_compress"] is True
    assert context_monitor_route(result) == "context_compressor"


def test_context_compressor_removes_old_messages_and_preserves_state(monkeypatch, tmp_path: Path) -> None:
    calls = {"count": 0}

    def fake_estimate(state):
        calls["count"] += 1
        return 1000 if calls["count"] == 1 else 50

    monkeypatch.setattr("mokioclaw.graph.nodes.estimate_context_tokens", fake_estimate)
    monkeypatch.setattr(
        "mokioclaw.graph.nodes._compress_context_with_model",
        lambda state: {
            "summary": "compressed summary",
            "active_goal": "finish demo",
            "completed_work": "wrote file",
            "open_todos": ["verify"],
            "important_files": ["demo.py"],
            "tool_findings": "ok",
            "sources": [],
            "next_steps": "verify",
            "risks": "",
        },
    )
    state = {
        "runtime": RuntimeState(workspace=tmp_path),
        "task": "demo",
        "messages": [HumanMessage(content="old " * 200)],
        "plan_summary": "plan",
        "todos": [{"id": "todo-1", "content": "verify", "status": "pending", "note": ""}],
        "acceptance_criteria": ["done"],
        "verification_commands": ["python --version"],
        "research_notes": "research " * 100,
        "context_next_node": "verifier",
    }

    result = context_compressor_node(state)

    assert isinstance(result["messages"][0], RemoveMessage)
    assert result["messages"][0].id == REMOVE_ALL_MESSAGES
    assert "compressed summary" in result["context_summary"]
    assert result["compression_events"][0]["before_tokens"] == 1000
    assert result["compression_events"][0]["after_tokens"] == 50
    assert result["compression_events"][0]["removed_messages"] == 1
    assert "compressed summary" in result["history_summary"]
    assert "compressed summary" in (tmp_path / "HISTORY_SUMMARY.md").read_text(encoding="utf-8")
    assert result["todos"][0]["content"] == "verify" if "todos" in result else state["todos"][0]["content"] == "verify"
    merged = add_messages(state["messages"], result["messages"])
    assert len(merged) == 1
    assert "compressed summary" in merged[0].content
    assert context_compressor_route({**state, **result}) == "verifier"


def test_planner_writes_default_customer_service_plan(monkeypatch, tmp_path: Path) -> None:
    class FakeBoundModel:
        async def ainvoke(self, messages, **kwargs):
            return self.invoke(messages)

        def invoke(self, messages):
            return AIMessage(content="plan ready")

    class FakeModel:
        def bind_tools(self, tools):
            return FakeBoundModel()

    monkeypatch.setattr("mokioclaw.graph.nodes.create_model", lambda: FakeModel())
    result = planner_node(
        {
            "task": "我想查一下我的话费余额",
            "runtime": RuntimeState(workspace=tmp_path),
            "attempts": 0,
            "max_attempts": 3,
        }
    )

    assert result["verification_commands"] == []
    assert result["todos"][0]["id"] == "todo-1"
    assert result["todos"][0]["status"] == "pending"
    assert (tmp_path / "TODO.md").exists()


def test_layered_memory_splits_rules_working_and_history(tmp_path: Path) -> None:
    runtime = RuntimeState(workspace=tmp_path)
    persist_history_summary(runtime, "Previous compressed history.")

    memory = build_layered_memory(
        {
            "runtime": runtime,
            "task": "demo",
            "plan_summary": "demo plan",
            "todos": [{"id": "todo-1", "content": "write", "status": "pending", "note": ""}],
            "acceptance_criteria": ["file exists"],
            "verification_commands": ["python --version"],
            "research_notes": "research",
            "sources": [{"title": "source", "url": "https://example.com"}],
        },
        node="planner",
    )

    assert set(memory) == {"rules", "working_memory", "history_summary_store"}
    assert memory["rules"]["scope"] == "telecom_customer_service"
    assert memory["working_memory"]["task"] == "demo"
    assert memory["working_memory"]["todos"][0]["content"] == "write"
    assert memory["working_memory"]["sources"][0]["url"] == "https://example.com"
    assert memory["history_summary_store"]["history_exists"] is True
    assert "Previous compressed history" in memory["history_summary_store"]["history_summary"]


def test_layered_memory_trims_long_history_and_handoffs(tmp_path: Path) -> None:
    runtime = RuntimeState(workspace=tmp_path)
    handoffs = [
        {"from_agent": "planner", "to_agent": "codeAgent", "instruction": "i" * 1000, "result": "r" * 1000}
        for _ in range(8)
    ]

    memory = build_layered_memory(
        {
            "runtime": runtime,
            "task": "demo",
            "research_notes": "research " * 1000,
            "agent_handoffs": handoffs,
        },
        node="planner",
    )

    assert len(memory["working_memory"]["research_notes"]) <= 1600
    assert len(memory["working_memory"]["agent_handoffs"]) == 6
    assert len(memory["working_memory"]["agent_handoffs"][0]["instruction"]) <= 500
    assert len(memory["history_summary_store"]["history_summary"]) <= 2200


def test_history_summary_read_missing_file(tmp_path: Path) -> None:
    result = read_history_summary(RuntimeState(workspace=tmp_path))

    assert result["ok"] is True
    assert result["exists"] is False
    assert result["content"] == ""
