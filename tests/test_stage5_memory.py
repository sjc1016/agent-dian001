"""阶段 5 验收：多轮对话记忆与查询重写（P5-1 ~ P5-9）。

覆盖：
- P5-1/P5-2/P5-3 query_rewrite_node：首轮无窗口透传（不调模型）、无指代透传、
  指代消解重写、模型异常/非法输出安全透传；
- P5-4/P5-5/P5-9 四层记忆组装（系统提示词 → 记忆 → 证据 → 用户问题）；
- P5-6/P5-7/P5-8 user_profile 落库、增量压缩（LLM + 确定性兜底、水位幂等）；
- 验收门①："查 5G 套餐 → 那这个多少钱 → 帮我办这个"三剧本；
- 验收门②：跨会话重开后装载历史咨询主题。
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from mokioclaw.core.agent import stream_session_events_async
from mokioclaw.db.engine import dispose_engine
from mokioclaw.graph import profile_store
from mokioclaw.graph.memory import (
    assemble_layered_messages,
    build_customer_memory,
    compose_layered_content,
    render_evidence_section,
    render_memory_sections,
)
from mokioclaw.graph.nodes import query_rewrite_node
from mokioclaw.graph.profile_store import (
    aconsolidate_user_profile,
    aget_user_profile,
    aupsert_user_profile,
)
from mokioclaw.graph.workflow import build_entry_workflow


# ---------------------------------------------------------------------------
# 公共夹具与打桩
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "stage5.db"))
    monkeypatch.setenv("SKILL_WATCH", "0")
    monkeypatch.setenv("RAG_PREWARM", "0")


def _run(coro):
    return asyncio.run(coro)


def _rewrite_json(*, changed: bool, rewritten: str, reason: str = "test") -> AIMessage:
    return AIMessage(
        content=json.dumps(
            {"changed": changed, "rewritten": rewritten, "reason": reason},
            ensure_ascii=False,
        )
    )


def _intent_json(category: str) -> AIMessage:
    return AIMessage(
        content=json.dumps(
            {"category": category, "confidence": 0.95, "reason": "stage5", "missing_slots": []},
            ensure_ascii=False,
        )
    )


def _patch_writer(monkeypatch) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    monkeypatch.setattr("mokioclaw.graph.nodes._get_writer", lambda: events.append)
    return events


def _stub_rag_empty_path(monkeypatch) -> None:
    async def empty_search(query, top_k):
        return []

    async def empty_rerank(query, hits):
        return []

    async def empty_parents(ordered_ids):
        return []

    class FakeRagModel:
        async def ainvoke(self, messages, **kwargs):
            return AIMessage(content="规范的检索问句")

    monkeypatch.setattr("mokioclaw.rag.nodes.bm25_search", empty_search)
    monkeypatch.setattr("mokioclaw.rag.nodes.dense_search", empty_search)
    monkeypatch.setattr("mokioclaw.rag.nodes.rerank_hits", empty_rerank)
    monkeypatch.setattr("mokioclaw.rag.nodes.build_parent_evidence", empty_parents)
    monkeypatch.setattr("mokioclaw.rag.nodes.create_model", lambda: FakeRagModel())


# ---------------------------------------------------------------------------
# P5-1/P5-3：query_rewrite_node 单测
# ---------------------------------------------------------------------------


def test_rewrite_first_turn_passthrough_without_model_call(monkeypatch) -> None:
    """首轮无短期窗口：不调用重写模型，原样透传（省 token 且不干扰首轮链路）。"""
    events = _patch_writer(monkeypatch)

    def _unexpected_model():
        raise AssertionError("首轮无窗口不应调用重写模型")

    monkeypatch.setattr("mokioclaw.graph.nodes.create_model", _unexpected_model)

    update = _run(query_rewrite_node({"task": "查一下5G畅享套餐", "messages": []}))

    assert update["rewritten_task"] == "查一下5G畅享套餐"
    assert update["rewrite_changed"] is False
    assert update["rewrite_reason"] == ""
    rewrite_event = next(e for e in events if e.get("type") == "query_rewrite")
    assert rewrite_event["changed"] is False
    assert "no conversation window" in rewrite_event["reason"]


def test_rewrite_empty_input_passthrough(monkeypatch) -> None:
    events = _patch_writer(monkeypatch)

    def _unexpected_model():
        raise AssertionError("空输入不应调用重写模型")

    monkeypatch.setattr("mokioclaw.graph.nodes.create_model", _unexpected_model)

    state = {
        "task": "   ",
        "messages": [],
        "recent_turns": [{"turn": 1, "role": "user", "content": "上一轮对话"}],
    }
    update = _run(query_rewrite_node(state))

    assert update["rewrite_changed"] is False
    assert any(e.get("type") == "query_rewrite" for e in events)


def test_rewrite_no_coreference_passthrough(monkeypatch) -> None:
    """模型判定无指代（changed=false）时原样透传（P5-3）。"""
    events = _patch_writer(monkeypatch)
    original = "5G畅享199元档套餐的月费是多少"
    monkeypatch.setattr(
        "mokioclaw.graph.nodes.create_model",
        lambda: _StaticModel(_rewrite_json(changed=False, rewritten=original, reason="问句自包含")),
    )

    state = {
        "task": original,
        "messages": [],
        "recent_turns": [
            {"turn": 1, "role": "user", "content": "有什么5G套餐"},
            {"turn": 1, "role": "assistant", "route": "rag_answer", "content": "有129/199/299元档"},
        ],
    }
    update = _run(query_rewrite_node(state))

    assert update["rewrite_changed"] is False
    assert update["rewritten_task"] == original
    event = next(e for e in events if e.get("type") == "query_rewrite")
    assert event["changed"] is False


def test_rewrite_resolves_coreference_with_window(monkeypatch) -> None:
    """有短期窗口且模型判定 changed=true：输出独立完整问句，记忆层文本已预渲染。"""
    events = _patch_writer(monkeypatch)
    rewritten = "5G畅享199元档套餐月费多少钱"
    monkeypatch.setattr(
        "mokioclaw.graph.nodes.create_model",
        lambda: _StaticModel(_rewrite_json(changed=True, rewritten=rewritten, reason="指代上文套餐")),
    )

    state = {
        "task": "那这个多少钱",
        "messages": [],
        "recent_turns": [
            {"turn": 1, "role": "user", "content": "查一下5G畅享199元档套餐"},
            {"turn": 1, "role": "assistant", "route": "rag_answer", "content": "该套餐月费199元"},
        ],
    }
    update = _run(query_rewrite_node(state))

    assert update["rewrite_changed"] is True
    assert update["rewritten_task"] == rewritten
    assert update["rewrite_reason"] == "指代上文套餐"
    # 预渲染记忆：短期窗口内容与长期摘要标签
    assert "【会话记忆" in update["memory_context"]
    assert "查一下5G畅享199元档套餐" in update["memory_context"]
    snapshot = next(e for e in events if e.get("type") == "customer_memory_snapshot")
    assert snapshot["window_turns"] == 2
    event = next(e for e in events if e.get("type") == "query_rewrite")
    assert event["original"] == "那这个多少钱"
    assert event["rewritten"] == rewritten


def test_rewrite_model_exception_passthrough(monkeypatch) -> None:
    """重写模型异常：安全透传且发出 error 事件，绝不阻断主链路。"""
    events = _patch_writer(monkeypatch)

    class BoomModel:
        async def ainvoke(self, messages, **kwargs):
            raise RuntimeError("llm unavailable")

    monkeypatch.setattr("mokioclaw.graph.nodes.create_model", lambda: BoomModel())

    state = {
        "task": "那这个怎么办",
        "messages": [],
        "recent_turns": [{"turn": 1, "role": "user", "content": "宽带报修进度"}],
    }
    update = _run(query_rewrite_node(state))

    assert update["rewrite_changed"] is False
    assert update["rewritten_task"] == "那这个怎么办"
    assert any(e.get("type") == "query_rewrite_error" for e in events)


def test_rewrite_invalid_json_passthrough(monkeypatch) -> None:
    """模型返回无法解析的 JSON 片段时原样透传。"""
    events = _patch_writer(monkeypatch)
    monkeypatch.setattr(
        "mokioclaw.graph.nodes.create_model",
        lambda: _StaticModel(AIMessage(content='{"changed": true, "rewritten":')),
    )

    state = {
        "task": "它呢",
        "messages": [],
        "recent_turns": [{"turn": 1, "role": "user", "content": "199元档套餐"}],
    }
    update = _run(query_rewrite_node(state))

    assert update["rewrite_changed"] is False
    assert update["rewritten_task"] == "它呢"
    assert any(e.get("type") == "query_rewrite" for e in events)


class _StaticModel:
    def __init__(self, message: AIMessage):
        self._message = message

    async def ainvoke(self, messages, **kwargs):
        return self._message


# ---------------------------------------------------------------------------
# P5-4/P5-5/P5-9：四层记忆组装
# ---------------------------------------------------------------------------


def test_four_layer_section_order_and_content() -> None:
    """统一入口组装：系统提示词 → 记忆（短期窗口+长期摘要）→ 证据 → 用户问题。"""
    state = {
        "task": "帮我办这个",
        "recent_turns": [
            {"turn": 1, "role": "user", "content": "查一下5G畅享199元档套餐"},
            {"turn": 1, "role": "assistant", "route": "rag_answer", "content": "月费199元"},
        ],
        "user_profile": {
            "phone": "13900001111",
            "summary": "该用户常住杭州，偏好高带宽融合套餐。",
            "topics": ["5G套餐资费咨询", "宽带故障报修"],
            "open_tickets": [
                {"ticket_id": "FT20260001", "fault_type": "宽带故障", "status": "处理中"}
            ],
            "preferred_package": "5G畅享199元档",
        },
    }
    evidence = [{"source_no": 1, "doc_source": "套餐手册.md", "text": "5G畅享199元档月费199元，含60GB国内流量。"}]

    messages = assemble_layered_messages("SYSTEM_PROMPT", state, question="帮我办理5G畅享199元档套餐", evidence=evidence, node="intent_router")

    assert messages[0].content == "SYSTEM_PROMPT"
    human = messages[1].content
    idx_window = human.find("【会话记忆")
    idx_profile = human.find("【长期用户摘要")
    idx_evidence = human.find("【检索到的知识库证据")
    idx_question = human.find("【用户问题】")
    assert 0 <= idx_window < idx_profile < idx_evidence < idx_question
    # 内容齐备
    assert "查一下5G畅享199元档套餐" in human
    assert "历史咨询主题：5G套餐资费咨询、宽带故障报修" in human
    assert "工单 FT20260001" in human
    assert "偏好/关注套餐：5G畅享199元档" in human
    assert "[1] 来源：套餐手册.md" in human
    assert human.rstrip().endswith("帮我办理5G畅享199元档套餐")


def test_assemble_without_memory_only_question() -> None:
    """无窗口、无摘要、无证据：只保留用户问题段。"""
    messages = assemble_layered_messages("SYS", {"task": "查余额"}, question="查余额")
    human = messages[1].content
    assert "【会话记忆" not in human
    assert "【长期用户摘要" not in human
    assert "【检索到的知识库证据" not in human
    assert human.strip() == "【用户问题】\n查余额"


def test_compose_extra_sections_between_evidence_and_question() -> None:
    content = compose_layered_content(
        question="办理199档",
        memory_text="【会话记忆】\n用户：想换套餐",
        evidence=[{"source_no": 2, "doc_source": "资费表", "text": "199元/月"}],
        extra_sections=["待补槽位：target_package"],
    )
    idx_memory = content.find("【会话记忆】")
    idx_evidence = content.find("【检索到的知识库证据")
    idx_extra = content.find("待补槽位")
    idx_question = content.find("【用户问题】")
    assert idx_memory < idx_evidence < idx_extra < idx_question
    assert "[2] 来源：资费表" in content
    assert render_evidence_section([]) == ""


def test_build_customer_memory_working_fields() -> None:
    memory = build_customer_memory(
        {
            "task": "流量呢",
            "intent_category": "clarify",
            "pending_slots": ["target_package"],
            "clarify_count": 1,
            "session_id": "s1",
            "session_turn": 2,
            "phone": "13900002222",
            "recent_turns": [
                {"turn": 1, "role": "user", "content": "推荐套餐"},
                {"turn": 1, "role": "assistant", "route": "rag_answer", "content": "199档不错"},
            ],
        },
        node="intent_router",
    )
    working = memory["working_memory"]
    assert working["current_intent"] == "clarify"
    assert working["pending_slots"] == ["target_package"]
    assert len(working["short_term_window"]) == 2
    profile = memory["long_term_profile"]
    assert profile["exists"] is False
    rendered = render_memory_sections(memory)
    assert "当前意图：clarify" in rendered
    assert "待确认槽位：target_package" in rendered


# ---------------------------------------------------------------------------
# P5-6/P5-7/P5-8：user_profile 仓储与增量压缩
# ---------------------------------------------------------------------------


def test_profile_upsert_get_roundtrip() -> None:
    phone = "13900000001"

    async def _scenario() -> None:
        assert await aget_user_profile(phone) is None
        await aupsert_user_profile(
            {
                "phone": phone,
                "summary": "用户咨询过5G套餐。",
                "topics": ["5G套餐资费咨询", "宽带故障报修", "5G套餐资费咨询"],
                "open_tickets": [
                    {"ticket_id": "FT1", "fault_type": "宽带故障", "status": "处理中"}
                ],
                "preferred_package": "5G畅享199元档",
                "turn_count": 2,
            }
        )
        got = await aget_user_profile(phone)
        assert got is not None
        assert got["preferred_package"] == "5G畅享199元档"
        assert got["topics"] == ["5G套餐资费咨询", "宽带故障报修"]  # upsert 去重
        assert got["open_tickets"][0]["ticket_id"] == "FT1"
        assert got["turn_count"] == 2

    _run(_scenario())


class _CountingBoomModel:
    """模拟无 LLM 环境：每次调用都抛错并计数（用于验证水位内不再尝试压缩）。"""

    calls = 0

    async def ainvoke(self, messages, **kwargs):
        type(self).calls += 1
        raise RuntimeError("no llm configured")


def _session_with_turn(turn: int, user_text: str, assistant: str = "已为您处理。", route: str = "agent_loop") -> dict[str, Any]:
    return {
        "turn_index": turn,
        "last_task": user_text,
        "recent_turns": [
            {"turn": turn, "role": "user", "content": user_text},
            {"turn": turn, "role": "assistant", "route": route, "content": assistant},
        ],
    }


def test_consolidate_rule_based_fallback_topics_package_and_ticket(monkeypatch, tmp_path: Path) -> None:
    """LLM 不可用时确定性兜底：工具轨迹 → 主题/工单，关键词 → 偏好套餐。"""
    _CountingBoomModel.calls = 0
    monkeypatch.setattr(profile_store, "create_model", lambda: _CountingBoomModel())
    phone = "13900000002"
    session = _session_with_turn(1, "帮我改成5G畅享199元档，另外家里宽带断了帮我报修")
    traces = [
        {"type": "skill_call", "name": "change_package", "args": {"target_package": "5G畅享199元档"}},
        {"type": "skill_call", "name": "report_fault", "args": {"fault_type": "宽带故障"}},
        {
            "type": "skill_result",
            "name": "report_fault",
            "ok": True,
            "preview": json.dumps(
                {"ticket_id": "FT20260001", "fault_type": "宽带故障", "status": "已受理"},
                ensure_ascii=False,
            ),
        },
    ]

    async def _scenario() -> None:
        profile = await aconsolidate_user_profile(
            phone,
            workspace=str(tmp_path / "ws-a"),
            session=session,
            route="agent_loop",
            response="已提交套餐变更确认与宽带报修。",
            tool_traces=traces,
        )
        assert profile["compression"] == "rule_based"
        assert "套餐变更办理" in profile["topics"]
        assert "故障报修" in profile["topics"]
        assert profile["preferred_package"] == "5G畅享199元档"
        tickets = profile["open_tickets"]
        assert len(tickets) == 1
        assert tickets[0]["ticket_id"] == "FT20260001"
        assert tickets[0]["fault_type"] == "宽带故障"
        assert "FT20260001" in profile["summary"]
        # 落库可读
        reloaded = await aget_user_profile(phone)
        assert reloaded is not None
        assert reloaded["turn_count"] == 1

        # 水位幂等：同样的 session（无新轮次）不再触发压缩/模型调用
        before = _CountingBoomModel.calls
        again = await aconsolidate_user_profile(
            phone,
            workspace=str(tmp_path / "ws-a"),
            session=session,
            route="agent_loop",
            response="x",
            tool_traces=traces,
        )
        assert again["turn_count"] == 1
        assert _CountingBoomModel.calls == before

    _run(_scenario())


def test_consolidate_rag_route_extracts_package_topic(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(profile_store, "create_model", lambda: _CountingBoomModel())
    phone = "13900000003"
    session = _session_with_turn(1, "5G畅享129元档套餐包含什么", "包含30GB国内流量。", route="rag_answer")

    async def _scenario() -> None:
        profile = await aconsolidate_user_profile(
            phone,
            workspace=str(tmp_path / "ws-rag"),
            session=session,
            route="rag_answer",
            response="包含30GB国内流量。",
            tool_traces=[],
        )
        assert profile["topics"][0] == "5G畅享129元档相关咨询"
        assert profile["preferred_package"] == "5G畅享129元档"

    _run(_scenario())


def test_consolidate_llm_merge_with_rule_fill(monkeypatch, tmp_path: Path) -> None:
    """LLM 压缩路径：模型主题保留，漏抽的偏好套餐由规则从工具轨迹补齐。"""

    class LlmProfileModel:
        async def ainvoke(self, messages, **kwargs):
            return AIMessage(
                content=json.dumps(
                    {
                        "topics": ["国际漫游开通咨询"],
                        "open_tickets": [],
                        "preferred_package": "",
                        "summary": "用户咨询国际漫游开通。",
                    },
                    ensure_ascii=False,
                )
            )

    monkeypatch.setattr(profile_store, "create_model", lambda: LlmProfileModel())
    phone = "13900000004"
    session = _session_with_turn(1, "帮我改成5G畅享299元档，顺便问下国际漫游")
    traces = [
        {"type": "skill_call", "name": "change_package", "args": {"target_package": "5G畅享299元档"}},
    ]

    async def _scenario() -> None:
        profile = await aconsolidate_user_profile(
            phone,
            workspace=str(tmp_path / "ws-llm"),
            session=session,
            route="agent_loop",
            response="已提交变更确认。",
            tool_traces=traces,
        )
        assert profile["compression"] == "llm"
        assert "套餐变更办理" in profile["topics"]  # 规则强信号置顶
        assert "国际漫游开通咨询" in profile["topics"]
        assert profile["preferred_package"] == "5G畅享299元档"
        assert "国际漫游" in profile["summary"]

    _run(_scenario())


def test_consolidate_completed_ticket_removed(monkeypatch, tmp_path: Path) -> None:
    """已完成/已取消的工单从在办列表移除。"""
    monkeypatch.setattr(profile_store, "create_model", lambda: _CountingBoomModel())
    phone = "13900000005"

    async def _scenario() -> None:
        session1 = _session_with_turn(1, "家里宽带断了，帮我报修")
        traces1 = [
            {"type": "skill_call", "name": "report_fault", "args": {"fault_type": "宽带故障"}},
            {
                "type": "skill_result",
                "name": "report_fault",
                "ok": True,
                "preview": json.dumps(
                    {"ticket_id": "FT9", "fault_type": "宽带故障", "status": "已受理"},
                    ensure_ascii=False,
                ),
            },
        ]
        first = await aconsolidate_user_profile(
            phone, workspace=str(tmp_path / "ws-f"), session=session1,
            route="agent_loop", response="报修已受理。", tool_traces=traces1,
        )
        assert [t["ticket_id"] for t in first["open_tickets"]] == ["FT9"]

        session2 = _session_with_turn(2, "工单处理好了吗")
        traces2 = [
            {
                "type": "skill_result",
                "name": "report_fault",
                "ok": True,
                "preview": json.dumps(
                    {"ticket_id": "FT9", "fault_type": "宽带故障", "status": "已完成"},
                    ensure_ascii=False,
                ),
            },
        ]
        second = await aconsolidate_user_profile(
            phone, workspace=str(tmp_path / "ws-f"), session=session2,
            route="agent_loop", response="工单已完成。", tool_traces=traces2,
        )
        assert second["open_tickets"] == []
        assert second["turn_count"] == 2

    _run(_scenario())


# ---------------------------------------------------------------------------
# 验收门①：查 5G 套餐 → 那这个多少钱 → 帮我办这个（图级三剧本）
# ---------------------------------------------------------------------------


class _ScriptedEntryModel:
    """按系统提示词区分重写/路由节点；重写按剧本表返回，路由按关键词分类。"""

    REWRITES = {
        "那这个多少钱": "5G畅享129元档套餐月费多少钱",
        "帮我办这个": "帮我办理5G畅享129元档套餐",
    }

    async def ainvoke(self, messages, **kwargs):
        system = str(messages[0].content)
        human = str(messages[-1].content)
        question = human.rsplit("【用户问题】", 1)[-1].strip()
        if "查询重写模块" in system:
            rewritten = self.REWRITES.get(question)
            if rewritten is not None:
                return _rewrite_json(changed=True, rewritten=rewritten, reason="指代上文5G套餐")
            return _rewrite_json(changed=False, rewritten=question, reason="自包含问句")
        # 意图识别
        category = "agent_service" if "办理" in question else "rag_query"
        return _intent_json(category)


class _ScriptedAgentModel:
    """think：从重写后问句中抽取套餐名作为 change_package 槽位（模拟模型槽位带出）。"""

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages, **kwargs):
        system = str(messages[0].content)
        human = str(messages[-1].content)
        if "业务推理" in system:
            match = re.search(r"5G畅享\d+元档", human)
            package = match.group(0) if match else ""
            return AIMessage(
                content="",
                tool_calls=[
                    {"id": "call-1", "name": "change_package", "args": {"target_package": package}}
                ],
            )
        if "结果反思校验" in system:
            return AIMessage(
                content=json.dumps({"decision": "pass", "reason": "ok", "checks": []}, ensure_ascii=False)
            )
        return AIMessage(content="已为您发起5G畅享129元档套餐变更确认。")


def _pair(turn: int, user: str, assistant: str, route: str) -> list[dict[str, Any]]:
    return [
        {"turn": turn, "role": "user", "content": user},
        {"turn": turn, "role": "assistant", "route": route, "content": assistant, "summary": assistant},
    ]


def _drive_graph(state: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    async def _collect():
        updates: dict[str, Any] = {}
        events: list[dict[str, Any]] = []
        async for mode, chunk in build_entry_workflow().astream(
            state, stream_mode=["updates", "custom"]
        ):
            if mode == "custom":
                events.append(chunk)
            else:
                for node, update in chunk.items():
                    updates[node] = update
                    if isinstance(update, dict):
                        state.update(update)
        return updates, events

    return asyncio.run(_collect())


def _event(events: list[dict[str, Any]], event_type: str) -> dict[str, Any]:
    matches = [e for e in events if isinstance(e, dict) and e.get("type") == event_type]
    assert matches, f"缺少事件 {event_type}，实际事件：{[e.get('type') for e in events if isinstance(e, dict)]}"
    return matches[-1]


def test_coreference_script_rewrite_then_route_agent_with_package_slot(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("mokioclaw.graph.nodes.create_model", lambda: _ScriptedEntryModel())
    _stub_rag_empty_path(monkeypatch)
    monkeypatch.setattr("mokioclaw.agent.nodes.create_model", lambda: _ScriptedAgentModel())

    state: dict[str, Any] = {
        "task": "查一下5G畅享129元档套餐",
        "messages": [],
        "workspace": str(tmp_path / "ws-script"),
        "phone": "13800138000",
        "approval_mode": "inline",
        "max_attempts": 3,
        "recent_turns": [],
    }

    # 第 1 轮：套餐咨询（首轮透传，走 RAG）
    updates1, events1 = _drive_graph(state)
    rewrite1 = _event(events1, "query_rewrite")
    assert rewrite1["changed"] is False
    assert updates1["intent_router"]["intent_route"] == "rag_answer"
    rag_start1 = _event(events1, "rag_start")
    assert rag_start1["query"] == "查一下5G畅享129元档套餐"
    answer1 = updates1["rag_answer"]["final_answer"]
    assert answer1

    # 第 2 轮："那这个多少钱" → 指代正确重写
    state["task"] = "那这个多少钱"
    state["recent_turns"] = _pair(1, "查一下5G畅享129元档套餐", answer1, "rag_answer")
    updates2, events2 = _drive_graph(state)
    rewrite2 = _event(events2, "query_rewrite")
    assert rewrite2["changed"] is True
    assert rewrite2["rewritten"] == "5G畅享129元档套餐月费多少钱"
    decision2 = _event(events2, "intent_decision")
    assert decision2["original_input"] == "那这个多少钱"
    assert decision2["rewritten_input"] == "5G畅享129元档套餐月费多少钱"
    assert decision2["rewrite_changed"] is True
    rag_start2 = _event(events2, "rag_start")
    assert rag_start2["query"] == "5G畅享129元档套餐月费多少钱"
    assert rag_start2["original_query"] == "那这个多少钱"
    snapshot2 = _event(events2, "customer_memory_snapshot")
    assert snapshot2["window_turns"] == 2
    answer2 = updates2["rag_answer"]["final_answer"]
    assert answer2

    # 第 3 轮："帮我办这个" → 重写含套餐名 → 路由办理 Agent，槽位自动带出套餐名
    state["task"] = "帮我办这个"
    state["recent_turns"] = [
        *_pair(1, "查一下5G畅享129元档套餐", answer1, "rag_answer"),
        *_pair(2, "那这个多少钱", answer2, "rag_answer"),
    ]
    updates3, events3 = _drive_graph(state)
    rewrite3 = _event(events3, "query_rewrite")
    assert rewrite3["rewritten"] == "帮我办理5G畅享129元档套餐"
    decision3 = _event(events3, "intent_decision")
    assert decision3["route"] == "agent_loop"
    agent_start = _event(events3, "agent_start")
    assert agent_start["query"] == "帮我办理5G畅享129元档套餐"
    assert agent_start["original_query"] == "帮我办这个"
    # 槽位自动带出：think 模型从重写后的问句中解析出套餐名（写操作在执行前短路到确认卡片）
    thinking = _event(events3, "agent_thinking")
    package_call = next(call for call in thinking["calls"] if call["name"] == "change_package")
    assert package_call["args"]["target_package"] == "5G畅享129元档"
    # 写操作必须出人工确认卡片，且未实际执行技能
    assert _event(events3, "agent_confirm_required")
    assert not [e for e in events3 if e.get("type") in ("skill_call", "skill_result")]
    assert updates3["agent_loop"]["final_answer"]


# ---------------------------------------------------------------------------
# 验收门②：跨会话重开后记起历史咨询主题
# ---------------------------------------------------------------------------


class _RagIntentModel:
    async def ainvoke(self, messages, **kwargs):
        return _intent_json("rag_query")


def _custom_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        e["event"]
        for e in events
        if e.get("type") == "custom_event" and isinstance(e.get("event"), dict)
    ]


def test_cross_session_profile_loaded_into_memory(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("mokioclaw.graph.nodes.create_model", lambda: _RagIntentModel())
    _stub_rag_empty_path(monkeypatch)

    def _no_llm():
        raise RuntimeError("profile compression runs without LLM in test")

    monkeypatch.setattr(profile_store, "create_model", _no_llm)

    phone = "13900999000"
    ws1 = tmp_path / "ws-old-customer"
    ws2 = tmp_path / "ws-new-session"

    async def _scenario() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        events1 = [
            e
            async for e in stream_session_events_async(
                "5G畅享199元档套餐资费是多少", session_workspace=ws1, phone=phone
            )
        ]
        # 直接验证长期摘要已按号码落库
        persisted = await aget_user_profile(phone)
        assert persisted is not None
        assert persisted["preferred_package"] == "5G畅享199元档"
        assert persisted["topics"]

        # 模拟跨会话重开（释放会话引擎，新 workspace）
        await dispose_engine()
        events2 = [
            e
            async for e in stream_session_events_async(
                "你好，我再问个事", session_workspace=ws2, phone=phone
            )
        ]
        return _custom_events(events1), _custom_events(events2)

    events1, events2 = asyncio.run(_scenario())

    updated = next(e for e in events1 if e["type"] == "profile_updated")
    assert updated["compression"] == "rule_based"
    assert updated["preferred_package"] == "5G畅享199元档"
    assert updated["topics"]

    loaded = next(e for e in events2 if e["type"] == "profile_loaded")
    assert loaded["exists"] is True
    assert loaded["preferred_package"] == "5G畅享199元档"
    assert loaded["topics"] == updated["topics"]

    # 长期摘要已进入新会话的四层记忆（首轮无短期窗口，但长期层有内容）
    snapshot = next(e for e in events2 if e["type"] == "customer_memory_snapshot")
    assert snapshot["profile_exists"] is True
    assert snapshot["profile_topics"] == updated["topics"]
    assert snapshot["window_turns"] == 0
