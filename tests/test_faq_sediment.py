"""阶段 7 验收：常见问答沉淀（门控规则 / 子图拓扑 / 落库 / 主图接线）。

所有 LLM 与向量召回都在 ``congclaw.faq.nodes`` 命名空间打桩，测试只验证
门控判定、图拓扑、事件序列与落库结果，不加载真实模型与本地向量模型。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from congclaw.api.main import app
from congclaw.faq.gate import evaluate_sediment_gate, resolve_sediment_question
from congclaw.faq.reflow import (
    REFLOW_SOURCE,
    areflow_published_faq,
    reflow_path,
    render_faq_markdown,
)
from congclaw.faq.store import (
    afaq_stats,
    aget_faq,
    alist_faq,
    aupdate_faq_status,
    aupsert_faq,
)
from congclaw.faq.workflow import build_faq_sediment_subgraph
from congclaw.graph.nodes import faq_sediment_node, sediment_route
from congclaw.graph.workflow import build_entry_workflow
from congclaw.rag.ingest import IngestStats


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "faq.db"))
    monkeypatch.setenv("SKILL_WATCH", "0")
    monkeypatch.setenv("RAG_PREWARM", "0")
    # 回流会往 knowledge/ 写文档，测试统一改到临时目录，避免污染真实知识库
    monkeypatch.setenv("KNOWLEDGE_DIR", str(tmp_path / "knowledge"))


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 门控规则：一次判定决定「是否值得沉淀」
# ---------------------------------------------------------------------------


def _rag_turn(**over: Any) -> dict[str, Any]:
    """一个典型的、可沉淀的 RAG 问答回合。"""
    state: dict[str, Any] = {
        "intent_route": "rag_answer",
        "final_answer": "5G 畅享套餐每月包含 100GB 流量。[1]",
        "sources": [{"title": "资费说明.md", "url": "", "score": 0.9}],
        "rewritten_task": "5G畅享套餐包含多少流量",
        "task": "5G畅享套餐包含多少流量",
    }
    state.update(over)
    return state


def _agent_turn(**over: Any) -> dict[str, Any]:
    """一个典型的、可沉淀的业务办理回合。"""
    state: dict[str, Any] = {
        "intent_route": "agent_loop",
        "final_answer": "已为您查询到当前话费余额 86.50 元。",
        "tool_traces": [{"type": "skill_result", "name": "query_balance", "ok": True}],
        "rewritten_task": "帮我查一下话费余额",
        "task": "帮我查一下话费余额",
    }
    state.update(over)
    return state


def test_gate_passes_for_rag_turn_with_sources() -> None:
    passed, reason = evaluate_sediment_gate(_rag_turn())
    assert (passed, reason) == (True, "")


def test_gate_passes_for_agent_turn_with_successful_skill() -> None:
    passed, reason = evaluate_sediment_gate(_agent_turn())
    assert (passed, reason) == (True, "")


@pytest.mark.parametrize("route", ["clarify", "fallback", "unknown", ""])
def test_gate_rejects_non_sedimentable_route(route: str) -> None:
    """clarify / fallback 等分支没有可复用的解决方案，规则层直接挡掉。"""
    passed, reason = evaluate_sediment_gate(_rag_turn(intent_route=route))
    assert passed is False
    assert reason == "route_not_sedimentable"


def test_gate_rejects_empty_answer() -> None:
    passed, reason = evaluate_sediment_gate(_rag_turn(final_answer="", sources=[]))
    assert (passed, reason) == (False, "empty_answer")


def test_gate_rejects_fallback_turn() -> None:
    """本轮是兜底回复：即使路由落在 rag_answer，也不沉淀。"""
    passed, reason = evaluate_sediment_gate(_rag_turn(fallback_reason="rag_empty"))
    assert (passed, reason) == (False, "fallback_turn")


def test_gate_rejects_pending_confirmation() -> None:
    """只推送了确认卡片、业务尚未执行，没有解决方案可沉淀。"""
    passed, reason = evaluate_sediment_gate(
        _agent_turn(metadata={"pending_confirmation": True})
    )
    assert (passed, reason) == (False, "pending_confirmation")


def test_gate_rejects_rag_turn_without_sources() -> None:
    passed, reason = evaluate_sediment_gate(_rag_turn(sources=[]))
    assert (passed, reason) == (False, "no_evidence")


def test_gate_rejects_agent_turn_without_successful_skill() -> None:
    """Skill 调用失败（ok=False）不足以支撑可复用结论。"""
    passed, reason = evaluate_sediment_gate(
        _agent_turn(tool_traces=[{"type": "skill_result", "name": "query_balance", "ok": False}])
    )
    assert (passed, reason) == (False, "no_tool_result")


def test_gate_rejects_agent_turn_without_any_skill() -> None:
    passed, reason = evaluate_sediment_gate(_agent_turn(tool_traces=[]))
    assert (passed, reason) == (False, "no_tool_result")


@pytest.mark.parametrize("question", ["确认", "好的", "多少钱", "5G套餐"])
def test_gate_rejects_too_short_question(question: str) -> None:
    passed, reason = evaluate_sediment_gate(_rag_turn(rewritten_task=question, task=question))
    assert (passed, reason) == (False, "question_too_short")


@pytest.mark.parametrize(
    "question",
    [
        "我的手机号13800138000能办什么套餐",  # 汉字紧邻手机号，\b 会漏判
        "手机号是13800138000",
        "身份证110101199003071234能办吗",
        "卡号6222021234567890123怎么解绑",
    ],
)
def test_gate_rejects_privacy_question(question: str) -> None:
    """含个人隐私标识的问句不具备通用性，且不应进入任何库。"""
    passed, reason = evaluate_sediment_gate(_rag_turn(rewritten_task=question, task=question))
    assert (passed, reason) == (False, "privacy_risk")


def test_resolve_sediment_question_backtracks_approval_reply() -> None:
    """用户只回"确认"时，沉淀问句回溯到上一轮真实业务诉求。"""
    state = _agent_turn(
        task="确认",
        rewritten_task="确认",
        recent_turns=[
            {"role": "user", "content": "帮我把套餐升级成199档"},
            {"role": "assistant", "content": "确认办理吗？"},
        ],
    )
    assert resolve_sediment_question(state) == "帮我把套餐升级成199档"
    passed, reason = evaluate_sediment_gate(state)
    assert (passed, reason) == (True, "")


def test_resolve_sediment_question_keeps_normal_question() -> None:
    state = _rag_turn(rewritten_task="5G畅享套餐包含多少流量")
    assert resolve_sediment_question(state) == "5G畅享套餐包含多少流量"


# ---------------------------------------------------------------------------
# 子图：打桩工具
# ---------------------------------------------------------------------------


class _ScriptedSedimentModel:
    """按预设文本返回的假模型（同时支持 ainvoke / invoke）。"""

    def __init__(self, content: str) -> None:
        self._content = content
        self.calls = 0

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        return AIMessage(content=self._content)


class _BoomModel:
    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003
        raise RuntimeError("model down")


def _create_payload(**entry_over: Any) -> str:
    entry: dict[str, Any] = {
        "canonical_question": "5G畅享套餐包含多少流量",
        "question_variants": ["5G套餐有多少流量"],
        "category": "咨询",
        "solution": "5G畅享套餐每月包含 100GB 流量，超出后按 3 元/GB 计费。",
        "preconditions": [],
        "related_skills": [],
        "keywords": "5G 畅享套餐 流量",
        "confidence": 0.9,
    }
    entry.update(entry_over)
    payload = {
        "should_sediment": True,
        "action": "create",
        "merge_into": "",
        "entry": entry,
        "reason": "通用资费咨询，具备复用价值",
    }
    return json.dumps(payload, ensure_ascii=False)


def _sub_input(**over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "task": "5G畅享套餐包含多少流量",
        "rewritten_task": "5G畅享套餐包含多少流量",
        "route": "rag_answer",
        "category": "咨询",
        "confidence": 0.95,
        "final_answer": "5G 畅享套餐每月包含 100GB 流量。[1]",
        "sources": [{"title": "资费说明.md", "url": "", "score": 0.9}],
        "tool_traces": [],
        "recent_turns": [],
        "session_id": "sess-1",
        "session_turn": 3,
    }
    data.update(over)
    return data


def _patch_subgraph(monkeypatch: pytest.MonkeyPatch, model: Any = None) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    monkeypatch.setattr("congclaw.faq.nodes._get_writer", lambda: events.append)

    async def _no_similar(question: str, top_k: int = 5):
        return []

    monkeypatch.setattr("congclaw.faq.nodes.asearch_similar", _no_similar)
    if model is not None:
        monkeypatch.setattr("congclaw.faq.nodes.create_model", lambda: model)
    return events


def _run_subgraph(state: dict[str, Any]) -> dict[str, Any]:
    async def _collect() -> dict[str, Any]:
        result: dict[str, Any] = {}
        async for mode, chunk in build_faq_sediment_subgraph().astream(
            state, stream_mode=["updates", "custom"]
        ):
            if mode == "updates" and isinstance(chunk, dict):
                for update in chunk.values():
                    if isinstance(update, dict):
                        result.update(update)
        return result

    return _run(_collect())


def _types(events: list[dict[str, Any]]) -> list[str]:
    return [str(event.get("type")) for event in events]


# ---------------------------------------------------------------------------
# 子图：门控短路与四条执行路径
# ---------------------------------------------------------------------------


def test_subgraph_short_circuits_without_model_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """门控不通过时短路结束：不召回、不调模型、不落库。"""

    def _unexpected():
        raise AssertionError("门控未通过却调用了 LLM")

    events = _patch_subgraph(monkeypatch)
    monkeypatch.setattr("congclaw.faq.nodes.create_model", _unexpected)

    result = _run_subgraph(_sub_input(route="clarify", sources=[]))

    assert result["gate_passed"] is False
    assert result["gate_reason"] == "route_not_sedimentable"
    assert "persisted" not in result
    assert _types(events) == ["faq_gate"]
    assert _run(alist_faq())["total"] == 0


def test_subgraph_creates_draft_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """正常路径：抽取成功 → 落库为 draft 待审条目，并带完整溯源信息。"""
    model = _ScriptedSedimentModel(_create_payload())
    events = _patch_subgraph(monkeypatch, model)

    result = _run_subgraph(_sub_input())

    assert result["action"] == "create"
    assert result["persisted"] is True
    assert result["faq_id"] == "FAQ0001"

    saved = _run(aget_faq("FAQ0001"))
    assert saved is not None
    assert saved["status"] == "draft"
    assert saved["canonical_question"] == "5G畅享套餐包含多少流量"
    assert saved["category"] == "咨询"
    assert saved["source_route"] == "rag_answer"
    assert saved["source_session_id"] == "sess-1"
    assert saved["source_turn_index"] == 3
    assert saved["merge_count"] == 1
    assert saved["sources"] == [{"title": "资费说明.md", "url": "", "score": 0.9}]

    types = _types(events)
    assert types == [
        "faq_gate",
        "faq_match",
        "faq_sediment_decision",
        "faq_sediment_saved",
    ]
    assert model.calls == 1


def test_subgraph_merge_keeps_published_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """合并路径：命中已有条目时递增常见度，且不把已发布条目打回草稿。"""
    existing = _run(
        aupsert_faq(
            {
                "canonical_question": "5G畅享套餐包含多少流量",
                "category": "咨询",
                "solution": "5G 畅享套餐每月包含 100GB 流量。",
                "status": "published",
                "merge_count": 1,
            }
        )
    )
    assert existing["faq_id"] == "FAQ0001"

    payload = json.dumps(
        {
            "should_sediment": True,
            "action": "merge",
            "merge_into": "FAQ0001",
            "entry": {
                "canonical_question": "5G畅享套餐包含多少流量",
                "question_variants": ["5G套餐流量有多少"],
                "category": "咨询",
                "solution": "5G 畅享套餐每月包含 100GB 流量，超出后 3 元/GB。",
                "keywords": "5G 流量",
                "confidence": 0.88,
            },
            "reason": "与已有条目同义",
        },
        ensure_ascii=False,
    )
    events = _patch_subgraph(monkeypatch, _ScriptedSedimentModel(payload))

    result = _run_subgraph(_sub_input())

    assert result["action"] == "merge"
    assert result["faq_id"] == "FAQ0001"
    merged = _run(aget_faq("FAQ0001"))
    assert merged["merge_count"] == 2
    assert merged["status"] == "published"
    assert merged["question_variants"] == ["5G套餐流量有多少"]
    assert _run(alist_faq())["total"] == 1
    assert _types(events)[-1] == "faq_sediment_saved"


def test_subgraph_merge_falls_back_to_create_when_target_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型给出不存在的合并目标（幻觉编号）时降级为新建，不丢本轮沉淀。"""
    payload = json.dumps(
        {
            "should_sediment": True,
            "action": "merge",
            "merge_into": "FAQ9999",
            "entry": {
                "canonical_question": "宽带报修后多久上门",
                "solution": "报修后 24 小时内安排工程师上门。",
                "category": "故障",
                "confidence": 0.8,
            },
            "reason": "疑似重复",
        },
        ensure_ascii=False,
    )
    events = _patch_subgraph(monkeypatch, _ScriptedSedimentModel(payload))

    result = _run_subgraph(
        _sub_input(
            route="agent_loop",
            sources=[],
            tool_traces=[{"type": "skill_result", "name": "repair", "ok": True}],
            rewritten_task="宽带报修后多久上门",
            task="宽带报修后多久上门",
        )
    )

    assert result["action"] == "create"
    assert result["faq_id"] == "FAQ0001"
    assert _types(events)[-1] == "faq_sediment_saved"


