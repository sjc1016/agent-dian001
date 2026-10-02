"""阶段 2 验收：五类意图分流 + 追问上限兜底 + 无关请求兜底 + 计数跨轮持久化。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from congclaw.core.agent import stream_session_events
from congclaw.core.session import load_or_create_session
from congclaw.db.engine import dispose_engine
from congclaw.graph.workflow import build_entry_workflow


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """每个用例独立 SQLite 库并关闭 Skill 目录监听线程。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "intent-test.db"))
    monkeypatch.setenv("SKILL_WATCH", "0")


def _fake_model(category: str, confidence: float = 0.9, reason: str = "test") -> type:
    """构造返回固定意图 JSON 的模型类（create_model 是工厂，须返回类本身）。"""
    payload = json.dumps(
        {"category": category, "confidence": confidence, "reason": reason, "missing_slots": []},
        ensure_ascii=False,
    )

    class FakeModel:
        def invoke(self, messages):
            return AIMessage(content=payload)

        async def ainvoke(self, messages, **kwargs):
            return AIMessage(content=payload)

    # create_model() 是工厂：调用后必须得到实例（类会导致未绑定 invoke 报错）
    return FakeModel()


def _stream_updates(state: dict[str, Any], graph=None) -> dict[str, Any]:
    """阶段 4：主图节点全部原生 async，统一用 astream 驱动（同步测试内 asyncio.run）。"""

    async def _collect() -> dict[str, Any]:
        updates: dict[str, Any] = {}
        async for mode, event in (graph or build_entry_workflow()).astream(
            state, stream_mode=["updates", "custom"]
        ):
            if mode == "updates":
                updates.update(event)
        return updates

    return asyncio.run(_collect())


def _stub_agent_direct_reply(monkeypatch, reply: str = "好的，已记录您的诉求（测试桩回复）。") -> None:
    """agent_service 用例：Agent 子图思考模型打桩为"不调工具直接回复"，不依赖真实 LLM/业务库。"""

    class FakeAgentModel:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages, **kwargs):
            return AIMessage(content=reply)

        async def ainvoke(self, messages, **kwargs):
            return AIMessage(content=reply)

    monkeypatch.setattr("congclaw.agent.nodes.create_model", lambda: FakeAgentModel())


def _stub_rag_empty_path(monkeypatch) -> None:
    """rag_query 用例会真实挂载 RAG 子图，打桩检索层使其走"重写→兜底"，不依赖本地模型/向量库。"""

    async def empty_search(query, top_k):
        return []

    async def empty_rerank(query, hits):
        return []

    async def empty_parents(ordered_ids):
        return []

    class FakeRagModel:
        def invoke(self, messages):
            return AIMessage(content="规范的检索问句")

        async def ainvoke(self, messages, **kwargs):
            return AIMessage(content="规范的检索问句")

    monkeypatch.setattr("congclaw.rag.nodes.bm25_search", empty_search)
    monkeypatch.setattr("congclaw.rag.nodes.dense_search", empty_search)
    monkeypatch.setattr("congclaw.rag.nodes.rerank_hits", empty_rerank)
    monkeypatch.setattr("congclaw.rag.nodes.build_parent_evidence", empty_parents)
    monkeypatch.setattr("congclaw.rag.nodes.create_model", lambda: FakeRagModel())


# P2-10：10 条意图测试输入，五类各覆盖（含模糊与无关）
@pytest.mark.parametrize(
    ("task", "category", "route"),
    [
        ("帮我查一下本月话费余额", "rag_query", "rag_answer"),
        ("5G 融合套餐的资费标准是什么", "rag_query", "rag_answer"),
        ("帮我开通国际漫游", "agent_service", "agent_loop"),
        ("我要办理停机保号", "agent_service", "agent_loop"),
        ("把我的套餐改成 129 元档", "agent_service", "agent_loop"),
        ("帮我办一下那个业务", "clarify", "clarify"),
        ("就上次说的那个东西", "clarify", "clarify"),
        ("帮我写一首诗", "irrelevant", "fallback"),
        ("今天天气怎么样", "irrelevant", "fallback"),
        ("啊？什么意思", "unknown", "clarify"),
    ],
)
def test_ten_intent_inputs_route_correctly(monkeypatch, task: str, category: str, route: str) -> None:
    monkeypatch.setattr("congclaw.graph.nodes.create_model", lambda: _fake_model(category))
    if route == "rag_answer":
        _stub_rag_empty_path(monkeypatch)
    if route == "agent_loop":
        _stub_agent_direct_reply(monkeypatch)

    updates = _stream_updates({"task": task})

    assert updates["intent_router"]["intent_category"] == category
    assert updates["intent_router"]["intent_route"] == route
    # 每条输入最终都产出回复话术
    assert updates[route]["final_answer"]


