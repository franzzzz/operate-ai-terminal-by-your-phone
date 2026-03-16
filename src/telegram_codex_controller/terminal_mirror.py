from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List

from telegram.ext import Application

from .console import SessionStatusSpec
from .config import Settings
from .reply_routes import ReplyRoute, send_chunked_message
from .utils import compact_summary_text, tail_lines


LOG = logging.getLogger(__name__)
CODEX_COMMAND_PATTERN = re.compile(r"(?:^|/|\s)codex(?:\s|$)")
MAX_HISTORY_CHARS = 50000


@dataclass
class TerminalTarget:
    tty: str
    pid: int
    title: str
    command: str
    alias: str | None = None


@dataclass
class MirrorState:
    target: TerminalTarget
    last_contents: str


class TerminalMirrorManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._application: Application | None = None
        self._task: asyncio.Task | None = None
        self._enabled = settings.mirror_enabled
        self._states: Dict[str, MirrorState] = {}
        self._alias_path = settings.assistant_state_path.parent / "mirror_aliases.json"
        self._aliases = self._load_aliases()
        self._bootstrapped = False

    async def start(self, application: Application) -> None:
        self._application = application
        if not self._enabled or self._task is not None:
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

    async def enable(self, application: Application) -> None:
        self._enabled = True
        await self.start(application)

    async def disable(self) -> None:
        self._enabled = False
        await self.stop()
        self._states.clear()

    def is_enabled(self) -> bool:
        return self._enabled

    def list_active_targets(self) -> List[TerminalTarget]:
        return [state.target for state in sorted(self._states.values(), key=lambda item: item.target.tty)]

    def discover_targets(self) -> List[TerminalTarget]:
        return self._discover_targets()

    def get_target(self, identifier: str) -> TerminalTarget | None:
        resolved = self.resolve_identifier(identifier)
        for target in self._discover_targets():
            if target.tty == resolved:
                return target
        return None

    def list_aliases(self) -> Dict[str, str]:
        return dict(sorted(self._aliases.items()))

    def alias_for(self, tty: str) -> str | None:
        return self._aliases.get(_normalize_tty(tty))

    def describe_tty(self, identifier: str) -> str:
        tty = self.resolve_identifier(identifier)
        alias = self._aliases.get(tty)
        if alias:
            return f"{alias} ({tty})"
        return tty

    def resolve_identifier(self, identifier: str) -> str:
        normalized = _normalize_tty(identifier)
        if normalized in self._aliases:
            return normalized
        for tty, alias in self._aliases.items():
            if alias == identifier:
                return tty
        return normalized

    def set_alias(self, identifier: str, alias: str) -> str:
        tty = self.resolve_identifier(identifier)
        clean_alias = alias.strip()
        if not clean_alias:
            raise ValueError("Alias cannot be empty")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", clean_alias):
            raise ValueError("Alias may contain only letters, numbers, dot, underscore, and hyphen")

        for existing_tty, existing_alias in list(self._aliases.items()):
            if existing_alias == clean_alias and existing_tty != tty:
                raise ValueError(f"Alias '{clean_alias}' is already assigned to {existing_tty}")

        self._aliases[tty] = clean_alias
        for state_tty, state in self._states.items():
            if state_tty == tty:
                state.target.alias = clean_alias
        self._persist_aliases()
        return tty

    def clear_alias(self, identifier: str) -> tuple[str, str]:
        tty = self.resolve_identifier(identifier)
        alias = self._aliases.pop(tty, None)
        if alias is None:
            raise ValueError(f"No alias is defined for '{identifier}'")
        state = self._states.get(tty)
        if state is not None:
            state.target.alias = None
        self._persist_aliases()
        return tty, alias

    async def refresh_statuses(self) -> None:
        for state in self._states.values():
            raw_summary = tail_lines(state.last_contents, self.settings.console_status_summary_lines) or "<no output yet>"
            summary = compact_summary_text(
                raw_summary,
                max_lines=self.settings.console_status_summary_lines,
                max_line_length=110,
            )
            session_state = _classify_state(raw_summary, "running")
            await self._publish_status(
                state.target,
                state=session_state,
                step="mirroring",
                summary=summary,
                alert=_alert_for_state(state.target, session_state, raw_summary),
            )

    async def note_user_input(self, identifier: str, text: str) -> None:
        target = self._target_for(identifier)
        await self._publish_status(
            target,
            state="running",
            step="input sent",
            summary=text,
            force=True,
        )

    async def send_snapshot(self, tty: str | None = None) -> int:
        count = 0
        resolved = self.resolve_identifier(tty) if tty else None
        for target in self._discover_targets():
            if resolved and target.tty != resolved:
                continue
            contents = self._read_terminal_contents(target.tty)
            if not contents.strip():
                continue
            snapshot = compact_summary_text(
                tail_lines(contents, self.settings.mirror_initial_lines) or "<no output yet>",
                max_lines=self.settings.console_status_summary_lines,
                max_line_length=110,
            )
            await self._send_message(
                f"[mirror-snapshot {target.tty}] {self._display_name(target)}\n\n{snapshot}",
                route=ReplyRoute(kind="mirror", target=target.tty),
            )
            count += 1
        return count

    def capture_history(self, identifier: str) -> str:
        target = self.get_target(identifier)
        if target is None:
            raise ValueError(f"Mirror target '{identifier}' was not found")
        payload = self._read_terminal_contents(target.tty).rstrip()
        return payload or "<no output yet>"

    def find_in_history(self, identifier: str, pattern: str, *, limit: int = 20) -> List[str]:
        needle = pattern.lower()
        matches = [
            line
            for line in self.capture_history(identifier).splitlines()
            if needle in line.lower()
        ]
        return matches[:limit]

    def history_text(self, identifier: str) -> str:
        tty = self.resolve_identifier(identifier)
        return self._read_terminal_contents(tty)

    def focus_target(self, identifier: str) -> str:
        target = self._target_for(identifier)
        script = [
            'tell application "Terminal"',
            "activate",
            'set targetTty to system attribute "TGC_TTY"',
            "repeat with w in windows",
            "repeat with t in tabs of w",
            "if (((tty of t) as text) is equal to targetTty) then",
            "set selected of t to true",
            "set frontmost of w to true",
            'return "ok"',
            "end if",
            "end repeat",
            "end repeat",
            'return "missing"',
            "end tell",
        ]
        result = self._run_osascript(
            script,
            env={"TGC_TTY": f"/dev/{target.tty}"},
        ).strip()
        if result != "ok":
            raise ValueError(f"Mirror target '{identifier}' was not found")
        return self.describe_tty(target.tty)

    def search_history(self, identifier: str, pattern: str) -> str:
        tty = self.resolve_identifier(identifier)
        payload = self._read_terminal_contents(tty)
        matches = [line for line in payload.splitlines() if pattern.lower() in line.lower()]
        if not matches:
            return f"No matches for '{pattern}' in Terminal tab '{self.describe_tty(tty)}'."
        return "\n".join(matches[-20:])

    def send_input(self, tty: str, text: str) -> None:
        normalized_tty = self.resolve_identifier(tty)
        env = {
            "TGC_TTY": f"/dev/{normalized_tty}" if not normalized_tty.startswith("/dev/") else normalized_tty,
        }
        paste_script = [
            'set targetTty to system attribute "TGC_TTY"',
            'set foundTab to false',
            'tell application "Terminal"',
            "activate",
            "repeat with w in windows",
            "repeat with t in tabs of w",
            "if (((tty of t) as text) is equal to targetTty) then",
            "set selected of t to true",
            "set frontmost of w to true",
            "set foundTab to true",
            "exit repeat",
            "end if",
            "end repeat",
            "if foundTab then exit repeat",
            "end repeat",
            "end tell",
            'if foundTab is false then return "missing"',
            "delay 0.1",
            'tell application "System Events"',
            'keystroke "v" using command down',
            "key code 36",
            "end tell",
            'return "ok"',
        ]
        saved_clipboard = self._read_clipboard_bytes()
        try:
            self._write_clipboard_bytes(text.encode("utf-8"))
            result = self._run_osascript(paste_script, env=env).strip()
            if result == "ok":
                return
            if result != "missing":
                raise ValueError(result)
        except subprocess.CalledProcessError:
            pass
        finally:
            with contextlib.suppress(Exception):
                self._write_clipboard_bytes(saved_clipboard)

        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            handle.write(text)
            payload_path = handle.name
        fallback_script = [
            'tell application "Terminal"',
            'set targetTty to system attribute "TGC_TTY"',
            'set payloadPath to system attribute "TGC_PAYLOAD_PATH"',
            'set payload to do shell script "/bin/cat " & quoted form of payloadPath',
            "repeat with w in windows",
            "repeat with t in tabs of w",
            "if (((tty of t) as text) is equal to targetTty) then",
            "do script payload in t",
            'return "ok"',
            "end if",
            "end repeat",
            "end repeat",
            'return "missing"',
            "end tell",
        ]
        try:
            result = self._run_osascript(
                fallback_script,
                env={**env, "TGC_PAYLOAD_PATH": payload_path},
            ).strip()
            if result != "ok":
                raise ValueError(f"Terminal tab '{tty}' was not found")
        finally:
            Path(payload_path).unlink(missing_ok=True)

    def _read_clipboard_bytes(self) -> bytes:
        result = subprocess.run(
            ["pbpaste"],
            capture_output=True,
            check=True,
        )
        return result.stdout

    def _write_clipboard_bytes(self, payload: bytes) -> None:
        subprocess.run(
            ["pbcopy"],
            input=payload,
            capture_output=True,
            check=True,
        )

    async def _run(self) -> None:
        while True:
            try:
                await self._poll_once()
            except Exception:
                LOG.exception("Terminal mirror poll failed")
            await asyncio.sleep(self.settings.mirror_poll_interval_seconds)

    async def _poll_once(self) -> None:
        current = {target.tty: target for target in self._discover_targets()}

        for tty in list(self._states):
            if tty in current:
                continue
            target = self._states.pop(tty).target
            await self._publish_status(
                target,
                state="stopped",
                step="session ended",
                summary="Mirror source disappeared.",
                event=f"⚫ {self._display_name(target)} ended.",
                alert=f"⚫ {self._display_name(target)} ended.",
            )

        for tty, target in current.items():
            contents = self._read_terminal_contents(tty)
            if not contents:
                continue

            current_contents = _trim_contents(contents)
            state = self._states.get(tty)
            if state is None:
                self._states[tty] = MirrorState(target=target, last_contents=current_contents)
                raw_snapshot = tail_lines(current_contents, self.settings.mirror_initial_lines) or "<no output yet>"
                session_state = _classify_state(raw_snapshot, "running")
                snapshot = compact_summary_text(
                    raw_snapshot,
                    max_lines=self.settings.console_status_summary_lines,
                    max_line_length=110,
                )
                await self._publish_status(
                    target,
                    state=session_state,
                    step="mirroring started",
                    summary=snapshot,
                    event=f"🟢 {self._display_name(target)} started." if self._bootstrapped else None,
                    alert=_alert_for_state(target, session_state, raw_snapshot) if self._bootstrapped else None,
                )
                continue

            state.target = target
            delta, reset = _compute_delta(state.last_contents, current_contents)
            if delta:
                current_snapshot = tail_lines(
                    current_contents,
                    self.settings.console_status_summary_lines,
                ) or delta
                session_state = _classify_state(current_snapshot, "running")
                summary = compact_summary_text(
                    delta,
                    max_lines=self.settings.console_status_summary_lines,
                    max_line_length=110,
                )
                await self._publish_status(
                    target,
                    state=session_state,
                    step="mirroring",
                    summary=summary,
                    alert=_alert_for_state(target, session_state, current_snapshot),
                )
            state.last_contents = current_contents
        self._bootstrapped = True

    def _discover_targets(self) -> List[TerminalTarget]:
        try:
            tabs = self._list_terminal_tabs()
        except subprocess.CalledProcessError as exc:
            LOG.warning("Failed to enumerate Terminal tabs: %s", exc.stderr.strip() if exc.stderr else exc)
            return []
        result = subprocess.run(
            ["ps", "-A", "-o", "pid=,tty=,command="],
            capture_output=True,
            text=True,
            check=True,
        )

        targets: Dict[str, TerminalTarget] = {}
        for line in result.stdout.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) != 3:
                continue
            pid_text, tty, command = parts
            if tty == "??":
                continue
            if "app-server" in command:
                continue
            if not CODEX_COMMAND_PATTERN.search(command):
                continue

            title = tabs.get(f"/dev/{tty}") or tabs.get(tty) or command
            targets[tty] = TerminalTarget(
                tty=tty,
                pid=int(pid_text),
                title=title,
                command=command,
                alias=self._aliases.get(tty),
            )
        return list(targets.values())

    def _target_for(self, identifier: str) -> TerminalTarget:
        tty = self.resolve_identifier(identifier)
        if tty in self._states:
            return self._states[tty].target
        for target in self._discover_targets():
            if target.tty == tty:
                return target
        return TerminalTarget(
            tty=tty,
            pid=0,
            title=self.describe_tty(tty),
            command="codex",
            alias=self.alias_for(tty),
        )

    def _list_terminal_tabs(self) -> Dict[str, str]:
        script = [
            'tell application "Terminal" to set out to ""',
            'tell application "Terminal"',
            "repeat with w in windows",
            "repeat with t in tabs of w",
            "set tabTty to ((tty of t) as text)",
            "set tabTitle to ((custom title of t) as text)",
            'if tabTitle is "" then set tabTitle to ((name of t) as text)',
            'set out to out & tabTty & "\\t" & tabTitle & linefeed',
            "end repeat",
            "end repeat",
            "return out",
            "end tell",
        ]
        result = self._run_osascript(script)
        tabs: Dict[str, str] = {}
        for line in result.splitlines():
            if "\t" not in line:
                continue
            tty, title = line.split("\t", 1)
            tabs[tty.strip()] = title.strip() or tty.strip()
        return tabs

    def _read_terminal_contents(self, tty: str) -> str:
        script = [
            'tell application "Terminal"',
            "repeat with w in windows",
            "repeat with t in tabs of w",
            f'if (((tty of t) as text) is equal to "/dev/{tty}") then return (history of t)',
            "end repeat",
            "end repeat",
            'return ""',
            "end tell",
        ]
        try:
            return self._run_osascript(script)
        except subprocess.CalledProcessError as exc:
            LOG.warning(
                "Failed to read Terminal contents for %s: %s",
                tty,
                exc.stderr.strip() if exc.stderr else exc,
            )
            return ""

    def _run_osascript(self, lines: Iterable[str], env: dict[str, str] | None = None) -> str:
        script = "\n".join(lines)
        result = subprocess.run(
            ["osascript", "-"],
            input=script,
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, **(env or {})},
        )
        return result.stdout

    async def _publish_status(
        self,
        target: TerminalTarget,
        *,
        state: str,
        step: str,
        summary: str,
        event: str | None = None,
        alert: str | None = None,
        force: bool = False,
    ) -> None:
        if self._application is None:
            return
        console = self._application.bot_data.get("console")
        if console is None:
            text = f"[mirror {target.tty}] {self._display_name(target)}\n\n{summary}"
            await self._send_message(text, route=ReplyRoute(kind="mirror", target=target.tty))
            return
        await console.update_status(
            SessionStatusSpec(
                route=ReplyRoute(kind="mirror", target=target.tty),
                kind="mirror",
                label=target.alias or target.tty,
                title=target.title,
                state=state,
                step=step,
                summary=summary,
                event=event,
                alert=alert,
                force=force,
            )
        )
        route = ReplyRoute(kind="mirror", target=target.tty)
        if state == "error" and alert and console.should_emit_artifact(route, "error_log", alert):
            await console.send_log_document(
                route,
                f"{self.describe_tty(target.tty).replace(' ', '_')}.log",
                self.capture_history(target.tty),
                f"Auto error log for {self.describe_tty(target.tty)}",
            )

    async def _send_message(self, text: str, route: ReplyRoute | None = None) -> None:
        if self._application is None:
            return
        console = self._application.bot_data.get("console")
        if console is not None and route is not None:
            await console.send_text(route, text)
            return
        for chat_id in sorted(self.settings.mirror_chat_ids):
            await send_chunked_message(self._application, chat_id, text, route=route)

    def _display_name(self, target: TerminalTarget) -> str:
        if target.alias:
            return f"{target.alias} | {target.title}"
        return target.title

    def _load_aliases(self) -> Dict[str, str]:
        if not self._alias_path.exists():
            return {}
        try:
            payload = json.loads(self._alias_path.read_text())
        except Exception:
            LOG.warning("Failed to load mirror aliases from %s", self._alias_path)
            return {}
        aliases = payload.get("aliases", {})
        if not isinstance(aliases, dict):
            return {}
        result: Dict[str, str] = {}
        for tty, alias in aliases.items():
            if isinstance(tty, str) and isinstance(alias, str) and alias.strip():
                result[_normalize_tty(tty)] = alias.strip()
        return result

    def _persist_aliases(self) -> None:
        self._alias_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._alias_path.with_suffix(".tmp")
        payload = json.dumps({"aliases": self._aliases}, ensure_ascii=True, indent=2)
        temp_path.write_text(payload)
        temp_path.replace(self._alias_path)


