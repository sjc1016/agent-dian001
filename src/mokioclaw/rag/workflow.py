"""P3-20：RAG 检索子图组装与编译。

拓扑（PRD 3.3 节）：
    START ─┬─ retrieve_bm25 ──┐
           └─ retrieve_dense ─┴─> rrf_fusion → rerank → evidence_gate
                                          ├─ 有命中  → parent_lookup → generate → END
                                          ├─ 低置信  → rewrite_once（≤1 次）→ 重新 fan-out
                                          └─ 仍无命中 → rag_fallback → END
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from mokioclaw.rag.nodes import (
    evidence_gate_node,
    evidence_gate_route,
    generate_node,
    parent_lookup_node,
    rag_fallback_node,
    retrieve_bm25_node,
    retrieve_dense_node,
    rewrite_once_node,
    rerank_node,
    rrf_fusion_node,
)
from mokioclaw.rag.state import RagSubState


def build_rag_subgraph():
    """编译 RAG 检索子图（独立 RagSubState，与主图解耦）。"""
    graph = StateGraph(RagSubState)

    graph.add_node("retrieve_bm25", retrieve_bm25_node)
    graph.add_node("retrieve_dense", retrieve_dense_node)
    graph.add_node("rrf_fusion", rrf_fusion_node)
    graph.add_node("rerank", rerank_node)
    graph.add_node("evidence_gate", evidence_gate_node)
    graph.add_node("parent_lookup", parent_lookup_node)
    graph.add_node("rewrite_once", rewrite_once_node)
    graph.add_node("generate", generate_node)
    graph.add_node("rag_fallback", rag_fallback_node)

    # START 两条出边：图调度器并发执行两路召回
    graph.add_edge(START, "retrieve_bm25")
    graph.add_edge(START, "retrieve_dense")
    # 双入边构成屏障：两路齐备后才执行融合
    graph.add_edge("retrieve_bm25", "rrf_fusion")
    graph.add_edge("retrieve_dense", "rrf_fusion")
    graph.add_edge("rrf_fusion", "rerank")
    graph.add_edge("rerank", "evidence_gate")

    graph.add_conditional_edges(
        "evidence_gate",
        evidence_gate_route,
        {
            "parent_lookup": "parent_lookup",
            "rewrite": "rewrite_once",
            "fallback": "rag_fallback",
        },
    )
    graph.add_edge("parent_lookup", "generate")
    graph.add_edge("generate", END)
    graph.add_edge("rag_fallback", END)

    # 放宽重写后重新 fan-out 召回（rag_attempts 限制最多 1 次，不会死循环）
    graph.add_edge("rewrite_once", "retrieve_bm25")
    graph.add_edge("rewrite_once", "retrieve_dense")

    return graph.compile()
