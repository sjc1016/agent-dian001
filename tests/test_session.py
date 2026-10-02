from __future__ import annotations

import asyncio

import pytest

from congclaw.core.session import (
    append_assistant_turn,
    append_user_turn,
    build_session_context,
    load_or_create_session,
    save_session,
    session_summary_file,
)
from congclaw.db import dispose_engine


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """每个测试用例使用独立的临时数据库，避免跨用例污染。"""
    db_path = tmp_path / "data" / "telecom_cs.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    yield
    asyncio.run(dispose_engine())


def test_session_create_save_and_context(tmp_path) -> None:
    session = load_or_create_session(tmp_path)
    turn = append_user_turn(session, "帮我创建一个 app.py")
    append_assistant_turn(session, turn=turn, route="workflow", content="created app.py")
    save_session(tmp_path, session)

    # 摘要文件仍会写出（便于事件展示与调试）
    assert session_summary_file(tmp_path).exists()

    # 重启后从 SQLite 重新加载，历史不丢失
    reloaded = load_or_create_session(tmp_path)
    assert reloaded["turn_index"] == 1
    assert reloaded["last_route"] == "workflow"

    context = build_session_context(tmp_path, reloaded)
    assert "帮我创建一个 app.py" in context
    assert "created app.py" in context


def test_session_history_is_compacted(tmp_path) -> None:
    session = load_or_create_session(tmp_path)

    for idx in range(25):
        turn = append_user_turn(session, f"user turn {idx}")
        append_assistant_turn(session, turn=turn, route="chat", content=f"assistant turn {idx}")

    save_session(tmp_path, session)
    reloaded = load_or_create_session(tmp_path)

    assert len(reloaded["recent_turns"]) <= 18
    assert "user turn 0" in reloaded["summary"]


def test_session_pending_slots_persisted(tmp_path) -> None:
    session = load_or_create_session(tmp_path)
    # 阶段 2：pending_slots 统一为字符串列表（待补槽位名）
    session["pending_slots"] = ["套餐档位", "联系电话"]
    save_session(tmp_path, session)

    reloaded = load_or_create_session(tmp_path)
    assert reloaded["pending_slots"] == ["套餐档位", "联系电话"]

    # 阶段 2：追问/unknown 计数随会话持久化
    session["clarify_count"] = 3
    session["unknown_count"] = 2
    save_session(tmp_path, session)

    reloaded = load_or_create_session(tmp_path)
    assert reloaded["clarify_count"] == 3
    assert reloaded["unknown_count"] == 2
