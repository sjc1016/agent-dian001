"""RAG 知识库入库命令行。

用法：
    uv run python -m congclaw.rag                # 入库 knowledge/ 全量
    uv run python -m congclaw.rag path/to/a.md   # 入库指定文件/目录
    uv run python -m congclaw.rag stats          # 查看知识库规模
"""

from __future__ import annotations

import argparse
import asyncio
import json

from congclaw.rag.ingest import ingest_directory, ingest_file
from congclaw.rag.retrieval import corpus_stats


async def _run_ingest(paths: list[str]) -> None:
    if not paths:
        batch = await ingest_directory()
        print(json.dumps(batch.as_dict(), ensure_ascii=False, indent=2))
        return
    for raw_path in paths:
        from pathlib import Path

        path = Path(raw_path)
        if path.is_dir():
            batch = await ingest_directory(path)
            print(json.dumps(batch.as_dict(), ensure_ascii=False, indent=2))
        else:
            stats = await ingest_file(path)
            print(json.dumps(stats.as_dict(), ensure_ascii=False, indent=2))


async def _run_stats() -> None:
    print(json.dumps(await corpus_stats(), ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="电信客服知识库入库工具")
    parser.add_argument("paths", nargs="*", help="待入库的文件/目录（缺省入库 knowledge/）")
    parser.add_argument("--stats", action="store_true", help="只查看知识库规模")
    args = parser.parse_args()
    if args.stats:
        asyncio.run(_run_stats())
    else:
        asyncio.run(_run_ingest(args.paths))


if __name__ == "__main__":
    main()
