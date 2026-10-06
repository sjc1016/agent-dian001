from __future__ import annotations

import asyncio
import json
import os
import queue
import threading
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

from dotenv import load_dotenv
from langgraph.graph import add_messages

from congclaw.core.checkpoint import (
    CheckpointManager,
    SessionCheckpointManager,
    load_resume_inputs,
    normalize_checkpoint_mode,
)
from congclaw.core.paths import default_workspace
from congclaw.core.session import (
    aload_or_create_session,
    append_assistant_turn,
    append_user_turn,
    asave_session,
    build_session_context,
    session_started_event,
    session_turn_saved_event,
    session_turn_started_event,
)
from congclaw.core.state import RuntimeState
from congclaw.core.trace import SessionTraceRecorder, TraceRecorder, normalize_trace_mode
from congclaw.graph.profile_store import aconsolidate_user_profile, aget_user_profile
from congclaw.graph.workflow import build_complex_workflow, build_entry_workflow
from congclaw.skills.business_store import DEFAULT_DEMO_PHONE


def create_runtime(
    workspace: Path | None = None,
    *,
    approval_mode: str = "inline",
    approval_handler=None,
    checkpoint_mode: str | None = None,
    resume_from: Path | None = None,
    trace_mode: str | None = None,
) -> RuntimeState:
    load_dotenv()
    selected = workspace or resume_from or default_workspace()
    selected.mkdir(parents=True, exist_ok=True)
    return RuntimeState(
        workspace=selected,
        approval_mode=approval_mode,
        approval_handler=approval_handler,
        bash_default_timeout_seconds=_env_int("CONG_BASH_DEFAULT_TIMEOUT_SECONDS", 120),
        bash_max_timeout_seconds=_env_int("CONG_BASH_MAX_TIMEOUT_SECONDS", 600),
        bash_max_output_chars=_env_int("CONG_BASH_MAX_OUTPUT_CHARS", 6000),
        bash_env_file=_env_path("CONG_BASH_ENV_FILE"),
        checkpoint_mode=normalize_checkpoint_mode(checkpoint_mode or os.getenv("CONG_CHECKPOINT_MODE", "light")),
        resume_from=resume_from,
        trace_mode=normalize_trace_mode(trace_mode or os.getenv("CONG_TRACE_MODE", "on")),
    )