def test_subgraph_model_declines_to_sediment(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型判定无复用价值：不写库，仅留 skip 事件。"""
    payload = json.dumps(
        {"should_sediment": False, "action": "skip", "entry": {}, "reason": "一次性个人事务"},
        ensure_ascii=False,
    )
    events = _patch_subgraph(monkeypatch, _ScriptedSedimentModel(payload))

    result = _run_subgraph(_sub_input())

    assert result["action"] == "skip"
    assert result["persisted"] is False
    assert _types(events)[-1] == "faq_sediment_skipped"
    assert _run(alist_faq())["total"] == 0


def test_subgraph_model_error_degrades_to_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型异常：本轮不沉淀，链路不抛错、不落库。"""
    events = _patch_subgraph(monkeypatch, _BoomModel())

    result = _run_subgraph(_sub_input())

    assert result["action"] == "skip"
    assert result["persisted"] is False
    assert "RuntimeError" in result["error"]
    error_events = [e for e in events if e.get("type") == "faq_sediment_error"]
    assert error_events and error_events[0]["stage"] == "extract"
    assert _run(alist_faq())["total"] == 0


def test_subgraph_invalid_json_degrades_to_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    events = _patch_subgraph(monkeypatch, _ScriptedSedimentModel("抱歉，我无法处理该请求。"))

    result = _run_subgraph(_sub_input())

    assert result["action"] == "skip"
    assert result["error"] == "invalid_json"
    assert _run(alist_faq())["total"] == 0


def test_subgraph_extract_timeout_degrades_to_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型挂起（不抛错也不返回）时按超时降级，绝不拖住已完成的回合。"""

    class _HangingModel:
        async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003
            await asyncio.sleep(30)
            return AIMessage(content=_create_payload())

    events = _patch_subgraph(monkeypatch, _HangingModel())
    monkeypatch.setattr("congclaw.faq.nodes.EXTRACT_TIMEOUT_SECONDS", 0.05)

    result = _run_subgraph(_sub_input())

    assert result["action"] == "skip"
    assert result["persisted"] is False
    assert result["error"].startswith("TimeoutError")
    error_events = [e for e in events if e.get("type") == "faq_sediment_error"]
    assert error_events and error_events[0]["stage"] == "extract"
    assert _run(alist_faq())["total"] == 0


def test_subgraph_accepts_fenced_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型输出带 ```json 围栏时仍能解析。"""
    fenced = f"```json\n{_create_payload()}\n```"
    _patch_subgraph(monkeypatch, _ScriptedSedimentModel(fenced))

    result = _run_subgraph(_sub_input())

    assert result["persisted"] is True
    assert result["faq_id"] == "FAQ0001"


def test_subgraph_skips_empty_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型声称要沉淀却没给方案：视为无效，不写库。"""
    payload = json.dumps(
        {
            "should_sediment": True,
            "action": "create",
            "entry": {"canonical_question": "5G畅享套餐包含多少流量", "solution": ""},
            "reason": "抽取遗漏",
        },
        ensure_ascii=False,
    )
    events = _patch_subgraph(monkeypatch, _ScriptedSedimentModel(payload))

    result = _run_subgraph(_sub_input())

    assert result["persisted"] is False
    assert result["error"] == "empty_entry"
    assert _types(events)[-1] == "faq_sediment_error"
    assert _run(alist_faq())["total"] == 0


def test_subgraph_recall_failure_does_not_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """判重召回失败（模型缺失等）退化为无候选，不阻断沉淀。"""
    _patch_subgraph(monkeypatch, _ScriptedSedimentModel(_create_payload()))

    async def _boom(question: str, top_k: int = 5):
        raise RuntimeError("embedding unavailable")

    monkeypatch.setattr("congclaw.faq.nodes.asearch_similar", _boom)

    result = _run_subgraph(_sub_input())

    assert result["persisted"] is True


# ---------------------------------------------------------------------------
# 仓储：编号分配、过滤、审核流转与统计
# ---------------------------------------------------------------------------


def test_store_assigns_sequential_ids() -> None:
    first = _run(aupsert_faq({"canonical_question": "A问题内容是什么", "solution": "A方案"}))
    second = _run(aupsert_faq({"canonical_question": "B问题内容是什么", "solution": "B方案"}))

    assert first["faq_id"] == "FAQ0001"
    assert second["faq_id"] == "FAQ0002"


def test_store_list_filters_by_status_and_keyword() -> None:
    _run(
        aupsert_faq(
            {
                "canonical_question": "5G畅享套餐包含多少流量",
                "solution": "每月 100GB",
                "category": "咨询",
                "keywords": "5G 流量",
                "status": "draft",
            }
        )
    )
    _run(
        aupsert_faq(
            {
                "canonical_question": "宽带故障如何报修",
                "solution": "拨打 10000 或 App 报修",
                "category": "故障",
                "status": "published",
            }
        )
    )

    assert _run(alist_faq())["total"] == 2
    assert _run(alist_faq(status="draft"))["total"] == 1
    assert _run(alist_faq(category="故障"))["total"] == 1
    assert _run(alist_faq(keyword="流量"))["total"] == 1
    assert _run(alist_faq(keyword="不存在的词"))["total"] == 0


def test_store_update_status_validates_and_persists() -> None:
    _run(aupsert_faq({"canonical_question": "5G畅享套餐包含多少流量", "solution": "每月 100GB"}))

    updated = _run(aupdate_faq_status("FAQ0001", "published"))
    assert updated["status"] == "published"

    with pytest.raises(ValueError):
        _run(aupdate_faq_status("FAQ0001", "unknown-status"))

    assert _run(aupdate_faq_status("FAQ9999", "draft")) is None


def test_store_stats_counts_pending_and_published() -> None:
    _run(
        aupsert_faq(
            {"canonical_question": "问题一内容是什么", "solution": "方案一", "category": "咨询"}
        )
    )
    _run(
        aupsert_faq(
            {
                "canonical_question": "问题二内容是什么",
                "solution": "方案二",
                "category": "故障",
                "status": "published",
                "merge_count": 3,
            }
        )
    )

    stats = _run(afaq_stats())

    assert stats["total"] == 2
    assert stats["pending_review"] == 1
    assert stats["published"] == 1
    assert stats["by_category"] == {"咨询": 1, "故障": 1}
    assert stats["top_entries"][0]["faq_id"] == "FAQ0002"  # merge_count 降序


# ---------------------------------------------------------------------------
# 主图接线：条件边与挂载节点
# ---------------------------------------------------------------------------


def test_sediment_route_returns_end_for_non_sedimentable_turn() -> None:
    assert sediment_route(_rag_turn(intent_route="clarify", sources=[])) == "end"
    assert sediment_route(_rag_turn(final_answer="", sources=[])) == "end"


def test_sediment_route_returns_node_for_eligible_turn() -> None:
    assert sediment_route(_rag_turn()) == "faq_sediment"
    assert sediment_route(_agent_turn()) == "faq_sediment"


@pytest.mark.parametrize("value", ["0", "false", "NO", "off"])
def test_gate_switch_disables_sediment(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """CONG_FAQ_SEDIMENT 关闭时条件边直接到 END，不产生任何 LLM 调用。"""
    monkeypatch.setenv("CONG_FAQ_SEDIMENT", value)

    passed, reason = evaluate_sediment_gate(_rag_turn())

    assert (passed, reason) == (False, "sediment_disabled")
    assert sediment_route(_rag_turn()) == "end"
    assert sediment_route(_agent_turn()) == "end"


def test_gate_switch_defaults_to_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONG_FAQ_SEDIMENT", raising=False)
    assert evaluate_sediment_gate(_rag_turn()) == (True, "")


def test_sediment_saved_is_eval_key_event() -> None:
    """落库事件纳入评测关键事件，trace 摘要可见本轮沉淀条数。"""
    from congclaw.core.trace import EVAL_KEY_EVENT_TYPES

    assert "faq_sediment_saved" in EVAL_KEY_EVENT_TYPES


def test_entry_workflow_registers_faq_sediment_node() -> None:
    """四类业务分支之后统一汇入 faq_sediment，再终结于 END。"""
    nodes = set(build_entry_workflow().get_graph().nodes)
    assert "faq_sediment" in nodes


def test_faq_sediment_node_records_trace_and_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """主图节点：转发子图 custom 事件并回写状态字段。"""
    events = _patch_subgraph(monkeypatch, _ScriptedSedimentModel(_create_payload()))
    monkeypatch.setattr("congclaw.graph.nodes._get_writer", lambda: events.append)

    update = _run(faq_sediment_node(_rag_turn(session_id="sess-9", session_turn=2)))

    assert update["faq_sediment_action"] == "create"
    assert update["faq_entry_id"] == "FAQ0001"

    types = _types(events)
    assert types[0] == "faq_sediment_start"
    assert "faq_sediment_trace" in types
    assert types[-1] == "faq_sediment_finished"
    trace_event = next(e for e in events if e.get("type") == "faq_sediment_trace")
    assert trace_event["node_sequence"] == ["rule_gate", "match_existing", "extract", "persist"]


def test_faq_sediment_node_degrades_on_subgraph_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """子图整体异常时写 error 事件并把状态标记为 error，绝不向上抛。"""
    events: list[dict[str, Any]] = []
    monkeypatch.setattr("congclaw.graph.nodes._get_writer", lambda: events.append)

    def _boom():
        raise RuntimeError("subgraph exploded")

    monkeypatch.setattr("congclaw.graph.nodes.build_faq_sediment_subgraph", _boom)

    update = _run(faq_sediment_node(_rag_turn()))

    assert update["faq_sediment_action"] == "error"
    assert "subgraph exploded" in update["faq_sediment_reason"]
    error_event = next(e for e in events if e.get("type") == "faq_sediment_error")
    assert error_event["stage"] == "subgraph"


def test_faq_sediment_node_records_skip_for_gate_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = _patch_subgraph(monkeypatch)
    monkeypatch.setattr("congclaw.graph.nodes._get_writer", lambda: events.append)

    update = _run(faq_sediment_node(_rag_turn(intent_route="clarify", sources=[])))

    assert update["faq_sediment_action"] == "skip"
    assert update["faq_entry_id"] == ""
    assert update["faq_sediment_reason"] == "route_not_sedimentable"


# ---------------------------------------------------------------------------
# 端到端排查：真实编译主图跑一轮，沉淀是否真的落库并透传事件
# ---------------------------------------------------------------------------


def _stub_rag_hit_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """打桩 RAG 子图：命中 → 重排通过 → 生成带引用的回答（不加载本地向量模型）。"""

    class _FakeRagModel:
        def invoke(self, messages):
            return self.aok(messages)

        async def ainvoke(self, messages, **kwargs):
            return self.aok(messages)

        def aok(self, messages):  # noqa: ANN001
            system = str(messages[0].content)
            if "改写" in system:
                return AIMessage(content="5G畅享套餐包含多少流量")
            return AIMessage(content="5G 畅享套餐每月包含 100GB 流量[1]。")

    hit = {
        "child_id": "c1",
        "parent_id": "doc#P001",
        "doc_source": "资费说明.md",
        "position": 1,
        "text": "5G 畅享套餐含 100GB 流量。",
    }

    async def _bm25(query: str, top_k: int):
        return [dict(hit, bm25_rank=1, bm25_score=10.0)]

    async def _dense(query: str, top_k: int):
        return [dict(hit, dense_rank=1, dense_score=0.7)]

    async def _rerank(query: str, hits: list[dict[str, Any]]):
        return [dict(h, rerank_score=2.0, rerank_prob=0.9) for h in hits]

    async def _parents(ordered_ids: list[str]):
        return [
            {
                "parent_id": "doc#P001",
                "doc_source": "资费说明.md",
                "position": 1,
                "text": "5G 畅享套餐含 100GB 流量。",
                "child_ids": ["c1"],
                "matched_child_ids": ["c1"],
            }
        ]

    monkeypatch.setattr("congclaw.rag.nodes.bm25_search", _bm25)
    monkeypatch.setattr("congclaw.rag.nodes.dense_search", _dense)
    monkeypatch.setattr("congclaw.rag.nodes.rerank_hits", _rerank)
    monkeypatch.setattr("congclaw.rag.nodes.build_parent_evidence", _parents)
    monkeypatch.setattr("congclaw.rag.nodes.create_model", lambda: _FakeRagModel())


def _stub_intent(monkeypatch: pytest.MonkeyPatch, category: str, confidence: float = 0.95) -> None:
    payload = json.dumps(
        {"category": category, "confidence": confidence, "reason": "排查用例", "missing_slots": []},
        ensure_ascii=False,
    )

    class _IntentModel:
        def invoke(self, messages):
            return AIMessage(content=payload)

        async def ainvoke(self, messages, **kwargs):
            return AIMessage(content=payload)

    monkeypatch.setattr("congclaw.graph.nodes.create_model", lambda: _IntentModel())


def _run_entry_graph(task: str) -> tuple[dict[str, Any], list[str]]:
    """跑真实编译主图，返回「节点名 → update」映射与全部 custom 事件类型。"""

    async def _collect() -> tuple[dict[str, Any], list[str]]:
        updates: dict[str, Any] = {}
        custom_types: list[str] = []
        async for mode, chunk in build_entry_workflow().astream(
            {"task": task}, stream_mode=["updates", "custom"]
        ):
            if mode == "updates" and isinstance(chunk, dict):
                updates.update(chunk)
            elif mode == "custom" and isinstance(chunk, dict):
                custom_types.append(str(chunk.get("type") or ""))
        return updates, custom_types

    return _run(_collect())


def test_entry_graph_sediments_rag_turn_end_to_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """真实主图 + 真实 faq 子图：一轮 RAG 问答后 FAQ 条目落进 SQLite。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "e2e.db"))
    _stub_intent(monkeypatch, "rag_query")
    _stub_rag_hit_path(monkeypatch)
    monkeypatch.setattr(
        "congclaw.faq.nodes.create_model", lambda: _ScriptedSedimentModel(_create_payload())
    )

    async def _no_similar(question: str, top_k: int = 5):
        return []

    monkeypatch.setattr("congclaw.faq.nodes.asearch_similar", _no_similar)

    updates, custom_types = _run_entry_graph("5G畅享套餐包含多少流量")

    # 主链路未被沉淀链路破坏
    assert updates["intent_router"]["intent_route"] == "rag_answer"
    assert updates["rag_answer"]["final_answer"]
    # 沉淀真的发生了，并且状态回写到主图状态
    assert updates["faq_sediment"]["faq_sediment_action"] == "create"
    assert updates["faq_sediment"]["faq_entry_id"] == "FAQ0001"
    # 子图内部事件透传到主流（前端可据此展示沉淀进度）
    for expected in ("faq_gate", "faq_match", "faq_sediment_saved"):
        assert expected in custom_types
    assert custom_types[-1] == "faq_sediment_finished"

    saved = _run(aget_faq("FAQ0001"))
    assert saved is not None
    assert saved["status"] == "draft"
    assert saved["canonical_question"] == "5G畅享套餐包含多少流量"
    assert saved["source_route"] == "rag_answer"


