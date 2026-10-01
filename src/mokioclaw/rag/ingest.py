"""P3-8：离线入库管线。

Docling 解析 → 父子分片 → BGE-M3 批量向量化 → Milvus Lite 写向量 +
SQLite 写 chunk_meta 元信息/父子映射。按 doc_source 幂等：重新入库同一文档
会先删旧向量与旧元信息再写入。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

from mokioclaw.db.engine import init_db
from mokioclaw.rag import bm25_index, config, embedding, parsing, store, vectorstore
from mokioclaw.rag.parsing import SUPPORTED_SUFFIXES, ParsedDocument
from mokioclaw.rag.store import ChunkRow

_SOURCE_ID_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")


@dataclass
class IngestStats:
    source: str
    path: str
    parents: int
    children: int

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "path": self.path,
            "parents": self.parents,
            "children": self.children,
        }


@dataclass
class IngestBatch:
    stats: list[IngestStats]
    errors: list[dict[str, str]]

    @property
    def total_children(self) -> int:
        return sum(item.children for item in self.stats)

    def as_dict(self) -> dict:
        return {
            "ingested": [item.as_dict() for item in self.stats],
            "total_children": self.total_children,
            "errors": self.errors,
        }


def source_id_of(source: str) -> str:
    """文件名 → 安全的分片 id 前缀（保留中英文与数字）。"""
    stem = Path(source).stem
    cleaned = _SOURCE_ID_RE.sub("_", stem).strip("_")
    return cleaned or "doc"


def _build_chunk_rows(document: ParsedDocument) -> list[ChunkRow]:
    source_id = source_id_of(document.source)
    rows: list[ChunkRow] = []
    for parent in document.parents:
        parent_id = f"{source_id}#P{parent.parent_index:03d}"
        for offset, child in enumerate(parent.children):
            child_id = f"{parent_id}-C{offset:02d}"
            rows.append(
                ChunkRow(
                    child_id=child_id,
                    parent_id=parent_id,
                    doc_source=document.source,
                    position=child.position,
                    child_text=child.text,
                )
            )
    return rows


async def ingest_file(
    path: str | Path, *, source: str | None = None
) -> IngestStats:
    """解析并入库单个文档（幂等）。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"知识文档不存在：{path}")
    doc_source = source or path.name
    await init_db()

    document = parsing.parse_and_chunk(path, source=doc_source)
    if document.child_count == 0:
        raise ValueError(f"文档解析后没有可用分片：{path}")
    rows = _build_chunk_rows(document)

    # 幂等：先清掉同来源旧数据（Milvus 向量 + SQLite 元信息）
    await asyncio.to_thread(vectorstore.get_vector_store().delete_by_source, doc_source)
    await store.replace_doc_chunks(doc_source, rows)

    # 批量向量化（CPU，to_thread 不阻塞事件循环）
    texts = [row.child_text for row in rows]
    vectors: list = []
    batch_size = config.EMBED_BATCH_SIZE
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        batch_vectors = await asyncio.to_thread(embedding.embed, batch)
        vectors.extend(batch_vectors)

    records = [
        {
            "child_id": row.child_id,
            "doc_source": row.doc_source,
            "vector": vector,
        }
        for row, vector in zip(rows, vectors)
    ]
    await asyncio.to_thread(vectorstore.get_vector_store().upsert_chunks, records)
    bm25_index.invalidate()  # 强制下次在线检索重建关键词索引

    return IngestStats(
        source=doc_source,
        path=str(path),
        parents=len(document.parents),
        children=len(rows),
    )


async def ingest_directory(directory: str | Path | None = None) -> IngestBatch:
    """入库目录下全部受支持文档（按文件名排序，单文档失败不阻断整批）。"""
    target = Path(directory) if directory else config.knowledge_dir()
    if not target.exists():
        raise FileNotFoundError(f"知识库目录不存在：{target}")
    files = sorted(
        path
        for path in target.rglob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
    )
    stats: list[IngestStats] = []
    errors: list[dict[str, str]] = []
    for path in files:
        try:
            stats.append(await ingest_file(path))
        except Exception as exc:  # 单文档失败不阻断整库入库
            errors.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})
    return IngestBatch(stats=stats, errors=errors)
