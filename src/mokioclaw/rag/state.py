"""P3-1：RAG 检索子图独立状态（与主图 MokioGraphState 解耦）。

主图只把"重写后的问句"传入子图、把"答案 + 来源"接回；
两路召回中间结果（bm25_hits / dense_hits / fused_hits / reranked_hits）
全部留在子图状态里，并通过 custom 事件写入 trace。
"""

from __future__ import annotations

from typing import TypedDict


class Hit(TypedDict, total=False):
    """单条 child 粒度召回结果；各阶段分数逐级追加。"""

    child_id: str
    parent_id: str
    doc_source: str
    position: int
    text: str
    # 召回阶段
    bm25_rank: int
    dense_rank: int
    dense_score: float
    bm25_score: float
    # RRF 融合阶段
    rrf_score: float
    # 精排阶段
    rerank_score: float
    rerank_prob: float


class Evidence(TypedDict, total=False):
    """parent_lookup 回溯+邻域扩展后的父分片证据。"""

    parent_id: str
    doc_source: str
    position: int  # 该父分片内首个 child 的全局位置（排序用）
    text: str  # 父分片完整文本（子句拼接，无断句）
    child_ids: list[str]  # 组成该父分片的 child（含邻域扩展）
    matched_child_ids: list[str]  # 真正命中的 child
    source_no: int  # 生成答案时引用的来源编号


class RagSubState(TypedDict, total=False):
    # 输入
    query: str  # 本轮实际检索用问句（首轮=主图传入，重写后被替换）
    original_query: str  # 用户原始问句（生成节点展示用）
    rewritten_query: str  # rewrite_once 产出的放宽问句
    session_context: str  # 记忆层（短期会话窗口 JSON，阶段 5 扩展为四层组装）
    # 并行两路召回
    bm25_hits: list[Hit]
    dense_hits: list[Hit]
    # 融合 / 精排
    fused_hits: list[Hit]
    reranked_hits: list[Hit]
    # 证据门与回溯
    gate_decision: str  # parent_lookup / rewrite / fallback
    gate_reason: str
    evidence: list[Evidence]
    # 输出
    answer: str
    sources: list[dict]  # 与主图 SourceItem 结构对齐（title/url/content/score）
    fallback_reason: str
    # 控制
    rag_attempts: int  # 已执行的放宽重写次数（最多 1 次）
