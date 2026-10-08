# -*- coding: utf-8 -*-
"""知识库增量同步脚本：监测 knowledge/ 目录变化并自动切片入库。

与 ``python -m congclaw.rag``（全量重灌）不同，本脚本用清单文件记录每个文档的
sha256 指纹，只处理真正发生变化的文档：

  - 新增 / 修改 → 复用标准入库管线（Docling 解析 → 父子分片 → BGE-M3 批量向量化
    → Milvus Lite 写向量 + SQLite 写 chunk_meta），同来源幂等覆盖；
  - 删除 → 同步清理该来源在 Milvus 的向量与 SQLite 的分片元信息；
  - 全部无变化时不做任何入库动作，仅输出空变更。

清单默认位于 ``data/knowledge_manifest.json``，首次接入已有知识库时可用
``--baseline`` 只记录当前指纹而不重复入库。

用法：
    uv run python scripts/watch_knowledge.py                # 检查一次并同步（缺省）
    uv run python scripts/watch_knowledge.py --status       # 只显示变更，不入库（干跑）
    uv run python scripts/watch_knowledge.py --baseline     # 仅记录当前指纹，不入库
    uv run python scripts/watch_knowledge.py --watch        # 常驻监听，按间隔轮询自动同步
    uv run python scripts/watch_knowledge.py --dir docs_kb --interval 30 --watch

注意：文档来源名（doc_source）取文件名，同一目录树内不要出现同名文件，否则会互相覆盖。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from congclaw.core.paths import find_project_root
from congclaw.rag import bm25_index, config, store, vectorstore
from congclaw.rag.ingest import ingest_file
from congclaw.rag.parsing import SUPPORTED_SUFFIXES

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = Path("data") / "knowledge_manifest.json"


def file_digest(path: Path) -> str:
    """分块读取文件计算 sha256，避免大文件一次性载入内存。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def scan(root: Path) -> dict[str, dict[str, Any]]:
    """扫描知识目录，返回 {相对路径: 指纹信息} 映射（按路径排序，结果稳定）。"""
    found: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        stat = path.stat()
        found[path.relative_to(root).as_posix()] = {
            "sha256": file_digest(path),
            "size": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            "doc_source": path.name,
        }
    return found


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"files": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"files": {}}
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        return {"files": {}}
    return data


def save_manifest(path: Path, root: Path, files: dict[str, dict[str, Any]]) -> None:
    payload = {
        "root": str(root),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "files": files,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def diff(previous: dict[str, Any], current: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    """对比指纹，返回新增 / 修改 / 删除的相对路径列表。"""
    prev_files: dict[str, Any] = previous.get("files", {})
    added = [rel for rel in current if rel not in prev_files]
    modified = [
        rel
        for rel, meta in current.items()
        if rel in prev_files and prev_files[rel].get("sha256") != meta["sha256"]
    ]
    removed = [rel for rel in prev_files if rel not in current]
    return {
        "added": sorted(added),
        "modified": sorted(modified),
        "removed": sorted(removed),
    }


async def remove_source(doc_source: str) -> None:
    """删除某来源的全部向量与分片元信息（文档被删除时调用）。"""
    await asyncio.to_thread(
        vectorstore.get_vector_store().delete_by_source, doc_source
    )
    await store.delete_source(doc_source)


async def sync_once(
    root: Path,
    manifest_path: Path,
    *,
    apply: bool = True,
) -> dict[str, Any]:
    """执行一次「扫描 → 比对 → 增量入库」，返回变更与处理结果。"""
    current = scan(root)
    manifest = load_manifest(manifest_path)
    changes = diff(manifest, current)
    touched = bool(changes["added"] or changes["modified"] or changes["removed"])

    result: dict[str, Any] = {
        "root": str(root),
        "manifest": str(manifest_path),
        "mode": "apply" if apply else "dry-run",
        "changes": changes,
        "ingested": [],
        "deleted": [],
        "errors": [],
        "total_children": 0,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }

    if not apply or not touched:
        result["changed"] = False
        return result

    for rel in changes["added"] + changes["modified"]:
        path = root / rel
        try:
            stats = await ingest_file(path)
            result["ingested"].append(stats.as_dict())
            result["total_children"] += stats.children
        except Exception as exc:  # noqa: BLE001 —— 单文档失败不阻断整轮同步
            result["errors"].append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})

    for rel in changes["removed"]:
        doc_source = manifest["files"][rel].get("doc_source") or Path(rel).name
        try:
            await remove_source(doc_source)
            result["deleted"].append({"doc_source": doc_source, "path": rel})
        except Exception as exc:  # noqa: BLE001
            result["errors"].append({"path": str(root / rel), "error": f"{type(exc).__name__}: {exc}"})

    if result["deleted"]:
        # 删除不会经过入库管线的 invalidate，需手动失效关键词索引
        bm25_index.invalidate()

    # 库中已无对应来源的文档（如删除动作失败）不写入清单，留待下一轮重试
    failed = {Path(item["path"]).relative_to(root).as_posix() for item in result["errors"]}
    save_manifest(manifest_path, root, {rel: meta for rel, meta in current.items() if rel not in failed})
    result["changed"] = True
    return result