def test_entry_graph_skips_sediment_for_clarify_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """clarify 分支不进沉淀节点：既不调 LLM 也不落库。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "e2e-clarify.db"))
    _stub_intent(monkeypatch, "clarify", confidence=0.7)

    def _unexpected():
        raise AssertionError("clarify 回合不应触发沉淀 LLM 调用")

    monkeypatch.setattr("congclaw.faq.nodes.create_model", _unexpected)

    updates, custom_types = _run_entry_graph("帮我办一下那个业务")

    assert updates["intent_router"]["intent_route"] == "clarify"
    assert "faq_sediment" not in updates
    assert not [t for t in custom_types if t.startswith("faq_")]
    assert _run(alist_faq())["total"] == 0


def test_entry_graph_respects_sediment_switch_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """总开关关闭时，即使本轮完全满足沉淀条件也不进节点、不调 LLM。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "e2e-off.db"))
    monkeypatch.setenv("CONG_FAQ_SEDIMENT", "0")
    _stub_intent(monkeypatch, "rag_query")
    _stub_rag_hit_path(monkeypatch)

    def _unexpected():
        raise AssertionError("开关关闭时不应触发沉淀 LLM 调用")

    monkeypatch.setattr("congclaw.faq.nodes.create_model", _unexpected)

    updates, custom_types = _run_entry_graph("5G畅享套餐包含多少流量")

    assert updates["intent_router"]["intent_route"] == "rag_answer"
    assert updates["rag_answer"]["final_answer"]
    assert "faq_sediment" not in updates
    assert not [t for t in custom_types if t.startswith("faq_")]
    assert _run(alist_faq())["total"] == 0


