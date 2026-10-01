"""工具注册表（阶段 0 占位）。

代码 Agent 时代的 Bash / File / Grep / Notepad / WebSearch 工具已随领域瘦身删除。
电信客服业务工具将在阶段 4 以 ``mokioclaw.skills`` 动态注册中心重新落地，
本模块暂时保留空的工具装配入口，保证既有 import 不报错。
"""

from __future__ import annotations

from langchain_core.tools import StructuredTool

from mokioclaw.core.state import RuntimeState


def build_tools(state: RuntimeState) -> list[StructuredTool]:
    """返回当前可用的（写）工具列表。阶段 0 为空，阶段 4 由 Skill 注册中心取代。"""
    return []


def build_read_only_tools(state: RuntimeState) -> list[StructuredTool]:
    """返回当前可用的只读工具列表。阶段 0 为空，阶段 4 由 Skill 注册中心取代。"""
    return []
