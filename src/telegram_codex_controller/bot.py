from __future__ import annotations

import asyncio
import contextlib
import html

from telegram import BotCommand, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from .assistant_sessions import AssistantSessionManager
from .console import SessionStatusSpec, TelegramConsoleManager
from .config import Settings
from .reply_routes import ReplyRoute, lookup_reply_route, remember_reply_route, send_chunked_message
from .security import ensure_authorized
from .session_manager import SessionManager
from .terminal_mirror import TerminalMirrorManager, _classify_state
from .tmux_monitor import TmuxSessionMonitor, _infer_state_and_summary
from .utils import chunk_text, coalesce_args, compact_summary_text, tail_lines


HELP_TEXT = """Telegram Codex Controller

Existing terminal + tmux control:
/start - help
/help - help
/ping - health check
/forum_on - enable forum/topic mode for the current console chat
/forum_off - disable forum/topic mode for the current console chat
/forum_bootstrap - create INDEX, ALERTS, and topics for currently tracked sessions
/index_here - bind the current chat/topic as INDEX
/alerts_here - bind the current chat/topic as ALERTS
/open <session> - show the current console card for a tracked session
/focus <mirror-session> - bring an existing Terminal Codex tab to the front
/topic_create <session> [topic name] - create and bind a forum topic for a session
/send <session> <text> - send text directly to a mirror/tmux/agent session
/find <session> <pattern> - search recent logs/transcript/history
/recent_errors [limit] - show recent error and waiting sessions
/sessions - list tracked tmux sessions
/run <name> <command...> - start a named command in tmux
/codex <name> <prompt...> - start a Codex CLI task using the configured template
/logs <session> - send a full log/transcript/history document
/tail <session> [lines] - show a short on-demand summary
/stop <name> - stop a tmux session
/shell <command...> - run a shell command when enabled

SDK assistants:
/agents - list tracked SDK sessions
/agent_new <codex|claude> <name> [cwd] - create a tracked SDK session
/agent <name> <prompt...> - send a prompt to an SDK session
/agent_log <name> - show recent transcript
/agent_cwd <name> <cwd> - change working directory and reset resume state
/agent_stop <name> - delete a tracked SDK session

Existing Terminal mirroring:
/mirror - show mirror status
/mirror on - enable auto-mirroring for existing Terminal Codex tabs
/mirror off - disable auto-mirroring
/mirror snapshot [tty-or-alias] - send a fresh snapshot
/mirror alias <tty-or-alias> <alias> - assign a persistent alias like ocna-vpn
/mirror unalias <tty-or-alias> - remove a persistent alias
/mirror aliases - list saved aliases
/mirrors - list currently mirrored Terminal Codex tabs

Reply routing:
Reply to a session message, or send text directly inside that session's forum topic, to route input automatically.
"""

CONTINUE_PROMPT = "find yourself a lead to make you keep continue your optimized work untill all in the plan are completed."

async def _send_chunked_text(
    application: Application,
    chat_id: int,
    text: str,
    route: ReplyRoute | None = None,
    message_thread_id: int | None = None,
) -> None:
    await send_chunked_message(
        application,
        chat_id,
        text,
        message_thread_id=message_thread_id,
        route=route,
    )


def _format_actor_message(actor: str, text: str, *, label: str | None = None) -> tuple[str, str | None]:
    body = text.strip() or "-"
    if actor == "user":
        return f"<b>{html.escape(label or 'You')}</b>\n{html.escape(body)}", "HTML"
    if actor == "codex":
        return f"<b>{html.escape(label or 'Codex')}</b>\n<pre>{html.escape(body)}</pre>", "HTML"
    return body, None


async def _send_actor_message(
    application: Application,
    chat_id: int,
    *,
    actor: str,
    text: str,
    label: str | None = None,
    route: ReplyRoute | None = None,
    message_thread_id: int | None = None,
) -> None:
    settings = application.bot_data["settings"]
    chunk_budget = max(200, settings.max_message_chars - 80)
    for raw_chunk in chunk_text(text.strip() or "-", chunk_budget):
        payload, parse_mode = _format_actor_message(actor, raw_chunk, label=label)
        sent = await application.bot.send_message(
            chat_id=chat_id,
            message_thread_id=message_thread_id,
            text=payload,
            parse_mode=parse_mode,
        )
        if route is not None:
            remember_reply_route(application, chat_id, sent.message_id, route)


