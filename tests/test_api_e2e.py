"""阶段 4 端到端验收：FastAPI HTTP 全链路（无真实 LLM，模型全部打桩）。

覆盖四条验收门：
①POST /api/v1/chat 查余额单工具链路；
②宽带报修多工具链路；
③套餐变更确认卡片两回合（含 intent_router 的 pending 短路）；
④POST /api/v1/tools 运行中新增 Tool，不重启即在随后对话中被发现调用。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from congclaw.prompts.agent import AGENT_REFLECT_PROMPT, AGENT_RESPOND_PROMPT


_ECHO_SKILL_SOURCE = '''
from congclaw.skills.base import Skill
from congclaw.tools.base import ToolParameter


class EchoSkill(Skill):
    name = "echo_demo"
    description = "编排演示：回显输入文本"
    intent_hint = "用户明确要求演示编排技能时"
    parameters = [ToolParameter(name="text", description="text", required=True)]

    async def run(self, context, **kwargs):
        return self._finished([], {"echo": kwargs.get("text")})


SKILL = EchoSkill()
'''

_ECHO_TOOL_SOURCE = '''
from congclaw.tools.base import Tool, ToolParameter


class EchoTool(Tool):
    name = "echo_demo"
    description = "echo back the input text"
    parameters = [ToolParameter(name="text", description="text", required=True)]

    async def run(self, context, **kwargs):
        return {"echo": kwargs.get("text")}


TOOL = EchoTool()
'''


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "e2e.db"))
    monkeypatch.setenv("SKILLS_EXTRA_DIR", str(tmp_path / "skills"))
    monkeypatch.setenv("TOOLS_EXTRA_DIR", str(tmp_path / "tools"))
    monkeypatch.setenv("HOTLOAD_WATCH", "0")
    monkeypatch.setenv("RAG_PREWARM", "0")

    from congclaw.api.main import app

    with TestClient(app) as test_client:
        yield test_client, tmp_path


def _parse_sse(text: str) -> list[dict]:
    frames = []
    for line in text.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            frames.append(json.loads(line[6:]))
    return frames


def _custom_types(frames: list[dict]) -> list[str]:
    return [
        frame["event"]["type"]
        for frame in frames
        if frame.get("type") == "custom_event" and isinstance(frame.get("event"), dict)
    ]


def _stub_intent(monkeypatch, category: str = "agent_service") -> None:
    payload = json.dumps(
        {"category": category, "confidence": 0.95, "reason": "e2e", "missing_slots": []},
        ensure_ascii=False,
    )

    class FakeIntentModel:
        def invoke(self, messages, **kwargs):
            return AIMessage(content=payload)

        async def ainvoke(self, messages, **kwargs):
            return AIMessage(content=payload)

    monkeypatch.setattr("congclaw.graph.nodes.create_model", lambda: FakeIntentModel())


class FakeAgentModel:
    """think 队列 + reflect 固定决策 + finalize 固定话术。"""

    def __init__(self, think_queue=None, *, reflect: str = "pass", final_text: str = "办理完成。"):
        self._queue = list(think_queue or [])
        self._index = 0
        self._reflect = reflect
        self._final = final_text

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages, **kwargs):
        system = str(messages[0].content) if messages else ""
        if system.startswith(AGENT_REFLECT_PROMPT[:16]):
            return AIMessage(
                content=json.dumps(
                    {"decision": self._reflect, "reason": "e2e", "checks": []},
                    ensure_ascii=False,
                )
            )
        if system.startswith(AGENT_RESPOND_PROMPT[:16]):
            return AIMessage(content=self._final)
        item = self._queue[min(self._index, len(self._queue) - 1)]
        self._index += 1
        return AIMessage(content="", tool_calls=item)


def _stub_agent(monkeypatch, model: FakeAgentModel) -> None:
    monkeypatch.setattr("congclaw.agent.nodes.create_model", lambda: model)


def _chat(client: TestClient, message: str, workspace: str) -> list[dict]:
    response = client.post(
        "/api/v1/chat",
        json={"message": message, "workspace": workspace},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    return _parse_sse(response.text)


# ---------------------------------------------------------------------------
# 能力管理接口（编排技能 /api/v1/skills + 原子工具 /api/v1/tools）
# ---------------------------------------------------------------------------


def test_skills_management_api_crud(client) -> None:
    """技能接口只收编排技能：不进 LLM 工具表，故元信息里没有 category/workflow 概念。"""
    test_client, _ = client

    resp = test_client.get("/api/v1/skills")
    assert resp.status_code == 200
    body = resp.json()
    builtin_names = {item["name"] for item in body["skills"]}
    # 内置编排型 Skill：不进 bind_tools，由 intent_router 挑选后确定性执行
    assert builtin_names == {
        "balance_query",
        "fault_repair",
        "package_change",
        "package_query",
    }
    assert body["total"] == 4
    assert body["builtin_count"] == 4
    assert body["external_count"] == 0
    assert all("intent_hint" in item for item in body["skills"])

    resp = test_client.post("/api/v1/skills", json={"name": "echo_demo", "content": _ECHO_SKILL_SOURCE})
    assert resp.status_code == 201
    assert resp.json()["name"] == "echo_demo"

    names = {item["name"] for item in test_client.get("/api/v1/skills").json()["skills"]}
    assert "echo_demo" in names

    # 非法文件名 → 400
    resp = test_client.post("/api/v1/skills", json={"name": "bad-name", "content": "x=1"})
    assert resp.status_code == 400

    # 类型守门：工具源码（暴露 TOOL）不得混装进技能目录
    resp = test_client.post("/api/v1/skills", json={"name": "echo_tool", "content": _ECHO_TOOL_SOURCE})
    assert resp.status_code == 400

    # 删除 → 200；再次删除 → 404
    assert test_client.delete("/api/v1/skills/echo_demo").status_code == 200
    names = {item["name"] for item in test_client.get("/api/v1/skills").json()["skills"]}
    assert "echo_demo" not in names
    assert test_client.delete("/api/v1/skills/echo_demo").status_code == 404


def test_tools_management_api_crud(client) -> None:
    """工具接口列出全部原子工具并附带 bind_tools 用的 OpenAI 描述。"""
    test_client, _ = client

    resp = test_client.get("/api/v1/tools")
    assert resp.status_code == 200
    body = resp.json()
    assert {item["name"] for item in body["tools"]} == {
        "change_package",
        "list_packages",
        "query_balance",
        "query_fault_status",
        "query_package",
        "report_fault",
    }
    assert body["total"] == 6
    assert body["builtin_count"] == 6
    assert body["external_count"] == 0
    assert {schema["function"]["name"] for schema in body["openai_tools"]} == {
        item["name"] for item in body["tools"]
    }

    resp = test_client.post("/api/v1/tools", json={"name": "echo_demo", "content": _ECHO_TOOL_SOURCE})
    assert resp.status_code == 201
    assert resp.json()["name"] == "echo_demo"

    after = test_client.get("/api/v1/tools").json()
    assert after["external_count"] == 1
    assert "echo_demo" in {schema["function"]["name"] for schema in after["openai_tools"]}
    # 技能接口不受影响（两类能力注册中心互相隔离）
    assert "echo_demo" not in {
        item["name"] for item in test_client.get("/api/v1/skills").json()["skills"]
    }

    # 非法文件名 → 400
    assert test_client.post("/api/v1/tools", json={"name": "bad-name", "content": "x=1"}).status_code == 400

    # 类型守门：技能源码（暴露 SKILL）不得混装进工具目录
    assert (
        test_client.post("/api/v1/tools", json={"name": "echo_skill", "content": _ECHO_SKILL_SOURCE}).status_code
        == 400
    )

    assert test_client.delete("/api/v1/tools/echo_demo").status_code == 200
    assert test_client.delete("/api/v1/tools/echo_demo").status_code == 404


# ---------------------------------------------------------------------------
# 验收门①：查余额单工具链路
# ---------------------------------------------------------------------------


def test_chat_query_balance_end_to_end(client, monkeypatch) -> None:
    test_client, tmp_path = client
    _stub_intent(monkeypatch)
    _stub_agent(
        monkeypatch,
        FakeAgentModel(
            think_queue=[[{"id": "c1", "name": "query_balance", "args": {}}]],
            final_text="张伟先生，您当前可用余额为 86.50 元。",
        ),
    )

    frames = _chat(test_client, "查一下我话费余额", str(tmp_path / "ws-balance"))
    types = _custom_types(frames)

    assert "agent_thinking" in types
    assert types.count("tool_call") == 1
    assert types.count("tool_result") == 1
    assert "agent_answer" in types
    assert response_text_contains(frames, "86.50")


# ---------------------------------------------------------------------------
# 验收门②：宽带报修多工具链路
# ---------------------------------------------------------------------------


def test_chat_report_fault_multi_tool_end_to_end(client, monkeypatch) -> None:
    test_client, tmp_path = client
    _stub_intent(monkeypatch)
    calls = [
        {
            "id": "c1",
            "name": "report_fault",
            "args": {
                "fault_type": "宽带故障",
                "description": "光猫亮红灯，全屋断网",
                "address": "西湖区文三路100号",
            },
        },
        {"id": "c2", "name": "query_fault_status", "args": {}},
    ]
    _stub_agent(
        monkeypatch,
        FakeAgentModel(think_queue=[calls], final_text="您的宽带报修工单已受理。"),
    )

    frames = _chat(test_client, "我家宽带断了帮我报修", str(tmp_path / "ws-fault"))
    types = _custom_types(frames)

    assert types.count("tool_call") == 2
    assert "FT" in json.dumps(frames, ensure_ascii=False)
    assert response_text_contains(frames, "报修工单已受理")


# ---------------------------------------------------------------------------
# 验收门④：运行中新增 Tool，不重启即在对话中被发现调用
# ---------------------------------------------------------------------------


def test_new_tool_discovered_without_restart(client, monkeypatch) -> None:
    test_client, tmp_path = client

    # 服务运行中通过管理接口投放新原子工具（思考节点 bind_tools 当轮即取到）
    resp = test_client.post(
        "/api/v1/tools", json={"name": "echo_demo", "content": _ECHO_TOOL_SOURCE}
    )
    assert resp.status_code == 201

    _stub_intent(monkeypatch)
    _stub_agent(
        monkeypatch,
        FakeAgentModel(
            think_queue=[
                [{"id": "c1", "name": "echo_demo", "args": {"text": "热插拔链路OK"}}]
            ],
            final_text="回声工具返回：热插拔链路OK",
        ),
    )

    frames = _chat(test_client, "调用一下回声工具", str(tmp_path / "ws-hot"))
    types = _custom_types(frames)

    assert "tool_call" in types
    assert response_text_contains(frames, "热插拔链路OK")


# ---------------------------------------------------------------------------
# 验收门③：套餐变更确认卡片两回合
# ---------------------------------------------------------------------------


def test_change_package_confirmation_two_turns_end_to_end(client, monkeypatch) -> None:
    test_client, tmp_path = client
    workspace = str(tmp_path / "ws-approval")
    _stub_intent(monkeypatch)

    # 第一轮：模型要求改 199 档 → 确认卡片，不执行
    _stub_agent(
        monkeypatch,
        FakeAgentModel(
            think_queue=[
                [{"id": "c1", "name": "change_package", "args": {"target_package": "P199"}}]
            ]
        ),
    )
    turn1 = _chat(test_client, "帮我把套餐换成199元档", workspace)
    types1 = _custom_types(turn1)
    assert "agent_confirm_required" in types1
    assert response_text_contains(turn1, "请确认")
    assert "tool_result" not in types1  # 未确认不执行

    # 第二轮：用户确认 → pending 短路直达 Agent 子图恢复执行
    _stub_agent(
        monkeypatch,
        FakeAgentModel(reflect="pass", final_text="已为您办理5G畅享199元档，次月1日生效。"),
    )
    turn2 = _chat(test_client, "确认办理", workspace)
    types2 = _custom_types(turn2)

    assert "approval_resumed" in types2
    assert "tool_result" in types2
    raw = json.dumps(turn2, ensure_ascii=False)
    assert "P199" in raw
    assert response_text_contains(turn2, "次月1日生效")
    # 不存在第三次确认请求
    assert "agent_confirm_required" not in types2


def response_text_contains(frames: list[dict], needle: str) -> bool:
    return needle in json.dumps(frames, ensure_ascii=False)
