"""阶段 3：RAG 快速检索引擎（LangGraph 子图）。

- 离线入库：:mod:`congclaw.rag.parsing`（Docling 解析 + 父子分片）、
  :mod:`congclaw.rag.ingest`（BGE-M3 向量化入 Milvus Lite + SQLite 元信息）。
- 在线检索：:func:`congclaw.rag.workflow.build_rag_subgraph` 编译的子图，
  节点定义见 :mod:`congclaw.rag.nodes`，状态见 :mod:`congclaw.rag.state`。
"""

from __future__ import annotations

from congclaw.rag.state import Evidence, Hit, RagSubState
from congclaw.rag.workflow import build_rag_subgraph

__all__ = ["Evidence", "Hit", "RagSubState", "build_rag_subgraph"]