async def _send_chunked(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    await _send_chunked_text(
        context.application,
        chat.id,
        text,
        message_thread_id=_thread_id_from_update(update),
    )


async def _send_routed_ack(
    application: Application,
    chat_id: int,
    route: ReplyRoute,
    text: str,
) -> None:
    await _send_chunked_text(
        application,
        chat_id,
        f"[routed {route.label}] {text}",
        route=route,
        message_thread_id=_current_thread_id(application, chat_id),
    )


def _console(application: Application) -> TelegramConsoleManager:
    return application.bot_data["console"]


def _thread_id_from_update(update: Update) -> int | None:
    message = update.effective_message
    return getattr(message, "message_thread_id", None) if message is not None else None


def _current_thread_id(application: Application, chat_id: int) -> int | None:
    return application.bot_data.get("active_threads", {}).get(chat_id)


def _remember_thread(application: Application, update: Update) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    application.bot_data.setdefault("active_threads", {})[chat.id] = _thread_id_from_update(update)


def _remember_thread_from_message(application: Application, message) -> None:
    if message is None or message.chat is None:
        return
    application.bot_data.setdefault("active_threads", {})[message.chat.id] = getattr(message, "message_thread_id", None)


def _set_console_context_from_update(application: Application, update: Update) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    _console(application).set_console_chat(chat.id)
    _remember_thread(application, update)


def _bind_route_to_current_topic(application: Application, update: Update, route: ReplyRoute) -> None:
    thread_id = _thread_id_from_update(update)
    _console(application).set_session_topic(route, thread_id)


def _looks_like_waiting(text: str) -> bool:
    lowered = text.lower()
    return any(
        token in lowered
        for token in [
            "press your yubikey",
            "approve duo",
            "waiting for connection",
            "run /login",
            "please run /login",
            "confirm",
            "manual input",
            "waiting",
            "tab to queue message",
            "queue message",
            "ready for input",
        ]
    )


def tail_text(text: str, line_limit: int = 2) -> str:
    return tail_lines(text.strip(), line_limit) if text.strip() else "-"


def _resolve_route_identifier(application: Application, identifier: str) -> ReplyRoute | None:
    clean = identifier.strip()
    console_route = _console(application).resolve_identifier(clean)
    if console_route is not None:
        return console_route

    assistant_manager: AssistantSessionManager = application.bot_data["assistant_manager"]
    if any(session.name == clean for session in assistant_manager.list_sessions()):
        return ReplyRoute(kind="agent", target=clean)

    tmux_manager: SessionManager = application.bot_data["session_manager"]
    if tmux_manager.session_exists(clean):
        return ReplyRoute(kind="tmux", target=clean)

    mirror_manager: TerminalMirrorManager = application.bot_data["terminal_mirror"]
    target = mirror_manager.get_target(clean)
    if target is not None:
        return ReplyRoute(kind="mirror", target=target.tty)
    return None


async def _record_tmux_status(
    application: Application,
    short_name: str,
    *,
    state: str,
    step: str,
    summary: str,
    event: str | None = None,
    alert: str | None = None,
    force: bool = False,
) -> None:
    await _console(application).update_status(
        SessionStatusSpec(
            route=ReplyRoute(kind="tmux", target=short_name),
            kind="tmux",
            label=short_name,
            title=short_name,
            state=state,
            step=step,
            summary=summary,
            event=event,
            alert=alert,
            force=force,
        )
    )


async def _record_agent_status(
    application: Application,
    name: str,
    *,
    provider: str,
    cwd: str,
    state: str,
    step: str,
    summary: str,
    event: str | None = None,
    alert: str | None = None,
    force: bool = False,
) -> None:
    await _console(application).update_status(
        SessionStatusSpec(
            route=ReplyRoute(kind="agent", target=name),
            kind=provider,
            label=name,
            title=cwd,
            state=state,
            step=step,
            summary=summary,
            event=event,
            alert=alert,
            force=force,
        )
    )


async def _stream_agent_prompt(
    application: Application,
    chat_id: int,
    name: str,
    prompt: str,
) -> bool:
    manager: AssistantSessionManager = application.bot_data["assistant_manager"]
    route = ReplyRoute(kind="agent", target=name)
    saw_output = False
    last_summary = prompt
    session_info = next((item for item in manager.list_sessions() if item.name == name), None)
    provider = session_info.provider if session_info else "agent"
    cwd = session_info.cwd if session_info else ""

    await _record_agent_status(
        application,
        name,
        provider=provider,
        cwd=cwd,
        state="running",
        step="processing prompt",
        summary=compact_summary_text(prompt, max_lines=3, max_line_length=110),
        event=f"🟢 {name} started a new turn.",
    )

    async for event in manager.run_prompt(name, prompt):
        event_type = event.get("type")
        if event_type == "assistant" and event.get("content"):
            saw_output = True
            last_summary = event["content"]
            await _record_agent_status(
                application,
                name,
                provider=provider,
                cwd=cwd,
                state="running",
                step="assistant response",
                summary=compact_summary_text(event["content"], max_lines=3, max_line_length=110),
            )
        elif event_type == "tool" and event.get("toolName"):
            saw_output = True
            last_summary = event.get("content") or event["toolName"]
            await _record_agent_status(
                application,
                name,
                provider=provider,
                cwd=cwd,
                state="running",
                step=f"tool: {event['toolName']}",
                summary=compact_summary_text(last_summary, max_lines=3, max_line_length=110),
            )
        elif event_type == "system" and event.get("content"):
            system_state = "waiting" if _looks_like_waiting(event["content"]) else "error"
            last_summary = event["content"]
            await _record_agent_status(
                application,
                name,
                provider=provider,
                cwd=cwd,
                state=system_state,
                step="system notice",
                summary=compact_summary_text(last_summary, max_lines=3, max_line_length=110),
                event=f"{'🟡' if system_state == 'waiting' else '🔴'} {name}: {tail_text(last_summary)}",
                alert=event["content"],
            )
            if system_state == "error":
                route = ReplyRoute(kind="agent", target=name)
                if _console(application).should_emit_artifact(route, "error_log", event["content"]):
                    await _console(application).send_log_document(
                        route,
                        f"{name}-transcript.log",
                        manager.export_transcript(name),
                        f"Auto error transcript for {name}",
                    )

    await _record_agent_status(
        application,
        name,
        provider=provider,
        cwd=cwd,
        state="done" if saw_output else "idle",
        step="turn complete",
        summary=compact_summary_text(last_summary, max_lines=3, max_line_length=110),
        event=f"✅ {name} finished.",
    )
    return saw_output


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update, context):
        return
    await _send_chunked(update, context, HELP_TEXT)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start_cmd(update, context)


async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update, context):
        return
    await update.effective_chat.send_message("pong")


async def _refresh_route_status(application: Application, route: ReplyRoute, *, force: bool = False) -> None:
    console = _console(application)
    if route.kind == "tmux":
        manager: SessionManager = application.bot_data["session_manager"]
        if not manager.session_exists(route.target):
            await _record_tmux_status(
                application,
                route.target,
                state="stopped",
                step="not running",
                summary="tmux session is not running.",
                force=force,
            )
            return
        logs = manager.export_logs(route.target, 120)
        recent_lines = [line for line in tail_lines(logs, 8).splitlines() if line.strip()]
        state, summary = _infer_state_and_summary(recent_lines)
        await _console(application).update_status(
            SessionStatusSpec(
                route=route,
                kind="tmux",
                label=route.target,
                title=route.target,
                state=state,
                step="manual open",
                summary=compact_summary_text(summary, max_lines=3, max_line_length=110),
                force=force,
            )
        )
        return

    if route.kind == "mirror":
        manager: TerminalMirrorManager = application.bot_data["terminal_mirror"]
        target = manager.get_target(route.target)
        if target is None:
            await console.update_status(
                SessionStatusSpec(
                    route=route,
                    kind="mirror",
                    label=manager.alias_for(route.target) or route.target,
                    title=route.target,
                    state="stopped",
                    step="not detected",
                    summary="Terminal tab is not currently visible.",
                    force=force,
                )
            )
            return
        history = manager.capture_history(route.target)
        summary = compact_summary_text(
            history,
            max_lines=application.bot_data["settings"].console_status_summary_lines,
            max_line_length=110,
        ) or "<no output yet>"
        await console.update_status(
            SessionStatusSpec(
                route=route,
                kind="mirror",
                label=target.alias or target.tty,
                title=target.title,
                state=_classify_state(summary, "running"),
                step="manual open",
                summary=summary,
                force=force,
            )
        )
        return

    if route.kind == "agent":
        manager: AssistantSessionManager = application.bot_data["assistant_manager"]
        session = next((item for item in manager.list_sessions() if item.name == route.target), None)
        if session is None:
            raise ValueError(f"Assistant session '{route.target}' does not exist")
        transcript = manager.export_transcript(route.target)
        summary = compact_summary_text(
            transcript,
            max_lines=application.bot_data["settings"].console_status_summary_lines,
            max_line_length=110,
        ) or "Session ready."
        state = "running" if session.busy else "idle"
        await console.update_status(
            SessionStatusSpec(
                route=route,
                kind=session.provider,
                label=session.name,
                title=session.cwd,
                state=state,
                step="manual open",
                summary=summary,
                force=force,
            )
        )
        return

    raise ValueError(f"Unsupported route kind: {route.kind}")


