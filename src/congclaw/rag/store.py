"""RAG 分片元信息 SQLite 仓储（chunk_meta 表的在线/离线路由）。

直接用 aiosqlite 短连接：演示场景读多写少，短连接天然规避"连接跨事件循环"
问题（主图工作线程每轮 asyncio.run 都会新建/关闭事件循环）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Sequence

import aiosqlite

from congclaw.db.engine import resolve_db_path


@dataclass(frozen=True)
class ChunkRow:
    child_id: str
    parent_id: str
    doc_source: str
    position: int
    child_text: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "child_id": self.child_id,
            "parent_id": self.parent_id,
            "doc_source": self.doc_source,
            "position": self.position,
            "child_text": self.child_text,
        }


def _connect() -> aiosqlite.core.Connection:
    return aiosqlite.connect(resolve_db_path())


async def replace_doc_chunks(doc_source: str, chunks: Sequence[ChunkRow]) -> int:
    """删除某文档旧分片后整批写入新分片（单事务，幂等重灌）。"""
    async with _connect() as connection:
        await connection.execute("PRAGMA journal_mode=WAL")
        await connection.execute(
            "DELETE FROM chunk_meta WHERE doc_source = ?", (doc_source,)
        )
        await connection.executemany(
            "INSERT INTO chunk_meta (child_id, parent_id, doc_source, position, child_text)"
            " VALUES (?, ?, ?, ?, ?)",
            [
                (
                    chunk.child_id,
                    chunk.parent_id,
                    chunk.doc_source,
                    chunk.position,
                    chunk.child_text,
                )
                for chunk in chunks
            ],
        )
        await connection.commit()
    return len(chunks)


async def delete_source(doc_source: str) -> int:
    async with _connect() as connection:
        await connection.execute("PRAGMA journal_mode=WAL")
        cursor = await connection.execute(
            "DELETE FROM chunk_meta WHERE doc_source = ?", (doc_source,)
        )
        deleted = cursor.rowcount
        await connection.commit()
    return int(deleted or 0)


async def load_all_chunks() -> list[dict[str, Any]]:
    """全量 child 元信息（BM25 建索引/结果 hydration 用），按来源与位置排序。"""
    async with _connect() as connection:
        connection.row_factory = aiosqlite.Row
        cursor = await connection.execute(
            "SELECT child_id, parent_id, doc_source, position, child_text"
            " FROM chunk_meta ORDER BY doc_source, position"
        )
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_chunks(child_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    """按 child_id 批量取元信息；缺失 id 自动剔除。"""
    ids = [cid for cid in dict.fromkeys(child_ids) if cid]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    async with _connect() as connection:
        connection.row_factory = aiosqlite.Row
        cursor = await connection.execute(
            f"SELECT child_id, parent_id, doc_source, position, child_text"
            f" FROM chunk_meta WHERE child_id IN ({placeholders})",
            ids,
        )
        rows = await cursor.fetchall()
    return {row["child_id"]: dict(row) for row in rows}


async def fetch_parent_groups(
    ordered_child_ids: Sequence[str],
    *,
    neighbor: int = 1,
    parent_max: int = 3,
) -> list[dict[str, Any]]:
    """父子回溯 + 邻域扩展 + 合并去重。

    1) 命中 child 定位所属 parent；
    2) 取这些 parent 的全部 child 还原父分片；
    3) 再取命中 child 前后 ``neighbor`` 个同文档邻域 child，把它们所属 parent
       一并纳入（跨父分片边界也不断句）；
    4) 按命中顺序去重，限制 parent 数量。
    """
    matched = await get_chunks(ordered_child_ids)
    if not matched:
        return []

    parent_ids = {row["parent_id"] for row in matched.values()}
    group_rows = await _rows_by_parents(parent_ids)

    # 邻域 child（同文档 position ± n）
    matched_sources = {row["doc_source"] for row in matched.values()}
    neighbor_positions: dict[str, set[int]] = {}
    for row in matched.values():
        bucket = neighbor_positions.setdefault(row["doc_source"], set())
        for offset in range(1, neighbor + 1):
            bucket.add(row["position"] - offset)
            bucket.add(row["position"] + offset)
    if neighbor_positions:
        extra_parents: set[str] = set()
        for source, positions in neighbor_positions.items():
            placeholders = ",".join("?" for _ in positions)
            params = [source, *positions]
            async with _connect() as connection:
                cursor = await connection.execute(
                    f"SELECT parent_id FROM chunk_meta"
                    f" WHERE doc_source = ? AND position IN ({placeholders})",
                    params,
                )
                for (parent_id,) in await cursor.fetchall():
                    extra_parents.add(parent_id)
        new_parents = extra_parents - parent_ids
        if new_parents:
            group_rows.extend(await _rows_by_parents(new_parents))

    groups_by_parent: dict[str, list[dict[str, Any]]] = {}
    for row in group_rows:
        groups_by_parent.setdefault(row["parent_id"], []).append(row)

    # 以命中 child 的先后顺序决定父分片顺序，去重；邻域引入的 parent 排其后
    ordered_parent_ids: list[str] = []
    for child_id in ordered_child_ids:
        row = matched.get(child_id)
        if row and row["parent_id"] not in ordered_parent_ids:
            ordered_parent_ids.append(row["parent_id"])
    for row in group_rows:
        if row["parent_id"] not in ordered_parent_ids:
            ordered_parent_ids.append(row["parent_id"])
    ordered_parent_ids = ordered_parent_ids[:parent_max]

    matched_by_parent: dict[str, list[str]] = {}
    for child_id in ordered_child_ids:
        row = matched.get(child_id)
        if row:
            matched_by_parent.setdefault(row["parent_id"], []).append(child_id)

    result = []
    for parent_id in ordered_parent_ids:
        rows = sorted(groups_by_parent[parent_id], key=lambda item: item["position"])
        result.append(
            {
                "parent_id": parent_id,
                "doc_source": rows[0]["doc_source"],
                "position": rows[0]["position"],
                "text": "".join(row["child_text"] for row in rows),
                "child_ids": [row["child_id"] for row in rows],
                "matched_child_ids": matched_by_parent.get(parent_id, []),
            }
        )
    return result


async def _rows_by_parents(parent_ids: set[str]) -> list[dict[str, Any]]:
    if not parent_ids:
        return []
    placeholders = ",".join("?" for _ in parent_ids)
    async with _connect() as connection:
        connection.row_factory = aiosqlite.Row
        cursor = await connection.execute(
            f"SELECT child_id, parent_id, doc_source, position, child_text"
            f" FROM chunk_meta WHERE parent_id IN ({placeholders})"
            f" ORDER BY doc_source, position",
            list(parent_ids),
        )
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def list_sources() -> list[dict[str, Any]]:
    async with _connect() as connection:
        cursor = await connection.execute(
            "SELECT doc_source, COUNT(*) AS chunk_count FROM chunk_meta"
            " GROUP BY doc_source ORDER BY doc_source"
        )
        rows = await cursor.fetchall()
    return [{"doc_source": source, "chunk_count": count} for source, count in rows]


async def count_chunks() -> int:
    async with _connect() as connection:
        cursor = await connection.execute("SELECT COUNT(*) FROM chunk_meta")
        (count,) = await cursor.fetchone()
    return int(count or 0)


async def describe_corpus() -> str:
    """调试用：返回语料规模摘要 JSON。"""
    return json.dumps(
        {"chunks": await count_chunks(), "sources": await list_sources()},
        ensure_ascii=False,
    )
