"""SQLite 持久化基础设施。

- 使用 SQLAlchemy 异步引擎 + aiosqlite 驱动；
- 数据库开启 WAL 日志模式，适合本地并发读写；
- :func:`init_db` 幂等执行 ``schema.sql``，供应用启动时调用；
- :mod:`congclaw.db.models` 提供 ORM 模型（阶段 1 起包含 ``session`` 表）。
"""

from __future__ import annotations

from congclaw.db.engine import (
    database_url,
    dispose_engine,
    get_engine,
    init_db,
    resolve_db_path,
    session_scope,
)
from congclaw.db.models import SessionModel

__all__ = [
    "database_url",
    "dispose_engine",
    "get_engine",
    "init_db",
    "resolve_db_path",
    "session_scope",
    "SessionModel",
]
