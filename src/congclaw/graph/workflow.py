from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from congclaw.graph.nodes import (
    agent_loop_node,
    clarify_node,
    context_compressor_node,
    context_compressor_route,
    context_monitor_node,
    context_monitor_route,
    fallback_node,
    faq_sediment_node,
    final_node,
    intent_route_fn,
    intent_router_node,
    planner_node,
    query_rewrite_node,
    rag_answer_node,
    sediment_route,
    verifier_node,
)
from congclaw.graph.state import CongGraphState


def build_workflow():
    return build_complex_workflow()


def build_complex_workflow():
    graph = StateGraph(CongGraphState)
    graph.add_node("planner", planner_node)
    graph.add_node("context_monitor", context_monitor_node)
    graph.add_node("context_compressor", context_compressor_node)
    graph.add_node("verifier", verifier_node)
    graph.add_node("final", final_node)

    graph.add_edge(START, "planner")
    graph.add_edge("planner", "context_monitor")
    graph.add_conditional_edges(
        "context_monitor",
        context_monitor_route,
        {"context_compressor": "context_compressor", "verifier": "verifier", "planner": "planner", "final": "final"},
    )
    graph.add_conditional_edges(
        "context_compressor",
        context_compressor_route,
        {"verifier": "verifier", "planner": "planner", "final": "final"},
    )
    graph.add_edge("verifier", "context_monitor")
    graph.add_edge("final", END)
    return graph.compile()


def build_entry_workflow():
    """阶段 2/5/7：入口对话图（查询重写 → 意图识别 → 任务调度 → 知识沉淀）。

    阶段 5 起 START 先进入 query_rewrite（指代消解/省略补全，无指代透传），
    再到 intent_router 条件边 → rag_answer / agent_loop / clarify / fallback，
    四类分支均产出 final_answer 后结束；unknown 未达阈值时先路由 clarify 追问。

    阶段 7 起四类分支不再直接到 END，而是经 ``sediment_route`` 规则门控：
    仅「成功且有实质结果」的回合（rag_answer 有来源 / agent_loop 有成功的工具
    调用，且无兜底、无待确认）才进入 faq_sediment 沉淀本次常见问答解决方案，
    其余回合原样结束，不附加任何模型调用。
    """
    graph = StateGraph(CongGraphState)
    graph.add_node("query_rewrite", query_rewrite_node)
    graph.add_node("intent_router", intent_router_node)
    graph.add_node("rag_answer", rag_answer_node)
    graph.add_node("agent_loop", agent_loop_node)
    graph.add_node("clarify", clarify_node)
    graph.add_node("fallback", fallback_node)
    graph.add_node("faq_sediment", faq_sediment_node)

    graph.add_edge(START, "query_rewrite")
    graph.add_edge("query_rewrite", "intent_router")
    graph.add_conditional_edges(
        "intent_router",
        intent_route_fn,
        {"rag_answer": "rag_answer", "agent_loop": "agent_loop", "clarify": "clarify", "fallback": "fallback"},
    )
    for node in ("rag_answer", "agent_loop", "clarify", "fallback"):
        graph.add_conditional_edges(
            node,
            sediment_route,
            {"faq_sediment": "faq_sediment", "end": END},
        )
    graph.add_edge("faq_sediment", END)
    return graph.compile()
