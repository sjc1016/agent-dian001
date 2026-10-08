"""阶段 7：常见问答沉淀库仓储（SQLite ``faq_entry`` 表）。

与 ``graph/profile_store``、``skills/business_store``、``rag/store`` 一致
采用 aiosqlite **短连接**，规避「连接跨事件循环」问题。

对外能力：

- :func:`aupsert_faq` / :func:`aget_faq` / :func:`alist_faq`：条目读写与分页查询；
- :func:`aupdate_faq_status`：人工审核流转（draft → approved / published / archived）；
- :func:`asearch_similar`：按问句向量召回相近条目，供沉淀节点判重与合并；
- :func:`afaq_stats`：沉淀库规模统计与高频条目。

向量相似度在**查询时实时计算**（不落库）：沉淀库规模远小于知识库分片，
逐条余弦比对的成本可接受，且天然避免了向量与文本不一致的失效问题。
Embedding 不可用（模型缺失/测试环境）时退化为关键词重合度，仍能返回候选。
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from congclaw.db.engine import init_db, resolve_db_path

MAX_QUESTION_CHARS = 200
MAX_SOLUTION_CHARS = 1200
MAX_VARIANT_CHARS = 60
MAX_VARIANTS = 6
MAX_PRECONDITIONS = 5
MAX_KEYWORDS_CHARS = 120
MAX_SOURCES = 8

FAQ_STATUSES = ("draft", "approved", "published", "archived")
FAQ_ID_PREFIX = "FAQ"
FAQ_ID_DIGITS = 4

# 列表/统计涉及的全部列（显式列出，避免 SELECT * 随表结构漂移）
_COLUMNS = (
    "faq_id",
    "canonical_question",
    "question_variants",
    "category",
    "solution",
    "preconditions",
    "related_skills",
    "keywords",
    "status",
    "confidence",
    "source_route",
    "source_session_id",
    "source_turn_index",
    "sources_json",
    "merge_count",
    "hit_count",
    "created_at",
    "updated_at",
)
_COLUMN_SQL = ", ".join(_COLUMNS)


def _connect() -> aiosqlite.core.Connection:
    return aiosqlite.connect(resolve_db_path())


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_entry() -> dict[str, Any]:
    now = utc_now()
    return {
        "faq_id": "",
        "canonical_question": "",
        "question_variants": [],
        "category": "",
        "solution": "",
        "preconditions": [],
        "related_skills": [],
        "keywords": "",
        "status": "draft",
        "confidence": 0.0,
        "source_route": "",
        "source_session_id": "",
        "source_turn_index": 0,
        "sources": [],
        "merge_count": 1,
        "hit_count": 0,
        "created_at": now,
        "updated_at": now,
    }


async def aget_faq(faq_id: str) -> dict[str, Any] | None:
    """按主键读取单条 FAQ；不存在返回 None。"""
    if not faq_id:
        return None
    await init_db()
    async with _connect() as connection:
        connection.row_factory = aiosqlite.Row
        cursor = await connection.execute(
            f"SELECT {_COLUMN_SQL} FROM faq_entry WHERE faq_id = ?", (faq_id,)
        )
        row = await cursor.fetchone()
    return _row_to_entry(dict(row)) if row is not None else None


async def alist_faq(
    *,
    status: str | None = None,
    category: str | None = None,
    keyword: str = "",
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """分页查询沉淀条目（按常见度 merge_count 降序，其次更新时间降序）。

    返回 ``{"total": int, "rows": [...]}``，字段结构与 :func:`aget_faq` 一致。
    """
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if category:
        clauses.append("category = ?")
        params.append(category)
    text = str(keyword or "").strip()
    if text:
        clauses.append(
            "(canonical_question LIKE ? OR solution LIKE ? OR keywords LIKE ? OR question_variants LIKE ?)"
        )
        like = f"%{text}%"
        params.extend([like, like, like, like])
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

    await init_db()
    async with _connect() as connection:
        connection.row_factory = aiosqlite.Row
        cursor = await connection.execute(
            f"SELECT COUNT(*) AS n FROM faq_entry{where}", tuple(params)
        )
        total = int((await cursor.fetchone())["n"] or 0)
        cursor = await connection.execute(
            f"SELECT {_COLUMN_SQL} FROM faq_entry{where}"
            " ORDER BY merge_count DESC, updated_at DESC LIMIT ? OFFSET ?",
            (*params, max(1, int(limit)), max(0, int(offset))),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
    return {"total": total, "rows": [_row_to_entry(row) for row in rows]}


async def aupsert_faq(entry: dict[str, Any]) -> dict[str, Any]:
    """插入或更新一条沉淀条目。

    ``faq_id`` 为空或不存在时分配新编号（``FAQ0001`` 递增）；
    已存在时执行更新——合并路径下由调用方负责把 ``merge_count`` 递增后传入。
    """
    normalized = _normalize_entry(entry)
    await init_db()
    async with _connect() as connection:
        faq_id = normalized["faq_id"]
        existing = None
        if faq_id:
            cursor = await connection.execute(
                "SELECT faq_id FROM faq_entry WHERE faq_id = ?", (faq_id,)
            )
            existing = await cursor.fetchone()
        if existing is None:
            faq_id = faq_id or await _next_faq_id(connection)
            normalized["faq_id"] = faq_id
            normalized["created_at"] = normalized.get("created_at") or utc_now()
            normalized["updated_at"] = utc_now()
            await connection.execute(
                f"INSERT INTO faq_entry ({_COLUMN_SQL})"
                f" VALUES ({', '.join('?' for _ in _COLUMNS)})",
                _entry_to_row(normalized),
            )
        else:
            normalized["faq_id"] = faq_id
            normalized["updated_at"] = utc_now()
            await connection.execute(
                "UPDATE faq_entry SET canonical_question = ?, question_variants = ?,"
                " category = ?, solution = ?, preconditions = ?, related_skills = ?,"
                " keywords = ?, status = ?, confidence = ?, source_route = ?,"
                " source_session_id = ?, source_turn_index = ?, sources_json = ?,"
                " merge_count = ?, hit_count = ?, updated_at = ? WHERE faq_id = ?",
                (
                    normalized["canonical_question"],
                    json.dumps(normalized["question_variants"], ensure_ascii=False),
                    normalized["category"],
                    normalized["solution"],
                    json.dumps(normalized["preconditions"], ensure_ascii=False),
                    json.dumps(normalized["related_skills"], ensure_ascii=False),
                    normalized["keywords"],
                    normalized["status"],
                    float(normalized["confidence"]),
                    normalized["source_route"],
                    normalized["source_session_id"],
                    int(normalized["source_turn_index"]),
                    json.dumps(normalized["sources"], ensure_ascii=False),
                    int(normalized["merge_count"]),
                    int(normalized["hit_count"]),
                    normalized["updated_at"],
                    faq_id,
                ),
            )
        await connection.commit()
    return normalized


async def aupdate_faq_status(faq_id: str, status: str) -> dict[str, Any] | None:
    """流转审核状态（draft / approved / published / archived）；非法值抛 ValueError。"""
    target = str(status or "").strip()
    if target not in FAQ_STATUSES:
        raise ValueError(f"非法的 FAQ 状态：{status}，可选 {list(FAQ_STATUSES)}")
    await init_db()
    async with _connect() as connection:
        cursor = await connection.execute(
            "UPDATE faq_entry SET status = ?, updated_at = ? WHERE faq_id = ?",
            (target, utc_now(), faq_id),
        )
        await connection.commit()
        if cursor.rowcount == 0:
            return None
    return await aget_faq(faq_id)


async def asearch_similar(question: str, top_k: int = 5) -> list[dict[str, Any]]:
    """按问句召回相近的已沉淀条目（供 LLM 判重/合并）。

    归档条目（archived）不参与召回；向量不可用时退化为关键词重合度排序。
    返回项含 ``faq_id / canonical_question / solution / similarity / merge_count``。
    """
    text = str(question or "").strip()
    if not text:
        return []
    listing = await alist_faq(limit=500)
    rows = [row for row in listing["rows"] if row.get("status") != "archived"]
    if not rows:
        return []

    limit = max(1, int(top_k))
    corpus = [_entry_search_text(row) for row in rows]
    similarities = await _similarities(text, corpus)
    ranked: list[dict[str, Any]] = []
    for row, score in zip(rows, similarities):
        ranked.append(
            {
                "faq_id": row.get("faq_id", ""),
                "canonical_question": row.get("canonical_question", ""),
                "solution": _clip(str(row.get("solution") or ""), 300),
                "category": row.get("category", ""),
                "merge_count": int(row.get("merge_count", 1) or 1),
                "similarity": round(float(score), 4),
            }
        )
    ranked.sort(key=lambda item: item["similarity"], reverse=True)
    return ranked[:limit]


async def afaq_stats() -> dict[str, Any]:
    """沉淀库规模统计：状态分布 + 分类分布 + 高频条目 TOP N。"""
    await init_db()
    async with _connect() as connection:
        connection.row_factory = aiosqlite.Row
        cursor = await connection.execute(
            "SELECT status, COUNT(*) AS n FROM faq_entry GROUP BY status"
        )
        by_status = {row["status"]: int(row["n"] or 0) for row in await cursor.fetchall()}
        cursor = await connection.execute(
            "SELECT category, COUNT(*) AS n FROM faq_entry WHERE category <> ''"
            " GROUP BY category ORDER BY n DESC"
        )
        by_category = {row["category"]: int(row["n"] or 0) for row in await cursor.fetchall()}
        cursor = await connection.execute(
            f"SELECT {_COLUMN_SQL} FROM faq_entry ORDER BY merge_count DESC, updated_at DESC"
            " LIMIT 5"
        )
        top_rows = [dict(row) for row in await cursor.fetchall()]
    total = sum(by_status.values())
    return {
        "total": total,
        "by_status": by_status,
        "by_category": by_category,
        "pending_review": by_status.get("draft", 0),
        "published": by_status.get("published", 0) + by_status.get("approved", 0),
        "top_entries": [_row_to_entry(row) for row in top_rows],
    }


# ---------------------------------------------------------------------------
# 向量相似度
# ---------------------------------------------------------------------------


async def _similarities(question: str, corpus: list[str]) -> list[float]:
    """问句与候选文本的相似度；向量不可用时退化为关键词重合度。"""
    try:
        return await _embedding_similarities(question, corpus)
    except Exception:  # noqa: BLE001 —— 模型缺失/离线等一律退回确定性算法
        return _keyword_similarities(question, corpus)


async def _embedding_similarities(question: str, corpus: list[str]) -> list[float]:
    from congclaw.rag.embedding import embed

    vectors = await asyncio.to_thread(embed, [question, *corpus])
    query_vector = vectors[0]
    return [float(query_vector @ vector) for vector in vectors[1:]]


def _keyword_similarities(question: str, corpus: list[str]) -> list[float]:
    """字符 bigram Jaccard 相似度：无模型环境下的确定性替代。"""
    query_grams = _bigrams(question)
    if not query_grams:
        return [0.0 for _ in corpus]
    scores: list[float] = []
    for text in corpus:
        grams = _bigrams(text)
        if not grams:
            scores.append(0.0)
            continue
        scores.append(len(query_grams & grams) / len(query_grams | grams))
    return scores


def _bigrams(text: str) -> set[str]:
    normalized = re.sub(r"\s+", "", str(text or ""))
    if len(normalized) < 2:
        return {normalized} if normalized else set()
    return {normalized[index : index + 2] for index in range(len(normalized) - 1)}


def _entry_search_text(entry: dict[str, Any]) -> str:
    """用于相似度比对的条目文本：标准问句 + 同义问法 + 关键词。"""
    parts = [str(entry.get("canonical_question") or "")]
    parts.extend(str(item) for item in entry.get("question_variants") or [])
    parts.append(str(entry.get("keywords") or ""))
    return " ".join(part for part in parts if part).strip()


# ---------------------------------------------------------------------------
# 编号与规范化
# ---------------------------------------------------------------------------


async def _next_faq_id(connection: aiosqlite.Connection) -> str:
    """生成下一个 FAQ 编号（``FAQ0001`` 递增）。

    与 :func:`aupsert_faq` 共用同一连接与事务，避免并发写入撞号。
    """
    cursor = await connection.execute(
        "SELECT faq_id FROM faq_entry ORDER BY faq_id DESC LIMIT 1"
    )
    row = await cursor.fetchone()
    sequence = 0
    if row is not None:
        match = re.search(r"(\d+)$", str(row[0] or ""))
        if match:
            sequence = int(match.group(1))
    next_id = f"{FAQ_ID_PREFIX}{sequence + 1:0{FAQ_ID_DIGITS}d}"
    while True:
        cursor = await connection.execute(
            "SELECT 1 FROM faq_entry WHERE faq_id = ?", (next_id,)
        )
        if await cursor.fetchone() is None:
            return next_id
        sequence += 1
        next_id = f"{FAQ_ID_PREFIX}{sequence + 1:0{FAQ_ID_DIGITS}d}"


def _normalize_entry(raw: dict[str, Any]) -> dict[str, Any]:
    """规范化条目字段：裁剪长度、清洗列表、约束状态与计数。"""
    entry = default_entry()
    status = str(raw.get("status") or "draft").strip()
    entry.update(
        {
            "faq_id": str(raw.get("faq_id") or "").strip(),
            "canonical_question": _clip(
                str(raw.get("canonical_question") or "").strip(), MAX_QUESTION_CHARS
            ),
            "question_variants": _clean_list(raw.get("question_variants"), MAX_VARIANTS),
            "category": str(raw.get("category") or "").strip(),
            "solution": _clip(str(raw.get("solution") or "").strip(), MAX_SOLUTION_CHARS),
            "preconditions": _clean_list(raw.get("preconditions"), MAX_PRECONDITIONS),
            "related_skills": _clean_list(raw.get("related_skills"), MAX_VARIANTS),
            "keywords": _clip(str(raw.get("keywords") or "").strip(), MAX_KEYWORDS_CHARS),
            "status": status if status in FAQ_STATUSES else "draft",
            "confidence": max(0.0, min(1.0, float(raw.get("confidence", 0.0) or 0.0))),
            "source_route": str(raw.get("source_route") or "").strip(),
            "source_session_id": str(raw.get("source_session_id") or "").strip(),
            "source_turn_index": int(raw.get("source_turn_index", 0) or 0),
            "sources": _clean_sources(raw.get("sources")),
            "merge_count": max(1, int(raw.get("merge_count", 1) or 1)),
            "hit_count": max(0, int(raw.get("hit_count", 0) or 0)),
            "created_at": str(raw.get("created_at") or entry["created_at"]),
            "updated_at": str(raw.get("updated_at") or entry["updated_at"]),
        }
    )
    return entry


def _entry_to_row(entry: dict[str, Any]) -> tuple[Any, ...]:
    return (
        entry["faq_id"],
        entry["canonical_question"],
        json.dumps(entry["question_variants"], ensure_ascii=False),
        entry["category"],
        entry["solution"],
        json.dumps(entry["preconditions"], ensure_ascii=False),
        json.dumps(entry["related_skills"], ensure_ascii=False),
        entry["keywords"],
        entry["status"],
        float(entry["confidence"]),
        entry["source_route"],
        entry["source_session_id"],
        int(entry["source_turn_index"]),
        json.dumps(entry["sources"], ensure_ascii=False),
        int(entry["merge_count"]),
        int(entry["hit_count"]),
        entry["created_at"],
        entry["updated_at"],
    )


def _row_to_entry(row: dict[str, Any]) -> dict[str, Any]:
    return _normalize_entry(
        {
            "faq_id": row.get("faq_id", ""),
            "canonical_question": row.get("canonical_question", ""),
            "question_variants": _maybe_json(row.get("question_variants")) or [],
            "category": row.get("category", ""),
            "solution": row.get("solution", ""),
            "preconditions": _maybe_json(row.get("preconditions")) or [],
            "related_skills": _maybe_json(row.get("related_skills")) or [],
            "keywords": row.get("keywords", ""),
            "status": row.get("status", "draft"),
            "confidence": row.get("confidence", 0.0),
            "source_route": row.get("source_route", ""),
            "source_session_id": row.get("source_session_id", ""),
            "source_turn_index": row.get("source_turn_index", 0),
            "sources": _maybe_json(row.get("sources_json")) or [],
            "merge_count": row.get("merge_count", 1),
            "hit_count": row.get("hit_count", 0),
            "created_at": row.get("created_at", ""),
            "updated_at": row.get("updated_at", ""),
        }
    )


def _clean_list(raw: Any, limit: int) -> list[str]:
    if not isinstance(raw, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = _clip(str(item or "").strip(), MAX_VARIANT_CHARS)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= limit:
            break
    return result


def _clean_sources(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    sources: list[dict[str, Any]] = []
    for item in raw[:MAX_SOURCES]:
        if not isinstance(item, dict):
            continue
        sources.append(
            {
                "title": str(item.get("title") or ""),
                "url": str(item.get("url") or ""),
                "score": float(item.get("score", 0.0) or 0.0),
            }
        )
    return sources


def _maybe_json(text: Any) -> Any:
    if not isinstance(text, str):
        return text
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def _clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."
