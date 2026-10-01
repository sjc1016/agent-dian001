"""阶段 3：RAG 快速检索引擎（LangGraph 子图）。

- 离线入库：:mod:`mokioclaw.rag.parsing`（Docling 解析 + 父子分片）、
  :mod:`mokioclaw.rag.ingest`（BGE-M3 向量化入 Milvus Lite + SQLite 元信息）。
- 在线检索：:func:`mokioclaw.rag.workflow.build_rag_subgraph` 编译的子图，
  节点定义见 :mod:`mokioclaw.rag.nodes`，状态见 :mod:`mokioclaw.rag.state`。
"""

from __future__ import annotations

from mokioclaw.rag.state import Evidence, Hit, RagSubState
from mokioclaw.rag.workflow import build_rag_subgraph

__all__ = ["Evidence", "Hit", "RagSubState", "build_rag_subgraph"]
