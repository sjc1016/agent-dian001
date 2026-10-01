"""阶段 4 验收：六个电信业务 Skill 的执行链路与参数校验。

全部使用 tmp_path 下的独立 SQLite 库（business_store 走 aiosqlite 短连接，
DB_PATH 环境变量即时生效），不依赖真实 BOSS/CRM 与 LLM。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import pytest

from mokioclaw.skills.base import SkillContext, SkillError
from mokioclaw.skills.catalog.change_package import SKILL as change_package_skill
from mokioclaw.skills.catalog.list_packages import SKILL as list_packages_skill
from mokioclaw.skills.catalog.query_balance import SKILL as query_balance_skill
from mokioclaw.skills.catalog.query_fault_status import SKILL as query_fault_status_skill
from mokioclaw.skills.catalog.report_fault import SKILL as report_fault_skill


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "skills-test.db"))


def _run(coro):
    return asyncio.run(coro)


def _ctx() -> SkillContext:
    return SkillContext(phone="13800138000", workspace="ws-test", approval_mode="inline")


# ---------------------------------------------------------------------------
# P4-2：余额查询（验收门①的业务底座）
# ---------------------------------------------------------------------------


def test_query_balance_returns_seed_account() -> None:
    result = _run(query_balance_skill.run(_ctx()))

    assert result["query"] == "balance"
    assert result["phone"] == "13800138000"
    assert result["owner_name"] == "张伟"
    assert result["balance"] == 86.50
    assert result["real_time_fee"] == 113.50
    assert result["bill_cycle"] == "2026-09"


def test_query_balance_explicit_phone_overrides_context() -> None:
    result = _run(query_balance_skill.run(_ctx(), phone=" 13800138000 "))

    assert result["phone"] == "13800138000"


def test_query_balance_unknown_account_raises_skill_error() -> None:
    with pytest.raises(SkillError) as excinfo:
        _run(query_balance_skill.run(_ctx(), phone="13900000000"))

    assert excinfo.value.code == "account_not_found"


# ---------------------------------------------------------------------------
# P4-5 + P4-6：宽带报修建单 + 工单状态查询（验收门②）
# ---------------------------------------------------------------------------


def test_report_fault_then_query_status() -> None:
    created = _run(
        report_fault_skill.run(
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
    by_id = _run(query_fault_status_skill.run(_ctx(), ticket_id=ticket_id))
    assert by_id["lookup"] == "by_ticket_id"
    assert by_id["ticket"]["ticket_id"] == ticket_id
    assert by_id["ticket"]["fault_type"] == "宽带故障"

    by_phone = _run(query_fault_status_skill.run(_ctx()))
    assert by_phone["lookup"] == "by_phone"
    assert by_phone["count"] >= 2  # 种子历史单 + 新建单
    assert by_phone["tickets"][0]["ticket_id"] == ticket_id  # 最新单排首位


def test_report_fault_rejects_unknown_fault_type() -> None:
    with pytest.raises(SkillError) as excinfo:
        _run(
            report_fault_skill.run(
                _ctx(), fault_type="卫星故障", description="看不了电视"
            )
        )

    assert excinfo.value.code == "invalid_enum"


def test_report_fault_requires_description() -> None:
    with pytest.raises(SkillError):
        _run(report_fault_skill.run(_ctx(), fault_type="宽带故障"))


def test_query_fault_status_unknown_ticket_raises() -> None:
    with pytest.raises(SkillError) as excinfo:
        _run(query_fault_status_skill.run(_ctx(), ticket_id="FT404404404"))

    assert excinfo.value.code == "ticket_not_found"


# ---------------------------------------------------------------------------
# P4-7：套餐变更（验收门③的业务底座，次月生效）
# ---------------------------------------------------------------------------


def test_change_package_success_same_package_and_not_found() -> None:
    # 1) 成功变更：P129 -> P199
    result = _run(change_package_skill.run(_ctx(), target_package="P199"))

    assert result["action"] == "change_package"
    assert result["status"] == "办理成功"
    assert result["old_package_id"] == "P129"
    assert result["new_package_id"] == "P199"
    assert result["new_package_name"] == "5G畅享199元档"
    expected_effective = _next_month_first_day()
    assert result["effective_date"] == expected_effective

    # 2) 重复变更到同档：same_package（致命业务错误，反思节点不重试）
    with pytest.raises(SkillError) as excinfo:
        _run(change_package_skill.run(_ctx(), target_package="199"))
    assert excinfo.value.code == "same_package"

    # 3) 目标套餐不存在
    with pytest.raises(SkillError) as excinfo:
        _run(change_package_skill.run(_ctx(), target_package="P999"))
    assert excinfo.value.code == "package_not_found"


def _next_month_first_day() -> str:
    now = datetime.now()
    year = now.year + (1 if now.month == 12 else 0)
    month = 1 if now.month == 12 else now.month + 1
    return f"{year:04d}-{month:02d}-01"


def test_change_package_fuzzy_name_resolves_unique_package() -> None:
    result = _run(change_package_skill.run(_ctx(), target_package="全屋WiFi"))

    assert result["new_package_id"] == "BB169"


# ---------------------------------------------------------------------------
# P4-3：套餐目录
# ---------------------------------------------------------------------------


def test_list_packages_keyword_filter() -> None:
    result = _run(list_packages_skill.run(_ctx(), keyword="199"))

    names = [item["name"] for item in result["packages"]]
    assert "5G畅享199元档" in names
    assert all(item["active"] for item in result["packages"])


# ---------------------------------------------------------------------------
# Skill 基类：元信息 / OpenAI tool schema / 参数校验
# ---------------------------------------------------------------------------


def test_change_package_openai_tool_schema() -> None:
    tool = change_package_skill.to_openai_tool()

    assert tool["type"] == "function"
    function = tool["function"]
    assert function["name"] == "change_package"
    assert "target_package" in function["parameters"]["required"]
    assert function["parameters"]["properties"]["target_package"]["type"] == "string"


def test_report_fault_metadata_exposes_enum_and_access() -> None:
    metadata = report_fault_skill.metadata()

    assert metadata["access"] == "write"
    assert metadata["requires_confirmation"] is False
    fault_param = next(p for p in metadata["parameters"] if p["name"] == "fault_type")
    assert "宽带故障" in fault_param["enum"]


def test_change_package_requires_confirmation_flag() -> None:
    assert change_package_skill.access == "write"
    assert change_package_skill.requires_confirmation is True


def test_validate_arguments_rejects_unknown_argument() -> None:
    with pytest.raises(SkillError):
        query_balance_skill.validate_arguments(
            {"phone": "13800138000", "unexpected": "x"}, _ctx()
        )


def test_validate_arguments_fills_defaults_and_strips() -> None:
    cleaned = query_balance_skill.validate_arguments({"phone": "  13800138000  "}, _ctx())

    assert cleaned == {"phone": "13800138000"}


def test_validate_arguments_empty_optional_skipped() -> None:
    cleaned = query_fault_status_skill.validate_arguments({}, _ctx())

    assert cleaned == {}
