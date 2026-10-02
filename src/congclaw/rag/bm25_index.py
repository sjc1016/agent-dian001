"""P3-11 基础设施：中文友好的 BM25 关键词索引（rank_bm25）。

不引入额外分词依赖：中文按"单字 + 相邻二元组"切分（二元组保证"套餐/宽带/
漫游/话费"等词的区分度），英文/数字按词切分并小写化（保留"5G""10086"
"199 元档"等关键信息）。

索引为进程内懒加载单例，语料来自 SQLite ``chunk_meta`` 全量 child；
入库管线写入后调用 :func:`invalidate` 强制下次检索重建。
"""

from __future__ import annotations

import re
import threading

from rank_bm25 import BM25Okapi

from congclaw.rag import store

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:[.][0-9]+)?|[\u4e00-\u9fff]")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
_LATIN_RUN_RE = re.compile(r"[A-Za-z0-9]+(?:[.][0-9]+)?")


def tokenize(text: str) -> list[str]:
    """中文 unigram+bigram，拉丁/数字按词；查询与语料使用同一套切分。"""
    tokens: list[str] = []
    for run in _CJK_RUN_RE.findall(text):
        tokens.extend(run)  # 单字
        if len(run) >= 2:
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))  # 二元组
    for word in _LATIN_RUN_RE.findall(text):
        tokens.append(word.lower())
    return tokens


class _BM25Index:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bm25: BM25Okapi | None = None
        self._rows: list[dict] = []

    def invalidate(self) -> None:
        with self._lock:
            self._bm25 = None
            self._rows = []

    def _ensure(self) -> tuple[BM25Okapi, list[dict]]:
        if self._bm25 is None:
            with self._lock:
                if self._bm25 is None:
                    raise RuntimeError("BM25 索引尚未加载，请先 await load()")
        return self._bm25, self._rows

    async def load(self) -> tuple[BM25Okapi, list[dict]]:
        """从 SQLite （重新）构建索引。"""
        if self._bm25 is None:
            with self._lock:
                if self._bm25 is None:
                    rows = await store.load_all_chunks()
                    corpus = [tokenize(row["child_text"]) for row in rows]
                    self._bm25 = BM25Okapi(corpus)
                    self._rows = rows
        return self._bm25, self._rows

    def is_loaded(self) -> bool:
        return self._bm25 is not None

    async def search(self, query: str, top_k: int) -> list[dict]:
        """返回 [{row, score, rank}]，仅保留正分命中，按 BM25 分降序。"""
        bm25, rows = await self.load()
        if not rows:
            return []
        with self._lock:
            scores = list(bm25.get_scores(tokenize(query)))
        ranked = sorted(
            ((index, score) for index, score in enumerate(scores) if score > 0),
            key=lambda item: item[1],
            reverse=True,
        )[:top_k]
        return [
            {"row": rows[index], "score": float(score), "rank": rank + 1}
            for rank, (index, score) in enumerate(ranked)
        ]


_index = _BM25Index()


def get_bm25_index() -> _BM25Index:
    return _index


def invalidate() -> None:
    """入库后调用，强制下一次检索重建索引。"""
    _index.invalidate()
