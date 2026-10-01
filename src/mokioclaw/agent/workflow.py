"""P4-12~P4-16：Agent 深度推理子图组装与编译。

拓扑（PRD 3.4 节）：

    START → approval_entry ─┬─ think ─→ act（asyncio.gather 多工具并行）
                            │              ├─ 确认卡片 → finalize（等待下一轮）
                            │              └─ reflect ─┬─ pass     → finalize
                            │                           ├─ retry    → think（attempts 限制）
                            │                           └─ fallback → agent_fallback
                            ├─ act（确认恢复执行）→ reflect ...
                            └─ finalize（取消/无需工具）

approval_entry 的三条分支都由条件边按 entry_decision 路由。
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from mokioclaw.agent.nodes import (
    act_node,
    act_route,
    agent_fallback_node,
    approval_entry_node,
    approval_entry_route,
    finalize_node,
    reflect_node,
    reflect_route,
    think_node,
    think_route,
)
from mokioclaw.agent.state import AgentSubState


def build_agent_subgraph():
    """编译 Agent 深度推理子图（独立 AgentSubState，与主图解耦）。"""
    graph = StateGraph(AgentSubState)

    graph.add_node("approval_entry", approval_entry_node)
    graph.add_node("think", think_node)
    graph.add_node("act", act_node)
    graph.add_node("reflect", reflect_node)
    graph.add_node("finalize", finalize_node)
    graph.add_node("agent_fallback", agent_fallback_node)

    graph.add_edge(START, "approval_entry")
    graph.add_conditional_edges(
        "approval_entry",
        approval_entry_route,
        {"think": "think", "act": "act", "cancel": "finalize"},
    )
    graph.add_conditional_edges("think", think_route, {"act": "act", "finalize": "finalize"})
    graph.add_conditional_edges(
        "act", act_route, {"reflect": "reflect", "finalize": "finalize"}
    )
    graph.add_conditional_edges(
        "reflect",
        reflect_route,
        {"pass": "finalize", "retry": "think", "fallback": "agent_fallback"},
    )
    graph.add_edge("finalize", END)
    graph.add_edge("agent_fallback", END)

    return graph.compile()
