"""P3-9：知识库管理路由。

- ``POST /api/v1/knowledge/ingest``：按路径入库文件/目录，或全量入库 knowledge/。
- ``POST /api/v1/knowledge/ingest/upload``：上传单个文档后入库。
- ``GET  /api/v1/knowledge/stats``：查看 SQLite 元信息与 Milvus 向量规模。
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from congclaw.rag import config
from congclaw.rag.ingest import ingest_directory, ingest_file
from congclaw.rag.parsing import SUPPORTED_SUFFIXES
from congclaw.rag.retrieval import corpus_stats

router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge"])

_FILENAME_SAFE_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff._-]+")


class IngestRequest(BaseModel):
    """知识库入库请求体。"""

    path: str | None = Field(None, description="文件/目录路径；相对路径基于 knowledge/ 解析")
    source: str | None = Field(None, description="自定义文档来源名（默认取文件名）")
    all: bool = Field(False, description="是否全量入库 knowledge/ 目录")


@router.post("/ingest")
async def ingest(request: IngestRequest) -> dict:
    """解析文档 → 父子分片 → 向量化入库（幂等：同来源先删后写）。"""
    if request.all or not request.path:
        batch = await ingest_directory()
        return batch.as_dict()

    target = Path(request.path)
    if not target.is_absolute():
        target = config.knowledge_dir() / target
    target = target.resolve()
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"文件或目录不存在：{target}")

    if target.is_dir():
        return (await ingest_directory(target)).as_dict()

    if target.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型：{target.suffix}，支持 {sorted(SUPPORTED_SUFFIXES)}",
        )
    stats = await ingest_file(target, source=request.source)
    return {"ingested": [stats.as_dict()], "total_children": stats.children, "errors": []}


@router.post("/ingest/upload")
async def ingest_upload(
    file: UploadFile = File(..., description="待入库的知识文档"),
    source: str | None = Form(None, description="自定义文档来源名（默认取上传文件名）"),
) -> dict:
    """上传文档（保存到 knowledge/uploads/）后执行入库。"""
    filename = Path(file.filename or "").name
    if not filename or Path(filename).suffix.lower() not in SUPPORTED_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail=f"文件名缺失或类型不支持：{filename}，支持 {sorted(SUPPORTED_SUFFIXES)}",
        )
    safe_name = _FILENAME_SAFE_RE.sub("_", filename).strip("_") or "uploaded"
    upload_dir = config.knowledge_dir() / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    target = upload_dir / safe_name
    target.write_bytes(await file.read())

    stats = await ingest_file(target, source=source or filename)
    return {"ingested": [stats.as_dict()], "total_children": stats.children, "errors": []}


@router.get("/stats")
async def stats() -> dict:
    """知识库规模：SQLite child 数、Milvus 向量数、来源分组。"""
    return await corpus_stats()