def stream_agent_events(
    task: str | None = None,
    *,
    workspace: Path | None = None,
    max_attempts: int = 3,
    approval_mode: str = "inline",
    approval_handler=None,
    checkpoint_mode: str | None = None,
    resume_workspace: Path | None = None,
    trace_mode: str | None = None,
) -> Iterator[dict[str, Any]]:
    resume_path = resume_workspace.expanduser() if resume_workspace is not None else None
    if resume_path is None:
        # 阶段 4：入口图节点全部原生 async，只能用 astream 驱动；
        # 本函数是给 CLI/历史测试用的同步入口，这里用一次性事件循环收集入口段事件。
        entry_state: dict[str, Any] = {"task": task or "", "messages": []}
        route, entry_events = asyncio.run(_collect_entry_events(entry_state))
        for mode, event in entry_events:
            if mode == "custom":
                yield {"type": "custom_event", "event": event}
            else:
                yield {"type": "graph_event", "event": event}
        if route != "workflow":
            # 阶段 2 起：五类意图均在入口图内完成，不再进入 complex 工作流
            return

    selected_workspace = resume_path or workspace
    state = create_runtime(
        selected_workspace,
        approval_mode=approval_mode,
        approval_handler=approval_handler,
        checkpoint_mode=checkpoint_mode,
        resume_from=resume_path,
        trace_mode=trace_mode,
    )
    workflow = build_complex_workflow()
    yield {"type": "workspace", "path": str(state.workspace)}

    resumed = False
    resume_event: dict[str, Any] | None = None
    if resume_path is not None:
        inputs, resume_event = load_resume_inputs(state, task=task, max_attempts=max_attempts)
        resumed = True
        yield {"type": "custom_event", "event": resume_event}
    else:
        inputs = {
            "task": task or "",
            "runtime": state,
            "messages": [],
            "attempts": 0,
            "max_attempts": max_attempts,
        }

    current_state: dict[str, Any] = dict(inputs)
    manager = CheckpointManager(state, task=str(current_state.get("task", "")))
    trace = TraceRecorder(state, task=str(current_state.get("task", "")))
    trace.start(current_state, resumed=resumed, resume_event=resume_event)
    if resume_event is not None:
        trace.record_custom_event(resume_event)
    started_checkpoint = manager.save(current_state, status="started", latest_node="start")
    if started_checkpoint:
        trace.record_custom_event(started_checkpoint)
    latest_node = "start"

    try:
        for mode, event in workflow.stream(inputs, stream_mode=["updates", "custom"]):
            if mode == "custom":
                trace.record_custom_event(event)
                if _custom_event_needs_checkpoint(event):
                    saved = manager.save(current_state, status="running", latest_node=latest_node, event={"mode": mode, "payload": event})
                    if saved:
                        trace.record_custom_event(saved)
                yield {"type": "custom_event", "event": event}
            else:
                latest_node = _latest_graph_node(event) or latest_node
                _merge_graph_update(current_state, event)
                trace.record_graph_update(event)
                saved = manager.save(current_state, status="running", latest_node=latest_node, event={"mode": mode, "payload": event})
                if saved:
                    trace.record_custom_event(saved)
                yield {"type": "graph_event", "event": event}
    except KeyboardInterrupt:
        saved = manager.save(current_state, status="interrupted", latest_node=latest_node)
        if saved:
            trace.record_custom_event(saved)
            yield {"type": "custom_event", "event": saved}
        trace_event = trace.end(status="interrupted", latest_node=latest_node, final_state=current_state)
        if trace_event:
            yield {"type": "custom_event", "event": trace_event}
        return

    saved = manager.save(current_state, status="finished", latest_node=latest_node)
    if saved:
        trace.record_custom_event(saved)
        yield {"type": "custom_event", "event": saved}
    trace_event = trace.end(status="finished", latest_node=latest_node, final_state=current_state)
    if trace_event:
        yield {"type": "custom_event", "event": trace_event}


async def _collect_entry_events(
    entry_state: dict[str, Any],
) -> tuple[str, list[tuple[str, Any]]]:
    """用 astream 驱动入口图，收集 (mode, event) 序列并同步合并状态到 entry_state。"""
    route = "workflow"
    events: list[tuple[str, Any]] = []
    async for mode, event in build_entry_workflow().astream(
        entry_state, stream_mode=["updates", "custom"]
    ):
        events.append((mode, event))
        if mode == "custom":
            if isinstance(event, dict) and event.get("type") == "intent_decision":
                route = str(event.get("route") or "workflow")
        else:
            _merge_graph_update(entry_state, event)
    return route, events


