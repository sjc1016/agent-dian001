"""P0-9 验证脚本：Milvus Lite 本地文件模式读写。

运行：uv run python scripts/verify_milvus_lite.py

流程：在 data/milvus_lite.db 创建本地集合 -> 写入 1 条向量 -> 按 id 读回 -> 向量检索命中。
零外部服务，数据全部落在本地文件。

注意：当项目绝对路径含非 ASCII 字符（如中文目录）时，faiss 在后台持久化 HNSW 索引
可能打印 "could not open ... for writing" 的非致命报错；不影响写入、按 id 读回与检索。
如需彻底无告警，可把数据库放到纯英文路径，例如：
    MILVUS_DB_PATH=C:/mokio_data/milvus_lite.db uv run python scripts/verify_milvus_lite.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 允许直接以脚本方式运行（scripts/ 不在包内）
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pymilvus import MilvusClient  # noqa: E402

COLLECTION = "p09_smoke"
DIMENSION = 4
DB_PATH = Path(os.getenv("MILVUS_DB_PATH", str(PROJECT_ROOT / "data" / "milvus_lite.db")))


def main() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    client = MilvusClient(uri=str(DB_PATH))
    try:
        if client.has_collection(collection_name=COLLECTION):
            client.drop_collection(collection_name=COLLECTION)
        client.create_collection(collection_name=COLLECTION, dimension=DIMENSION)

        vector = [0.1, 0.2, 0.3, 0.4]
        client.insert(
            collection_name=COLLECTION,
            data=[{"id": 1, "vector": vector, "tag": "hello-milvus-lite"}],
        )
        client.flush(collection_name=COLLECTION)

        read_back = client.get(collection_name=COLLECTION, ids=[1])
        results = client.search(
            collection_name=COLLECTION,
            data=[vector],
            limit=1,
            output_fields=["tag"],
        )
        hit = results[0][0]

        print(f"[Milvus Lite] 数据库文件: {DB_PATH}")
        print(f"[Milvus Lite] 读回记录: {read_back[0]}")
        print(
            "[Milvus Lite] 检索命中: "
            f"id={hit['id']} tag={hit['entity'].get('tag')} distance={hit['distance']}"
        )
        assert int(hit["id"]) == 1, "向量检索未命中写入的记录"
        print("[Milvus Lite] 验证通过：本地写入 -> 读回/检索成功")
    finally:
        client.close()


if __name__ == "__main__":
    main()