def _export_route_text(application: Application, route: ReplyRoute) -> tuple[str, str]:
    if route.kind == "tmux":
        manager: SessionManager = application.bot_data["session_manager"]
        return f"{route.target}.log", manager.export_logs(route.target, 4000)
    if route.kind == "mirror":
        manager: TerminalMirrorManager = application.bot_data["terminal_mirror"]
        return f"{manager.describe_tty(route.target).replace(' ', '_')}.log", manager.capture_history(route.target)
    if route.kind == "agent":
        manager: AssistantSessionManager = application.bot_data["assistant_manager"]
        return f"{route.target}-transcript.log", manager.export_transcript(route.target)
    raise ValueError(f"Unsupported route kind: {route.kind}")


async def _bootstrap_current_sessions(application: Application, *, force: bool = False) -> None:
    session_manager: SessionManager = application.bot_data["session_manager"]
    assistant_manager: AssistantSessionManager = application.bot_data["assistant_manager"]
    mirror_manager: TerminalMirrorManager = application.bot_data["terminal_mirror"]

    for session in session_manager.list_sessions():
        await _refresh_route_status(application, ReplyRoute(kind="tmux", target=session.short_name), force=force)

    for session in assistant_manager.list_sessions():
        await _refresh_route_status(application, ReplyRoute(kind="agent", target=session.name), force=force)

    for target in mirror_manager.discover_targets():
        await _refresh_route_status(application, ReplyRoute(kind="mirror", target=target.tty), force=force)


async def _bootstrap_console_layout(application: Application) -> None:
    console = _console(application)
    await _bootstrap_current_sessions(application, force=False)
    if not console.forum_enabled():
        await console.update_index()
        return

    await console.ensure_alerts_ready()
    for route in console.routes():
        await console.ensure_session_topic(route)
    await console.update_index()


async def _stop_route(application: Application, route: ReplyRoute) -> str:
    if route.kind == "tmux":
        manager: SessionManager = application.bot_data["session_manager"]
        manager.stop_session(route.target)
        await _record_tmux_status(
            application,
            route.target,
            state="stopped",
            step="stopped by user",
            summary="tmux session stopped.",
            event=f"⚫ tmux session '{route.target}' stopped.",
            force=True,
        )
        return f"Stopped tmux session {route.target}."
    if route.kind == "agent":
        manager: AssistantSessionManager = application.bot_data["assistant_manager"]
        session_info = next((item for item in manager.list_sessions() if item.name == route.target), None)
        manager.stop_session(route.target)
        if session_info is not None:
            await _record_agent_status(
                application,
                session_info.name,
                provider=session_info.provider,
                cwd=session_info.cwd,
                state="stopped",
                step="stopped by user",
                summary="Assistant session stopped.",
                event=f"⚫ Assistant session '{session_info.name}' stopped.",
            force=True,
        )
        return f"Stopped assistant session {route.target}."
    raise ValueError("Stop is only supported for tmux and assistant sessions.")


async def _send_to_route(
    application: Application,
    chat_id: int,
    route: ReplyRoute,
    payload: str,
) -> str:
    if route.kind == "agent":
        _console(application).request_topic_bump(route, reason="input")
        saw_output = await _stream_agent_prompt(application, chat_id, route.target, payload)
        if not saw_output:
            return f"Assistant session '{route.target}' completed."
        return f"Sent to assistant session {route.target}."

    if route.kind == "tmux":
        manager: SessionManager = application.bot_data["session_manager"]
        manager.send_input(route.target, payload)
        _console(application).request_topic_bump(route, reason="input")
        await _record_tmux_status(
            application,
            route.target,
            state="running",
            step="input sent",
            summary=payload,
            force=True,
        )
        return f"Sent to tmux session {route.target}."

    if route.kind == "mirror":
        manager: TerminalMirrorManager = application.bot_data["terminal_mirror"]
        manager.send_input(route.target, payload)
        _console(application).request_topic_bump(route, reason="input")
        await manager.note_user_input(route.target, payload)
        return f"Sent to Terminal tab {manager.describe_tty(route.target)}."

    raise ValueError(f"Unsupported reply target: {route.kind}")


async def open_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update, context):
        return
    _set_console_context_from_update(context.application, update)
    if len(context.args) != 1:
        await update.effective_chat.send_message("Usage: /open <session>")
        return

    route = _resolve_route_identifier(context.application, context.args[0])
    if route is None:
        await update.effective_chat.send_message(f"Session '{context.args[0]}' is not tracked.")
        return
    _bind_route_to_current_topic(context.application, update, route)
    try:
        await _refresh_route_status(context.application, route, force=True)
    except Exception as exc:
        await update.effective_chat.send_message(f"Failed to open session: {exc}")
        return


async def index_here_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update, context):
        return
    _set_console_context_from_update(context.application, update)
    _console(context.application).set_special_topic("index", _thread_id_from_update(update))
    await _console(context.application).update_index()
    await update.effective_chat.send_message("INDEX is now bound to this chat/topic.")


async def alerts_here_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update, context):
        return
    _set_console_context_from_update(context.application, update)
    _console(context.application).set_special_topic("alerts", _thread_id_from_update(update))
    await update.effective_chat.send_message("ALERTS are now bound to this chat/topic.")


