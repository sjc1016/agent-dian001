"""P0-12 模型冒烟脚本：验证本地 Embedding 与 Reranker 可加载、可推理。

前置：先运行 uv run python scripts/download_models.py 把模型下载到 models/。

运行：uv run python scripts/smoke_models.py

输出：
- BGE-M3 对一句话生成的 1 条向量（维度应为 1024）及前若干维；
- Cross-Encoder 对 1 组（问题, 文档）打出的 rerank 分数（相关段落分数应高于无关段落）。
全程 CPU、本地离线，不访问任何外部服务。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# 强制离线，确保只加载本地权重（零外部服务）
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

EMBED_DIR = PROJECT_ROOT / "models" / "bge-m3"
RERANK_DIR = PROJECT_ROOT / "models" / "bge-reranker"

QUERY = "我的话费余额怎么查询？"
RELEVANT_DOC = "您可以通过手机营业厅、客服热线或发送短信 10086 查询当前话费余额。"
IRRELEVANT_DOC = "公司年会将于下周五晚在总部大礼堂举行，请各部门提前准备节目。"


def _require_model_dir(path: Path) -> None:
    if not path.exists() or not any(path.iterdir()):
        print(
            f"[smoke] 未找到本地模型目录：{path}\n"
            "请先运行：uv run python scripts/download_models.py",
            file=sys.stderr,
        )
        raise SystemExit(1)


def main() -> None:
    _require_model_dir(EMBED_DIR)
    _require_model_dir(RERANK_DIR)

    from sentence_transformers import CrossEncoder, SentenceTransformer

    print("[smoke] 加载 BGE-M3 embedding（CPU，首次加载较慢）...")
    embedder = SentenceTransformer(str(EMBED_DIR), device="cpu")
    vector = embedder.encode(QUERY, normalize_embeddings=True)
    print(f"[smoke] 文本: {QUERY}")
    print(f"[smoke] 向量维度: {len(vector)}")
    print(f"[smoke] 向量前 5 维: {[round(float(x), 6) for x in vector[:5]]}")

    print("[smoke] 加载 bge-reranker CrossEncoder...")
    reranker = CrossEncoder(str(RERANK_DIR), device="cpu")
    pairs = [(QUERY, RELEVANT_DOC), (QUERY, IRRELEVANT_DOC)]
    scores = reranker.predict(pairs)
    rel_score, irr_score = float(scores[0]), float(scores[1])
    print(f"[smoke] rerank 分数（相关段落） : {rel_score:.6f}  <- {RELEVANT_DOC}")
    print(f"[smoke] rerank 分数（无关段落） : {irr_score:.6f}  <- {IRRELEVANT_DOC}")

    assert len(vector) == 1024, "BGE-M3 向量维度应为 1024"
    assert rel_score > irr_score, "相关段落的 rerank 分数应高于无关段落"
    print("[smoke] 验证通过：embedding 与 rerank 均正常工作")


if __name__ == "__main__":
    main()
