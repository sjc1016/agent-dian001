"""P3-7：BGE-M3 Embedding 与 bge-reranker Cross-Encoder 的本地懒加载封装。

- 进程内单例 + 锁：首次推理时才加载权重（CPU），避免服务启动即占满内存。
- 全部推理为同步 CPU 调用，在线节点通过 ``asyncio.to_thread`` 包装为 async，
  不阻塞 LangGraph 子图的事件循环。
- 强制离线：只从项目 ``models/`` 目录加载本地权重，零外部服务。
"""

from __future__ import annotations

import os
import threading
from typing import Iterable

# 模型加载前固定离线环境变量（本地权重，不访问 HuggingFace Hub）
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

from mokioclaw.rag import config


class _BgeEmbedder:
    """BGE-M3 句向量（1024 维，L2 归一化，适配 Milvus COSINE/IP）。"""

    def __init__(self) -> None:
        self._model = None
        self._lock = threading.Lock()

    def _ensure(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from sentence_transformers import SentenceTransformer

                    model_dir = config.embed_model_dir()
                    if not model_dir.exists():
                        raise FileNotFoundError(
                            f"未找到本地 Embedding 模型目录：{model_dir}，"
                            "请先运行 uv run python scripts/download_models.py"
                        )
                    self._model = SentenceTransformer(str(model_dir), device="cpu")
        return self._model

    def encode(self, texts: str | Iterable[str]):
        """归一化向量；单条返回 1-D，多条返回 2-D ndarray。"""
        import numpy as np

        single = isinstance(texts, str)
        inputs = [texts] if single else list(texts)
        vectors = self._ensure().encode(
            inputs,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        vectors = np.asarray(vectors, dtype="float32")
        return vectors[0] if single else vectors


class _BgeReranker:
    """bge-reranker Cross-Encoder 精排（输出原始 logit，调用方自行 sigmoid）。"""

    def __init__(self) -> None:
        self._model = None
        self._lock = threading.Lock()

    def _ensure(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from sentence_transformers import CrossEncoder

                    model_dir = config.rerank_model_dir()
                    if not model_dir.exists():
                        raise FileNotFoundError(
                            f"未找到本地 reranker 模型目录：{model_dir}，"
                            "请先运行 uv run python scripts/download_models.py"
                        )
                    self._model = CrossEncoder(str(model_dir), device="cpu")
        return self._model

    def score_pairs(self, pairs: list[tuple[str, str]]) -> list[float]:
        """对 (query, doc_text) 配对批量打分。"""
        if not pairs:
            return []
        raw = self._ensure().predict(pairs, show_progress_bar=False)
        return [float(value) for value in raw]


_embedder = _BgeEmbedder()
_reranker = _BgeReranker()


def embed(texts: str | Iterable[str]):
    """同步归一化编码（供入库管线与 to_thread 包装使用）。"""
    return _embedder.encode(texts)


def rerank_scores(pairs: list[tuple[str, str]]) -> list[float]:
    """同步 Cross-Encoder 打分。"""
    return _reranker.score_pairs(pairs)


def reset_models() -> None:
    """释放单例（主要给测试与显式重载使用）。"""
    global _embedder, _reranker
    _embedder = _BgeEmbedder()
    _reranker = _BgeReranker()
