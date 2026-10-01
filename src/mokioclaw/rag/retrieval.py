"""在线检索服务层：把 BM25 / 稠密向量 / Cross-Encoder / 父子回溯封装为 async 函数。

子图节点（:mod:`mokioclaw.rag.nodes`）只依赖本模块的函数签名，
单测可直接 monkeypatch ``mokioclaw.rag.nodes`` 命名空间里的同名导入。
"""

from __future__ import annotations

import asyncio
import math
from typing import Any

from mokioclaw.rag import bm25_index, config, embedding, store, vectorstore


def sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, float(value)))))


async def bm25_search(query: str, top_k: int) -> list[dict[str, Any]]:
    """关键词召回，返回 Hit 字典列表（含 child 文本与元信息）。"""
    results = await bm25_index.get_bm25_index().search(query, top_k)
    hits: list[dict[str, Any]] = []
    for item in results:
        row = item["row"]
        hits.append(
            {
                "child_id": row["child_id"],
                "parent_id": row["parent_id"],
                "doc_source": row["doc_source"],
                "position": row["position"],
                "text": row["child_text"],
                "bm25_score": float(item["score"]),
                "bm25_rank": int(item["rank"]),
            }
        )
    return hits


async def dense_search(query: str, top_k: int) -> list[dict[str, Any]]:
    """BGE-M3 向量 + Milvus 稠密语义召回。"""
    query_vector = await asyncio.to_thread(embedding.embed, query)
    raw_hits = await asyncio.to_thread(
        vectorstore.get_vector_store().search, list(query_vector), top_k
    )
    rows = await store.get_chunks([hit["child_id"] for hit in raw_hits])
    hits: list[dict[str, Any]] = []
    rank = 0
    for raw in raw_hits:
        row = rows.get(raw["child_id"])
        if row is None:
            continue  # Milvus 残留向量（元信息已删）直接剔除
        rank += 1
        hits.append(
            {
                "child_id": row["child_id"],
                "parent_id": row["parent_id"],
                "doc_source": row["doc_source"],
                "position": row["position"],
                "text": row["child_text"],
                "dense_score": float(raw["score"]),
                "dense_rank": rank,
            }
        )
    return hits


async def rerank_hits(query: str, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cross-Encoder 精排：为每条 hit 附加原始分与 sigmoid 归一化分，按分数降序。"""
    if not hits:
        return []
    pairs = [(query, str(hit.get("text", ""))) for hit in hits]
    scores = await asyncio.to_thread(embedding.rerank_scores, pairs)
    scored = []
    for hit, score in zip(hits, scores):
        enriched = dict(hit)
        enriched["rerank_score"] = float(score)
        enriched["rerank_prob"] = sigmoid(float(score))
        scored.append(enriched)
    scored.sort(key=lambda item: item["rerank_score"], reverse=True)
    return scored


async def build_parent_evidence(
    ordered_child_ids: list[str],
) -> list[dict[str, Any]]:
    """父子回溯 + 邻域扩展（参数取自 config）。"""
    return await store.fetch_parent_groups(
        ordered_child_ids,
        neighbor=config.NEIGHBOR_CHILDREN,
        parent_max=config.PARENT_MAX,
    )


async def corpus_stats() -> dict[str, Any]:
    """知识库规模统计（SQLite 元信息 + Milvus 向量行数）。"""
    chunk_count = await store.count_chunks()
    sources = await store.list_sources()
    milvus_count = await asyncio.to_thread(vectorstore.get_vector_store().count)
    return {
        "chunk_count": chunk_count,
        "milvus_count": milvus_count,
        "sources": sources,
    }
