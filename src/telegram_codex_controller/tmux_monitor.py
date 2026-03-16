from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Dict

from telegram.ext import Application

from .console import SessionStatusSpec, TelegramConsoleManager
from .reply_routes import ReplyRoute
from .session_manager import SessionManager
from .utils import compact_summary_lines, compact_summary_text, tail_lines


LOG = logging.getLogger(__name__)


@dataclass
class TmuxState:
    state: str
    summary: str
    recent_lines: list[str]


class TmuxSessionMonitor:
    def __init__(
        self,
        session_manager: SessionManager,
        console: TelegramConsoleManager,
        poll_interval_seconds: int,
    ) -> None:
        self.session_manager = session_manager
        self.console = console
        self.poll_interval_seconds = poll_interval_seconds
        self._application: Application | None = None
        self._task: asyncio.Task | None = None
        self._states: Dict[str, TmuxState] = {}
        self._bootstrapped = False

    async def start(self, application: Application) -> None:
        self._application = application
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        while True:
            try:
                await self._poll_once()
            except Exception:
                LOG.exception("tmux session monitor failed")
            await asyncio.sleep(self.poll_interval_seconds)

    async def _poll_once(self) -> None:
        current = {session.short_name: session for session in self.session_manager.list_sessions()}

        for name in list(self._states):
            if name in current:
                continue
            previous = self._states.pop(name)
            route = ReplyRoute(kind="tmux", target=name)
            await self.console.update_status(
                SessionStatusSpec(
                    route=route,
                    kind="tmux",
                    label=name,
                    title=name,
                    state="done",
                    step="session ended",
                    summary=previous.summary or "Session ended.",
                    event=f"✅ {name} finished",
                    alert=f"✅ {name} finished",
                    force=True,
                )
            )

        for name in current:
            logs = self.session_manager.capture_logs(name, 120)
            raw_lines = [line for line in tail_lines(logs, 12).splitlines() if line.strip()]
            state, _raw_summary = _infer_state_and_summary(raw_lines)
            summary = compact_summary_text(
                logs,
                max_lines=3,
                max_line_length=110,
            )
            recent_lines = compact_summary_lines(
                logs,
                max_lines=8,
                max_line_length=110,
            )
            route = ReplyRoute(kind="tmux", target=name)
            previous = self._states.get(name)
            self._states[name] = TmuxState(state=state, summary=summary, recent_lines=recent_lines)

            event = None
            alert = None
            force = False
            if previous is None:
                if self._bootstrapped:
                    event = f"🟢 {name} started"
                force = True
            elif previous.state != state:
                force = True
                if state == "error":
                    event = f"🔴 {name} error: {summary}"
                    alert = event
                elif state == "waiting":
                    event = f"🟡 {name} waiting: {summary}"
                    alert = event

            await self.console.update_status(
                SessionStatusSpec(
                    route=route,
                    kind="tmux",
                    label=name,
                    title=name,
                    state=state,
                    step="watching output",
                    summary=summary,
                    event=event,
                    alert=alert,
                    force=force,
                )
            )

            if state == "error" and previous is not None and previous.state != "error":
                route = ReplyRoute(kind="tmux", target=name)
                if self.console.should_emit_artifact(route, "error_log", summary):
                    await self.console.send_log_document(
                        route,
                        f"{name}.log",
                        self.session_manager.export_logs(name, 4000),
                        f"Auto error log for {name}",
                    )
        self._bootstrapped = True


def _infer_state_and_summary(lines: list[str]) -> tuple[str, str]:
    if not lines:
        return "running", "<no output yet>"

    lowered_lines = [line.lower() for line in lines]
    if any(
        token in line
        for line in lowered_lines
        for token in ["traceback", "exception", "error", "failed", "fatal"]
    ):
        return "error", lines[-1]
    if any(
        token in line
        for line in lowered_lines
        for token in [
            "press your yubikey",
            "approve duo",
            "waiting for connection",
            "run /login",
            "input",
            "confirm",
            "reply required",
            "tab to queue message",
            "queue message",
            "ready for input",
        ]
    ):
        return "waiting", lines[-1]

    for line in reversed(lines):
        clean = line.strip()
        if clean and clean not in {"❯", "$"}:
            return "running", clean
    return "running", lines[-1]
