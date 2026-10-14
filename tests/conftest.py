"""pytest 全局隔离：把每个用例的 SQLite 存储与能力外置目录指向临时目录。

用例常以 ``tmp_path`` 作为工作区（位于系统临时目录，Windows 上即 C 盘）。
若不隔离 ``DB_PATH``，这些临时工作区路径会被写进项目的生产库
``data/telecom_cs.db``，表现为「C 盘 workspace 混入生产会话数据」。
同理，工具/技能的外部目录与目录监听线程也需隔离，避免用例往项目
``data/tools``、``data/skills`` 落文件或留下常驻线程。

用例内部自行 ``monkeypatch.setenv(...)`` 时会覆盖此默认值。
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "data" / "telecom_cs.db"))
    monkeypatch.setenv("TOOLS_EXTRA_DIR", str(tmp_path / "data" / "tools"))
    monkeypatch.setenv("SKILLS_EXTRA_DIR", str(tmp_path / "data" / "skills"))
    monkeypatch.setenv("HOTLOAD_WATCH", "0")
    yield
    from congclaw.db import dispose_engine

    asyncio.run(dispose_engine())
