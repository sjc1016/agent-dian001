"""阶段 4 核心验收：Agent 深度推理子图（approval_entry/think/act/reflect/finalize）。

四条验收门中与 Agent 相关的三条在此闭环：
①查余额单工具链路；②宽带报修多工具并行链路；③套餐变更人工确认卡片两回合；
另覆盖反思重试上限兜底、deny 审批策略、取消确认单、custom 轨迹事件。

LLM 全部打桩（mokioclaw.agent.nodes.create_model），业务 Skill 与 SQLite 仓储
使用真实实现（独立 tmp 库 + 真实内置 catalog 注册中心）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import aiosqlite
import pytest
from langchain_core.messages import AIMessage

from mokioclaw.agent.nodes import detect_approval_resolution
from mokioclaw.agent.workflow import build_agent_subgraph
from mokioclaw.db.engine import resolve_db_path
from mokioclaw.prompts.agent import (
    AGENT_FALLBACK_REPLY,
    AGENT_REFLECT_PROMPT,
    AGENT_RESPOND_PROMPT,
    APPROVAL_CANCELLED_REPLY,
)
from mokioclaw.skills.business_store import get_pending_approval, save_pending_approval
from mokioclaw.skills.registry import BUILTIN_CATALOG_DIR, SkillRegistry


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "agent-subgraph-test.db"))
    monkeypatch.setenv("SKILL_WATCH", "0")
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
        "_registry": _env["registry"],
    }
    state.update(overrides)
    return state


def _stub_model(monkeypatch, model: FakeAgentModel) -> None:
    monkeypatch.setattr("mokioclaw.agent.nodes.create_model", lambda: model)


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
    assert confirmation["skill_name"] == "change_package"
    assert confirmation["args"] == {"target_package": "P199"}
    assert "请确认" in turn1["answer"]
    assert turn1["tool_results"] == []  # 未确认前绝不执行写操作

    pending = _run(get_pending_approval(_env["workspace"]))
    assert pending is not None
    assert pending["skill_name"] == "change_package"
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
            skill_name="change_package",
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


def test_think_filters_unknown_skill_and_direct_answers(monkeypatch, _env) -> None:
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
                tool_calls=[{"id": "c1", "name": "not_registered_skill", "args": {"x": 1}}],
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
    assert events.count("skill_call") == 1
    assert events.count("skill_result") == 1
    assert "agent_reflect" in events
    assert "agent_answer" in events


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
