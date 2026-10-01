"""阶段 3：RAG 检索子图节点（全部 async，由 LangGraph 调度器并发调度）。

拓扑见 :func:`mokioclaw.rag.workflow.build_rag_subgraph`：
START  fan-out 到 retrieve_bm25 / retrieve_dense（并行）
→ rrf_fusion → rerank → evidence_gate（条件边）
   ├─ parent_lookup → generate（四层提示词，附来源编号）
   ├─ rewrite_once（限 1 次）→ 重新 fan-out 召回
   └─ rag_fallback

每个节点的关键中间结果都通过 custom 事件写出（P3-22），
外层主图包装节点会把这些事件转发到 SSE/trace。
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.config import get_stream_writer

from mokioclaw.prompts.rag import (
    RAG_FALLBACK_REPLY,
    RAG_GENERATE_ERROR_REPLY,
    RAG_GENERATE_SYSTEM_PROMPT,
    RAG_REWRITE_PROMPT,
)
from mokioclaw.providers.openai_provider import create_model
from mokioclaw.rag import config
from mokioclaw.rag.retrieval import (
    bm25_search,
    build_parent_evidence,
    dense_search,
    rerank_hits,
)
from mokioclaw.rag.state import RagSubState

SNIPPET_CHARS = 80


# ---------------------------------------------------------------------------
# P3-11 / P3-12：两路并行召回
# ---------------------------------------------------------------------------


async def retrieve_bm25_node(state: RagSubState) -> dict[str, Any]:
    """rank_bm25 关键词召回（单字+二元组中文索引）。"""
    writer = _get_writer()
    query = str(state.get("query") or "")
    hits: list[dict[str, Any]] = []
    error = ""
    try:
        hits = await bm25_search(query, config.recall_top_k())
    except Exception as exc:  # 语料为空/索引异常时该路降级为空，不拖垮整图
        error = f"{type(exc).__name__}: {exc}"
    writer(
        {
            "type": "rag_retrieve",
            "channel": "bm25",
            "query": query,
            "attempt": int(state.get("rag_attempts", 0) or 0),
            "count": len(hits),
            "error": error,
            "hits": _hit_summaries(hits, score_key="bm25_score"),
        }
    )
    return {"bm25_hits": hits}


async def retrieve_dense_node(state: RagSubState) -> dict[str, Any]:
    """BGE-M3 + Milvus Lite 稠密语义召回。"""
    writer = _get_writer()
    query = str(state.get("query") or "")
    hits: list[dict[str, Any]] = []
    error = ""
    try:
        hits = await dense_search(query, config.recall_top_k())
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    writer(
        {
            "type": "rag_retrieve",
            "channel": "dense",
            "query": query,
            "attempt": int(state.get("rag_attempts", 0) or 0),
            "count": len(hits),
            "error": error,
            "hits": _hit_summaries(hits, score_key="dense_score"),
        }
    )
    return {"dense_hits": hits}


# ---------------------------------------------------------------------------
# P3-13：RRF 融合
# ---------------------------------------------------------------------------


async def rrf_fusion_node(state: RagSubState) -> dict[str, Any]:
    """等待两路召回齐备后做 RRF 融合：score = Σ 1/(k + rank)。"""
    bm25_hits = list(state.get("bm25_hits") or [])
    dense_hits = list(state.get("dense_hits") or [])
    merged: dict[str, dict[str, Any]] = {}
    for hit in bm25_hits:
        merged[hit["child_id"]] = dict(hit)
    for hit in dense_hits:
        target = merged.get(hit["child_id"])
        if target is None:
            merged[hit["child_id"]] = dict(hit)
        else:
            target.update(hit)

    for child_id, hit in merged.items():
        score = 0.0
        if hit.get("bm25_rank"):
            score += 1.0 / (config.RRF_K + int(hit["bm25_rank"]))
        if hit.get("dense_rank"):
            score += 1.0 / (config.RRF_K + int(hit["dense_rank"]))
        hit["rrf_score"] = score

    fused = sorted(merged.values(), key=lambda item: item["rrf_score"], reverse=True)
    fused = fused[: config.FUSION_KEEP]
    _get_writer()(
        {
            "type": "rag_fusion",
            "bm25_count": len(bm25_hits),
            "dense_count": len(dense_hits),
            "fused_count": len(fused),
            "rrf_k": config.RRF_K,
            "top": _hit_summaries(fused[:8], score_key="rrf_score"),
        }
    )
    return {"fused_hits": fused}


# ---------------------------------------------------------------------------
# P3-14：Cross-Encoder 精排
# ---------------------------------------------------------------------------


async def rerank_node(state: RagSubState) -> dict[str, Any]:
    """Cross-Encoder 对融合结果精排，截取 Top-K。"""
    fused = list(state.get("fused_hits") or [])
    # CPU 上 Cross-Encoder 成本与候选数线性相关：只精排 Top-N 候选
    candidates = fused[: config.rerank_input_top_n()]
    reranked: list[dict[str, Any]] = []
    error = ""
    if candidates:
        try:
            reranked = await rerank_hits(str(state.get("query") or ""), candidates)
            reranked = reranked[: config.rerank_top_k()]
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            # reranker 不可用时降级：按 RRF 顺序截断，不阻断链路
            reranked = candidates[: config.rerank_top_k()]
            for index, hit in enumerate(reranked):
                hit.setdefault("rerank_score", 0.0)
                hit.setdefault("rerank_prob", 1.0)
    _get_writer()(
        {
            "type": "rag_rerank",
            "count": len(reranked),
            "error": error,
            "top": _hit_summaries(reranked, score_key="rerank_score", include_prob=True),
        }
    )
    return {"reranked_hits": reranked}


# ---------------------------------------------------------------------------
# P3-15：证据门（条件路由）
# ---------------------------------------------------------------------------


async def evidence_gate_node(state: RagSubState) -> dict[str, Any]:
    """有命中 → parent_lookup；空/低置信且未重写过 → rewrite_once；否则 fallback。"""
    hits = list(state.get("reranked_hits") or [])
    attempts = int(state.get("rag_attempts", 0) or 0)
    top_prob = float(hits[0].get("rerank_prob", 0.0)) if hits else 0.0
    top_raw = float(hits[0].get("rerank_score", 0.0)) if hits else 0.0

    if (
        hits
        and top_prob >= config.rerank_gate_prob()
        and top_raw >= config.rerank_gate_raw()
    ):
        decision, reason = "parent_lookup", "evidence_hit"
    elif attempts < config.MAX_RAG_ATTEMPTS:
        decision, reason = "rewrite", (
            "no_evidence" if not hits else f"low_confidence(prob={top_prob:.3f})"
        )
    else:
        decision, reason = "fallback", (
            "no_evidence_after_rewrite" if not hits else "low_confidence_after_rewrite"
        )

    _get_writer()(
        {
            "type": "rag_gate",
            "decision": decision,
            "reason": reason,
            "attempts": attempts,
            "hit_count": len(hits),
            "top_rerank_prob": round(top_prob, 4),
            "top_rerank_raw": round(top_raw, 4),
            "gate_prob": config.rerank_gate_prob(),
        }
    )
    return {"gate_decision": decision, "gate_reason": reason}


def evidence_gate_route(state: RagSubState) -> str:
    return str(state.get("gate_decision") or "fallback")


# ---------------------------------------------------------------------------
# P3-16：父分片回溯 + 邻域扩展
# ---------------------------------------------------------------------------


async def parent_lookup_node(state: RagSubState) -> dict[str, Any]:
    """命中 child 回溯父分片，前后邻域 child 扩展，合并去重并编号来源。"""
    reranked = list(state.get("reranked_hits") or [])
    ordered_ids = [hit["child_id"] for hit in reranked]
    score_by_child = {
        hit["child_id"]: float(hit.get("rerank_score", 0.0)) for hit in reranked
    }
    groups = await build_parent_evidence(ordered_ids)

    evidence: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    budget = config.EVIDENCE_MAX_CHARS
    for index, group in enumerate(groups, start=1):
        text = group["text"]
        if budget <= 0:
            text = ""
        elif len(text) > budget:
            text = text[:budget]
        budget -= len(text)
        item = {
            "parent_id": group["parent_id"],
            "doc_source": group["doc_source"],
            "position": group["position"],
            "text": text,
            "child_ids": group["child_ids"],
            "matched_child_ids": group["matched_child_ids"],
            "source_no": index,
        }
        evidence.append(item)
        best_score = max(
            (score_by_child.get(cid, 0.0) for cid in group["matched_child_ids"]),
            default=0.0,
        )
        sources.append(
            {
                "title": group["doc_source"],
                "url": "",
                "content": text[:240],
                "score": round(best_score, 4),
            }
        )

    _get_writer()(
        {
            "type": "rag_parent_lookup",
            "parent_count": len(evidence),
            "evidence": [
                {
                    "source_no": item["source_no"],
                    "parent_id": item["parent_id"],
                    "doc_source": item["doc_source"],
                    "char_count": len(item["text"]),
                    "matched_children": item["matched_child_ids"],
                }
                for item in evidence
            ],
        }
    )
    return {"evidence": evidence, "sources": sources}


# ---------------------------------------------------------------------------
# P3-17：放宽重写（限 1 次）
# ---------------------------------------------------------------------------


async def rewrite_once_node(state: RagSubState) -> dict[str, Any]:
    """放宽重写一次：口语化/省略问句改写为规范检索问句，并清空旧召回结果。"""
    writer = _get_writer()
    query = str(state.get("query") or "")
    attempts = int(state.get("rag_attempts", 0) or 0) + 1
    rewritten = ""
    error = ""
    try:
        response = await asyncio.to_thread(
            create_model().invoke,
            [
                SystemMessage(content=RAG_REWRITE_PROMPT),
                HumanMessage(content=query),
            ],
        )
        rewritten = str(getattr(response, "content", "") or "").strip()
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    if not rewritten:
        # 模型不可用时不改变查询意图，直接用原问句再召回一次
        rewritten = query
        error = error or "empty rewrite, fallback to original query"

    writer(
        {
            "type": "rag_rewrite",
            "original_query": query,
            "rewritten_query": rewritten,
            "attempts": attempts,
            "error": error,
        }
    )
    return {
        "query": rewritten,
        "rewritten_query": rewritten,
        "rag_attempts": attempts,
        "bm25_hits": [],
        "dense_hits": [],
        "fused_hits": [],
        "reranked_hits": [],
    }


# ---------------------------------------------------------------------------
# P3-18：四层组装生成答案
# ---------------------------------------------------------------------------


async def generate_node(state: RagSubState) -> dict[str, Any]:
    """系统 → 记忆 → 证据 → 问题 四层组装，生成带来源编号的答案。"""
    writer = _get_writer()
    evidence = list(state.get("evidence") or [])
    query = str(state.get("original_query") or state.get("query") or "")
    messages = [
        SystemMessage(content=RAG_GENERATE_SYSTEM_PROMPT),
        HumanMessage(content=_build_generation_payload(query, evidence, state)),
    ]
    answer = ""
    error = ""
    try:
        response = await asyncio.to_thread(create_model().invoke, messages)
        answer = str(getattr(response, "content", "") or "").strip()
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    if not answer:
        answer = RAG_GENERATE_ERROR_REPLY

    writer(
        {
            "type": "rag_answer",
            "sources_count": len(evidence),
            "answer_preview": answer[:200],
            "error": error,
        }
    )
    return {"answer": answer}


def _build_generation_payload(
    query: str, evidence: list[dict[str, Any]], state: RagSubState
) -> str:
    sections: list[str] = []
    session_context = str(state.get("session_context") or "").strip()
    if session_context:
        sections.append("【会话记忆（近期对话，仅供理解指代，勿泄露原始 JSON）】\n" + session_context)
    evidence_lines = []
    for item in evidence:
        evidence_lines.append(
            f"[{item['source_no']}] 来源：{item['doc_source']}\n{item['text']}"
        )
    sections.append("【检索到的知识库证据（仅可依据以下内容作答）】\n" + "\n\n".join(evidence_lines))
    sections.append("【用户问题】\n" + query)
    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# P3-19：无证据兜底
# ---------------------------------------------------------------------------


async def rag_fallback_node(state: RagSubState) -> dict[str, Any]:
    """放宽重写后仍无证据：回兜底话术并回传 fallback 原因。"""
    reason = str(state.get("gate_reason") or "rag_no_evidence")
    _get_writer()(
        {
            "type": "rag_fallback",
            "reason": reason,
            "attempts": int(state.get("rag_attempts", 0) or 0),
            "query": str(state.get("original_query") or state.get("query") or ""),
        }
    )
    return {
        "answer": RAG_FALLBACK_REPLY,
        "fallback_reason": "rag_no_evidence",
        "sources": [],
        "evidence": [],
    }


# ---------------------------------------------------------------------------


def _hit_summaries(
    hits: list[dict[str, Any]], *, score_key: str, include_prob: bool = False
) -> list[dict[str, Any]]:
    summaries = []
    for hit in hits:
        item = {
            "child_id": hit.get("child_id", ""),
            "doc_source": hit.get("doc_source", ""),
            "snippet": str(hit.get("text", ""))[:SNIPPET_CHARS],
            score_key: round(float(hit.get(score_key, 0.0)), 4),
        }
        if include_prob:
            item["rerank_prob"] = round(float(hit.get("rerank_prob", 0.0)), 4)
        summaries.append(item)
    return summaries


def _get_writer():
    try:
        return get_stream_writer()
    except RuntimeError:
        return lambda _: None
