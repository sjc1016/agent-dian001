"""原子工具验收：六个电信业务工具的执行链路、参数校验与工具注册中心。

原子工具（Tool）是 LLM 唯一可直接调用的能力：注册中心把 ``to_openai_tool()``
交给思考节点 ``bind_tools``；编排技能另见 ``tests/test_skills.py``。

业务数据全部走 tmp_path 下的独立 SQLite 库（conftest 全局隔离 DB_PATH），
不依赖真实 BOSS/CRM 与 LLM。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import pytest

from congclaw.core.state import RuntimeState
from congclaw.tools.base import ToolContext, ToolError
from congclaw.tools.catalog.change_package import TOOL as change_package_tool
from congclaw.tools.catalog.list_packages import TOOL as list_packages_tool
from congclaw.tools.catalog.query_balance import TOOL as query_balance_tool
from congclaw.tools.catalog.query_fault_status import TOOL as query_fault_status_tool
from congclaw.tools.catalog.report_fault import TOOL as report_fault_tool
from congclaw.tools.registry import BUILTIN_CATALOG_DIR, ToolRegistry
from congclaw.tools.todo_tool import persist_todos, render_todo_markdown, update_todo, write_todos

_ECHO_TOOL_SOURCE = '''
from congclaw.tools.base import Tool, ToolParameter


class EchoTool(Tool):
    name = "echo_demo"
    description = "echo back the input text"
    parameters = [ToolParameter(name="text", description="text to echo", required=True)]

    async def run(self, context, **kwargs):
        return {"echo": kwargs.get("text")}


TOOL = EchoTool()
'''

_ECHO_SKILL_SOURCE = '''
from congclaw.skills.base import Skill


class EchoSkill(Skill):
    name = "echo_demo"

    async def run(self, context, **kwargs):
        return {}


SKILL = EchoSkill()
'''


def _run(coro):
    return asyncio.run(coro)


def _ctx() -> ToolContext:
    return ToolContext(phone="13800138000", workspace="ws-test", approval_mode="inline")


# ---------------------------------------------------------------------------
# 工具注册中心：内置目录扫描 / OpenAI 工具表 / 外置热插拔
# ---------------------------------------------------------------------------


def test_registry_loads_builtin_catalog_and_openai_schemas(tmp_path: Path) -> None:
    registry = ToolRegistry(builtin_dir=BUILTIN_CATALOG_DIR, extra_dir=tmp_path / "ext", watch=False)

    assert registry.names() == [
        "change_package",
        "list_packages",
        "query_balance",
        "query_fault_status",
        "query_package",
        "report_fault",
    ]
    # bind_tools 用的工具描述与注册表同源（热插拔后模型当轮即可见新工具）
    schemas = registry.openai_tools()
    assert {schema["function"]["name"] for schema in schemas} == set(registry.names())
    assert all(schema["type"] == "function" for schema in schemas)


def test_registry_hot_registers_external_tool(tmp_path: Path) -> None:
    registry = ToolRegistry(builtin_dir=BUILTIN_CATALOG_DIR, extra_dir=tmp_path / "ext", watch=False)

    info = registry.write_external_item("echo_demo", _ECHO_TOOL_SOURCE)

    assert info["name"] == "echo_demo"
    assert Path(info["file"]).exists()
    assert registry.get("echo_demo").name == "echo_demo"
    names = {schema["function"]["name"] for schema in registry.openai_tools()}
    assert "echo_demo" in names


def test_registry_rejects_skill_source_in_tool_dir(tmp_path: Path) -> None:
    """工具目录只收 ``TOOL``：暴露 ``SKILL`` 的编排技能文件加载失败并回滚。"""
    registry = ToolRegistry(builtin_dir=BUILTIN_CATALOG_DIR, extra_dir=tmp_path / "ext", watch=False)

    with pytest.raises(ValueError):
        registry.write_external_item("echo_demo", _ECHO_SKILL_SOURCE)

    assert not (tmp_path / "ext" / "echo_demo.py").exists()
    assert registry.try_get("echo_demo") is None


# ---------------------------------------------------------------------------
# P4-2：余额查询（验收门①的业务底座）
# ---------------------------------------------------------------------------


def test_query_balance_returns_seed_account() -> None:
    result = _run(query_balance_tool.run(_ctx()))

    assert result["query"] == "balance"
    assert result["phone"] == "13800138000"
    assert result["owner_name"] == "张伟"
    assert result["balance"] == 86.50
    assert result["real_time_fee"] == 113.50
    assert result["bill_cycle"] == "2026-09"


def test_query_balance_explicit_phone_overrides_context() -> None:
    result = _run(query_balance_tool.run(_ctx(), phone=" 13800138000 "))

    assert result["phone"] == "13800138000"


def test_query_balance_unknown_account_raises_tool_error() -> None:
    with pytest.raises(ToolError) as excinfo:
        _run(query_balance_tool.run(_ctx(), phone="13900000000"))

    assert excinfo.value.code == "account_not_found"


# ---------------------------------------------------------------------------
# P4-5 + P4-6：宽带报修建单 + 工单状态查询（验收门②）
# ---------------------------------------------------------------------------


def test_report_fault_then_query_status() -> None:
    created = _run(
        report_fault_tool.run(
            _ctx(),
            fault_type="宽带故障",
            description="昨晚开始光猫亮红灯，全屋断网",
            address="杭州市西湖区文三路100号",
        )
    )

    assert created["action"] == "report_fault"
    assert created["ticket_id"].startswith("FT")
    assert created["status"] == "已受理"
    assert created["owner_name"] == "张伟"
    assert created["contact"] == "13800138000"

    ticket_id = created["ticket_id"]
    by_id = _run(query_fault_status_tool.run(_ctx(), ticket_id=ticket_id))
    assert by_id["lookup"] == "by_ticket_id"
    assert by_id["ticket"]["ticket_id"] == ticket_id
    assert by_id["ticket"]["fault_type"] == "宽带故障"

    by_phone = _run(query_fault_status_tool.run(_ctx()))
    assert by_phone["lookup"] == "by_phone"
    assert by_phone["count"] >= 2  # 种子历史单 + 新建单
    assert by_phone["tickets"][0]["ticket_id"] == ticket_id  # 最新单排首位


def test_report_fault_rejects_unknown_fault_type() -> None:
    with pytest.raises(ToolError) as excinfo:
        _run(report_fault_tool.run(_ctx(), fault_type="卫星故障", description="看不了电视"))

    assert excinfo.value.code == "invalid_enum"


def test_report_fault_requires_description() -> None:
    with pytest.raises(ToolError):
        _run(report_fault_tool.run(_ctx(), fault_type="宽带故障"))


def test_query_fault_status_unknown_ticket_raises() -> None:
    with pytest.raises(ToolError) as excinfo:
        _run(query_fault_status_tool.run(_ctx(), ticket_id="FT404404404"))

    assert excinfo.value.code == "ticket_not_found"


# ---------------------------------------------------------------------------
# P4-7：套餐变更（验收门③的业务底座，次月生效）
# ---------------------------------------------------------------------------


def test_change_package_success_same_package_and_not_found() -> None:
    # 1) 成功变更：P129 -> P199
    result = _run(change_package_tool.run(_ctx(), target_package="P199"))

    assert result["action"] == "change_package"
    assert result["status"] == "办理成功"
    assert result["old_package_id"] == "P129"
    assert result["new_package_id"] == "P199"
    assert result["new_package_name"] == "5G畅享199元档"
    assert result["effective_date"] == _next_month_first_day()

    # 2) 重复变更到同档：same_package（致命业务错误，反思节点不重试）
    with pytest.raises(ToolError) as excinfo:
        _run(change_package_tool.run(_ctx(), target_package="199"))
    assert excinfo.value.code == "same_package"

    # 3) 目标套餐不存在
    with pytest.raises(ToolError) as excinfo:
        _run(change_package_tool.run(_ctx(), target_package="P999"))
    assert excinfo.value.code == "package_not_found"


def _next_month_first_day() -> str:
    now = datetime.now()
    year = now.year + (1 if now.month == 12 else 0)
    month = 1 if now.month == 12 else now.month + 1
    return f"{year:04d}-{month:02d}-01"


def test_change_package_fuzzy_name_resolves_unique_package() -> None:
    result = _run(change_package_tool.run(_ctx(), target_package="全屋WiFi"))

    assert result["new_package_id"] == "BB169"


# ---------------------------------------------------------------------------
# P4-3：套餐目录
# ---------------------------------------------------------------------------


def test_list_packages_keyword_filter() -> None:
    result = _run(list_packages_tool.run(_ctx(), keyword="199"))

    names = [item["name"] for item in result["packages"]]
    assert "5G畅享199元档" in names
    assert all(item["active"] for item in result["packages"])


# ---------------------------------------------------------------------------
# Tool 基类：元信息 / OpenAI tool schema / 参数校验
# ---------------------------------------------------------------------------


def test_change_package_openai_tool_schema() -> None:
    tool = change_package_tool.to_openai_tool()

    assert tool["type"] == "function"
    function = tool["function"]
    assert function["name"] == "change_package"
    assert "target_package" in function["parameters"]["required"]
    assert function["parameters"]["properties"]["target_package"]["type"] == "string"


def test_report_fault_metadata_exposes_enum_and_access() -> None:
    metadata = report_fault_tool.metadata()

    assert metadata["access"] == "write"
    assert metadata["requires_confirmation"] is False
    fault_param = next(p for p in metadata["parameters"] if p["name"] == "fault_type")
    assert "宽带故障" in fault_param["enum"]


def test_change_package_requires_confirmation_flag() -> None:
    assert change_package_tool.access == "write"
    assert change_package_tool.requires_confirmation is True


def test_validate_arguments_rejects_unknown_argument() -> None:
    with pytest.raises(ToolError):
        query_balance_tool.validate_arguments({"phone": "13800138000", "unexpected": "x"}, _ctx())


def test_validate_arguments_fills_defaults_and_strips() -> None:
    cleaned = query_balance_tool.validate_arguments({"phone": "  13800138000  "}, _ctx())

    assert cleaned == {"phone": "13800138000"}


def test_validate_arguments_empty_optional_skipped() -> None:
    cleaned = query_fault_status_tool.validate_arguments({}, _ctx())

    assert cleaned == {}


# ---------------------------------------------------------------------------
# 规划工具（仅绑给 planner_node，独立于工具/技能注册中心）
# ---------------------------------------------------------------------------


def make_state(tmp_path: Path) -> RuntimeState:
    return RuntimeState(workspace=tmp_path)


def test_todo_write_tool_records_plan_parts() -> None:
    result = write_todos(
        ["write tests", "implement"],
        ["tests pass"],
        ["python -m pytest -q"],
    )

    assert result["ok"] is True
    assert result["todos"] == ["write tests", "implement"]
    assert result["acceptance_criteria"] == ["tests pass"]
    assert result["verification_commands"] == ["python -m pytest -q"]


def test_todo_write_tool_normalizes_json_strings() -> None:
    result = write_todos(
        '[{"title": "write tests"}, {"title": "implement"}]',
        "- tests pass\n- demo runs",
        '["python -m pytest -q"]',
    )

    assert result["todos"] == ["write tests", "implement"]
    assert result["acceptance_criteria"] == ["tests pass", "demo runs"]
    assert result["verification_commands"] == ["python -m pytest -q"]


def test_todo_write_tool_accepts_id_keyed_description_dict() -> None:
    result = write_todos(
        '{"todo-1": {"status": "completed", "description": "Research Qwen"}, "todo-2": {"description": "Write HTML"}}',
        '["HTML exists"]',
        '["ls -la qwen.html"]',
    )

    assert result["ok"] is True
    assert result["todos"] == ["Research Qwen", "Write HTML"]
    assert result["acceptance_criteria"] == ["HTML exists"]
    assert result["verification_commands"] == ["ls -la qwen.html"]


def test_todo_update_tool_updates_existing_todo() -> None:
    todos = [{"id": "todo-1", "content": "write tests", "status": "pending", "note": ""}]

    result = update_todo(todos, "todo-1", "completed", "tests written")

    assert result["ok"] is True
    assert result["todos"][0]["status"] == "completed"
    assert result["todos"][0]["note"] == "tests written"


def test_todo_update_tool_rejects_unknown_todo() -> None:
    todos = [{"id": "todo-1", "content": "write tests", "status": "pending", "note": ""}]

    result = update_todo(todos, "todo-2", "completed")

    assert result["ok"] is False
    assert result["todos"][0]["status"] == "pending"


def test_todo_markdown_persistence(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    todos = [{"id": "todo-1", "content": "write page", "status": "pending", "note": ""}]

    result = persist_todos(state, todos, ["page exists"], ["python --version"], "demo plan")

    content = (tmp_path / "TODO.md").read_text(encoding="utf-8")
    assert result["ok"] is True
    assert "demo plan" in content
    assert "todo-1" in content
    assert "python --version" in content


def test_render_todo_markdown_marks_completed() -> None:
    content = render_todo_markdown(
        [{"id": "todo-1", "content": "done", "status": "completed", "note": "verified"}],
        [],
        [],
    )

    assert "- [x]" in content
    assert "verified" in content