async def forum_on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update, context):
        return
    _set_console_context_from_update(context.application, update)
    console = _console(context.application)
    console.set_forum_enabled(True)
    await update.effective_chat.send_message("Forum/topic mode enabled for this console chat.")


async def forum_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update, context):
        return
    _set_console_context_from_update(context.application, update)
    console = _console(context.application)
    console.set_forum_enabled(False)
    await update.effective_chat.send_message("Forum/topic mode disabled for this console chat.")


async def forum_bootstrap_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update, context):
        return
    _set_console_context_from_update(context.application, update)
    console = _console(context.application)
    console.set_forum_enabled(True)

    try:
        await console.update_index()
        await console.ensure_alerts_ready()
        await _bootstrap_current_sessions(context.application, force=True)
        created = 0
        for route in console.routes():
            topic_id = await console.ensure_session_topic(route)
            if topic_id is not None:
                created += 1
                await _refresh_route_status(context.application, route, force=True)
    except Exception as exc:
        await update.effective_chat.send_message(f"Forum bootstrap failed: {exc}")
        return

    await update.effective_chat.send_message(
        f"Forum bootstrap complete. INDEX/ALERTS ready, ensured topics for {created} sessions."
    )


async def focus_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update, context):
        return
    _set_console_context_from_update(context.application, update)
    if len(context.args) != 1:
        await update.effective_chat.send_message("Usage: /focus <mirror-session>")
        return

    route = _resolve_route_identifier(context.application, context.args[0])
    if route is None or route.kind != "mirror":
        await update.effective_chat.send_message(
            f"Mirror session '{context.args[0]}' was not found."
        )
        return

    manager: TerminalMirrorManager = context.bot_data["terminal_mirror"]
    try:
        label = manager.focus_target(route.target)
    except Exception as exc:
        await update.effective_chat.send_message(f"Failed to focus Terminal tab: {exc}")
        return
    await update.effective_chat.send_message(f"Focused Terminal tab {label}.")


async def topic_create_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update, context):
        return
    _set_console_context_from_update(context.application, update)
    if not context.args:
        await update.effective_chat.send_message("Usage: /topic_create <session> [topic name]")
        return

    route = _resolve_route_identifier(context.application, context.args[0])
    if route is None:
        await update.effective_chat.send_message(f"Session '{context.args[0]}' is not tracked.")
        return

    custom_name = coalesce_args(context.args[1:]) or None
    existing_topic_id = None
    with contextlib.suppress(Exception):
        existing_topic_id = _console(context.application).open_record(route.label).get("topic_id")
    try:
        topic_id = await _console(context.application).create_session_topic(route, topic_name=custom_name)
        await _refresh_route_status(context.application, route, force=True)
    except Exception as exc:
        await update.effective_chat.send_message(f"Failed to create topic: {exc}")
        return
    if existing_topic_id:
        await update.effective_chat.send_message(
            f"Session '{route.target}' is already bound to topic {topic_id}. Reused the existing topic."
        )
        return
    await update.effective_chat.send_message(f"Created and bound topic {topic_id} for session '{route.target}'.")


async def send_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update, context):
        return
    _set_console_context_from_update(context.application, update)
    if len(context.args) < 2:
        await update.effective_chat.send_message("Usage: /send <session> <text>")
        return

    route = _resolve_route_identifier(context.application, context.args[0])
    if route is None:
        await update.effective_chat.send_message(f"Session '{context.args[0]}' is not tracked.")
        return

    payload = coalesce_args(context.args[1:])
    _bind_route_to_current_topic(context.application, update, route)
    try:
        message = await _send_to_route(context.application, update.effective_chat.id, route, payload)
    except Exception as exc:
        await update.effective_chat.send_message(f"Failed to send input: {exc}")
        return
    await update.effective_chat.send_message(message)


async def recent_errors_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update, context):
        return
    limit = 10
    if context.args:
        try:
            limit = max(1, min(20, int(context.args[0])))
        except ValueError:
            await update.effective_chat.send_message("Usage: /recent_errors [limit]")
            return

    rows = _console(context.application).recent_errors(limit)
    if not rows:
        await update.effective_chat.send_message("No recent errors are recorded.")
        return

    lines = ["Recent errors:"]
    for row in rows:
        lines.append(
            f"- {row.get('label', '?')} | {row.get('state', '?')} | {row.get('last_error_at', '?')}\n  {tail_text(row.get('last_error', ''), 2)}"
        )
    await _send_chunked(update, context, "\n".join(lines))


async def find_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update, context):
        return
    _set_console_context_from_update(context.application, update)
    if len(context.args) < 2:
        await update.effective_chat.send_message("Usage: /find <session> <pattern>")
        return

    identifier = context.args[0]
    pattern = coalesce_args(context.args[1:])
    route = _resolve_route_identifier(context.application, identifier)
    if route is None:
        await update.effective_chat.send_message(f"Session '{identifier}' is not tracked.")
        return

    try:
        if route.kind == "mirror":
            payload = context.bot_data["terminal_mirror"].search_history(route.target, pattern)
        elif route.kind == "tmux":
            payload = context.bot_data["session_manager"].search_logs(route.target, pattern)
        elif route.kind == "agent":
            payload = context.bot_data["assistant_manager"].search_transcript(route.target, pattern)
        else:
            payload = f"Unsupported session kind: {route.kind}"
    except Exception as exc:
        await update.effective_chat.send_message(f"Search failed: {exc}")
        return

    await _send_chunked(update, context, payload)


async def sessions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update, context):
        return
    manager: SessionManager = context.bot_data["session_manager"]
    sessions = manager.list_sessions()
    if not sessions:
        await update.effective_chat.send_message("No tracked tmux sessions found.")
        return

    lines = ["Tracked tmux sessions:"]
    for session in sessions:
        lines.append(
            f"- {session.short_name} | windows={session.windows} | attached={session.attached} | created={session.created}"
        )
    await _send_chunked(update, context, "\n".join(lines))


async def run_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update, context):
        return
    _set_console_context_from_update(context.application, update)
    if len(context.args) < 2:
        await update.effective_chat.send_message("Usage: /run <name> <command...>")
        return

    manager: SessionManager = context.bot_data["session_manager"]
    short_name = context.args[0]
    command = coalesce_args(context.args[1:])

    try:
        manager.start_session(short_name, command)
    except Exception as exc:
        await update.effective_chat.send_message(f"Failed to start session: {exc}")
        return

    _bind_route_to_current_topic(context.application, update, ReplyRoute(kind="tmux", target=short_name))
    await _record_tmux_status(
        context.application,
        short_name,
        state="running",
        step="command started",
        summary=command,
        event=f"🟢 tmux session '{short_name}' started.",
        force=True,
    )


