"""历史会话列表模态框。

从后端拉取全部会话摘要，供用户选择切换；同时提供「新建会话」入口。
返回值约定：

- 选中某条历史会话 → 返回其 workspace 路径字符串；
- 点击「新建会话」或按 N → 返回 ``"__new__"``；
- 取消/关闭 → 返回 ``None``。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Container, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, OptionList, Static
from textual.widgets.option_list import Option

from congclaw.cli.sse_client import DEFAULT_API_URL, fetch_sessions

NEW_SESSION_MARKER = "__new__"


def _format_time(ts: str) -> str:
    """将 ISO 时间戳压缩为 ``MM-DD HH:MM`` 展示。"""
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts[:16]
    return dt.strftime("%m-%d %H:%M")


def _one_line(text: Any, limit: int) -> str:
    """压缩为单行并截断，避免选项内容换行撑破布局。"""
    text = " ".join(str(text or "").split())
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _session_prompt(item: dict[str, Any], current_workspace: str) -> Text:
    """单行摘要：``▶ session-id · 3轮 · 10-02 08:00 · 问题 → 回答``。"""
    session_id = str(item.get("session_id", ""))
    turns = int(item.get("turn_index", 0) or 0)
    updated = _format_time(str(item.get("updated_at", "")))
    last_task = _one_line(item.get("last_task", "") or "(无提问)", 24)
    last_answer = _one_line(item.get("last_final_answer", ""), 28)
    is_current = str(item.get("workspace", "")) == current_workspace

    prompt = Text()
    prompt.append("▶ " if is_current else "  ", style="bold #7fd6c2")
    prompt.append(session_id, style="bold #f4bf75")
    prompt.append(f"  {turns}轮 · {updated}", style="#9aa4a6")
    prompt.append(f"  {last_task}", style="#d7d1c9")
    if last_answer:
        prompt.append(f" → {last_answer}", style="#7fd6c2")
    if is_current:
        prompt.append("  (当前)", style="#9aa4a6")
    return prompt


class SessionListModal(ModalScreen[str | None]):
    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("n", "new_session", "New Session"),
    ]

    DEFAULT_CSS = """
    SessionListModal {
        align: center middle;
    }

    SessionListModal #session-dialog {
        width: 84;
        max-width: 94%;
        height: 70%;
        min-height: 16;
        border: round #4a8f86;
        background: #151719;
        padding: 1 2;
    }

    SessionListModal #session-title {
        text-style: bold;
        color: #7fd6c2;
        margin-bottom: 1;
    }

    SessionListModal #session-hint {
        color: #8a9294;
        margin-bottom: 1;
    }

    SessionListModal #session-list {
        height: 1fr;
        border: solid #2f3437;
        background: #101113;
        margin-bottom: 1;
    }

    SessionListModal #session-buttons {
        height: auto;
    }
    """

    def __init__(
        self,
        sessions: list[dict[str, Any]] | None = None,
        current_workspace: str = "",
        api_url: str = DEFAULT_API_URL,
    ) -> None:
        super().__init__()
        self._sessions = sessions
        self.current_workspace = current_workspace
        self.api_url = api_url

    def compose(self) -> ComposeResult:
        with Container(id="session-dialog"):
            yield Static("历史会话", id="session-title")
            yield Static("↑/↓ 或鼠标选择 · Enter/点击打开 · N 新建 · Esc 取消", id="session-hint")
            yield OptionList(id="session-list")
            with Horizontal(id="session-buttons"):
                yield Button("+ 新建会话", variant="primary", id="btn-new")
                yield Button("取消 (Esc)", variant="default", id="btn-cancel")

    def on_mount(self) -> None:
        option_list = self.query_one("#session-list", OptionList)
        sessions = self._sessions
        if sessions is None:
            try:
                sessions = fetch_sessions(api_url=self.api_url)
            except Exception as exc:  # noqa: BLE001
                option_list.add_option(Option(f"无法加载会话列表：{type(exc).__name__}: {exc}"))
                self._sessions = []
                return
        self._sessions = sessions
        if not sessions:
            option_list.add_option(Option("暂无历史会话，点击「新建会话」开始。"))
            return
        for idx, item in enumerate(sessions):
            option_list.add_option(
                Option(_session_prompt(item, self.current_workspace), id=str(idx))
            )
        option_list.highlighted = 0
        option_list.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        # OptionList 仅在用户主动选择（点击/Enter）时派发该消息，挂载时不会误触发
        sessions = self._sessions or []
        try:
            idx = int(event.option.id)
        except (TypeError, ValueError):
            return
        if 0 <= idx < len(sessions):
            self.dismiss(str(sessions[idx].get("workspace", "")))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-new":
            self.dismiss(NEW_SESSION_MARKER)
        elif event.button.id == "btn-cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_new_session(self) -> None:
        self.dismiss(NEW_SESSION_MARKER)