def test_fuzzy_input_falls_back_after_fifth_clarify_round(monkeypatch) -> None:
    """验收门：模糊输入连续追问第 5 轮后自动兜底。"""
    monkeypatch.setattr("congclaw.graph.nodes.create_model", lambda: _fake_model("clarify", confidence=0.7))

    graph = build_entry_workflow()
    state: dict[str, Any] = {"task": "帮我办一下那个", "clarify_count": 0, "unknown_count": 0}
    routes: list[str] = []
    updates: dict[str, Any] = {}
    for round_no in range(6):
        state["task"] = f"帮我办一下那个（第 {round_no + 1} 次描述）"
        updates = _stream_updates(state, graph)
        routes.append(updates["intent_router"]["intent_route"])
        # intent_router 的输出携带计数，必须合并回 state 才能驱动阈值判定
        for node in ("intent_router", "clarify", "fallback"):
            if node in updates:
                state.update(updates[node])

    assert routes == ["clarify"] * 5 + ["fallback"]
    assert updates["fallback"]["fallback_reason"] == "clarify_exceeded"


def test_irrelevant_request_falls_back_immediately(monkeypatch) -> None:
    """验收门：无关请求（如写诗）不追问、不进业务分支，直接兜底。"""
    monkeypatch.setattr("congclaw.graph.nodes.create_model", lambda: _fake_model("irrelevant", confidence=0.95))

    updates = _stream_updates({"task": "帮我写一首诗"})

    assert updates["intent_router"]["intent_route"] == "fallback"
    assert updates["fallback"]["fallback_reason"] == "irrelevant_request"
    assert updates["fallback"]["final_answer"]
    assert "rag_answer" not in updates
    assert "agent_loop" not in updates
    assert "clarify" not in updates


def test_unknown_streak_falls_back_on_second_consecutive_round(monkeypatch) -> None:
    """连续 unknown 达到阈值（2）后强制兜底。"""
    monkeypatch.setattr("congclaw.graph.nodes.create_model", lambda: _fake_model("unknown", confidence=0.2))

    graph = build_entry_workflow()
    state: dict[str, Any] = {"task": "？？"}
    routes: list[str] = []
    updates: dict[str, Any] = {}
    for _ in range(2):
        updates = _stream_updates(state, graph)
        routes.append(updates["intent_router"]["intent_route"])
        for node in ("intent_router", "clarify", "fallback"):
            if node in updates:
                state.update(updates[node])

    assert routes == ["clarify", "fallback"]
    assert updates["fallback"]["fallback_reason"] == "unknown_streak"


def test_clear_intent_resets_counts_across_turns(monkeypatch) -> None:
    """明确业务后追问/unknown 计数清零（连续语义）。"""
    monkeypatch.setattr("congclaw.graph.nodes.create_model", lambda: _fake_model("clarify", confidence=0.7))

    graph = build_entry_workflow()
    state: dict[str, Any] = {"task": "帮我办一下那个", "clarify_count": 3, "unknown_count": 1}
    updates = _stream_updates(state, graph)
    assert updates["intent_router"]["intent_route"] == "clarify"
    assert updates["clarify"]["clarify_count"] == 4

    monkeypatch.setattr("congclaw.graph.nodes.create_model", lambda: _fake_model("rag_query"))
    _stub_rag_empty_path(monkeypatch)
    updates = _stream_updates({**state, **updates["clarify"], "task": "话费怎么查"}, graph)
    assert updates["intent_router"]["intent_route"] == "rag_answer"
    assert updates["intent_router"]["clarify_count"] == 0
    assert updates["intent_router"]["unknown_count"] == 0


def test_session_counts_persist_across_turns(monkeypatch, tmp_path: Path) -> None:
    """追问计数写入会话存储，重启后（重新 load）继续累计并可在明确业务时清零。"""
    workspace = tmp_path / "ws"
    monkeypatch.setattr("congclaw.graph.nodes.create_model", lambda: _fake_model("clarify", confidence=0.7))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "cs.db"))

    events_1 = list(stream_session_events("帮我办一下那个", session_workspace=workspace))
    saved_1 = [
        e["event"]
        for e in events_1
        if e.get("type") == "custom_event" and e["event"].get("type") == "session_turn_saved"
    ]
    assert saved_1 and saved_1[0]["route"] == "clarify"
    session = load_or_create_session(workspace)
    assert session["clarify_count"] == 1

    # 模拟进程重启：释放引擎后重新走一轮，计数应延续
    asyncio.run(dispose_engine())
    monkeypatch.setattr("congclaw.graph.nodes.create_model", lambda: _fake_model("clarify", confidence=0.7))
    list(stream_session_events("还是那个事", session_workspace=workspace))
    session = load_or_create_session(workspace)
    assert session["clarify_count"] == 2

    # 明确业务后计数清零
    asyncio.run(dispose_engine())
    monkeypatch.setattr("congclaw.graph.nodes.create_model", lambda: _fake_model("rag_query"))
    _stub_rag_empty_path(monkeypatch)
    list(stream_session_events("话费怎么查", session_workspace=workspace))
    session = load_or_create_session(workspace)
    assert session["clarify_count"] == 0
    assert session["unknown_count"] == 0

    asyncio.run(dispose_engine())