def _compute_delta(
    previous: str,
    current: str,
    reset_line_limit: int = 120,
) -> tuple[str, bool]:
    if current == previous:
        return "", False
    if not previous:
        return current, False
    if current.startswith(previous):
        return current[len(previous):].lstrip("\n"), False

    previous_lines = previous.splitlines()
    current_lines = current.splitlines()
    max_overlap = min(len(previous_lines), len(current_lines))
    for size in range(max_overlap, 0, -1):
        if previous_lines[-size:] == current_lines[:size]:
            return "\n".join(current_lines[size:]).strip(), False

    return tail_lines(current, reset_line_limit), True


_extract_delta = _compute_delta


def _trim_contents(text: str) -> str:
    if len(text) <= MAX_HISTORY_CHARS:
        return text
    return text[-MAX_HISTORY_CHARS:]


def _normalize_tty(identifier: str) -> str:
    clean = identifier.strip()
    if clean.startswith("/dev/"):
        clean = clean[5:]
    return clean


def _classify_state(text: str, fallback: str) -> str:
    lowered = text.lower()
    if (
        "press your yubikey" in lowered
        or "approve duo" in lowered
        or "waiting for connection" in lowered
        or "tab to queue message" in lowered
        or "queue message" in lowered
        or "ready for input" in lowered
    ):
        return "waiting"
    if "traceback" in lowered or "runtimeerror:" in lowered or "zsh: command not found" in lowered:
        return "error"
    return fallback


def _alert_for_state(target: TerminalTarget, state: str, text: str) -> str | None:
    if state == "waiting":
        return f"🟡 {target.alias or target.tty} is waiting for human input.\n{tail_lines(text, 3)}"
    return None
