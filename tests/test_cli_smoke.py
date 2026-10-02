from __future__ import annotations

from typer.testing import CliRunner

from congclaw.cli.app import app


def test_cli_shows_help_without_task() -> None:
    runner = CliRunner()

    result = runner.invoke(app, [])

    assert result.exit_code == 0
    assert "congclaw" in result.output


def test_cli_accepts_max_attempts_option_without_task() -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["--max-attempts", "2"])

    assert result.exit_code == 0
    assert "congclaw" in result.output


def test_cli_accepts_approval_mode_option_without_task() -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["--approval-mode", "deny"])

    assert result.exit_code == 0
    assert "congclaw" in result.output


def test_cli_accepts_checkpoint_mode_option_without_task() -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["--checkpoint-mode", "strict"])

    assert result.exit_code == 0
    assert "congclaw" in result.output


def test_cli_accepts_trace_mode_option_without_task() -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["--trace-mode", "off"])

    assert result.exit_code == 0
    assert "congclaw" in result.output


def test_cli_accepts_resume_option_without_task(monkeypatch, tmp_path) -> None:
    runner = CliRunner()
    calls = []

    def fake_stream(*args, **kwargs):
        calls.append((args, kwargs))
        yield {"type": "workspace", "path": str(tmp_path)}

    monkeypatch.setattr("congclaw.cli.app.stream_chat_events", fake_stream)
    result = runner.invoke(app, ["--resume", str(tmp_path)])

    assert result.exit_code == 0
    assert calls


def test_cli_passes_max_attempts(monkeypatch, tmp_path) -> None:
    runner = CliRunner()
    calls = []

    def fake_stream(*args, **kwargs):
        calls.append((args, kwargs))
        yield {"type": "workspace", "path": str(tmp_path)}

    monkeypatch.setattr("congclaw.cli.app.stream_chat_events", fake_stream)
    result = runner.invoke(app, ["--max-attempts", "5", "demo task"])

    assert result.exit_code == 0
    assert calls
    assert calls[0][1]["max_attempts"] == 5


def test_cli_passes_api_url(monkeypatch, tmp_path) -> None:
    runner = CliRunner()
    calls = []

    def fake_stream(*args, **kwargs):
        calls.append((args, kwargs))
        yield {"type": "workspace", "path": str(tmp_path)}

    monkeypatch.setattr("congclaw.cli.app.stream_chat_events", fake_stream)
    result = runner.invoke(app, ["--api-url", "http://localhost:9999", "demo task"])

    assert result.exit_code == 0
    assert calls
    assert calls[0][1]["api_url"] == "http://localhost:9999"


def test_cli_natural_task_entry_still_works(monkeypatch, tmp_path) -> None:
    runner = CliRunner()
    calls = []

    def fake_stream(*args, **kwargs):
        calls.append((args, kwargs))
        yield {"type": "workspace", "path": str(tmp_path)}

    monkeypatch.setattr("congclaw.cli.app.stream_chat_events", fake_stream)

    result = runner.invoke(app, ["demo task"])

    assert result.exit_code == 0
    assert calls[0][0][0] == "demo task"
