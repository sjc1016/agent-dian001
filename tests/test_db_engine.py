from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite
import pytest
from sqlalchemy import text

from mokioclaw.db import dispose_engine, init_db, resolve_db_path, session_scope


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "data" / "telecom_cs.db"
    monkeypatch.setenv("DB_PATH", str(path))

    async def _dispose() -> None:
        await dispose_engine()

    asyncio.run(_dispose())
    yield path
    asyncio.run(_dispose())


async def _pragma(db_path: Path, name: str) -> object:
    async with aiosqlite.connect(db_path) as connection:
        rows = await connection.execute_fetchall(f"PRAGMA {name}")
    return rows[0][0]


def test_resolve_db_path_honours_env(db_path: Path) -> None:
    assert resolve_db_path() == db_path


def test_init_db_creates_file_with_wal_and_version(db_path: Path) -> None:
    result = asyncio.run(init_db())

    assert result == db_path
    assert db_path.exists()
    assert asyncio.run(_pragma(db_path, "journal_mode")) == "wal"

    async def read_version() -> str:
        async with aiosqlite.connect(db_path) as connection:
            rows = await connection.execute_fetchall(
                "SELECT value FROM app_meta WHERE key = 'schema_version'"
            )
        return str(rows[0][0])

    assert asyncio.run(read_version()) == "0"


def test_init_db_is_idempotent(db_path: Path) -> None:
    asyncio.run(init_db())
    asyncio.run(init_db())  # 再次执行不应报错

    assert db_path.exists()


async def _write_and_read() -> tuple[object, object, object]:
    async with session_scope() as session:
        await session.execute(
            text("INSERT INTO app_meta (key, value) VALUES ('k', 'v')")
        )
    async with session_scope() as session:
        journal = (await session.execute(text("PRAGMA journal_mode"))).scalar()
        foreign_keys = (await session.execute(text("PRAGMA foreign_keys"))).scalar()
        value = (
            await session.execute(text("SELECT value FROM app_meta WHERE key = 'k'"))
        ).scalar()
    return journal, foreign_keys, value  # type: ignore[return-value]


def test_session_scope_roundtrip_and_pragmas(db_path: Path) -> None:
    asyncio.run(init_db())

    journal, foreign_keys, value = asyncio.run(_write_and_read())

    assert journal == "wal"
    assert foreign_keys == 1
    assert value == "v"
