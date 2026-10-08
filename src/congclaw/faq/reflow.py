"""阶段 7：已发布 FAQ 回流到 RAG 知识库。

人工把条目审核为 ``published`` 之后，把**全部**已发布条目渲染成一份 markdown
落到 ``knowledge/`` 下，再调用现成的离线入库管线。入库按 ``doc_source`` 幂等
（先删旧向量与元信息再写），所以「全量重渲染 + 重入库」天然幂等：

- 新发布的条目 → 下一次重渲染出现，随即入库；
- 从 published 转 archived 的条目 → 下一次重渲染不再出现，其旧分片被同来源
  删除一并清掉，不会再被检索召回。

代价是每次触发都要重新向量化全部已发布条目；本项目沉淀条目量级很小（数十条），
换来的是不需要维护「逐条增删」的对称逻辑与孤儿分片清理。

回流失败绝不阻断审核动作：审核已经在 SQLite 提交，回流只作为附加结果返回。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from congclaw.faq.store import alist_faq
from congclaw.rag import bm25_index, config, store as rag_store, vectorstore
from congclaw.rag.ingest import ingest_file

# 回流文档落在 knowledge/ 下，因此必须让「来源名」等于「文件名」：目录全量入库
# （admin/knowledge 的 ingest all）按 path.name 推 doc_source，若两者不一致，同一
# 份文件会被写成两个来源、检索时返回近似重复的命中。
# 入库幂等以 doc_source 为键，改动会留下旧分片，谨慎变更。
REFLOW_FILENAME = "FAQ沉淀库.md"
REFLOW_SOURCE = REFLOW_FILENAME
# 单次重渲染的条目上限（与 asearch_similar 的取数口径保持一致）
MAX_ENTRIES = 500

_BLANK_LINE_RE = re.compile(r"\n\s*\n+")


def reflow_path() -> Path:
    """回流文档的落盘位置（knowledge/ 下，随 KNOWLEDGE_DIR 配置走）。"""
    return config.knowledge_dir() / REFLOW_FILENAME


def render_faq_markdown(entries: list[dict[str, Any]]) -> str:
    """把条目渲染成知识文档。

    每条 FAQ 渲染为**一个连续段落**（段内不出现空行），因为解析器的分片规则是
    「空行切段、标题并入紧邻段落」——只有让问句与答案同段，检索命中任意一句时
    回溯到的父分片才会同时包含问题与完整答案。
    """
    blocks: list[str] = []
    for entry in entries:
        question = str(entry.get("canonical_question") or "").strip()
        solution = _inline(str(entry.get("solution") or ""))
        if not question or not solution:
            continue

        lines = [f"问：{_inline(question)}"]
        variants = _joined(entry.get("question_variants"))
        if variants:
            lines.append(f"同义问法：{variants}")
        lines.append(f"答：{solution}")
        preconditions = _joined(entry.get("preconditions"))
        if preconditions:
            lines.append(f"适用前提：{preconditions}")

        blocks.append("\n".join(lines))

    if not blocks:
        return ""
    return "\n\n".join(blocks) + "\n"


async def areflow_published_faq() -> dict[str, Any]:
    """把已发布条目全量回流到知识库，返回可直接写进接口响应的摘要。"""
    listing = await alist_faq(status="published", limit=MAX_ENTRIES)
    entries = list(listing.get("rows") or [])
    text = render_faq_markdown(entries)
    path = reflow_path()

    if not text:
        # 已无已发布条目：清掉回流来源，避免已下架内容继续被召回
        existed = path.exists()
        if existed:
            path.unlink()
        await _purge_source()
        return {
            "status": "ok",
            "entries": 0,
            "children": 0,
            "source": REFLOW_SOURCE,
            "cleared": True,
        }

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    stats = await ingest_file(path, source=REFLOW_SOURCE)
    return {
        "status": "ok",
        "entries": len(entries),
        "children": stats.children,
        "source": REFLOW_SOURCE,
        "path": str(path),
        "cleared": False,
    }


async def _purge_source() -> None:
    """删除回流来源在向量库与元信息中的全部分片（条目全部下架时用）。"""
    await asyncio.to_thread(vectorstore.get_vector_store().delete_by_source, REFLOW_SOURCE)
    await rag_store.replace_doc_chunks(REFLOW_SOURCE, [])
    bm25_index.invalidate()


def _inline(text: str) -> str:
    """压掉段内空行：保证一条 FAQ 落成解析器眼里的单个段落。"""
    return _BLANK_LINE_RE.sub("\n", text.strip())


def _joined(value: Any) -> str:
    """把字符串列表拼成「；」分隔的一行（兼容空值与非列表输入）。"""
    if not isinstance(value, list):
        return ""
    items = [str(item).strip() for item in value if str(item).strip()]
    return "；".join(items)
