"""阶段 3：RAG 检索子图的运行时配置（全部可被环境变量覆盖）。

零外部服务约束：Embedding（BGE-M3）与 Cross-Encoder（bge-reranker）均从
项目本地 ``models/`` 目录加载，默认 CPU 推理、进程内懒加载单例。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from dotenv import load_dotenv

from congclaw.core.paths import find_project_root

# Milvus Lite 本地文件（中文绝对路径下 faiss 后台持久化会打印非致命告警，
# 可用 MILVUS_DB_PATH 指向纯英文路径规避，详见踩坑记录 P0-9）
DEFAULT_MILVUS_PATH = Path("data") / "milvus_lite.db"
DEFAULT_EMBED_MODEL_DIR = Path("models") / "bge-m3"
DEFAULT_RERANK_MODEL_DIR = Path("models") / "bge-reranker"
DEFAULT_KNOWLEDGE_DIR = Path("knowledge")

# 子图检索参数
COLLECTION_NAME = "telecom_kb_chunks"
EMBED_DIM = 1024
RECALL_TOP_K = 10  # 每路召回条数（BM25 / dense 各取 Top-N）
FUSION_KEEP = 20  # RRF 融合后保留条数
RERANK_INPUT_TOP_N = 8  # 实际送 Cross-Encoder 的候选数（bge-reranker CPU 热身后每批约 1.5s）
RERANK_TOP_K = 5  # Cross-Encoder 精排后保留条数
RRF_K = 60  # RRF 常数 k
MAX_RAG_ATTEMPTS = 1  # 放宽重写最多 1 次
PARENT_MAX = 3  # parent_lookup 最多回溯的父分片数量
NEIGHBOR_CHILDREN = 1  # 命中 child 前后各扩展的邻域 child 数
EVIDENCE_MAX_CHARS = 2000  # 送入生成节点的证据总字数上限
EMBED_BATCH_SIZE = 16

# bge-reranker 输出原始 logit（可能为负），用 sigmoid 归一化后做证据门限。
# 阈值按 8 份库内文档 + 库外问题实测分布校准（2026-10）：
# 库内最弱相关 Top-1 ≈ raw 0.21 / prob 0.55；库外问题 Top-1 ≈ raw 0.00 / prob 0.50。
RERANK_GATE_PROB = 0.53  # Top-1 归一化分数低于该值视为低置信
RERANK_GATE_RAW = 0.15  # 原始 logit 硬底（低于此值直接判无证据）


def _resolve(raw: str, default: Path) -> Path:
    path = Path(raw) if raw else default
    if not path.is_absolute():
        path = find_project_root() / path
    return path


def milvus_db_path() -> Path:
    load_dotenv()
    return _resolve(os.getenv("MILVUS_DB_PATH", "").strip(), DEFAULT_MILVUS_PATH)


def ensure_milvus_db_path() -> Path:
    """返回 Milvus 数据路径；盘符映射丢失时按需重建。

    Milvus Lite 的 faiss 后端在 Windows 上读不了非 ASCII 路径，索引加载会失败并
    退化为 brute-force（再撞上 ``allow_pickle`` 报错），表现为 dense 召回静默为空。
    因此 ``MILVUS_DB_PATH`` 常配成 ``Z:\\milvus_lite.db`` 这类盘符映射。

    ``subst`` 映射只在当前登录会话内有效，重启/注销后丢失——此时应用会静默降级为
    BM25 单路。这里在首次访问向量库时自动补建映射（指向项目 ``data`` 目录），
    使映射丢失后自愈，无需人工干预。
    """
    path = milvus_db_path()
    drive = path.drive
    if os.name != "nt" or not drive or Path(f"{drive}\\").exists():
        return path

    target = find_project_root() / "data"
    target.mkdir(parents=True, exist_ok=True)
    subprocess.run(["subst", drive, str(target)], capture_output=True, text=True)
    return path


def embed_model_dir() -> Path:
    load_dotenv()
    return _resolve(os.getenv("EMBED_MODEL_PATH", "").strip(), DEFAULT_EMBED_MODEL_DIR)


def rerank_model_dir() -> Path:
    load_dotenv()
    return _resolve(os.getenv("RERANK_MODEL_PATH", "").strip(), DEFAULT_RERANK_MODEL_DIR)


def knowledge_dir() -> Path:
    load_dotenv()
    return _resolve(os.getenv("KNOWLEDGE_DIR", "").strip(), DEFAULT_KNOWLEDGE_DIR)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def recall_top_k() -> int:
    return _env_int("RAG_RECALL_TOP_K", RECALL_TOP_K)


def rerank_top_k() -> int:
    return _env_int("RAG_RERANK_TOP_K", RERANK_TOP_K)


def rerank_input_top_n() -> int:
    return _env_int("RAG_RERANK_INPUT_TOP_N", RERANK_INPUT_TOP_N)


def rerank_gate_prob() -> float:
    return _env_float("RAG_RERANK_GATE_PROB", RERANK_GATE_PROB)


def rerank_gate_raw() -> float:
    return _env_float("RAG_RERANK_GATE_RAW", RERANK_GATE_RAW)
