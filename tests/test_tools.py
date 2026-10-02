from __future__ import annotations

from pathlib import Path

from congclaw.core.state import RuntimeState
from congclaw.tools.registry import build_read_only_tools, build_tools
from congclaw.tools.todo_tool import persist_todos, render_todo_markdown, update_todo, write_todos


def make_state(tmp_path: Path) -> RuntimeState:
    return RuntimeState(workspace=tmp_path)


def test_tool_registry_is_empty_after_domain_slimdown(tmp_path: Path) -> None:
    state = make_state(tmp_path)

    # 代码类工具已删除；阶段 0 注册表里不应再有任何工具，阶段 4 由 Skill 注册中心取代。
    assert build_tools(state) == []
    assert build_read_only_tools(state) == []


def test_todo_write_tool_records_plan_parts() -> None:
    result = write_todos(
        ["write tests", "implement"],
        ["tests pass"],
        ["python -m pytest -q"],
    )

    assert result["ok"] is True
    assert result["todos"] == ["write tests", "implement"]
    assert result["acceptance_criteria"] == ["tests pass"]
    assert result["verification_commands"] == ["python -m pytest -q"]


def test_todo_write_tool_normalizes_json_strings() -> None:
    result = write_todos(
        '[{"title": "write tests"}, {"title": "implement"}]',
        "- tests pass\n- demo runs",
        '["python -m pytest -q"]',
    )

    assert result["todos"] == ["write tests", "implement"]
    assert result["acceptance_criteria"] == ["tests pass", "demo runs"]
    assert result["verification_commands"] == ["python -m pytest -q"]


def test_todo_write_tool_accepts_id_keyed_description_dict() -> None:
    result = write_todos(
        '{"todo-1": {"status": "completed", "description": "Research Qwen"}, "todo-2": {"description": "Write HTML"}}',
        '["HTML exists"]',
        '["ls -la qwen.html"]',
    )

    assert result["ok"] is True
    assert result["todos"] == ["Research Qwen", "Write HTML"]
    assert result["acceptance_criteria"] == ["HTML exists"]
    assert result["verification_commands"] == ["ls -la qwen.html"]


def test_todo_update_tool_updates_existing_todo() -> None:
    todos = [{"id": "todo-1", "content": "write tests", "status": "pending", "note": ""}]

    result = update_todo(todos, "todo-1", "completed", "tests written")

    assert result["ok"] is True
    assert result["todos"][0]["status"] == "completed"
    assert result["todos"][0]["note"] == "tests written"


def test_todo_update_tool_rejects_unknown_todo() -> None:
    todos = [{"id": "todo-1", "content": "write tests", "status": "pending", "note": ""}]

    result = update_todo(todos, "todo-2", "completed")

    assert result["ok"] is False
    assert result["todos"][0]["status"] == "pending"


def test_todo_markdown_persistence(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    todos = [{"id": "todo-1", "content": "write page", "status": "pending", "note": ""}]

    result = persist_todos(state, todos, ["page exists"], ["python --version"], "demo plan")

    content = (tmp_path / "TODO.md").read_text(encoding="utf-8")
    assert result["ok"] is True
    assert "demo plan" in content
    assert "todo-1" in content
    assert "python --version" in content


def test_render_todo_markdown_marks_completed() -> None:
    content = render_todo_markdown(
        [{"id": "todo-1", "content": "done", "status": "completed", "note": "verified"}],
        [],
        [],
    )

    assert "- [x]" in content
    assert "verified" in content