async def codex_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update, context):
        return
    _set_console_context_from_update(context.application, update)
    if len(context.args) < 2:
        await update.effective_chat.send_message("Usage: /codex <name> <prompt...>")
        return

    manager: SessionManager = context.bot_data["session_manager"]
    short_name = context.args[0]
    prompt = coalesce_args(context.args[1:])

    try:
        command = manager.render_codex_command(prompt)
        manager.start_session(short_name, command)
    except Exception as exc:
        await update.effective_chat.send_message(f"Failed to start Codex CLI session: {exc}")
        return

    _bind_route_to_current_topic(context.application, update, ReplyRoute(kind="tmux", target=short_name))
    await _record_tmux_status(
        context.application,
        short_name,
        state="running",
        step="codex cli started",
        summary=prompt,
        event=f"🟢 Codex CLI session '{short_name}' started.",
        force=True,
    )


async def logs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update, context):
        return
    _set_console_context_from_update(context.application, update)
    if len(context.args) != 1:
        await update.effective_chat.send_message("Usage: /logs <session>")
        return

    try:
        route = _resolve_route_identifier(context.application, context.args[0])
        if route is None:
            raise ValueError(f"Session '{context.args[0]}' is not tracked.")
        _bind_route_to_current_topic(context.application, update, route)
        filename, payload = _export_route_text(context.application, route)
    except Exception as exc:
        await update.effective_chat.send_message(f"Failed to export logs: {exc}")
        return

    console = _console(context.application)
    if context.bot_data["settings"].console_send_log_documents:
        await console.send_log_document(
            route,
            filename,
            payload,
            f"Logs for {context.args[0]}",
        )
    else:
        await _send_chunked_text(
            context.application,
            update.effective_chat.id,
            f"Logs for '{context.args[0]}':\n\n{payload}",
            route=route,
            message_thread_id=_thread_id_from_update(update),
        )


async def tail_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update, context):
        return
    _set_console_context_from_update(context.application, update)
    if not context.args:
        await update.effective_chat.send_message("Usage: /tail <session> [lines]")
        return

    identifier = context.args[0]
    try:
        line_limit = max(1, min(200, int(context.args[1]))) if len(context.args) > 1 else 50
    except ValueError:
        await update.effective_chat.send_message("Usage: /tail <session> [lines]")
        return

    route = _resolve_route_identifier(context.application, identifier)
    if route is None:
        await update.effective_chat.send_message(f"Session '{identifier}' is not tracked.")
        return

    try:
        _bind_route_to_current_topic(context.application, update, route)
        if route.kind == "tmux":
            payload = context.bot_data["session_manager"].export_logs(route.target, max(120, line_limit * 3))
        elif route.kind == "mirror":
            payload = context.bot_data["terminal_mirror"].capture_history(route.target)
        elif route.kind == "agent":
            payload = context.bot_data["assistant_manager"].export_transcript(route.target)
        else:
            raise ValueError(f"Unsupported session kind: {route.kind}")
    except Exception as exc:
        await update.effective_chat.send_message(f"Failed to read session summary: {exc}")
        return

    actor_label = "Codex" if route.kind in {"mirror", "agent"} else "Output"
    await _send_actor_message(
        context.application,
        update.effective_chat.id,
        actor="codex",
        label=actor_label,
        text=compact_summary_text(payload, max_lines=line_limit, max_line_length=110),
        route=route,
        message_thread_id=_thread_id_from_update(update),
    )


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update, context):
        return
    _set_console_context_from_update(context.application, update)
    if len(context.args) != 1:
        await update.effective_chat.send_message("Usage: /stop <name>")
        return

    manager: SessionManager = context.bot_data["session_manager"]
    short_name = context.args[0]

    try:
        manager.stop_session(short_name)
    except Exception as exc:
        await update.effective_chat.send_message(f"Failed to stop session: {exc}")
        return

    await _record_tmux_status(
        context.application,
        short_name,
        state="stopped",
        step="stopped by user",
        summary="tmux session stopped.",
        event=f"⚫ tmux session '{short_name}' stopped.",
        force=True,
    )


async def shell_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update, context):
        return
    settings: Settings = context.bot_data["settings"]
    if not settings.allow_shell:
        await update.effective_chat.send_message("/shell is disabled. Set ALLOW_SHELL=true to enable it.")
        return
    if not context.args:
        await update.effective_chat.send_message("Usage: /shell <command...>")
        return

    manager: SessionManager = context.bot_data["session_manager"]
    command = coalesce_args(context.args)
    try:
        output = manager.run_shell(command)
    except Exception as exc:
        await update.effective_chat.send_message(f"Shell command failed: {exc}")
        return

    await _send_chunked(update, context, output)


async def agents_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update, context):
        return
    manager: AssistantSessionManager = context.bot_data["assistant_manager"]
    sessions = manager.list_sessions()
    if not sessions:
        await update.effective_chat.send_message("No tracked SDK assistant sessions found.")
        return

    lines = ["Tracked SDK assistant sessions:"]
    for session in sessions:
        status = "busy" if session.busy else "idle"
        resume = "yes" if session.assistant_session_id else "no"
        lines.append(
            f"- {session.name} | provider={session.provider} | cwd={session.cwd} | resumable={resume} | status={status} | updated={session.updated_at}"
        )
    await _send_chunked(update, context, "\n".join(lines))


async def agent_new_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update, context):
        return
    _set_console_context_from_update(context.application, update)
    if len(context.args) < 2:
        await update.effective_chat.send_message("Usage: /agent_new <codex|claude> <name> [cwd]")
        return

    manager: AssistantSessionManager = context.bot_data["assistant_manager"]
    provider = context.args[0]
    name = context.args[1]
    cwd = coalesce_args(context.args[2:]) or None

    try:
        session = manager.create_session(name, provider, cwd)
    except Exception as exc:
        await update.effective_chat.send_message(f"Failed to create assistant session: {exc}")
        return

    _bind_route_to_current_topic(context.application, update, ReplyRoute(kind="agent", target=session.name))
    await _record_agent_status(
        context.application,
        session.name,
        provider=session.provider,
        cwd=session.cwd,
        state="idle",
        step="ready",
        summary=f"Use /agent {session.name} <prompt...> to send prompts.",
        event=f"⚪ Assistant session '{session.name}' created.",
        force=True,
    )


