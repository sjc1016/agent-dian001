"""管理员后台路由。

提供两大部分能力：
1. 后台数据库管理：查看表结构、浏览数据、增删改查（通用 SQLite CRUD）。
2. RAG 知识库管理：查看来源、执行入库、上传入库、删除来源。

管理员口令固定为 ``123456``（演示用途）。
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import aiosqlite
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from congclaw.db.engine import init_db, resolve_db_path
from congclaw.rag import bm25_index, config, vectorstore
from congclaw.rag.ingest import ingest_directory, ingest_file
from congclaw.rag.parsing import SUPPORTED_SUFFIXES
from congclaw.rag.retrieval import corpus_stats
from congclaw.rag.store import delete_source as delete_doc_source, list_source_detail, list_sources

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

ADMIN_PASSWORD = "123456"

_SYSTEM_TABLE_RE = re.compile(r"^sqlite_")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FILENAME_SAFE_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff._-]+")


class AdminLoginRequest(BaseModel):
    """管理员登录请求。"""

    password: str = Field(..., description="管理员口令", min_length=1)


class RowCreateRequest(BaseModel):
    """新增行请求：列名 → 值。"""

    data: dict[str, Any] = Field(..., description="待插入的列值映射")


class RowUpdateRequest(BaseModel):
    """更新行请求：列名 → 值。"""

    data: dict[str, Any] = Field(..., description="待更新的列值映射")


class IngestRequest(BaseModel):
    """RAG 入库请求。"""

    path: str | None = Field(None, description="相对 knowledge/ 或绝对路径；留空且 all=true 时全量入库")
    all: bool = Field(False, description="是否全量入库 knowledge/ 目录")


def _connect() -> aiosqlite.core.Connection:
    return aiosqlite.connect(resolve_db_path())


def _validate_identifier(name: str) -> None:
    if not name or not _IDENTIFIER_RE.match(name):
        raise HTTPException(status_code=400, detail=f"非法标识符：{name}")


def _split_pk(pk: str) -> list[str]:
    return pk.split("|") if pk else []


async def _table_names() -> list[str]:
    async with _connect() as connection:
        connection.row_factory = aiosqlite.Row
        cursor = await connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        rows = await cursor.fetchall()
    return sorted(
        name for (name,) in rows if name and not _SYSTEM_TABLE_RE.match(name)
    )


async def _table_info(table: str) -> list[dict[str, Any]]:
    _validate_identifier(table)
    async with _connect() as connection:
        connection.row_factory = aiosqlite.Row
        cursor = await connection.execute(f"PRAGMA table_info({table})")
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


def _pk_columns(columns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted((col for col in columns if col.get("pk")), key=lambda c: c.get("pk", 0))


@router.post("/login")
async def admin_login(payload: AdminLoginRequest) -> dict:
    """校验管理员口令；通过返回简单令牌。"""
    if payload.password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="管理员口令不正确")
    return {"ok": True, "token": "admin", "role": "admin"}


@router.get("/db/tables")
async def list_tables() -> dict:
    """列出数据库中所有用户表。"""
    await init_db()
    return {"tables": await _table_names()}


@router.get("/db/tables/{table}/schema")
async def table_schema(table: str) -> dict:
    """获取指定表的列结构（含主键信息）。"""
    await init_db()
    columns = await _table_info(table)
    return {"table": table, "columns": columns, "pk_columns": [col["name"] for col in _pk_columns(columns)]}


@router.get("/db/tables/{table}/rows")
async def list_rows(
    table: str,
    limit: int = 50,
    offset: int = 0,
    q: str = "",
) -> dict:
    """分页查询表数据；q 为任意列的模糊匹配关键词。"""
    await init_db()
    _validate_identifier(table)
    columns = await _table_info(table)
    if not columns:
        raise HTTPException(status_code=404, detail=f"表不存在：{table}")
    col_names = [col["name"] for col in columns]

    params: list[Any] = []
    where_clause = ""
    keyword = (q or "").strip()
    if keyword:
        # 仅对 TEXT/字符串友好列做 LIKE；REAL/INTEGER 用字符串比较也安全
        likes = [f"CAST({name} AS TEXT) LIKE ?" for name in col_names]
        where_clause = "WHERE " + " OR ".join(likes)
        params.extend(f"%{keyword}%" for _ in col_names)

    count_sql = f"SELECT COUNT(*) FROM {table} {where_clause}".strip()
    data_sql = f"SELECT * FROM {table} {where_clause} LIMIT ? OFFSET ?"

    async with _connect() as connection:
        connection.row_factory = aiosqlite.Row
        count_cursor = await connection.execute(count_sql, params)
        (total,) = await count_cursor.fetchone()

        data_params = params + [max(1, min(limit, 200)), max(0, offset)]
        cursor = await connection.execute(data_sql, data_params)
        rows = [dict(row) for row in await cursor.fetchall()]

    pks = [col["name"] for col in _pk_columns(columns)]
    return {
        "table": table,
        "columns": col_names,
        "pk_columns": pks,
        "total": total,
        "limit": limit,
        "offset": offset,
        "rows": rows,
    }


@router.post("/db/tables/{table}/rows")
async def create_row(table: str, payload: RowCreateRequest) -> dict:
    """向指定表插入一行。"""
    await init_db()
    _validate_identifier(table)
    if not payload.data:
        raise HTTPException(status_code=400, detail="data 不能为空")

    columns = await _table_info(table)
    valid_names = {col["name"] for col in columns}
    for name in payload.data:
        if name not in valid_names:
            raise HTTPException(status_code=400, detail=f"表 {table} 不存在列：{name}")

    names = list(payload.data.keys())
    placeholders = ",".join("?" for _ in names)
    sql = f"INSERT INTO {table} ({','.join(names)}) VALUES ({placeholders})"
    values = [json.dumps(v) if isinstance(v, (list, dict)) else v for v in payload.data.values()]

    async with _connect() as connection:
        try:
            cursor = await connection.execute(sql, values)
            await connection.commit()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"插入失败：{exc}") from exc
    return {"ok": True, "rowid": cursor.lastrowid}


@router.put("/db/tables/{table}/rows/{pk}")
async def update_row(table: str, pk: str, payload: RowUpdateRequest) -> dict:
    """按主键更新一行；复合主键用 ``|`` 分隔各列值。"""
    await init_db()
    _validate_identifier(table)
    if not payload.data:
        raise HTTPException(status_code=400, detail="data 不能为空")

    columns = await _table_info(table)
    valid_names = {col["name"] for col in columns}
    for name in payload.data:
        if name not in valid_names:
            raise HTTPException(status_code=400, detail=f"表 {table} 不存在列：{name}")

    pk_cols = _pk_columns(columns)
    pk_values = _split_pk(pk)
    if len(pk_cols) != len(pk_values):
        raise HTTPException(status_code=400, detail=f"主键值数量不匹配：需要 {len(pk_cols)} 个，收到 {len(pk_values)} 个")

    set_clause = ",".join(f"{name}=?" for name in payload.data)
    where_clause = " AND ".join(f"{col['name']}=?" for col in pk_cols)
    values = [json.dumps(v) if isinstance(v, (list, dict)) else v for v in payload.data.values()]
    values.extend(pk_values)

    sql = f"UPDATE {table} SET {set_clause} WHERE {where_clause}"
    async with _connect() as connection:
        try:
            cursor = await connection.execute(sql, values)
            await connection.commit()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"更新失败：{exc}") from exc
    return {"ok": True, "updated": cursor.rowcount}


@router.delete("/db/tables/{table}/rows/{pk}")
async def delete_row(table: str, pk: str) -> dict:
    """按主键删除一行；复合主键用 ``|`` 分隔各列值。"""
    await init_db()
    _validate_identifier(table)
    columns = await _table_info(table)
    pk_cols = _pk_columns(columns)
    pk_values = _split_pk(pk)
    if len(pk_cols) != len(pk_values):
        raise HTTPException(status_code=400, detail=f"主键值数量不匹配：需要 {len(pk_cols)} 个，收到 {len(pk_values)} 个")

    where_clause = " AND ".join(f"{col['name']}=?" for col in pk_cols)
    sql = f"DELETE FROM {table} WHERE {where_clause}"
    async with _connect() as connection:
        cursor = await connection.execute(sql, pk_values)
        await connection.commit()
    return {"ok": True, "deleted": cursor.rowcount}


@router.get("/rag/stats")
async def rag_stats() -> dict:
    """RAG 知识库统计。"""
    return await corpus_stats()


@router.get("/rag/sources")
async def rag_sources() -> dict:
    """列出所有知识库来源。"""
    return {"sources": await list_sources()}


@router.get("/rag/sources/{source}/chunks")
async def rag_source_detail(source: str) -> dict:
    """预览单个来源的全部分片：子分片原样返回，父分片按 parent_id 拼接还原。"""
    await init_db()
    detail = await list_source_detail(source)
    if not detail["child_count"]:
        raise HTTPException(status_code=404, detail=f"来源不存在或已无分片：{source}")
    return detail


@router.post("/rag/ingest")
async def rag_ingest(request: IngestRequest) -> dict:
    """执行 RAG 入库：按路径或全量入库 knowledge/ 目录。"""
    await init_db()
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
    stats = await ingest_file(target)
    return {"ingested": [stats.as_dict()], "total_children": stats.children, "errors": []}


@router.post("/rag/upload")
async def rag_upload(
    file: UploadFile = File(..., description="待入库的知识文档"),
    source: str | None = Form(None, description="自定义文档来源名"),
) -> dict:
    """上传文档并执行 RAG 入库。"""
    await init_db()
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


@router.delete("/rag/sources/{source}")
async def rag_delete_source(source: str) -> dict:
    """删除指定来源的 SQLite 元信息和 Milvus 向量。"""
    await init_db()
    # 删除 Milvus 向量（同步客户端，卸载到线程避免阻塞事件循环）
    await asyncio.to_thread(vectorstore.get_vector_store().delete_by_source, source)
    # 删除 SQLite 元信息
    deleted = await delete_doc_source(source)
    # 使 BM25 索引失效，下次检索自动重建
    bm25_index.invalidate()
    return {"ok": True, "deleted": deleted}
