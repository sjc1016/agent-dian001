"""阶段 7：常见问答沉淀子图组装与编译。

拓扑：
    START → rule_gate ─┬─ 通过 ─> match_existing → extract → persist → END
                       └─ 不通过 ─────────────────────────────────> END

与 RAG / Agent 子图一致使用独立状态（:class:`FaqSedimentState`），
主图与子图之间是「本轮问答过程进、沉淀结果出」的纯函数式接口。
子图由主图 ``faq_sediment_node`` 通过 ``astream`` 驱动，custom 事件实时转发到 trace。
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from congclaw.faq.nodes import (
    extract_node,
    gate_route,
    match_existing_node,
    persist_node,
    rule_gate_node,
)
from congclaw.faq.state import FaqSedimentState


def build_faq_sediment_subgraph():
    """编译常见问答沉淀子图（独立 FaqSedimentState，与主图解耦）。"""
    graph = StateGraph(FaqSedimentState)

    graph.add_node("rule_gate", rule_gate_node)
    graph.add_node("match_existing", match_existing_node)
    graph.add_node("extract", extract_node)
    graph.add_node("persist", persist_node)

    graph.add_edge(START, "rule_gate")
    graph.add_conditional_edges(
        "rule_gate",
        gate_route,
        {"match_existing": "match_existing", "end": END},
    )
    graph.add_edge("match_existing", "extract")
    graph.add_edge("extract", "persist")
    graph.add_edge("persist", END)

    return graph.compile()