async def agent_cwd_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update, context):
        return
    _set_console_context_from_update(context.application, update)
    if len(context.args) < 2:
        await update.effective_chat.send_message("Usage: /agent_cwd <name> <cwd>")
        return

    manager: AssistantSessionManager = context.bot_data["assistant_manager"]
    name = context.args[0]
    cwd = coalesce_args(context.args[1:])

    try:
        session = manager.set_cwd(name, cwd)
    except Exception as exc:
        await update.effective_chat.send_message(f"Failed to update assistant session cwd: {exc}")
        return

    await _record_agent_status(
        context.application,
        session.name,
        provider=session.provider,
        cwd=session.cwd,
        state="idle",
        step="cwd updated",
        summary="Resume state cleared.",
        event=f"⚪ Assistant session '{session.name}' cwd updated.",
        force=True,
    )


async def agent_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update, context):
        return
    _set_console_context_from_update(context.application, update)
    if len(context.args) < 2:
        await update.effective_chat.send_message("Usage: /agent <name> <prompt...>")
        return

    name = context.args[0]
    prompt = coalesce_args(context.args[1:])
    chat_id = update.effective_chat.id
    _bind_route_to_current_topic(context.application, update, ReplyRoute(kind="agent", target=name))

    try:
        saw_output = await _stream_agent_prompt(context.application, chat_id, name, prompt)
    except Exception as exc:
        session_info = next((item for item in context.bot_data["assistant_manager"].list_sessions() if item.name == name), None)
        if session_info is not None:
            await _record_agent_status(
                context.application,
                name,
                provider=session_info.provider,
                cwd=session_info.cwd,
                state="error",
                step="execution failed",
                summary=str(exc),
                event=f"🔴 Assistant session '{name}' failed.",
                alert=str(exc),
            )
            route = ReplyRoute(kind="agent", target=name)
            if _console(context.application).should_emit_artifact(route, "error_log", str(exc)):
                await _console(context.application).send_log_document(
                    route,
                    f"{name}-transcript.log",
                    context.bot_data["assistant_manager"].export_transcript(name),
                    f"Auto error transcript for {name}",
                )
        await update.effective_chat.send_message(f"Assistant session failed: {exc}")
        return

    session_info = next((item for item in context.bot_data["assistant_manager"].list_sessions() if item.name == name), None)
    if session_info is not None:
        await _record_agent_status(
            context.application,
            name,
            provider=session_info.provider,
            cwd=session_info.cwd,
            state="done",
            step="completed",
            summary="Turn completed successfully.",
            event=f"✅ Assistant session '{name}' completed.",
        )

    if not saw_output:
        await _send_routed_ack(
            context.application,
            chat_id,
            ReplyRoute(kind="agent", target=name),
            "Assistant completed.",
        )


async def agent_log_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update, context):
        return
    _set_console_context_from_update(context.application, update)
    if len(context.args) != 1:
        await update.effective_chat.send_message("Usage: /agent_log <name>")
        return

    manager: AssistantSessionManager = context.bot_data["assistant_manager"]
    try:
        payload = manager.render_transcript(context.args[0])
    except Exception as exc:
        await update.effective_chat.send_message(f"Failed to load assistant transcript: {exc}")
        return
    console = _console(context.application)
    route = ReplyRoute(kind="agent", target=context.args[0])
    _bind_route_to_current_topic(context.application, update, route)
    if context.bot_data["settings"].console_send_log_documents:
        await console.send_log_document(
            route,
            f"{context.args[0]}-transcript.log",
            payload,
            f"Transcript for {context.args[0]}",
        )
    else:
        await _send_chunked_text(
            context.application,
            update.effective_chat.id,
            payload,
            route=route,
        )


async def agent_stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update, context):
        return
    _set_console_context_from_update(context.application, update)
    if len(context.args) != 1:
        await update.effective_chat.send_message("Usage: /agent_stop <name>")
        return

    manager: AssistantSessionManager = context.bot_data["assistant_manager"]
    try:
        session_info = next((item for item in manager.list_sessions() if item.name == context.args[0]), None)
        manager.stop_session(context.args[0])
    except Exception as exc:
        await update.effective_chat.send_message(f"Failed to stop assistant session: {exc}")
        return
    if session_info is not None:
        await _record_agent_status(
            context.application,
            session_info.name,
            provider=session_info.provider,
            cwd=session_info.cwd,
            state="stopped",
            step="stopped by user",
            summary="Assistant session stopped.",
            event=f"⚫ Assistant session '{session_info.name}' stopped.",
            force=True,
        )


async def replied_text_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update, context):
        return
    _set_console_context_from_update(context.application, update)

    chat = update.effective_chat
    message = update.effective_message
    if chat is None or message is None or not message.text:
        return

    route = None
    reply_to = message.reply_to_message
    if reply_to is not None:
        route = _console(context.application).lookup_route_by_message(chat.id, reply_to.message_id)
        if route is None:
            route = lookup_reply_route(
                context.application,
                chat.id,
                reply_to.message_id,
                reply_to.text or reply_to.caption or "",
            )
        if route is None:
            route = _console(context.application).resolve_route_by_topic(chat.id, _thread_id_from_update(update))
    else:
        route = _console(context.application).resolve_route_by_topic(chat.id, _thread_id_from_update(update))

    if route is None:
        await update.effective_chat.send_message(
            "Reply to a session card, or send text directly inside that session's topic."
        )
        return

    payload = message.text.strip()
    if not payload:
        return
    _bind_route_to_current_topic(context.application, update, route)

    try:
        message_text = await _send_to_route(context.application, chat.id, route, payload)
    except Exception as exc:
        await update.effective_chat.send_message(f"Failed to send input: {exc}")
        return
    await _send_routed_ack(context.application, chat.id, route, message_text)


