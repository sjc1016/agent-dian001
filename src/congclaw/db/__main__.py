"""手动初始化数据库入口。

阶段 0 用 ``uv run python -m congclaw.db`` 验证建库；
阶段 1 起由 FastAPI 生命周期（P1-1）自动调用 :func:`congclaw.db.init_db`。
"""

from __future__ import annotations

import asyncio

import aiosqlite

from congclaw.db.engine import init_db, resolve_db_path


async def _main() -> None:
    path = await init_db()
    async with aiosqlite.connect(path) as connection:
        journal_mode = (await connection.execute_fetchall("PRAGMA journal_mode"))[0][0]
        version_rows = await connection.execute_fetchall(
            "SELECT value FROM app_meta WHERE key = 'schema_version'"
        )
    print(f"数据库已初始化: {path}")
    print(f"journal_mode = {journal_mode}")
    print(f"schema_version = {version_rows[0][0] if version_rows else '(missing)'}")


if __name__ == "__main__":
    asyncio.run(_main())
