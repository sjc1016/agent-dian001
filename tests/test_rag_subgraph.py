"""阶段 3 验收：RAG LangGraph 子图。

所有外部依赖（BM25 / Milvus / CrossEncoder / LLM）均在 ``mokioclaw.rag.nodes``
命名空间打桩，测试只验证图拓扑、融合/门控/回退逻辑与事件序列，不加载本地模型。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from mokioclaw.db import init_db
from mokioclaw.graph.workflow import build_entry_workflow
from mokioclaw.prompts.rag import RAG_FALLBACK_REPLY
from mokioclaw.rag import nodes as rag_nodes
from mokioclaw.rag.store import ChunkRow, fetch_parent_groups, replace_doc_chunks
from mokioclaw.rag.workflow import build_rag_subgraph


# ---------------------------------------------------------------------------
# 打桩工具
# ---------------------------------------------------------------------------


def _hit(child_id: str, **extra: Any) -> dict[str, Any]:
    hit: dict[str, Any] = {
        "child_id": child_id,
        "parent_id": f"doc#P00{child_id[-1]}",
        "doc_source": "资费说明.md",
        "position": int(child_id[-1]),
        "text": f"{child_id} 的正文片段",
    }
    hit.update(extra)
    return hit


def _parent_group(text: str = "父分片完整文本，无断句。") -> dict[str, Any]:
    return {
        "parent_id": "doc#P001",
        "doc_source": "资费说明.md",
        "position": 1,
        "text": text,
        "child_ids": ["c1", "c2"],
        "matched_child_ids": ["c1"],
    }


class _FakeRagModel:
    """同时服务 rewrite 与 generate：按系统提示区分调用场景。"""

    def __init__(self, rewrite: str = "规范的检索问句", answer: str = "199 元档含 100GB 流量[1]。"):
        self._rewrite = rewrite
        self._answer = answer
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        system = str(messages[0].content)
        if "改写" in system:
            return AIMessage(content=self._rewrite)
        return AIMessage(content=self._answer)

    async def ainvoke(self, messages, **kwargs):
        return self.invoke(messages)


def _patch_hit_path(
    monkeypatch,
    *,
    bm25_hits: list[dict[str, Any]] | None = None,
    dense_hits: list[dict[str, Any]] | None = None,
    rerank_prob: float = 0.9,
    rerank_raw: float = 2.0,
    groups: list[dict[str, Any]] | None = None,
    model: _FakeRagModel | None = None,
) -> _FakeRagModel:
    """打桩一条完整的"命中→生成"链路。"""
    bm25_hits = bm25_hits if bm25_hits is not None else [_hit("c1", bm25_rank=1, bm25_score=10.0)]
    dense_hits = dense_hits if dense_hits is not None else [
        _hit("c1", dense_rank=1, dense_score=0.7),
        _hit("c2", dense_rank=2, dense_score=0.6),
    ]
    groups = groups if groups is not None else [_parent_group()]
    model = model or _FakeRagModel()

    async def fake_bm25(query: str, top_k: int):
        return [dict(h) for h in bm25_hits]

    async def fake_dense(query: str, top_k: int):
        return [dict(h) for h in dense_hits]

    async def fake_rerank(query: str, hits: list[dict[str, Any]]):
        scored = [dict(h) for h in hits]
        for index, hit in enumerate(scored):
            hit["rerank_score"] = rerank_raw - index
            hit["rerank_prob"] = max(0.01, rerank_prob - index * 0.1)
        scored.sort(key=lambda h: h["rerank_score"], reverse=True)
        return scored

    async def fake_parents(ordered_ids: list[str]):
        return groups

    monkeypatch.setattr(rag_nodes, "bm25_search", fake_bm25)
    monkeypatch.setattr(rag_nodes, "dense_search", fake_dense)
    monkeypatch.setattr(rag_nodes, "rerank_hits", fake_rerank)
    monkeypatch.setattr(rag_nodes, "build_parent_evidence", fake_parents)
    monkeypatch.setattr(rag_nodes, "create_model", lambda: model)
    return model


def _patch_empty_path(monkeypatch, model: _FakeRagModel | None = None) -> _FakeRagModel:
    """打桩一条永远召不回任何证据的链路（用于重写/兜底测试）。"""
    model = model or _FakeRagModel()

    async def empty_search(query: str, top_k: int):
        return []

    async def empty_rerank(query: str, hits: list[dict[str, Any]]):
        return []

    async def empty_parents(ordered_ids: list[str]):
        return []

    monkeypatch.setattr(rag_nodes, "bm25_search", empty_search)
    monkeypatch.setattr(rag_nodes, "dense_search", empty_search)
    monkeypatch.setattr(rag_nodes, "rerank_hits", empty_rerank)
    monkeypatch.setattr(rag_nodes, "build_parent_evidence", empty_parents)
    monkeypatch.setattr(rag_nodes, "create_model", lambda: model)
    return model


def _drive(graph, payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
    """用 astream 驱动 async 子图，收集最终状态、节点序列与 custom 事件。"""

    async def run():
        final_state: dict[str, Any] = {}
        sequence: list[str] = []
        customs: list[dict[str, Any]] = []
        async for mode, chunk in graph.astream(payload, stream_mode=["updates", "custom"]):
            if mode == "custom":
                customs.append(chunk)
            elif isinstance(chunk, dict):
                for node_name, update in chunk.items():
                    sequence.append(str(node_name))
                    if isinstance(update, dict):
                        final_state.update(update)
        return final_state, sequence, customs

    return asyncio.run(run())


# ---------------------------------------------------------------------------
# P3-13：RRF 融合
# ---------------------------------------------------------------------------


def test_rrf_fusion_scores_and_order() -> None:
    state = {
        "bm25_hits": [
            _hit("c1", bm25_rank=1),
            _hit("c2", bm25_rank=2),
        ],
        "dense_hits": [
            _hit("c2", dense_rank=1),
            _hit("c3", dense_rank=2),
        ],
    }

    result = asyncio.run(rag_nodes.rrf_fusion_node(state))
    fused = result["fused_hits"]
    scores = {hit["child_id"]: hit["rrf_score"] for hit in fused}

    # c2 两路都命中且排名靠前 → 第一；c1 仅 BM25 rank1 → 第二；c3 仅 dense rank2 → 第三
    assert [hit["child_id"] for hit in fused] == ["c2", "c1", "c3"]
    assert scores["c2"] == pytest.approx(1 / 61 + 1 / 62)
    assert scores["c1"] == pytest.approx(1 / 61)
    assert scores["c3"] == pytest.approx(1 / 62)


def test_rrf_fusion_single_channel_and_missing_rank() -> None:
    """单路召回可用；另一路缺 rank 的命中只计一路分数。"""
    result = asyncio.run(
        rag_nodes.rrf_fusion_node(
            {
                "bm25_hits": [_hit("c1", bm25_rank=1)],
                "dense_hits": [_hit("c1"), _hit("c2")],  # 无 dense_rank
            }
        )
    )
    fused = {hit["child_id"]: hit for hit in result["fused_hits"]}
    assert fused["c1"]["rrf_score"] == pytest.approx(1 / 61)
    assert fused["c2"]["rrf_score"] == pytest.approx(0.0)


def test_rrf_fusion_empty_channels() -> None:
    result = asyncio.run(rag_nodes.rrf_fusion_node({"bm25_hits": [], "dense_hits": []}))
    assert result["fused_hits"] == []


# ---------------------------------------------------------------------------
# P3-15：证据门三路由
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hits", "attempts", "decision", "reason_prefix"),
    [
        ([{"rerank_prob": 0.9, "rerank_score": 2.0}], 0, "parent_lookup", "evidence_hit"),
        ([{"rerank_prob": 0.1, "rerank_score": -20.0}], 0, "rewrite", "low_confidence"),
        ([], 0, "rewrite", "no_evidence"),
        ([{"rerank_prob": 0.1, "rerank_score": -20.0}], 1, "fallback", "low_confidence_after"),
        ([], 1, "fallback", "no_evidence_after"),
    ],
)
def test_evidence_gate_routes(hits, attempts, decision, reason_prefix) -> None:
    result = asyncio.run(
        rag_nodes.evidence_gate_node({"reranked_hits": hits, "rag_attempts": attempts})
    )
    assert result["gate_decision"] == decision
    assert result["gate_reason"].startswith(reason_prefix)
    assert rag_nodes.evidence_gate_route(result) == decision


# ---------------------------------------------------------------------------
# 端到端子图路径
# ---------------------------------------------------------------------------


def test_subgraph_hit_path_runs_full_sequence_and_events(monkeypatch) -> None:
    """命中路径：双路并行召回 → 融合 → 精排 → 证据门 → 父子回溯 → 生成。"""
    model = _patch_hit_path(monkeypatch)

    final_state, sequence, customs = _drive(
        build_rag_subgraph(),
        {"query": "199套餐多少流量", "original_query": "199套餐多少流量", "rag_attempts": 0},
    )

    assert final_state["answer"] == "199 元档含 100GB 流量[1]。"
    assert len(final_state["sources"]) == 1
    assert final_state["sources"][0]["title"] == "资费说明.md"
    assert final_state["evidence"][0]["source_no"] == 1
    assert final_state["evidence"][0]["matched_child_ids"] == ["c1"]
    # 生成模型只调一次（无重写）
    assert model.calls == 1

    # 屏障语义：融合节点必须在两路召回之后，且都只出现一次
    assert sequence.index("retrieve_bm25") < sequence.index("rrf_fusion")
    assert sequence.index("retrieve_dense") < sequence.index("rrf_fusion")
    assert sequence == [
        "retrieve_bm25",
        "retrieve_dense",
        "rrf_fusion",
        "rerank",
        "evidence_gate",
        "parent_lookup",
        "generate",
    ]

    event_types = [event["type"] for event in customs]
    assert event_types == [
        "rag_retrieve",
        "rag_retrieve",
        "rag_fusion",
        "rag_rerank",
        "rag_gate",
        "rag_parent_lookup",
        "rag_answer",
    ]
    channels = {event["channel"] for event in customs if event["type"] == "rag_retrieve"}
    assert channels == {"bm25", "dense"}
    gate_event = next(event for event in customs if event["type"] == "rag_gate")
    assert gate_event["decision"] == "parent_lookup"


def test_subgraph_rewrite_once_then_hit(monkeypatch) -> None:
    """首轮无证据 → rewrite_once（限 1 次）→ 重新 fan-out 召回 → 命中生成。"""
    calls = {"search": 0}

    async def rotating_search(query: str, top_k: int):
        calls["search"] += 1
        if calls["search"] <= 2:  # 首轮 bm25+dense 都为空
            return []
        return [_hit("c1", dense_rank=1, bm25_rank=1, dense_score=0.7, bm25_score=9.0)]

    async def rerank(query: str, hits: list[dict[str, Any]]):
        for hit in hits:
            hit["rerank_score"] = 1.5
            hit["rerank_prob"] = 0.85
        return hits

    async def parents(ordered_ids: list[str]):
        return [_parent_group()]

    model = _FakeRagModel(rewrite="中国电信199元5G套餐包含多少流量")
    monkeypatch.setattr(rag_nodes, "bm25_search", rotating_search)
    monkeypatch.setattr(rag_nodes, "dense_search", rotating_search)
    monkeypatch.setattr(rag_nodes, "rerank_hits", rerank)
    monkeypatch.setattr(rag_nodes, "build_parent_evidence", parents)
    monkeypatch.setattr(rag_nodes, "create_model", lambda: model)

    final_state, sequence, customs = _drive(
        build_rag_subgraph(),
        {"query": "那个多少钱", "original_query": "那个多少钱", "rag_attempts": 0},
    )

    assert final_state["rag_attempts"] == 1
    assert final_state["rewritten_query"] == "中国电信199元5G套餐包含多少流量"
    assert final_state["answer"] == "199 元档含 100GB 流量[1]。"
    # 重写后重新 fan-out：每路召回恰好执行两次
    assert sequence.count("retrieve_bm25") == 2
    assert sequence.count("retrieve_dense") == 2
    assert sequence.count("rewrite_once") == 1
    assert sequence[-1] == "generate"
    assert calls["search"] == 4
    # 模型第一次调用是重写、第二次是生成
    assert model.calls == 2
    rewrite_event = next(event for event in customs if event["type"] == "rag_rewrite")
    assert rewrite_event["attempts"] == 1


def test_subgraph_fallback_after_rewrite_still_empty(monkeypatch) -> None:
    """库外问题：重写一次后仍无证据 → 静态兜底话术 + fallback_reason。"""
    model = _patch_empty_path(monkeypatch)

    final_state, sequence, customs = _drive(
        build_rag_subgraph(),
        {"query": "今天天气怎么样", "original_query": "今天天气怎么样", "rag_attempts": 0},
    )

    assert final_state["answer"] == RAG_FALLBACK_REPLY
    assert final_state["fallback_reason"] == "rag_no_evidence"
    assert final_state["sources"] == []
    assert final_state["rag_attempts"] == 1
    assert sequence.count("rewrite_once") == 1
    assert sequence[-1] == "rag_fallback"
    # 没有第三次召回（重写严格限 1 次，不会死循环）
    assert sequence.count("retrieve_bm25") == 2
    fallback_event = next(event for event in customs if event["type"] == "rag_fallback")
    assert fallback_event["reason"] == "no_evidence_after_rewrite"


# ---------------------------------------------------------------------------
# 节点级降级
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("channel", ["bm25", "dense"])
def test_retrieve_node_degrades_to_empty_on_error(monkeypatch, channel: str) -> None:
    async def boom(query: str, top_k: int):
        raise RuntimeError("index unavailable")

    monkeypatch.setattr(rag_nodes, "bm25_search" if channel == "bm25" else "dense_search", boom)
    node = rag_nodes.retrieve_bm25_node if channel == "bm25" else rag_nodes.retrieve_dense_node

    result = asyncio.run(node({"query": "x", "rag_attempts": 0}))

    assert result[f"{channel}_hits"] == []


def test_rerank_node_falls_back_to_rrf_order(monkeypatch) -> None:
    async def boom(query: str, hits: list[dict[str, Any]]):
        raise RuntimeError("reranker down")

    monkeypatch.setattr(rag_nodes, "rerank_hits", boom)
    fused = [_hit("c1", rrf_score=0.03), _hit("c2", rrf_score=0.02)]

    result = asyncio.run(rag_nodes.rerank_node({"query": "q", "fused_hits": fused}))

    reranked = result["reranked_hits"]
    assert [hit["child_id"] for hit in reranked] == ["c1", "c2"]
    assert all(hit["rerank_prob"] == 1.0 for hit in reranked)


def test_fetch_parent_groups_neighborhood_and_dedup(tmp_path, monkeypatch) -> None:
    """真实 SQLite：命中 child 回溯父分片 + 邻域扩展跨父边界 + 按命中序去重。"""
    db_path = tmp_path / "rag-store.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    asyncio.run(init_db())

    rows = [
        # doc-a：P1 覆盖 position 1-3，P2 覆盖 4-6
        ChunkRow("a1", "doc-a#P1", "a.md", 1, "甲"),
        ChunkRow("a2", "doc-a#P1", "a.md", 2, "乙"),
        ChunkRow("a3", "doc-a#P1", "a.md", 3, "丙"),
        ChunkRow("a4", "doc-a#P2", "a.md", 4, "丁"),
        ChunkRow("a5", "doc-a#P2", "a.md", 5, "戊"),
        # doc-b：独立文档，不应被 doc-a 的邻域扩展牵连
        ChunkRow("b1", "doc-b#P1", "b.md", 1, "子"),
    ]
    asyncio.run(replace_doc_chunks("a.md", rows[:5]))
    asyncio.run(replace_doc_chunks("b.md", rows[5:]))

    # 命中 a3（P1 末尾）与 a1：邻域 ±1 把 a4 所属的 P2 一并纳入；同父命中去重
    groups = asyncio.run(fetch_parent_groups(["a3", "a1"], neighbor=1, parent_max=3))

    assert [g["parent_id"] for g in groups] == ["doc-a#P1", "doc-a#P2"]
    assert groups[0]["text"] == "甲乙丙"
    assert groups[0]["matched_child_ids"] == ["a3", "a1"]
    assert groups[1]["text"] == "丁戊"
    assert groups[1]["matched_child_ids"] == []  # 纯邻域扩展带入
    assert all(g["doc_source"] == "a.md" for g in groups)  # 邻域不跨文档


def test_fetch_parent_groups_parent_max_and_empty(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "rag-store2.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    asyncio.run(init_db())
    rows = [
        ChunkRow(f"c{i}", f"d#P{i}", "d.md", i, f"段{i}")
        for i in range(1, 5)
    ]
    asyncio.run(replace_doc_chunks("d.md", rows))

    # parent_max=2：只保留按命中顺序的前两个父分片
    groups = asyncio.run(fetch_parent_groups(["c1", "c2", "c3", "c4"], neighbor=0, parent_max=2))
    assert [g["parent_id"] for g in groups] == ["d#P1", "d#P2"]
    # 未知 child_id → 空结果
    assert asyncio.run(fetch_parent_groups(["nope"], neighbor=0, parent_max=3)) == []


def test_parent_lookup_numbers_sources_and_respects_char_budget(monkeypatch) -> None:
    groups = [_parent_group("甲" * 900), _parent_group("乙" * 900),
              _parent_group("丙" * 900), _parent_group("丁" * 900)]
    _patch_hit_path(monkeypatch, groups=groups)

    result = asyncio.run(
        rag_nodes.parent_lookup_node(
            {"reranked_hits": [_hit("c1", rerank_score=1.0)]}
        )
    )

    assert [item["source_no"] for item in result["evidence"]] == [1, 2, 3, 4]
    # 总字数不超过证据预算（2000）：第三条截断到剩余 200 字，第四条预算耗尽置空
    assert sum(len(item["text"]) for item in result["evidence"]) == 2000
    assert len(result["evidence"][2]["text"]) == 200
    assert result["evidence"][3]["text"] == ""
    assert [source["title"] for source in result["sources"]] == ["资费说明.md"] * 4


# ---------------------------------------------------------------------------
# P3-21：主图分流进入子图
# ---------------------------------------------------------------------------


def test_main_graph_rag_query_routes_into_rag_subgraph(monkeypatch, tmp_path) -> None:
    """主图 rag_query → rag_answer 节点内挂载子图，custom 事件透传到主流。"""
    _patch_hit_path(monkeypatch)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "rag-main-test.db"))
    monkeypatch.setenv("SKILL_WATCH", "0")

    payload = json.dumps(
        {"category": "rag_query", "confidence": 0.95, "reason": "资费咨询", "missing_slots": []},
        ensure_ascii=False,
    )

    class FakeIntentModel:
        def invoke(self, messages):
            return AIMessage(content=payload)

        async def ainvoke(self, messages, **kwargs):
            return AIMessage(content=payload)

    monkeypatch.setattr("mokioclaw.graph.nodes.create_model", lambda: FakeIntentModel())

    async def run() -> tuple[dict[str, Any], list[str]]:
        updates: dict[str, Any] = {}
        rag_event_types: list[str] = []
        async for mode, chunk in build_entry_workflow().astream(
            {"task": "199套餐多少流量"}, stream_mode=["updates", "custom"]
        ):
            if mode == "updates" and isinstance(chunk, dict):
                updates.update(chunk)
            elif mode == "custom" and isinstance(chunk, dict) and str(chunk.get("type", "")).startswith("rag_"):
                rag_event_types.append(chunk["type"])
        return updates, rag_event_types

    updates, rag_event_types = asyncio.run(run())

    assert updates["intent_router"]["intent_route"] == "rag_answer"
    assert updates["rag_answer"]["final_answer"] == "199 元档含 100GB 流量[1]。"
    assert len(updates["rag_answer"]["sources"]) == 1
    # 子图事件完整透传：启动/双路召回/门控/生成/结束
    assert "rag_retrieve" in rag_event_types
    assert "rag_gate" in rag_event_types
    assert "rag_answer" in rag_event_types
    assert rag_event_types[0] == "rag_start"
    assert rag_event_types[-1] == "rag_finished"