async def callback_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    if not await ensure_authorized(update, context):
        with contextlib.suppress(Exception):
            await query.answer("Unauthorized.", show_alert=True)
        return

    _set_console_context_from_update(context.application, update)
    _remember_thread_from_message(context.application, query.message)

    data = query.data or ""
    parts = data.split("|", 2)
    if len(parts) != 3 or parts[0] not in {"c", "i"}:
        await query.answer("Unknown action.", show_alert=True)
        return

    scope, op, key = parts
    route = None
    if scope == "c":
        route = _console(context.application).resolve_callback_key(key)
        if route is None:
            await query.answer("Session not found.", show_alert=True)
            return

    thread_id = getattr(query.message, "message_thread_id", None) if query.message is not None else None
    chat_id = query.message.chat.id if query.message is not None and query.message.chat is not None else None

    try:
        if scope == "i":
            if op == "r":
                await _console(context.application).update_index()
                await query.answer("INDEX refreshed.")
                return
            if op == "e":
                rows = _console(context.application).recent_errors(10)
                payload = "No recent errors are recorded."
                if rows:
                    lines = ["Recent errors:"]
                    for row in rows:
                        lines.append(
                            f"- {row.get('label', '?')} | {row.get('state', '?')} | {row.get('last_error_at', '?')}\n  {tail_text(row.get('last_error', ''), 2)}"
                        )
                    payload = "\n".join(lines)
                if chat_id is not None:
                    await _send_chunked_text(
                        context.application,
                        chat_id,
                        payload,
                        message_thread_id=thread_id,
                    )
                await query.answer("Recent errors sent.")
                return
            if op == "o":
                route = _console(context.application).resolve_callback_key(key)
                if route is None:
                    raise ValueError("Session not found.")
                if _console(context.application).forum_enabled():
                    await _console(context.application).ensure_session_topic(route)
                await _refresh_route_status(context.application, route, force=True)
                await query.answer(f"Opened {route.target}.")
                return
            await query.answer("Unknown action.", show_alert=True)
            return

        if op == "r":
            await _refresh_route_status(context.application, route, force=True)
            await query.answer("Refreshed.")
            return

        if op == "c":
            if chat_id is None:
                raise ValueError("Chat context is missing.")
            _bind_route_to_current_topic(context.application, update, route)
            await _send_actor_message(
                context.application,
                chat_id,
                actor="user",
                text=CONTINUE_PROMPT,
                route=route,
                message_thread_id=thread_id,
            )
            await _send_to_route(
                context.application,
                chat_id,
                route,
                CONTINUE_PROMPT,
            )
            await query.answer("Continue sent.")
            return

        if op == "t":
            _bind_route_to_current_topic(context.application, update, route)
            _, payload = _export_route_text(context.application, route)
            if chat_id is not None:
                actor_label = "Codex" if route.kind in {"mirror", "agent"} else "Output"
                await _send_actor_message(
                    context.application,
                    chat_id,
                    actor="codex",
                    label=actor_label,
                    text=tail_lines(payload, 50),
                    route=route,
                    message_thread_id=thread_id,
                )
            await query.answer("Recent sent.")
            return

        if op == "l":
            filename, payload = _export_route_text(context.application, route)
            await _console(context.application).send_log_document(
                route,
                filename,
                payload,
                f"Logs for {route.target}",
            )
            await query.answer("Log sent.")
            return

        if op == "e":
            if route.kind == "mirror":
                payload = context.bot_data["terminal_mirror"].search_history(route.target, "error")
            elif route.kind == "tmux":
                payload = context.bot_data["session_manager"].search_logs(route.target, "error")
            elif route.kind == "agent":
                payload = context.bot_data["assistant_manager"].search_transcript(route.target, "error")
            else:
                raise ValueError(f"Unsupported route kind: {route.kind}")
            if chat_id is not None:
                await _send_chunked_text(
                    context.application,
                    chat_id,
                    payload,
                    route=route,
                    message_thread_id=thread_id,
                )
            await query.answer("Search sent.")
            return

        if op == "f":
            if route.kind != "mirror":
                raise ValueError("Focus is only available for mirrored Terminal sessions.")
            label = context.bot_data["terminal_mirror"].focus_target(route.target)
            await query.answer(f"Focused {label}.")
            return

        if op == "x":
            message = await _stop_route(context.application, route)
            await query.answer(message)
            return

        await query.answer("Unknown action.", show_alert=True)
    except Exception as exc:
        message = str(exc).strip() or "Action failed."
        await query.answer(message[:180], show_alert=True)


def _render_mirror_status(manager: TerminalMirrorManager, settings: Settings) -> str:
    lines = [
        f"Terminal mirror enabled: {'yes' if manager.is_enabled() else 'no'}",
        f"Mirror chat IDs: {', '.join(str(chat_id) for chat_id in sorted(settings.mirror_chat_ids))}",
        "",
    ]
    targets = manager.list_active_targets()
    if not targets:
        lines.append("No active Terminal Codex tabs discovered.")
    else:
        lines.append("Active Terminal Codex tabs:")
        for target in targets:
            alias_part = f" | alias={target.alias}" if target.alias else ""
            lines.append(f"- {target.tty}{alias_part} | pid={target.pid} | title={target.title}")

    aliases = manager.list_aliases()
    if aliases:
        lines.extend(["", "Saved aliases:"])
        for tty, alias in aliases.items():
            lines.append(f"- {alias} -> {tty}")
    return "\n".join(lines)


