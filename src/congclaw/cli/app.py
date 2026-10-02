from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated, Literal

import typer
from rich import box
from rich.panel import Panel
from typer.core import TyperGroup

from congclaw.cli.formatter import print_event, safe_echo, safe_secho
from congclaw.cli.sse_client import DEFAULT_API_URL, stream_chat_events
from congclaw.core.approval import ApprovalDecision, ApprovalRequest


class CongClawGroup(TyperGroup):
    """Let ``congclaw "task"`` coexist with real subcommands."""

    def parse_args(self, ctx, args):  # type: ignore[no-untyped-def]
        commands = set(self.commands)
        remaining: list[str] = []
        task_parts: list[str] = []
        index = 0
        while index < len(args):
            arg = args[index]
            if arg in commands or arg == "--help":
                remaining.extend(args[index:])
                break
            if arg.startswith("-"):
                remaining.append(arg)
                if "=" not in arg and index + 1 < len(args) and not args[index + 1].startswith("-"):
                    remaining.append(args[index + 1])
                    index += 2
                    continue
                index += 1
                continue
            task_parts.extend(args[index:])
            break
        if task_parts:
            ctx.obj = dict(ctx.obj or {})
            ctx.obj["task_arg"] = " ".join(task_parts)
        return super().parse_args(ctx, remaining)


app = typer.Typer(
    cls=CongClawGroup,
    help='congclaw: 电信客服智能体演示客户端。使用 `congclaw "问题"` 经 HTTP/SSE 调用后端服务，或 `congclaw tui` 打开 Textual 界面。',
)


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="会话工作区路径。默认为服务端自动分配。"),
    ] = None,
    max_attempts: Annotated[
        int,
        typer.Option("--max-attempts", help="最大重试次数。"),
    ] = 3,
    api_url: Annotated[
        str,
        typer.Option("--api-url", help="FastAPI 服务地址，默认 http://127.0.0.1:8000。"),
    ] = DEFAULT_API_URL,
    approval_mode: Annotated[
        Literal["inline", "auto", "deny"],
        typer.Option("--approval-mode", help="（兼容旧选项，阶段 1 审批交互暂屏蔽）"),
    ] = "inline",
    checkpoint_mode: Annotated[
        Literal["light", "strict", "off"],
        typer.Option("--checkpoint-mode", help="（兼容旧选项，由服务端控制）"),
    ] = "light",
    trace_mode: Annotated[
        Literal["on", "off"],
        typer.Option("--trace-mode", help="（兼容旧选项，由服务端控制）"),
    ] = "on",
    resume: Annotated[
        Path | None,
        typer.Option("--resume", help="（兼容旧选项，阶段 1 暂未接入服务端恢复）"),
    ] = None,
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    configure_console()
    task = None
    if isinstance(ctx.obj, dict):
        task = ctx.obj.get("task_arg")
    if not task and resume is None:
        safe_echo(ctx.get_help())
        raise typer.Exit()

    safe_secho("电信客服智能体 · 阶段 1 FastAPI 服务化", fg=typer.colors.MAGENTA)
    try:
        for event in stream_chat_events(
            task or "",
            api_url=api_url,
            workspace=str(workspace) if workspace is not None else None,
            max_attempts=max_attempts,
        ):
            print_event(event)
    except Exception as exc:
        safe_secho(f"无法连接后端服务 {api_url}：{type(exc).__name__}: {exc}", fg=typer.colors.RED)
        safe_echo("请先启动服务：uvicorn congclaw.api.main:app")
        raise typer.Exit(code=1)


@app.command("tui")
def tui(
    task: Annotated[str | None, typer.Argument(help="可选的初始问题。")] = None,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="会话工作区路径。"),
    ] = None,
    max_attempts: Annotated[
        int,
        typer.Option("--max-attempts", help="最大重试次数。"),
    ] = 3,
    api_url: Annotated[
        str,
        typer.Option("--api-url", help="FastAPI 服务地址，默认 http://127.0.0.1:8000。"),
    ] = DEFAULT_API_URL,
    approval_mode: Annotated[
        Literal["inline", "auto", "deny"],
        typer.Option("--approval-mode", help="（兼容旧选项，阶段 1 审批交互暂屏蔽）"),
    ] = "inline",
    checkpoint_mode: Annotated[
        Literal["light", "strict", "off"],
        typer.Option("--checkpoint-mode", help="（兼容旧选项，由服务端控制）"),
    ] = "light",
    trace_mode: Annotated[
        Literal["on", "off"],
        typer.Option("--trace-mode", help="（兼容旧选项，由服务端控制）"),
    ] = "on",
    resume: Annotated[
        Path | None,
        typer.Option("--resume", help="（兼容旧选项，阶段 1 暂未接入服务端恢复）"),
    ] = None,
) -> None:
    """Open the Textual terminal interface."""
    configure_console()
    from congclaw.cli.tui import CongClawTuiApp

    CongClawTuiApp(
        initial_task=task,
        workspace=workspace,
        max_attempts=max_attempts,
        api_url=api_url,
        approval_mode=approval_mode,
        checkpoint_mode=checkpoint_mode,
        trace_mode=trace_mode,
        resume=resume,
    ).run()


def _inline_approval_handler(request: ApprovalRequest) -> ApprovalDecision:
    from congclaw.cli.formatter import console

    console.print(
        Panel(
            f"Command:\n{request.command}\n\nRisk:\n{request.risk_reason}",
            title=f"Human Approval · {request.tool_name}",
            border_style="yellow",
            box=box.ROUNDED,
        )
    )
    answer = typer.prompt("Approve? [y/N]", default="n", show_default=False).strip().lower()
    console.print()
    approved = answer in {"y", "yes"}
    return ApprovalDecision(approved=approved, reason="" if approved else "Rejected by human operator.")
