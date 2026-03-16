from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from typing import List

from .config import Settings


@dataclass
class SessionInfo:
    full_name: str
    short_name: str
    attached: bool
    windows: int
    created: str


class SessionManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _full_session_name(self, short_name: str) -> str:
        clean = short_name.strip()
        if not clean:
            raise ValueError("Session name cannot be empty")
        return f"{self.settings.session_name_prefix}{clean}"

    def _run(self, args: list[str], *, check: bool = True, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            check=check,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def start_session(self, short_name: str, command: str) -> str:
        full_name = self._full_session_name(short_name)
        if self.session_exists(short_name):
            raise ValueError(f"Session '{short_name}' already exists")

        self._run([self.settings.tmux_bin, "new-session", "-d", "-s", full_name, command])
        return full_name

    def stop_session(self, short_name: str) -> None:
        full_name = self._full_session_name(short_name)
        self._run([self.settings.tmux_bin, "kill-session", "-t", full_name])

    def session_exists(self, short_name: str) -> bool:
        full_name = self._full_session_name(short_name)
        result = self._run([self.settings.tmux_bin, "has-session", "-t", full_name], check=False)
        return result.returncode == 0

    def list_sessions(self) -> List[SessionInfo]:
        fmt = "#{session_name}\t#{?session_attached,yes,no}\t#{session_windows}\t#{session_created_string}"
        result = self._run([self.settings.tmux_bin, "list-sessions", "-F", fmt], check=False)
        if result.returncode != 0:
            stderr = (result.stderr or "").strip().lower()
            if "no server running" in stderr or "failed to connect" in stderr:
                return []
            return []

        sessions: List[SessionInfo] = []
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 4:
                continue
            full_name, attached, windows, created = parts
            if not full_name.startswith(self.settings.session_name_prefix):
                continue
            sessions.append(
                SessionInfo(
                    full_name=full_name,
                    short_name=full_name[len(self.settings.session_name_prefix):],
                    attached=attached == "yes",
                    windows=int(windows),
                    created=created,
                )
            )
        return sessions

    def capture_logs(self, short_name: str, lines: int) -> str:
        full_name = self._full_session_name(short_name)
        result = self._run(
            [self.settings.tmux_bin, "capture-pane", "-p", "-S", f"-{lines}", "-t", full_name],
            check=False,
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").strip() or "Unknown tmux error"
            raise ValueError(stderr)
        return result.stdout.rstrip() or "<no output yet>"

    def export_logs(self, short_name: str, lines: int = 4000) -> str:
        return self.capture_logs(short_name, lines)

    def find_in_logs(self, short_name: str, pattern: str, *, lines: int = 4000, limit: int = 20) -> List[str]:
        needle = pattern.lower()
        matches = [
            line
            for line in self.export_logs(short_name, lines).splitlines()
            if needle in line.lower()
        ]
        return matches[:limit]

    def search_logs(self, short_name: str, pattern: str, lines: int = 400) -> str:
        payload = self.capture_logs(short_name, lines)
        matches = [line for line in payload.splitlines() if pattern.lower() in line.lower()]
        if not matches:
            return f"No matches for '{pattern}' in tmux session '{short_name}'."
        return "\n".join(matches[-20:])

    def send_input(self, short_name: str, text: str) -> None:
        full_name = self._full_session_name(short_name)
        if not self.session_exists(short_name):
            raise ValueError(f"Session '{short_name}' does not exist")

        lines = text.splitlines() or [text]
        for line in lines:
            if line:
                self._run([self.settings.tmux_bin, "send-keys", "-t", full_name, "-l", line])
            self._run([self.settings.tmux_bin, "send-keys", "-t", full_name, "Enter"])

    def render_codex_command(self, prompt: str) -> str:
        template = self.settings.codex_command_template
        if "{prompt}" not in template:
            raise ValueError("CODEX_COMMAND_TEMPLATE must contain '{prompt}'")
        safe_prompt = prompt.replace('"', '\\"')
        return template.replace("{prompt}", safe_prompt)

    def run_shell(self, command: str) -> str:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=self.settings.shell_timeout_seconds,
        )
        combined = ""
        if result.stdout:
            combined += result.stdout
        if result.stderr:
            combined += ("\n" if combined else "") + result.stderr
        combined = combined.strip() or "<no output>"
        return f"exit_code={result.returncode}\n\n{combined}"