def print_result(result: dict[str, Any], *, quiet_when_unchanged: bool = False) -> None:
    changes = result["changes"]
    if quiet_when_unchanged and not result.get("changed"):
        return
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


async def watch(root: Path, manifest_path: Path, interval: float) -> None:
    """常驻监听：按固定间隔轮询，仅在检测到变更时输出并入库。"""
    print(f"开始监听知识库目录：{root}（间隔 {interval:g}s，Ctrl+C 退出）", flush=True)
    while True:
        try:
            result = await sync_once(root, manifest_path, apply=True)
            print_result(result, quiet_when_unchanged=True)
        except Exception as exc:  # noqa: BLE001 —— 监听循环不因单轮异常退出
            print(f"[{datetime.now().isoformat(timespec='seconds')}] 同步异常: {exc}", flush=True)
        await asyncio.sleep(interval)


async def run_once(
    root: Path, manifest_path: Path, *, apply: bool, baseline: bool
) -> dict[str, Any]:
    if baseline:
        current = scan(root)
        save_manifest(manifest_path, root, current)
        return {
            "root": str(root),
            "manifest": str(manifest_path),
            "mode": "baseline",
            "changes": {"added": [], "modified": [], "removed": []},
            "ingested": [],
            "deleted": [],
            "errors": [],
            "total_children": 0,
            "baseline_files": len(current),
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "changed": False,
        }
    return await sync_once(root, manifest_path, apply=apply)


def resolve_manifest(raw: str | None) -> Path:
    path = Path(raw) if raw else DEFAULT_MANIFEST
    if not path.is_absolute():
        path = find_project_root() / path
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="知识库目录增量同步：检测新增/修改/删除并自动切片入库")
    parser.add_argument("--dir", default=None, help="知识库目录（缺省取 .env 的 KNOWLEDGE_DIR / knowledge）")
    parser.add_argument("--manifest", default=None, help="指纹清单文件（缺省 data/knowledge_manifest.json）")
    parser.add_argument("--status", action="store_true", help="只显示变更，不入库（干跑）")
    parser.add_argument("--baseline", action="store_true", help="仅记录当前指纹，不执行入库")
    parser.add_argument("--watch", action="store_true", help="常驻监听，按间隔轮询自动同步")
    parser.add_argument("--interval", type=float, default=10.0, help="监听轮询间隔秒数（缺省 10）")
    args = parser.parse_args()

    root = Path(args.dir).resolve() if args.dir else config.knowledge_dir()
    if not root.exists():
        print(f"知识库目录不存在：{root}")
        return 2
    manifest_path = resolve_manifest(args.manifest)

    try:
        if args.watch:
            asyncio.run(watch(root, manifest_path, args.interval))
            return 0
        result = asyncio.run(
            run_once(root, manifest_path, apply=not args.status, baseline=args.baseline)
        )
    except KeyboardInterrupt:
        return 130

    print_result(result)
    return 0 if not result["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