# ---------------------------------------------------------------------------
# RAG 回流：发布 → 知识库
# ---------------------------------------------------------------------------


class _ReflowSpy:
    """接管 ingest_file / _purge_source，避免测试真跑 BGE-M3 向量化与 Milvus。"""

    def __init__(self) -> None:
        self.ingested: list[tuple[str, str]] = []
        self.purged = 0

    async def ingest(self, path, *, source=None):  # noqa: ANN001, ANN003
        self.ingested.append((str(path), str(source or "")))
        return IngestStats(source=str(source or ""), path=str(path), parents=1, children=2)

    async def purge(self) -> None:
        self.purged += 1


def _patch_reflow(monkeypatch: pytest.MonkeyPatch) -> _ReflowSpy:
    spy = _ReflowSpy()
    monkeypatch.setattr("congclaw.faq.reflow.ingest_file", spy.ingest)
    monkeypatch.setattr("congclaw.faq.reflow._purge_source", spy.purge)
    return spy


def _seed_entry(question: str, status: str) -> str:
    entry = _run(
        aupsert_faq(
            {
                "canonical_question": question,
                "solution": f"{question}的解决办法：按资费规则办理即可。",
                "category": "咨询",
                "status": status,
            }
        )
    )
    return entry["faq_id"]


def test_render_markdown_keeps_one_entry_in_a_single_paragraph() -> None:
    """问句与答案必须同段——解析器按空行切段，跨段会被拆成两个父分片。"""
    text = render_faq_markdown(
        [
            {
                "canonical_question": "5G畅享套餐各档包含多少流量？",
                "question_variants": ["5G套餐多少流量", "5G畅享套餐流量是多少"],
                "solution": "共分三档：\n\n129元档 30GB；\n169元档 60GB。",
                "preconditions": ["仅限新入网用户"],
            }
        ]
    )

    assert "\n\n" not in text.strip()
    assert text.startswith("问：5G畅享套餐各档包含多少流量？")
    assert "同义问法：5G套餐多少流量；5G畅享套餐流量是多少" in text
    assert "答：共分三档：" in text
    assert "适用前提：仅限新入网用户" in text


