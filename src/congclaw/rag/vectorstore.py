"""P3-3：Milvus Lite 本地向量库封装（child 粒度向量 collection）。

- 单文件模式（免部署服务端），集合 ``telecom_kb_chunks``：int64 主键
  （child_id 的确定性哈希，支持幂等重灌）+ 1024 维向量 + child_id/doc_source 标量。
- COSINE 度量，配合 BGE-M3 的 L2 归一化向量。
- 所有方法为同步客户端调用，在线侧通过 ``asyncio.to_thread`` 包装。
"""

from __future__ import annotations

import hashlib
import threading
from typing import Any

from congclaw.rag import config


def child_id_to_pk(child_id: str) -> int:
    """child_id → 有符号 int64 主键（md5 前 8 字节，确定性、可重复入库）。"""
    digest = hashlib.md5(child_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


class ChunkVectorStore:
    def __init__(self) -> None:
        self._client = None
        self._lock = threading.Lock()

    def _ensure(self):
        if self._client is None:
            with self._lock:
                if self._client is None:
                    from pymilvus import DataType, MilvusClient

                    db_path = config.milvus_db_path()
                    db_path.parent.mkdir(parents=True, exist_ok=True)
                    client = MilvusClient(uri=str(db_path))
                    if client.has_collection(config.COLLECTION_NAME):
                        # Milvus Lite 新客户端连接已存在集合时处于 released 状态，需显式 load
                        client.load_collection(config.COLLECTION_NAME)
                    else:
                        schema = client.create_schema(
                            auto_id=False, enable_dynamic_field=False
                        )
                        schema.add_field("id", DataType.INT64, is_primary=True)
                        schema.add_field(
                            "vector", DataType.FLOAT_VECTOR, dim=config.EMBED_DIM
                        )
                        schema.add_field("child_id", DataType.VARCHAR, max_length=256)
                        schema.add_field("doc_source", DataType.VARCHAR, max_length=512)
                        index_params = client.prepare_index_params()
                        index_params.add_index(
                            field_name="vector",
                            index_type="AUTOINDEX",
                            metric_type="COSINE",
                        )
                        client.create_collection(
                            config.COLLECTION_NAME,
                            schema=schema,
                            index_params=index_params,
                        )
                    self._client = client
        return self._client

    def upsert_chunks(self, records: list[dict[str, Any]]) -> int:
        """写入/覆盖 child 向量记录（record: child_id/doc_source/vector）。"""
        if not records:
            return 0
        rows = [
            {
                "id": child_id_to_pk(str(record["child_id"])),
                "vector": list(record["vector"]),
                "child_id": str(record["child_id"]),
                "doc_source": str(record["doc_source"]),
            }
            for record in records
        ]
        client = self._ensure()
        result = client.upsert(config.COLLECTION_NAME, rows)
        client.flush(config.COLLECTION_NAME)
        return int(result.get("upsert_count", len(rows)))

    def search(
        self, query_vector: list[float], top_k: int
    ) -> list[dict[str, Any]]:
        """稠密语义检索，返回 [{child_id, doc_source, score}]，按相似度降序。"""
        client = self._ensure()
        batches = client.search(
            config.COLLECTION_NAME,
            data=[list(query_vector)],
            limit=top_k,
            output_fields=["child_id", "doc_source"],
        )
        hits = []
        for hit in (batches[0] if batches else []):
            entity = hit.get("entity", {})
            hits.append(
                {
                    "child_id": entity.get("child_id", ""),
                    "doc_source": entity.get("doc_source", ""),
                    "score": float(hit.get("distance", 0.0)),
                }
            )
        return hits

    def delete_by_source(self, doc_source: str) -> None:
        """按文档来源删除全部向量（重新入库前去重）。"""
        client = self._ensure()
        safe_source = doc_source.replace('"', "")
        client.delete(
            config.COLLECTION_NAME, filter=f'doc_source == "{safe_source}"'
        )
        client.flush(config.COLLECTION_NAME)

    def count(self) -> int:
        client = self._ensure()
        stats = client.get_collection_stats(config.COLLECTION_NAME)
        return int(stats.get("row_count", 0))

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None


_store: ChunkVectorStore | None = None
_store_lock = threading.Lock()


def get_vector_store() -> ChunkVectorStore:
    """进程内向量库单例。"""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = ChunkVectorStore()
    return _store


def reset_vector_store() -> None:
    """关闭并释放单例（测试/换库路径时使用）。"""
    global _store
    with _store_lock:
        if _store is not None:
            _store.close()
        _store = None