async def mirror_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update, context):
        return
    _set_console_context_from_update(context.application, update)
    settings: Settings = context.bot_data["settings"]
    manager: TerminalMirrorManager = context.bot_data["terminal_mirror"]

    if not context.args or context.args[0] == "status":
        await _send_chunked(update, context, _render_mirror_status(manager, settings))
        return

    action = context.args[0].lower()
    if action == "on":
        await manager.enable(context.application)
        await update.effective_chat.send_message("Terminal mirroring enabled.")
        return
    if action == "off":
        await manager.disable()
        await update.effective_chat.send_message("Terminal mirroring disabled.")
        return
    if action == "snapshot":
        identifier = context.args[1] if len(context.args) > 1 else None
        count = await manager.send_snapshot(identifier)
        await update.effective_chat.send_message(f"Sent {count} mirror snapshot(s).")
        return
    if action == "alias":
        if len(context.args) != 3:
            await update.effective_chat.send_message("Usage: /mirror alias <tty-or-alias> <alias>")
            return
        try:
            tty = manager.set_alias(context.args[1], context.args[2])
        except Exception as exc:
            await update.effective_chat.send_message(f"Failed to set mirror alias: {exc}")
            return
        await manager.refresh_statuses()
        await _console(context.application).update_index()
        await update.effective_chat.send_message(
            f"Mirror alias set: {context.args[2]} -> {tty}"
        )
        return
    if action == "unalias":
        if len(context.args) != 2:
            await update.effective_chat.send_message("Usage: /mirror unalias <tty-or-alias>")
            return
        try:
            tty, alias = manager.clear_alias(context.args[1])
        except Exception as exc:
            await update.effective_chat.send_message(f"Failed to remove mirror alias: {exc}")
            return
        await manager.refresh_statuses()
        await _console(context.application).update_index()
        await update.effective_chat.send_message(f"Removed mirror alias: {alias} -> {tty}")
        return
    if action == "aliases":
        aliases = manager.list_aliases()
        if not aliases:
            await update.effective_chat.send_message("No mirror aliases saved.")
            return
        lines = ["Saved mirror aliases:"]
        for tty, alias in aliases.items():
            lines.append(f"- {alias} -> {tty}")
        await _send_chunked(update, context, "\n".join(lines))
        return

    await update.effective_chat.send_message(
        "Usage: /mirror [status|on|off|snapshot [tty-or-alias]|alias <tty-or-alias> <alias>|unalias <tty-or-alias>|aliases]"
    )


async def mirrors_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await mirror_cmd(update, context)


async def _set_my_commands(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("open", "Open or bind a session in this topic"),
            BotCommand("focus", "Focus an existing Terminal Codex tab"),
            BotCommand("topic_create", "Create a forum topic for a session"),
            BotCommand("send", "Send text directly to a session"),
            BotCommand("forum_on", "Enable forum mode for this chat"),
            BotCommand("forum_off", "Disable forum mode for this chat"),
            BotCommand("forum_bootstrap", "Create INDEX, ALERTS, and session topics"),
            BotCommand("run", "Start a named tmux session"),
            BotCommand("codex", "Start a Codex CLI tmux session"),
            BotCommand("agent_new", "Create a new SDK assistant session"),
            BotCommand("agent", "Send a prompt to an SDK assistant session"),
            BotCommand("tail", "Show a short session summary"),
            BotCommand("logs", "Send a full log or transcript file"),
            BotCommand("find", "Search a session for text"),
            BotCommand("recent_errors", "Show recent error/waiting sessions"),
            BotCommand("mirror", "Manage mirrored Terminal Codex tabs"),
            BotCommand("index_here", "Bind INDEX to this chat/topic"),
            BotCommand("alerts_here", "Bind ALERTS to this chat/topic"),
        ]
    )


async def _post_init(application: Application) -> None:
    async def bootstrap() -> None:
        while not getattr(application, "running", False):
            await asyncio.sleep(0.2)
        console: TelegramConsoleManager = application.bot_data["console"]
        await console.start(application)
        await _set_my_commands(application)
        mirror: TerminalMirrorManager = application.bot_data["terminal_mirror"]
        await mirror.start(application)
        tmux_monitor: TmuxSessionMonitor = application.bot_data["tmux_monitor"]
        await tmux_monitor.start(application)
        await _bootstrap_console_layout(application)

    application.bot_data["startup_task"] = asyncio.create_task(bootstrap())


async def _post_shutdown(application: Application) -> None:
    startup_task: asyncio.Task | None = application.bot_data.get("startup_task")
    if startup_task is not None:
        startup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await startup_task

    mirror: TerminalMirrorManager = application.bot_data["terminal_mirror"]
    await mirror.stop()
    tmux_monitor: TmuxSessionMonitor = application.bot_data["tmux_monitor"]
    await tmux_monitor.stop()
    console: TelegramConsoleManager = application.bot_data["console"]
    await console.stop()


def build_application(settings: Settings) -> Application:
    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    application.bot_data["settings"] = settings
    application.bot_data["session_manager"] = SessionManager(settings)
    application.bot_data["assistant_manager"] = AssistantSessionManager(settings)
    application.bot_data["terminal_mirror"] = TerminalMirrorManager(settings)
    application.bot_data["console"] = TelegramConsoleManager(settings)
    application.bot_data["tmux_monitor"] = TmuxSessionMonitor(
        application.bot_data["session_manager"],
        application.bot_data["console"],
        settings.poll_interval_seconds,
    )
    application.bot_data["reply_routes"] = {}
    application.bot_data["active_threads"] = {}

    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("ping", ping_cmd))
    application.add_handler(CommandHandler("forum_on", forum_on_cmd))
    application.add_handler(CommandHandler("forum_off", forum_off_cmd))
    application.add_handler(CommandHandler("forum_bootstrap", forum_bootstrap_cmd))
    application.add_handler(CommandHandler("index_here", index_here_cmd))
    application.add_handler(CommandHandler("alerts_here", alerts_here_cmd))
    application.add_handler(CommandHandler("open", open_cmd))
    application.add_handler(CommandHandler("focus", focus_cmd))
    application.add_handler(CommandHandler("topic_create", topic_create_cmd))
    application.add_handler(CommandHandler("send", send_cmd))
    application.add_handler(CommandHandler("find", find_cmd))
    application.add_handler(CommandHandler("recent_errors", recent_errors_cmd))
    application.add_handler(CommandHandler("sessions", sessions_cmd))
    application.add_handler(CommandHandler("run", run_cmd))
    application.add_handler(CommandHandler("codex", codex_cmd))
    application.add_handler(CommandHandler("logs", logs_cmd))
    application.add_handler(CommandHandler("tail", tail_cmd))
    application.add_handler(CommandHandler("stop", stop_cmd))
    application.add_handler(CommandHandler("shell", shell_cmd))
    application.add_handler(CommandHandler("agents", agents_cmd))
    application.add_handler(CommandHandler("agent_new", agent_new_cmd))
    application.add_handler(CommandHandler("agent", agent_cmd))
    application.add_handler(CommandHandler("agent_log", agent_log_cmd))
    application.add_handler(CommandHandler("agent_cwd", agent_cwd_cmd))
    application.add_handler(CommandHandler("agent_stop", agent_stop_cmd))
    application.add_handler(CommandHandler("mirror", mirror_cmd))
    application.add_handler(CommandHandler("mirrors", mirrors_cmd))
    application.add_handler(CallbackQueryHandler(callback_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, replied_text_cmd))
    return application