def test_render_markdown_separates_entries_by_blank_line() -> None:
    text = render_faq_markdown(
        [
            {"canonical_question": "问题一", "solution": "答案一。"},
            {"canonical_question": "问题二", "solution": "答案二。"},
        ]
    )

    assert len(text.strip().split("\n\n")) == 2


def test_render_markdown_skips_incomplete_entries() -> None:
    """缺少问句或答案的条目不进知识库，否则会污染检索。"""
    assert render_faq_markdown([{"canonical_question": "只有问句", "solution": ""}]) == ""
    assert render_faq_markdown([{"canonical_question": "", "solution": "只有答案。"}]) == ""


def test_reflow_source_matches_file_name() -> None:
    """回流文档就在 knowledge/ 下，而目录全量入库按文件名推来源名。

    两者必须一致，否则同一份文件会写成两个 doc_source，检索时出现近似重复命中。
    """
    assert REFLOW_SOURCE == reflow_path().name


def test_render_markdown_ignores_malformed_list_fields() -> None:
    """字段类型异常（历史数据/手工改库）不能让回流整体失败。"""
    text = render_faq_markdown(
        [
            {
                "canonical_question": "字段异常的问题",
                "solution": "答案。",
                "question_variants": "不是列表",
                "preconditions": None,
            }
        ]
    )

    assert "问：字段异常的问题" in text
    assert "同义问法：" not in text
    assert "适用前提：" not in text


