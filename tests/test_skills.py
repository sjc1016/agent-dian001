"""编排技能验收：四个确定性编排（余额/套餐咨询/套餐变更/故障报修）。

编排技能（Skill）不进 LLM 的工具表，由主图 ``intent_router`` 依据 ``intent_hint``
挑选后用代码顺序调用原子工具；原子工具本身另见 ``tests/test_tools.py``。

业务数据全部走 tmp_path 下的独立 SQLite 库（conftest 全局隔离 DB_PATH），
不依赖真实 BOSS/CRM 与 LLM。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from congclaw.skills.base import SkillContext
from congclaw.skills.catalog.balance_query import SKILL as balance_query_skill
from congclaw.skills.catalog.fault_repair import SKILL as fault_repair_skill
from congclaw.skills.catalog.fault_repair import normalize_fault_type
from congclaw.skills.catalog.package_change import SKILL as package_change_skill
from congclaw.skills.catalog.package_query import SKILL as package_query_skill
from congclaw.skills.catalog.package_query import normalize_scope
from congclaw.tools.base import ConfirmationRequired, ToolError
from congclaw.tools.registry import BUILTIN_CATALOG_DIR, ToolRegistry


def _run(coro):
    return asyncio.run(coro)


def _ctx(tmp_path: Path, *, approval_mode: str = "inline") -> SkillContext:
    """编排上下文：注入只含内置目录的工具注册中心（不启监听线程）。"""
    return SkillContext(
        phone="13800138000",
        workspace="ws-test",
        approval_mode=approval_mode,
        tools=ToolRegistry(builtin_dir=BUILTIN_CATALOG_DIR, extra_dir=tmp_path / "ext", watch=False),
    )


# ---------------------------------------------------------------------------
# balance_query：单步编排（不消耗 think，纯粹把「选工具」收回代码）
# ---------------------------------------------------------------------------


def test_balance_query_runs_single_tool(tmp_path: Path) -> None:
    result = _run(balance_query_skill.run(_ctx(tmp_path)))

    assert result["skill"] == "balance_query"
    assert result["ok"] is True
    assert [step["name"] for step in result["steps"]] == ["query_balance"]
    assert all(step["ok"] for step in result["steps"])
    assert result["data"]["account"]["balance"] == 86.50
    assert result["data"]["account"]["owner_name"] == "张伟"


def test_balance_query_failure_keeps_tool_error_code(tmp_path: Path) -> None:
    result = _run(balance_query_skill.run(_ctx(tmp_path), phone="13900000000"))

    assert result["ok"] is False
    assert result["error_code"] == "account_not_found"
    assert result["steps"][0]["ok"] is False
    assert result["data"] == {}


# ---------------------------------------------------------------------------
# fault_repair：建单 → 按工单号复核（两步确定性顺序）
# ---------------------------------------------------------------------------


def test_fault_repair_creates_and_verifies_ticket(tmp_path: Path) -> None:
    result = _run(
        fault_repair_skill.run(
            _ctx(tmp_path),
            fault_type="",
            description="从昨晚开始光猫亮红灯，全屋断网",
            address="杭州市西湖区文三路100号",
        )
    )

    assert result["ok"] is True
    assert [step["name"] for step in result["steps"]] == ["report_fault", "query_fault_status"]
    assert result["data"]["fault_type"] == "宽带故障"  # 模型未给枚举值时按描述归一
    assert result["data"]["ticket"]["ticket_id"].startswith("FT")
    assert result["data"]["verified"] is True


@pytest.mark.parametrize(
    ("raw", "description", "expected"),
    [
        ("宽带故障", "", "宽带故障"),
        ("", "光猫亮红灯上不了网", "宽带故障"),
        ("", "机顶盒看不了电视", "IPTV故障"),
        ("", "打电话拨不出去", "通话故障"),
        ("", "手机没信号", "手机网络故障"),
        ("", "说不清楚的怪现象", "其他"),
    ],
)
def test_normalize_fault_type(raw: str, description: str, expected: str) -> None:
    assert normalize_fault_type(raw, description) == expected


# ---------------------------------------------------------------------------
# package_query：按 scope 条件分支（当前套餐 / 可办理套餐目录）
# ---------------------------------------------------------------------------


def test_package_query_defaults_to_current_package(tmp_path: Path) -> None:
    result = _run(package_query_skill.run(_ctx(tmp_path), scope="当前套餐及流量余量"))

    assert result["ok"] is True
    assert [step["name"] for step in result["steps"]] == ["query_package"]
    assert result["data"]["scope"] == "current"
    assert result["data"]["package"]["query"] == "user_package"


def test_package_query_catalog_scope_lists_packages(tmp_path: Path) -> None:
    result = _run(package_query_skill.run(_ctx(tmp_path), scope="catalog", keyword="199"))

    assert result["ok"] is True
    assert [step["name"] for step in result["steps"]] == ["list_packages"]
    assert result["data"]["scope"] == "catalog"
    names = [item["name"] for item in result["data"]["catalog"]["packages"]]
    assert "5G畅享199元档" in names


@pytest.mark.parametrize(
    ("raw", "keyword", "expected"),
    [
        ("当前套餐及流量余量", "", "current"),
        ("", "", "current"),
        ("我不确定", "", "current"),
        ("catalog", "", "catalog"),
        ("你们有哪些套餐可以办理", "", "catalog"),
        ("", "199", "current"),
    ],
)
def test_normalize_scope(raw: str, keyword: str, expected: str) -> None:
    assert normalize_scope(raw, keyword) == expected


# ---------------------------------------------------------------------------
# package_change：查当前套餐 → 提交变更（高危写操作，命中即请人工确认）
# ---------------------------------------------------------------------------


def test_package_change_raises_confirmation_in_inline_mode(tmp_path: Path) -> None:
    with pytest.raises(ConfirmationRequired) as excinfo:
        _run(package_change_skill.run(_ctx(tmp_path), target_package="P199"))

    # 确认单落的是原子工具名与入参，执行节点据此在同一轮给出确认卡片
    assert excinfo.value.tool_name == "change_package"
    assert excinfo.value.tool_args == {"target_package": "P199"}
    assert "请确认" in excinfo.value.summary


def test_package_change_executes_under_auto_mode(tmp_path: Path) -> None:
    result = _run(package_change_skill.run(_ctx(tmp_path, approval_mode="auto"), target_package="P199"))

    assert result["ok"] is True
    assert [step["name"] for step in result["steps"]] == ["query_package", "change_package"]
    assert result["data"]["change"]["new_package_id"] == "P199"
    assert result["data"]["current"]["query"] == "user_package"


def test_package_change_rejected_under_deny_mode(tmp_path: Path) -> None:
    with pytest.raises(ToolError) as excinfo:
        _run(package_change_skill.run(_ctx(tmp_path, approval_mode="deny"), target_package="P199"))

    assert excinfo.value.code == "approval_denied"


def test_package_change_requires_target_package(tmp_path: Path) -> None:
    with pytest.raises(ToolError):
        _run(package_change_skill.run(_ctx(tmp_path), target_package="  "))


# ---------------------------------------------------------------------------
# Skill 基类：元信息 / 参数校验
# ---------------------------------------------------------------------------


def test_skill_metadata_exposes_intent_hint_and_parameters() -> None:
    metadata = package_change_skill.metadata()

    assert metadata["name"] == "package_change"
    assert metadata["access"] == "write"
    assert metadata["intent_hint"]  # intent_router 挑选候选即读这个字段
    assert "requires_confirmation" not in metadata  # 确认策略由原子工具的声明决定
    target = next(p for p in metadata["parameters"] if p["name"] == "target_package")
    assert target["required"] is True


def test_skill_validate_arguments_rejects_unknown_argument(tmp_path: Path) -> None:
    with pytest.raises(ToolError):
        balance_query_skill.validate_arguments({"phone": "13800138000", "x": 1}, _ctx(tmp_path))


def test_skill_validate_arguments_strips_and_fills_defaults(tmp_path: Path) -> None:
    cleaned = package_query_skill.validate_arguments({"phone": " 13800138000 "}, _ctx(tmp_path))

    assert cleaned == {"phone": "13800138000", "scope": "current"}


def test_skill_raises_when_dependency_unregistered(tmp_path: Path) -> None:
    """依赖的原子工具被热卸载：编排以 unknown_tool 失败，而不是静默降级。"""
    context = _ctx(tmp_path)
    context.tools.unregister("query_balance")

    result = _run(balance_query_skill.run(context))

    assert result["ok"] is False
    assert result["error_code"] == "unknown_tool"
