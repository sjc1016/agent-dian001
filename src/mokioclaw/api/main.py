"""FastAPI 应用入口。

- 生命周期：启动时执行 :func:`mokioclaw.db.init_db` 建库（幂等），
  并预留模型预热占位（阶段 3/4 加载 BGE-M3 / reranker）。
- 健康检查：``GET /health`` 返回服务状态与数据库版本。
- 路由挂载：阶段 1 仅挂载 ``/api/v1/chat``，后续阶段追加 sessions / skills / knowledge / eval。
"""

from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI

from mokioclaw.api.routes.chat import router as chat_router
from mokioclaw.api.routes.knowledge import router as knowledge_router
from mokioclaw.db import init_db, resolve_db_path
from mokioclaw.db.engine import dispose_engine


def _prewarm_rag_models() -> None:
    """后台预加载 BGE-M3 / bge-reranker，避免首个用户请求承担约 30s 冷启动。"""
    try:
        from mokioclaw.rag.embedding import embed, rerank_scores

        embed("预热")
        rerank_scores([("预热", "预热")])
    except Exception:
        # 模型缺失或加载失败不阻断启动：检索节点自身仍有降级逻辑
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动建库，关闭释放引擎。"""
    load_dotenv()
    # 建库（幂等）；RAG 模型后台预加载（RAG_PREWARM=0 可关闭）
    await init_db()
    if os.getenv("RAG_PREWARM", "1") != "0":
        threading.Thread(target=_prewarm_rag_models, name="rag-prewarm", daemon=True).start()
    try:
        yield
    finally:
        await dispose_engine()


app = FastAPI(
    title="电信客服智能体 API",
    description="基于 MokioClaw 改造的电信客服业务智能体服务",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(chat_router)
app.include_router(knowledge_router)


@app.get("/health", tags=["system"])
async def health() -> dict[str, str]:
    """健康检查：返回服务状态与数据库路径。"""
    return {
        "status": "ok",
        "db_path": str(resolve_db_path()),
        "model_warmup": "BGE-M3 / bge-reranker 启动后后台预加载（RAG_PREWARM=0 关闭）",
    }


@app.get("/", tags=["system"])
async def root() -> dict[str, str]:
    return {"service": "电信客服智能体 API", "docs": "/docs"}