async def _stream_session_events_native(
    task: str | None = None,
    *,
    session_workspace: Path | None = None,
    max_attempts: int = 3,
    approval_mode: str = "inline",
    phone: str | None = None,
    checkpoint_mode: str | None = None,
    resume_workspace: Path | None = None,
    trace_mode: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """阶段 4：会话事件流的原生 async 实现（无工作线程桥接）。

    会话读写走 aiosqlite，入口图（含 RAG / Agent 两个全 async 子图）由 astream
    直接驱动；五类意图全部在入口图内闭环，不再进入旧 complex 工作流。
    """
    workspace = (resume_workspace or session_workspace or default_workspace()).expanduser()
    workspace.mkdir(parents=True, exist_ok=True)
    # 阶段 6（P6-1）：原生 async 会话路径的 trace 持久化（供评测流水线采集）
    trace = SessionTraceRecorder(
        workspace,
        task=task or "",
        mode=trace_mode or os.getenv("CONG_TRACE_MODE", "on"),
    )
    # 会话检查点：按回合保存，供中断恢复与右侧面板展示（CONG_CHECKPOINT_MODE=off 可关）
    checkpoint = SessionCheckpointManager(
        workspace,
        mode=checkpoint_mode or os.getenv("CONG_CHECKPOINT_MODE", "light"),
        task=task or "",
    )
    session = await aload_or_create_session(workspace)
    resumed = resume_workspace is not None
    started_event = session_started_event(workspace, session, resumed=resumed)
    await trace.record_event(started_event)
    yield {"type": "custom_event", "event": started_event}
    yield {"type": "workspace", "path": str(workspace)}

    if not task:
        trace_event = await trace.end(status="finished")
        if trace_event:
            yield {"type": "custom_event", "event": trace_event}
        return

    # 阶段 5（P5-8）：按手机号装载跨会话长期用户摘要，注入长期记忆层
    bound_phone = str(phone or DEFAULT_DEMO_PHONE)
    user_profile = await aget_user_profile(bound_phone)
    profile_event = {
        "type": "profile_loaded",
        "phone": bound_phone,
        "exists": user_profile is not None,
        "topics": list((user_profile or {}).get("topics") or []),
        "open_tickets": list((user_profile or {}).get("open_tickets") or []),
        "preferred_package": (user_profile or {}).get("preferred_package", ""),
    }
    await trace.record_event(profile_event)
    yield {"type": "custom_event", "event": profile_event}

    # 短期窗口只含本轮之前的历史轮次（当前问题经 task 单独传递）：
    # 否则首轮也会出现长度为 1 的"窗口"，导致查询重写误判为多轮、白调一次模型
    prior_turns = list(session.get("recent_turns", []) or [])

    turn = append_user_turn(session, task)
    await asave_session(workspace, session)
    turn_started_event = session_turn_started_event(workspace, session, turn=turn, task=task)
    await trace.record_event(turn_started_event)
    yield {"type": "custom_event", "event": turn_started_event}

    # 会话检查点（回合开始）：先落一次盘，进程中断后可据此恢复本轮任务
    checkpoint_event = await checkpoint.save(
        {"task": task or "", "attempts": 0, "max_attempts": max_attempts, "todos": []},
        status="running",
        latest_node="entry_graph",
    )
    if checkpoint_event:
        await trace.record_event(checkpoint_event)
        yield {"type": "custom_event", "event": checkpoint_event}

    session_context = await asyncio.to_thread(build_session_context, workspace, session)

    entry_state: dict[str, Any] = {
        "task": task or "",
        "messages": [],
        "session_id": session.get("session_id", ""),
        "session_turn": turn,
        "session_context": session_context,
        # 阶段 2：跨轮对话管控计数与待确认槽位（由会话持久化注入）
        "clarify_count": int(session.get("clarify_count", 0) or 0),
        "unknown_count": int(session.get("unknown_count", 0) or 0),
        "pending_slots": list(session.get("pending_slots", []) or []),
        # 阶段 5：短期会话窗口 + 跨会话长期摘要（四层记忆装配）
        "recent_turns": prior_turns,
        "user_profile": user_profile or {},
        # 阶段 4：业务 Agent 子图入参
        "workspace": str(workspace),
        "phone": bound_phone,
        "approval_mode": approval_mode,
        "max_attempts": max_attempts,
    }

    route = "workflow"
    trace_status = "finished"
    # trace_summary 是否已显式补发给前端；未补发时由 finally 兜底仅落盘，
    # 避免在客户端断开（GeneratorExit）场景下 yield 触发 RuntimeError
    trace_summary_sent = False
    try:
        try:
            async for mode, event in build_entry_workflow().astream(
                entry_state, stream_mode=["updates", "custom"]
            ):
                if mode == "custom":
                    await trace.record_event(event if isinstance(event, dict) else {"type": str(event)})
                    yield {"type": "custom_event", "event": event}
                    if isinstance(event, dict) and event.get("type") == "intent_decision":
                        route = str(event.get("route") or "workflow")
                else:
                    await trace.record_graph_update(event)
                    _merge_graph_update(entry_state, event)
                    yield {"type": "graph_event", "event": event}
        except Exception:
            trace_status = "interrupted"
            raise

        if route == "workflow":
            # 遗留路径：仅旧 complex 工作流（planner/verifier 同步图）仍从此进入。
            final_answer = ""
            complex_events = _stream_complex_workflow(
                task=task,
                workspace=workspace,
                max_attempts=max_attempts,
                approval_mode=approval_mode,
                approval_handler=None,
                checkpoint_mode=checkpoint_mode,
                resume_workspace=resume_workspace,
                trace_mode=trace_mode,
                session=session,
                turn=turn,
                session_context=session_context,
            )
            async for event in _aiter_sync(complex_events):
                final_answer = _final_answer_from_event(event) or final_answer
                yield event
            append_assistant_turn(
                session, turn=turn, route="workflow", content=final_answer, summary=final_answer
            )
            await asave_session(workspace, session)
            saved_event = session_turn_saved_event(workspace, session, turn=turn, route="workflow")
            await trace.record_event(saved_event)
            yield {"type": "custom_event", "event": saved_event}
            # 会话检查点（回合结束）：落盘本轮结果，供中断恢复与右侧面板展示
            checkpoint_event = await checkpoint.save(
                {
                    "task": task or "",
                    "attempts": 0,
                    "max_attempts": max_attempts,
                    "answer": final_answer,
                    "route": "workflow",
                },
                status="finished",
                latest_node="workflow",
            )
            if checkpoint_event:
                await trace.record_event(checkpoint_event)
                yield {"type": "custom_event", "event": checkpoint_event}
            trace_summary_sent = True
            trace_event = await trace.end(status=trace_status)
            if trace_event:
                yield {"type": "custom_event", "event": trace_event}
            return

        # 五类意图全部在入口图内完成（rag_answer / agent_loop / clarify / fallback）
        response = str(entry_state.get("final_answer") or entry_state.get("chat_response") or "")
        session["clarify_count"] = int(entry_state.get("clarify_count", 0) or 0)
        session["unknown_count"] = int(entry_state.get("unknown_count", 0) or 0)
        session["pending_slots"] = list(entry_state.get("pending_slots") or [])
        append_assistant_turn(session, turn=turn, route=route, content=response, summary=response)
        await asave_session(workspace, session)
        saved_event = session_turn_saved_event(workspace, session, turn=turn, route=route)
        await trace.record_event(saved_event)
        yield {"type": "custom_event", "event": saved_event}

        # 阶段 5（P5-7）：回合落库后增量压缩为跨会话长期摘要（水位内幂等；失败不阻断对话）
        if _profile_auto_compress():
            try:
                profile = await aconsolidate_user_profile(
                    bound_phone,
                    workspace=str(workspace),
                    session=session,
                    route=route,
                    response=response,
                    tool_traces=list(entry_state.get("tool_traces") or []),
                )
                prof_event = {
                    "type": "profile_updated",
                    "phone": bound_phone,
                    "topics": list(profile.get("topics") or []),
                    "open_tickets": list(profile.get("open_tickets") or []),
                    "preferred_package": profile.get("preferred_package", ""),
                    "turn_count": int(profile.get("turn_count", 0) or 0),
                    "compression": profile.get("compression", ""),
                }
                await trace.record_event(prof_event)
                yield {"type": "custom_event", "event": prof_event}
            except Exception as exc:  # noqa: BLE001
                err_event = {"type": "profile_update_error", "error": f"{type(exc).__name__}: {exc}"}
                await trace.record_event(err_event)
                yield {"type": "custom_event", "event": err_event}

        # 会话检查点（回合结束）：落盘本轮结果，供中断恢复与右侧面板展示
        checkpoint_event = await checkpoint.save(
            {
                "task": task or "",
                "attempts": 0,
                "max_attempts": max_attempts,
                "answer": response,
                "route": route,
                "session_turn": turn,
            },
            status="finished",
            latest_node=f"entry_graph:{route}",
        )
        if checkpoint_event:
            await trace.record_event(checkpoint_event)
            yield {"type": "custom_event", "event": checkpoint_event}

        # 会话正常结束：补发追踪摘要（含 trace_dir），右侧面板据此显示追踪目录
        trace_summary_sent = True
        trace_event = await trace.end(status=trace_status)
        if trace_event:
            yield {"type": "custom_event", "event": trace_event}
    finally:
        if not trace_summary_sent:
            await trace.end(status=trace_status)


def _profile_auto_compress() -> bool:
    """是否在回合结束后自动压缩长期摘要（PROFILE_AUTO_COMPRESS=0 可关）。"""
    return os.getenv("PROFILE_AUTO_COMPRESS", "1").strip().lower() not in ("0", "false", "no", "off")


async def _aiter_sync(iterator: Iterator[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    """把同步迭代器逐元素搬到工作线程（仅遗留 complex workflow 分支使用）。"""
    sentinel = object()

    def _next():
        try:
            return next(iterator)
        except StopIteration:
            return sentinel
        except BaseException as exc:  # noqa: BLE001
            return exc

    while True:
        item = await asyncio.to_thread(_next)
        if item is sentinel:
            break
        if isinstance(item, BaseException):
            raise item
        yield item


def stream_session_events(
    task: str | None = None,
    *,
    session_workspace: Path | None = None,
    max_attempts: int = 3,
    approval_mode: str = "inline",
    approval_handler=None,
    checkpoint_mode: str | None = None,
    resume_workspace: Path | None = None,
    trace_mode: str | None = None,
) -> Iterator[dict[str, Any]]:
    """同步会话事件流：阶段 4 起为原生 async 核心的线程桥接适配层（供 CLI/历史测试）。

    生产 HTTP 链路请使用 :func:`stream_session_events_async`，全程无桥接。
    """
    yield from _iter_async(
        _stream_session_events_native(
            task,
            session_workspace=session_workspace,
            max_attempts=max_attempts,
            approval_mode=approval_mode,
            checkpoint_mode=checkpoint_mode,
            resume_workspace=resume_workspace,
            trace_mode=trace_mode,
        )
    )


def _stream_complex_workflow(
    *,
    task: str | None,
    workspace: Path,
    max_attempts: int,
    approval_mode: str,
    approval_handler,
    checkpoint_mode: str | None,
    resume_workspace: Path | None,
    trace_mode: str | None,
    session: dict[str, Any] | None = None,
    turn: int | None = None,
    session_context: str = "",
) -> Iterator[dict[str, Any]]:
    resume_path = resume_workspace.expanduser() if resume_workspace is not None else None
    state = create_runtime(
        workspace,
        approval_mode=approval_mode,
        approval_handler=approval_handler,
        checkpoint_mode=checkpoint_mode,
        resume_from=resume_path,
        trace_mode=trace_mode,
    )
    workflow = build_complex_workflow()

    resumed = False
    resume_event: dict[str, Any] | None = None
    if resume_path is not None:
        inputs, resume_event = load_resume_inputs(state, task=task, max_attempts=max_attempts)
        resumed = True
        yield {"type": "custom_event", "event": resume_event}
    else:
        inputs = {
            "task": task or "",
            "runtime": state,
            "messages": [],
            "attempts": 0,
            "max_attempts": max_attempts,
        }

    if session is not None:
        inputs["session_id"] = session.get("session_id", "")
    if turn is not None:
        inputs["session_turn"] = turn
    if session_context:
        inputs["session_context"] = session_context
    metadata = dict(inputs.get("metadata", {}))
    if session is not None:
        metadata["session_id"] = session.get("session_id", "")
    if turn is not None:
        metadata["session_turn"] = turn
    if metadata:
        inputs["metadata"] = metadata

    current_state: dict[str, Any] = dict(inputs)
    manager = CheckpointManager(state, task=str(current_state.get("task", "")))
    trace = TraceRecorder(state, task=str(current_state.get("task", "")))
    trace.start(current_state, resumed=resumed, resume_event=resume_event)
    if resume_event is not None:
        trace.record_custom_event(resume_event)
    started_checkpoint = manager.save(current_state, status="started", latest_node="start")
    if started_checkpoint:
        trace.record_custom_event(started_checkpoint)
    latest_node = "start"

    try:
        for mode, event in workflow.stream(inputs, stream_mode=["updates", "custom"]):
            if mode == "custom":
                trace.record_custom_event(event)
                if _custom_event_needs_checkpoint(event):
                    saved = manager.save(current_state, status="running", latest_node=latest_node, event={"mode": mode, "payload": event})
                    if saved:
                        trace.record_custom_event(saved)
                yield {"type": "custom_event", "event": event}
            else:
                latest_node = _latest_graph_node(event) or latest_node
                _merge_graph_update(current_state, event)
                trace.record_graph_update(event)
                saved = manager.save(current_state, status="running", latest_node=latest_node, event={"mode": mode, "payload": event})
                if saved:
                    trace.record_custom_event(saved)
                yield {"type": "graph_event", "event": event}
    except KeyboardInterrupt:
        saved = manager.save(current_state, status="interrupted", latest_node=latest_node)
        if saved:
            trace.record_custom_event(saved)
            yield {"type": "custom_event", "event": saved}
        trace_event = trace.end(status="interrupted", latest_node=latest_node, final_state=current_state)
        if trace_event:
            yield {"type": "custom_event", "event": trace_event}
        return

    saved = manager.save(current_state, status="finished", latest_node=latest_node)
    if saved:
        trace.record_custom_event(saved)
        yield {"type": "custom_event", "event": saved}
    trace_event = trace.end(status="finished", latest_node=latest_node, final_state=current_state)
    if trace_event:
        yield {"type": "custom_event", "event": trace_event}


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _env_path(name: str) -> Path | None:
    raw = os.getenv(name, "").strip()
    return Path(raw).expanduser() if raw else None


def _latest_graph_node(event: Any) -> str | None:
    if isinstance(event, dict) and event:
        return str(next(reversed(event)))
    return None


def _merge_graph_update(state: dict[str, Any], event: Any) -> None:
    if not isinstance(event, dict):
        return
    for update in event.values():
        if not isinstance(update, dict):
            continue
        for key, value in update.items():
            if key == "messages":
                state["messages"] = list(add_messages(state.get("messages", []), value))
            else:
                state[key] = value


def _custom_event_needs_checkpoint(event: Any) -> bool:
    if not isinstance(event, dict):
        return False
    if event.get("type") != "tool_result":
        return False
    result = event.get("result")
    if not isinstance(result, dict):
        return False
    return result.get("ok") is False or bool(result.get("requires_approval"))


def _final_answer_from_event(event: dict[str, Any]) -> str:
    if event.get("type") != "graph_event":
        return ""
    payload = event.get("event")
    if not isinstance(payload, dict):
        return ""
    update = payload.get("final")
    if not isinstance(update, dict):
        return ""
    return str(update.get("final_answer") or "")


# ---------------------------------------------------------------------------
# 阶段 4：原生 async 会话流 + 同步适配层 + SSE 格式转换
#
# HTTP/SSE 链路（chat.py）直接 async for 原生 async 生成器
# ``_stream_session_events_native``：会话 aiosqlite 读写、入口图 astream、
# RAG/Agent 子图全部原生 async，全链路无工作线程桥接。
#
# 仅历史同步调用方（CLI 直调/旧测试）通过 ``_iter_async`` 在独立线程的
# 一次性事件循环里驱动同一 async 核心，事件经队列回流为同步迭代器。
# ---------------------------------------------------------------------------


_SENTINEL = object()


async def stream_session_events_async(
    task: str | None = None,
    *,
    session_workspace: Path | None = None,
    max_attempts: int = 3,
    approval_mode: str = "inline",
    approval_handler=None,
    phone: str | None = None,
    checkpoint_mode: str | None = None,
    resume_workspace: Path | None = None,
    trace_mode: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """会话事件流的原生 async 版本（FastAPI SSE 直接消费，无任何桥接线程）。"""
    async for event in _stream_session_events_native(
        task,
        session_workspace=session_workspace,
        max_attempts=max_attempts,
        approval_mode=approval_mode,
        phone=phone,
        checkpoint_mode=checkpoint_mode,
        resume_workspace=resume_workspace,
        trace_mode=trace_mode,
    ):
        yield event


def _iter_async(aiterator: AsyncIterator[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """把 async 生成器适配为同步迭代器：独立线程跑一次性事件循环 + 队列回流。"""
    outbox: queue.Queue[Any] = queue.Queue()

    def _worker() -> None:
        async def _drain() -> None:
            try:
                async for event in aiterator:
                    outbox.put(event)
            except BaseException as exc:  # noqa: BLE001
                outbox.put(exc)
            finally:
                outbox.put(_SENTINEL)

        asyncio.run(_drain())

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    while True:
        item = outbox.get()
        if item is _SENTINEL:
            break
        if isinstance(item, BaseException):
            raise item
        yield item
    thread.join(timeout=1)


def event_to_sse(event: dict[str, Any]) -> str:
    """将事件字典转换为 SSE ``data:`` 帧。"""
    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