def test_reflow_ingests_only_published_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _patch_reflow(monkeypatch)
    _seed_entry("已发布的套餐问题", "published")
    _seed_entry("还没审的草稿问题", "draft")

    result = _run(areflow_published_faq())

    assert result["status"] == "ok"
    assert result["entries"] == 1
    assert result["children"] == 2
    assert result["cleared"] is False
    assert spy.purged == 0
    assert spy.ingested == [(result["path"], REFLOW_SOURCE)]
    document = Path(result["path"]).read_text(encoding="utf-8")
    assert "已发布的套餐问题" in document
    assert "还没审的草稿问题" not in document


def test_reflow_clears_knowledge_source_when_nothing_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已无发布条目：删掉回流文档并清空该来源，避免下架内容继续被召回。"""
    spy = _patch_reflow(monkeypatch)
    _seed_entry("仅草稿", "draft")
    path = reflow_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("上一次回流留下的旧内容", encoding="utf-8")

    result = _run(areflow_published_faq())

    assert result["cleared"] is True
    assert result["entries"] == 0
    assert spy.ingested == []
    assert spy.purged == 1
    assert not path.exists()


def test_api_publish_triggers_reflow(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _patch_reflow(monkeypatch)
    faq_id = _seed_entry("发布即回流的问题", "draft")

    response = TestClient(app).put(f"/api/v1/faq/{faq_id}/status", json={"status": "published"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "published"
    assert body["reflow"]["status"] == "ok"
    assert body["reflow"]["entries"] == 1
    assert len(spy.ingested) == 1


def test_api_approve_does_not_trigger_reflow(monkeypatch: pytest.MonkeyPatch) -> None:
    """仅「通过」还不算对外生效，不必付出一次全量向量化的代价。"""
    spy = _patch_reflow(monkeypatch)
    faq_id = _seed_entry("只通过不回流", "draft")

    body = (
        TestClient(app)
        .put(f"/api/v1/faq/{faq_id}/status", json={"status": "approved"})
        .json()
    )

    assert body["status"] == "approved"
    assert "reflow" not in body
    assert spy.ingested == []


def test_api_archive_published_triggers_reflow(monkeypatch: pytest.MonkeyPatch) -> None:
    """已发布条目下架后必须重跑回流，否则归档内容仍留在知识库里。"""
    spy = _patch_reflow(monkeypatch)
    faq_id = _seed_entry("先发布后归档", "published")

    body = (
        TestClient(app)
        .put(f"/api/v1/faq/{faq_id}/status", json={"status": "archived"})
        .json()
    )

    assert body["status"] == "archived"
    assert body["reflow"]["cleared"] is True
    assert spy.purged == 1


def test_api_repeated_publish_does_not_reflow_again(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _patch_reflow(monkeypatch)
    faq_id = _seed_entry("重复发布", "published")

    body = (
        TestClient(app)
        .put(f"/api/v1/faq/{faq_id}/status", json={"status": "published"})
        .json()
    )

    assert "reflow" not in body
    assert spy.ingested == []


def test_api_publish_survives_reflow_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """回流炸了也不能回滚审核——状态已提交，只把错误挂在响应里。"""
    faq_id = _seed_entry("回流失败但发布成功", "draft")

    async def _boom() -> dict:
        raise RuntimeError("milvus busy")

    monkeypatch.setattr("congclaw.api.routes.faq.areflow_published_faq", _boom)

    response = TestClient(app).put(f"/api/v1/faq/{faq_id}/status", json={"status": "published"})

    assert response.status_code == 200
    body = response.json()
    assert body["reflow"]["status"] == "error"
    assert "milvus busy" in body["reflow"]["error"]
    assert _run(aget_faq(faq_id))["status"] == "published"


def test_api_manual_reflow_ingests_published_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """状态变更之外的兜底入口：不改状态也能重跑回流。"""
    spy = _patch_reflow(monkeypatch)
    _seed_entry("已发布但副本过期的问题", "published")

    response = TestClient(app).post("/api/v1/faq/reflow")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["entries"] == 1
    assert len(spy.ingested) == 1


def test_api_manual_reflow_reports_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """手动回流是用户显式动作，失败必须报错，不能像审核流转那样静默降级。"""
    _seed_entry("回流会失败的问题", "published")

    async def _boom() -> dict:
        raise RuntimeError("milvus busy")

    monkeypatch.setattr("congclaw.api.routes.faq.areflow_published_faq", _boom)

    response = TestClient(app).post("/api/v1/faq/reflow")

    assert response.status_code == 500
    assert "milvus busy" in response.json()["detail"]


def test_api_faq_routes_are_registered() -> None:
    """路由必须挂在 /api/v1/faq 前缀下，且审核接口需要状态请求体。"""
    paths = TestClient(app).get("/openapi.json").json()["paths"]

    assert "/api/v1/faq" in paths
    assert "/api/v1/faq/stats" in paths
    assert "/api/v1/faq/{faq_id}" in paths
    assert "put" in paths["/api/v1/faq/{faq_id}/status"]
    assert "post" in paths["/api/v1/faq/reflow"]

