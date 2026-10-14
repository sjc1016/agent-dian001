"""阶段 4 核心验收：Agent 深度推理子图（approval_entry/think/act/reflect/finalize）。

四条验收门中与 Agent 相关的三条在此闭环：
①查余额单工具链路；②宽带报修多工具并行链路；③套餐变更人工确认卡片两回合；
另覆盖反思重试上限兜底、deny 审批策略、取消确认单、custom 轨迹事件。

LLM 全部打桩（congclaw.agent.nodes.create_model），业务原子工具与 SQLite 仓储
使用真实实现（独立 tmp 库 + 真实内置 catalog 注册中心）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import aiosqlite
import pytest
from langchain_core.messages import AIMessage

from congclaw.agent.nodes import detect_approval_resolution
from congclaw.agent.workflow import build_agent_subgraph
from congclaw.db.engine import resolve_db_path
from congclaw.domain.business_store import get_pending_approval, save_pending_approval
from congclaw.prompts.agent import (
    AGENT_FALLBACK_REPLY,
    AGENT_REFLECT_PROMPT,
    AGENT_RESPOND_PROMPT,
    APPROVAL_CANCELLED_REPLY,
)
from congclaw.skills.registry import BUILTIN_CATALOG_DIR, SkillRegistry


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "agent-subgraph-test.db"))
    monkeypatch.setenv("HOTLOAD_WATCH", "0")
    registry = SkillRegistry(
        builtin_dir=BUILTIN_CATALOG_DIR, extra_dir=tmp_path / "ext", watch=False
    )
    workspace = str(tmp_path / "ws")
    return {"registry": registry, "workspace": workspace}


def _run(coro):
    return asyncio.run(coro)


class FakeAgentModel:
    """按系统提示词分流 think/reflect/finalize 三类调用的模型桩。"""

    def __init__(self, think_queue=None, *, reflect: str = "pass", final_text: str = "这是最终答复。"):
        self._think_queue = list(think_queue or [])
        self._think_index = 0
        self._reflect = reflect
        self._final = final_text
        self.bound_tools: list | None = None

    def bind_tools(self, tools):
        self.bound_tools = tools
        return self

    async def ainvoke(self, messages, **kwargs):
        system = str(messages[0].content) if messages else ""
        if system.startswith(AGENT_REFLECT_PROMPT[:16]):
            return AIMessage(
                content=json.dumps(
                    {
                        "decision": self._reflect,
                        "reason": "stub reflect",
                        "hint": "修正参数" if self._reflect == "retry" else "",
                        "checks": [
                            {"name": "stub", "passed": self._reflect == "pass", "detail": "ok"}
                        ],
                    },
                    ensure_ascii=False,
                )
            )
        if system.startswith(AGENT_RESPOND_PROMPT[:16]):
            return AIMessage(content=self._final)
        # think：按队列依次返回（纯文本=直接回复；list=tool_calls）
        item = self._think_queue[min(self._think_index, len(self._think_queue) - 1)]
        self._think_index += 1
        if isinstance(item, str):
            return AIMessage(content=item)
        return AIMessage(content="", tool_calls=item)


def _base_state(_env, **overrides):
    state = {
        "query": "查话费余额",
        "phone": "13800138000",
        "workspace": _env["workspace"],
        "approval_mode": "inline",
        "attempts": 0,
        "max_attempts": 3,
        "_skills": _env["registry"],
    }
    state.update(overrides)
    return state


def _stub_model(monkeypatch, model: FakeAgentModel) -> None:
    monkeypatch.setattr("congclaw.agent.nodes.create_model", lambda: model)


# ---------------------------------------------------------------------------
# 验收门①：单工具链路（查余额）
# ---------------------------------------------------------------------------


def test_single_tool_query_balance_flow(monkeypatch, _env) -> None:
    model = FakeAgentModel(
        think_queue=[[{"id": "c1", "name": "query_balance", "args": {}}]],
        final_text="张伟先生您好，您当前可用余额为 86.50 元。",
    )
    _stub_model(monkeypatch, model)

    final = _run(build_agent_subgraph().ainvoke(_base_state(_env)))

    assert final["verify_decision"] == "pass"
    assert final["answer"] == "张伟先生您好，您当前可用余额为 86.50 元。"
    assert final["chat_response"] == final["answer"] == final["final_answer"]
    result = final["tool_results"][0]
    assert result["call_id"] == "c1"
    assert result["name"] == "query_balance"
    assert result["ok"] is True
    assert result["data"]["balance"] == 86.50
    assert result["data"]["owner_name"] == "张伟"
    # bind_tools 拿到了注册中心的实时工具清单（热插拔感知）
    assert model.bound_tools is not None
    assert {tool["function"]["name"] for tool in model.bound_tools} >= {"query_balance"}


# ---------------------------------------------------------------------------
# 验收门②：宽带报修多工具并行链路（建单 + 查状态）
# ---------------------------------------------------------------------------


def test_multi_tool_report_fault_and_query_status(monkeypatch, _env) -> None:
    calls = [
        {
            "id": "c1",
            "name": "report_fault",
            "args": {
                "fault_type": "宽带故障",
                "description": "昨晚开始光猫亮红灯，全屋无法上网",
                "address": "杭州市西湖区文三路100号3栋502",
            },
        },
        {"id": "c2", "name": "query_fault_status", "args": {}},
    ]
    model = FakeAgentModel(
        think_queue=[calls], final_text="您的宽带报修工单已受理，可凭工单号查询进度。"
    )
    _stub_model(monkeypatch, model)

    state = _base_state(_env, query="我家宽带断了，帮我报修并查一下进度")
    final = _run(build_agent_subgraph().ainvoke(state))

    assert final["verify_decision"] == "pass"
    assert [r["call_id"] for r in final["tool_results"]] == ["c1", "c2"]
    created, queried = final["tool_results"]
    assert created["ok"] is True
    assert created["data"]["ticket_id"].startswith("FT")
    assert created["data"]["status"] == "已受理"
    assert queried["ok"] is True
    assert queried["data"]["lookup"] == "by_phone"
    assert queried["data"]["count"] >= 1
    # 两工具并行调度（asyncio.gather）：查状态与建单并发，不保证读到本轮新单，
    # 但号码维度的历史种子单必然可见；建单结果自身已携带工单号与受理状态
    ticket_ids = [item["ticket_id"] for item in queried["data"]["tickets"]]
    assert "FT202609280001" in ticket_ids
    assert created["data"]["ticket_id"].startswith("FT")
    assert "报修工单已受理" in final["answer"]


# ---------------------------------------------------------------------------
# P4-15：反思 retry 受 max_attempts 限制，超限走静态兜底
# ---------------------------------------------------------------------------


def test_reflect_retry_respects_max_attempts_and_falls_back(monkeypatch, _env) -> None:
    calls = [[{"id": "c1", "name": "query_balance", "args": {}}]]
    # 每次思考都调余额；反思始终要求 retry
    model = FakeAgentModel(think_queue=calls * 2, reflect="retry")
    _stub_model(monkeypatch, model)

    state = _base_state(_env, max_attempts=2)
    final = _run(build_agent_subgraph().ainvoke(state))

    assert model._think_index == 2  # think 确实被重试了一轮
    assert final["verify_decision"] == "fallback"
    assert "attempts_exceeded" in final["verify_reason"]
    assert final["answer"] == AGENT_FALLBACK_REPLY
    assert final["fallback_reason"]


# ---------------------------------------------------------------------------
# 验收门③：套餐变更人工确认卡片两回合
# ---------------------------------------------------------------------------


async def _approval_status(approval_id: str) -> str | None:
    async with aiosqlite.connect(resolve_db_path()) as connection:
        cursor = await connection.execute(
            "SELECT status FROM pending_approval WHERE approval_id = ?", (approval_id,)
        )
        row = await cursor.fetchone()
    return row[0] if row else None


def test_change_package_confirmation_two_turns(monkeypatch, _env) -> None:
    # ---- 第一轮：inline 模式只出确认卡片，不执行变更，pending 落库 ----
    model_turn1 = FakeAgentModel(
        think_queue=[
            [{"id": "c1", "name": "change_package", "args": {"target_package": "P199"}}]
        ]
    )
    _stub_model(monkeypatch, model_turn1)

    state = _base_state(_env, query="帮我把套餐换成199元档")
    turn1 = _run(build_agent_subgraph().ainvoke(state))

    confirmation = turn1["confirmation_request"]
    assert confirmation["tool_name"] == "change_package"
    assert confirmation["args"] == {"target_package": "P199"}
    assert "请确认" in turn1["answer"]
    assert turn1["tool_results"] == []  # 未确认前绝不执行写操作

    pending = _run(get_pending_approval(_env["workspace"]))
    assert pending is not None
    assert pending["tool_name"] == "change_package"
    assert pending["status"] == "pending"
    approval_id = pending["approval_id"]

    # ---- 第二轮：用户确认 → approval_entry 恢复执行 ----
    model_turn2 = FakeAgentModel(reflect="pass", final_text="已为您办理 5G畅享199元档，次月1日生效。")
    _stub_model(monkeypatch, model_turn2)

    state2 = _base_state(
        _env,
        query="确认办理",
        pending_approval=pending,
        approval_resolution="confirmed",
    )
    turn2 = _run(build_agent_subgraph().ainvoke(state2))

    result = turn2["tool_results"][0]
    assert result["name"] == "change_package"
    assert result["ok"] is True
    assert result["data"]["new_package_id"] == "P199"
    assert result["data"]["old_package_id"] == "P129"
    assert "次月1日生效" in turn2["answer"]

    # 确认单状态回写 executed，且无残留未决单
    assert _run(_approval_status(approval_id)) == "executed"
    assert _run(get_pending_approval(_env["workspace"])) is None


def test_approval_deny_mode_rejects_write_and_falls_back(monkeypatch, _env) -> None:
    model = FakeAgentModel(
        think_queue=[
            [{"id": "c1", "name": "change_package", "args": {"target_package": "P299"}}]
        ]
    )
    _stub_model(monkeypatch, model)

    state = _base_state(_env, approval_mode="deny", query="帮我换成299档")
    final = _run(build_agent_subgraph().ainvoke(state))

    result = final["tool_results"][0]
    assert result["ok"] is False
    assert result["error_code"] == "approval_denied"  # 致命错误，反思节点不重试
    assert final["verify_decision"] == "fallback"
    assert final["answer"] == AGENT_FALLBACK_REPLY
    # deny 模式不留确认单
    assert _run(get_pending_approval(_env["workspace"])) is None


def test_approval_cancelled_turn_cancels_pending(monkeypatch, _env) -> None:
    saved = _run(
        save_pending_approval(
            _env["workspace"],
            approval_id="approval-test-1",
            tool_name="change_package",
            args={"target_package": "P199"},
            summary="确认卡片文案",
        )
    )
    model = FakeAgentModel()  # 取消路径不调用任何 LLM
    _stub_model(monkeypatch, model)

    state = _base_state(
        _env,
        query="算了不用了",
        pending_approval=saved,
        approval_resolution="cancelled",
    )
    final = _run(build_agent_subgraph().ainvoke(state))

    assert final["answer"] == APPROVAL_CANCELLED_REPLY
    assert _run(_approval_status("approval-test-1")) == "cancelled"
    assert _run(get_pending_approval(_env["workspace"])) is None


# ---------------------------------------------------------------------------
# think 过滤未注册工具名 + custom 轨迹事件
# ---------------------------------------------------------------------------


def test_think_filters_unknown_tool_and_direct_answers(monkeypatch, _env) -> None:
    # 模型同时返回文本与一个未注册工具名：未注册调用被过滤，本轮按直接回复结束
    class _Model(FakeAgentModel):
        async def ainvoke(self, messages, **kwargs):
            system = str(messages[0].content) if messages else ""
            if system.startswith(AGENT_REFLECT_PROMPT[:16]):
                return AIMessage(content="{}")
            if system.startswith(AGENT_RESPOND_PROMPT[:16]):
                return AIMessage(content=self._final)
            self._think_index += 1
            return AIMessage(
                content="抱歉，该能力暂未开通，请问还有其他可以帮您的吗？",
                tool_calls=[{"id": "c1", "name": "not_registered_tool", "args": {"x": 1}}],
            )

    _stub_model(monkeypatch, _Model())

    final = _run(build_agent_subgraph().ainvoke(_base_state(_env)))

    assert final.get("tool_results", []) == []
    assert "暂未开通" in final["answer"]


def test_custom_trace_events_emitted(monkeypatch, _env) -> None:
    model = FakeAgentModel(
        think_queue=[[{"id": "c1", "name": "query_balance", "args": {}}]],
        final_text="余额查询完成。",
    )
    _stub_model(monkeypatch, model)

    async def collect():
        events = []
        async for event in build_agent_subgraph().astream(
            _base_state(_env), stream_mode="custom"
        ):
            events.append(event["type"])
        return events

    events = _run(collect())

    assert "agent_thinking" in events
    assert events.count("tool_call") == 1
    assert events.count("tool_result") == 1
    assert "agent_reflect" in events
    assert "agent_answer" in events


# ---------------------------------------------------------------------------
# 编排驱动：intent_router 选定编排型 Skill 后由代码控制流执行（跳过 think）
# ---------------------------------------------------------------------------


def test_skill_orchestration_runs_without_think_or_reflect(monkeypatch, _env) -> None:
    """全步成功：直接 finalize，think / reflect 一次都不调用（长尾被消除）。"""
    model = FakeAgentModel(final_text="您的账户可用余额为 86.50 元。")
    _stub_model(monkeypatch, model)

    state = _base_state(_env, query="查话费余额", skill_name="balance_query", skill_slots={})
    final = _run(build_agent_subgraph().ainvoke(state))

    assert final["orchestration"] == "skill"
    assert final["skill_result"]["skill"] == "balance_query"
    assert final["skill_result"]["ok"] is True
    assert final["answer"] == "您的账户可用余额为 86.50 元。"
    result = final["tool_results"][0]
    assert result["ok"] is True
    assert result["data"]["account"]["balance"] == 86.50
    assert model._think_index == 0  # 未经过 think
    assert not final.get("verify_decision")  # 未经过 reflect


def test_skill_orchestration_trace_events(monkeypatch, _env) -> None:
    """编排链路的轨迹事件：技能事件 + 原子工具 call/result 配对，不含 think/reflect。"""
    _stub_model(monkeypatch, FakeAgentModel(final_text="余额查询完成。"))

    async def collect():
        events = []
        async for event in build_agent_subgraph().astream(
            _base_state(_env, skill_name="balance_query", skill_slots={}),
            stream_mode="custom",
        ):
            events.append(event)
        return events

    events = _run(collect())
    types = [event["type"] for event in events]
    assert "agent_skill_start" in types
    assert "agent_skill" in types
    assert types.count("tool_call") == 1
    assert types.count("tool_result") == 1
    assert "agent_answer" in types
    assert "agent_thinking" not in types
    assert "agent_reflect" not in types
    # 每步工具事件带 operator 标注，便于评测按编排归属统计
    call_event = next(event for event in events if event["type"] == "tool_call")
    assert call_event["operator"] == "balance_query"


def test_skill_orchestration_failure_falls_back_without_replanning(monkeypatch, _env) -> None:
    """编排失败（致命业务错误）：反思规则判定直接兜底，不触发 think 重规划。"""
    model = FakeAgentModel(reflect="retry")
    _stub_model(monkeypatch, model)

    state = _base_state(
        _env,
        query="查余额",
        skill_name="balance_query",
        skill_slots={"phone": "13900000000"},
    )
    final = _run(build_agent_subgraph().ainvoke(state))

    assert final["orchestration"] == "skill"
    assert final["skill_result"]["ok"] is False
    assert final["tool_results"][0]["error_code"] == "account_not_found"
    assert final["verify_decision"] == "fallback"
    assert final["answer"] == AGENT_FALLBACK_REPLY
    assert model._think_index == 0


def test_skill_package_change_raises_confirmation_card(monkeypatch, _env) -> None:
    """编排命中需确认写操作：不执行变更、落 pending 确认单、本轮不给工具结果。"""
    _stub_model(monkeypatch, FakeAgentModel())

    state = _base_state(
        _env,
        query="帮我把套餐改成199元档",
        skill_name="package_change",
        skill_slots={"target_package": "P199"},
    )
    final = _run(build_agent_subgraph().ainvoke(state))

    confirmation = final["confirmation_request"]
    assert confirmation["tool_name"] == "change_package"
    assert confirmation["args"] == {"target_package": "P199"}
    assert "请确认" in final["answer"]
    assert final["tool_results"] == []  # 未确认前绝不执行写操作
    pending = _run(get_pending_approval(_env["workspace"]))
    assert pending is not None
    assert pending["status"] == "pending"


def test_skill_missing_falls_back_to_think(monkeypatch, _env) -> None:
    """编排技能缺失（未注册/被热卸载）：回退 Agent 自主规划，保底可用。"""
    model = FakeAgentModel(
        think_queue=[[{"id": "c1", "name": "query_balance", "args": {}}]],
        final_text="已按 Agent 自主规划完成查询。",
    )
    _stub_model(monkeypatch, model)

    state = _base_state(_env, skill_name="not_a_skill", skill_slots={})
    final = _run(build_agent_subgraph().ainvoke(state))

    assert model._think_index == 1  # 确实回退到 think
    assert final["orchestration"] == "agent"
    assert final["tool_results"][0]["name"] == "query_balance"


def test_think_rejects_orchestration_skill_names(monkeypatch, _env) -> None:
    """编排型 Skill 不允许被 LLM 当工具调用：think 过滤后按直接回复结束。"""
    model = FakeAgentModel(
        think_queue=[
            AIMessage(content="", tool_calls=[{"id": "c1", "name": "balance_query", "args": {}}])
        ],
        final_text="请问您是要查询哪个号码的余额？",
    )
    _stub_model(monkeypatch, model)

    final = _run(build_agent_subgraph().ainvoke(_base_state(_env)))

    assert final.get("tool_results", []) == []
    assert final["answer"] == "请问您是要查询哪个号码的余额？"


def test_package_query_skill_coerces_natural_language_scope(monkeypatch, _env) -> None:
    """scope 被抽成半句自然语言时归一为 current，而不是校验失败作废整轮编排。"""
    from congclaw.skills.catalog.package_query import normalize_scope

    assert normalize_scope("当前套餐及流量余量") == "current"
    assert normalize_scope("") == "current"
    assert normalize_scope("我不确定") == "current"
    assert normalize_scope("catalog") == "catalog"
    assert normalize_scope("你们有哪些套餐可以办理") == "catalog"

    _stub_model(monkeypatch, FakeAgentModel(final_text="您当前使用的是 5G畅享199元档。"))
    state = _base_state(
        _env,
        query="我现在用的是什么套餐？",
        skill_name="package_query",
        skill_slots={"scope": "当前套餐及流量余量"},
    )
    final = _run(build_agent_subgraph().ainvoke(state))

    assert final["skill_result"]["ok"] is True
    assert [step["name"] for step in final["skill_steps"]] == ["query_package"]
    assert final["tool_results"][0]["ok"] is True


# ---------------------------------------------------------------------------
# P4-16：确认/取消关键词识别（规则法，不耗 LLM）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("确认", "confirmed"),
        ("好的，办理吧", "confirmed"),
        ("嗯", "confirmed"),
        ("yes", "confirmed"),
        ("算了，不用了", "cancelled"),
        ("取消办理", "cancelled"),
        ("我想先查一下我的流量还有多少", "unknown"),
        ("", "unknown"),
    ],
)
def test_detect_approval_resolution(text: str, expected: str) -> None:
    assert detect_approval_resolution(text) == expected
