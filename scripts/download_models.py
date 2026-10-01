"""P0-10 / P0-11：下载本地检索模型到项目 models/ 目录。

运行：
    uv run python scripts/download_models.py

- Embedding：BAAI/bge-m3        -> models/bge-m3
- 精排：BAAI/bge-reranker-v2-m3  -> models/bge-reranker（CrossEncoder）

默认使用 hf-mirror 镜像（国内更稳）；如需官方源可设置：
    HF_ENDPOINT=https://huggingface.co uv run python scripts/download_models.py

断点续传 + 看门狗：大权重经镜像偶发会长时间无数据，脚本把每次下载放进限时子进程，
超时即终止并基于 .incomplete 分片续传，循环重试直到完成，可安全重复执行。
"""

from __future__ import annotations

import multiprocessing as mp
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
# 放宽单次读超时，减少镜像长连接被判停滞
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "60")
os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "0")

MODELS_DIR = PROJECT_ROOT / "models"

# 名称 -> HF 仓库
REPOS = {
    "bge-m3": "BAAI/bge-m3",
    "bge-reranker": "BAAI/bge-reranker-v2-m3",
}

# 仅跳过 ONNX 运行时副本（体积大且本项目用 PyTorch 推理）。
# 注意：不要排除 pytorch_model.bin —— bge-m3 在镜像上的权重就是该文件名。
IGNORE_PATTERNS = [
    "onnx/*",
    "*.onnx",
    "sentencepiece.bpe.model.orig",
]

MAX_WORKERS = 4
ATTEMPT_TIMEOUT_SECONDS = 420  # 单次尝试 7 分钟无进展则看门狗终止后续传
MAX_ATTEMPTS = 12


def _snapshot_worker(repo_id: str, target: str) -> None:
    """子进程入口：执行一次快照下载（可复用已有 .incomplete 分片）。"""
    from huggingface_hub import snapshot_download

    snapshot_download(
        repo_id=repo_id,
        local_dir=target,
        ignore_patterns=IGNORE_PATTERNS,
        max_workers=MAX_WORKERS,
    )


def _downloaded_size(target: Path) -> int:
    total = 0
    for path in target.rglob("*"):
        if path.is_file():
            try:
                total += path.stat().st_size
            except OSError:
                pass
    return total


def download(name: str, repo_id: str) -> Path:
    target = MODELS_DIR / name
    target.mkdir(parents=True, exist_ok=True)
    print(f"[download] {repo_id} -> {target}", flush=True)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        before = _downloaded_size(target)
        process = mp.Process(target=_snapshot_worker, args=(repo_id, str(target)))
        process.start()
        process.join(ATTEMPT_TIMEOUT_SECONDS)

        if process.is_alive():
            print(
                f"[download] {name} 第 {attempt} 次尝试超过 "
                f"{ATTEMPT_TIMEOUT_SECONDS}s，终止并断点续传……",
                flush=True,
            )
            process.terminate()
            process.join(10)
            if process.is_alive():
                process.kill()
                process.join(10)
            after = _downloaded_size(target)
            print(f"[download] {name} 已下载约 {after / 1024 / 1024:.0f} MB", flush=True)
            if after <= before:
                # 本轮无进展，稍等再试
                time.sleep(5)
            continue
        if process.exitcode == 0:
            print(f"[download] {name} 完成: {target}", flush=True)
            return target
        print(f"[download] {name} 第 {attempt} 次尝试退出码 {process.exitcode}，重试……", flush=True)
        time.sleep(3)

    raise RuntimeError(f"{name} 经过 {MAX_ATTEMPTS} 次尝试仍未下载完成，请检查网络后重跑脚本")


def main() -> None:
    for name, repo_id in REPOS.items():
        download(name, repo_id)
    print("[download] 全部模型就绪，目录：", MODELS_DIR, flush=True)


if __name__ == "__main__":
    mp.freeze_support()
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - 下载脚本需要把错误打印出来便于排查
        print(f"[download] 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
