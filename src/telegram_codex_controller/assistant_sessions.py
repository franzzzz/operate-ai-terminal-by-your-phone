from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List

from .config import Settings


MAX_TRANSCRIPT_ENTRIES = 80
MAX_TRANSCRIPT_CHARS = 8000


@dataclass
class AssistantSessionInfo:
    name: str
    provider: str
    cwd: str
    assistant_session_id: str | None
    updated_at: str
    busy: bool


class AssistantSessionManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.settings.assistant_state_path.parent.mkdir(parents=True, exist_ok=True)
        self._locks: Dict[str, asyncio.Lock] = {}
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._load()

    def list_sessions(self) -> List[AssistantSessionInfo]:
        return [self._build_info(name) for name in sorted(self._sessions)]

    def create_session(self, name: str, provider: str, cwd: str | None = None) -> AssistantSessionInfo:
        clean_name = name.strip()
        clean_provider = provider.strip().lower()
        if not clean_name:
            raise ValueError("Session name cannot be empty")
        if clean_provider not in {"codex", "claude"}:
            raise ValueError("Provider must be 'codex' or 'claude'")
        if clean_name in self._sessions:
            raise ValueError(f"Assistant session '{clean_name}' already exists")

        target_cwd = (cwd or str(Path.home())).strip()
        target_path = Path(target_cwd).expanduser()
        if not target_path.exists():
            raise ValueError(f"Working directory does not exist: {target_path}")
        if not target_path.is_dir():
            raise ValueError(f"Working directory is not a directory: {target_path}")

        self._sessions[clean_name] = {
            "provider": clean_provider,
            "cwd": str(target_path),
            "assistant_session_id": None,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "transcript": [],
        }
        self._persist()
        return self._build_info(clean_name)

    def set_cwd(self, name: str, cwd: str) -> AssistantSessionInfo:
        session = self._require(name)
        target_path = Path(cwd.strip()).expanduser()
        if not target_path.exists():
            raise ValueError(f"Working directory does not exist: {target_path}")
        if not target_path.is_dir():
            raise ValueError(f"Working directory is not a directory: {target_path}")

        session["cwd"] = str(target_path)
        session["assistant_session_id"] = None
        session["updated_at"] = _now_iso()
        self._append_transcript(
            session,
            "system",
            f"Working directory changed to {target_path}. Resume state cleared.",
        )
        self._persist()
        return self._build_info(name)

    def stop_session(self, name: str) -> None:
        if name not in self._sessions:
            raise ValueError(f"Assistant session '{name}' does not exist")
        del self._sessions[name]
        self._locks.pop(name, None)
        self._persist()

    def render_transcript(self, name: str, max_entries: int = 20) -> str:
        session = self._require(name)
        entries = session.get("transcript", [])[-max_entries:]
        if not entries:
            return f"No transcript for '{name}' yet."

        lines = [
            f"Transcript for '{name}' ({session['provider']}, cwd={session['cwd']}):",
            "",
        ]
        for entry in entries:
            lines.append(f"[{entry['time']}] {entry['role']}")
            lines.append(entry["content"])
            lines.append("")
        return "\n".join(lines).rstrip()

    def export_transcript(self, name: str) -> str:
        session = self._require(name)
        entries = session.get("transcript", [])
        if not entries:
            return f"No transcript for '{name}' yet."
        lines = [
            f"Transcript for '{name}' ({session['provider']}, cwd={session['cwd']}):",
            "",
        ]
        for entry in entries:
            lines.append(f"[{entry['time']}] {entry['role']}")
            lines.append(entry["content"])
            lines.append("")
        return "\n".join(lines).rstrip()

    def find_in_transcript(self, name: str, pattern: str, *, limit: int = 20) -> List[str]:
        needle = pattern.lower()
        session = self._require(name)
        matches: List[str] = []
        for entry in session.get("transcript", []):
            content = entry["content"]
            if needle not in content.lower():
                continue
            matches.append(f"[{entry['time']}] {entry['role']}: {content}")
            if len(matches) >= limit:
                break
        return matches

    def search_transcript(self, name: str, pattern: str, max_entries: int = 80) -> str:
        session = self._require(name)
        entries = session.get("transcript", [])[-max_entries:]
        matches = [
            f"[{entry['time']}] {entry['role']}: {entry['content']}"
            for entry in entries
            if pattern.lower() in entry["content"].lower()
        ]
        if not matches:
            return f"No matches for '{pattern}' in assistant session '{name}'."
        return "\n".join(matches[-20:])

    async def run_prompt(self, name: str, prompt: str) -> AsyncIterator[dict[str, Any]]:
        clean_name = name.strip()
        if not prompt.strip():
            raise ValueError("Prompt cannot be empty")
        if not self.settings.assistant_sidecar_script.exists():
            raise ValueError(f"Assistant sidecar script not found: {self.settings.assistant_sidecar_script}")
        if not (self.settings.assistant_sidecar_workdir / "node_modules").exists():
            raise ValueError(
                f"Assistant sidecar dependencies are missing in {self.settings.assistant_sidecar_workdir / 'node_modules'}"
            )

        session = self._require(clean_name)
        lock = self._locks.setdefault(clean_name, asyncio.Lock())
        if lock.locked():
            raise ValueError(f"Assistant session '{clean_name}' is already running")

        args = [
            self.settings.node_bin,
            str(self.settings.assistant_sidecar_script),
        ]

        async with lock:
            self._append_transcript(session, "user", prompt)
            self._persist()

            request = {
                "assistant": session["provider"],
                "cwd": session["cwd"],
                "prompt": prompt,
                "resumeSessionId": session.get("assistant_session_id"),
            }

            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.settings.assistant_sidecar_workdir),
                env=self._build_subprocess_env(session["provider"]),
            )

            assert process.stdin is not None
            process.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
            await process.stdin.drain()
            process.stdin.close()

            stderr_task = asyncio.create_task(_read_stream(process.stderr))
            saw_visible_output = False

            try:
                assert process.stdout is not None
                while True:
                    raw_line = await process.stdout.readline()
                    if not raw_line:
                        break

                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue

                    payload = json.loads(line)
                    event_type = payload.get("type", "")
                    if event_type == "assistant" and payload.get("content"):
                        self._append_transcript(session, "assistant", payload["content"])
                        saw_visible_output = True
                    elif event_type == "tool" and payload.get("toolName"):
                        tool_message = f"[tool] {payload['toolName']}"
                        tool_input = payload.get("toolInput")
                        if tool_input:
                            tool_message += f" {json.dumps(tool_input, ensure_ascii=False)}"
                        self._append_transcript(session, "tool", tool_message)
                        saw_visible_output = True
                    elif event_type == "system" and payload.get("content"):
                        self._append_transcript(session, "system", payload["content"])
                    elif event_type == "result" and payload.get("sessionId"):
                        session["assistant_session_id"] = payload["sessionId"]

                    session["updated_at"] = _now_iso()
                    yield payload

                stderr_output = await stderr_task
                return_code = await process.wait()
            finally:
                session["updated_at"] = _now_iso()
                self._persist()

            if return_code != 0:
                error_text = stderr_output.strip() or "Assistant bridge exited with an error."
                self._append_transcript(session, "system", error_text)
                self._persist()
                raise ValueError(error_text)

            if stderr_output.strip():
                self._append_transcript(session, "system", stderr_output.strip())
                self._persist()

            if not saw_visible_output:
                yield {"type": "system", "content": "Assistant completed with no visible output."}

    def _build_info(self, name: str) -> AssistantSessionInfo:
        payload = self._sessions[name]
        lock = self._locks.setdefault(name, asyncio.Lock())
        return AssistantSessionInfo(
            name=name,
            provider=payload["provider"],
            cwd=payload["cwd"],
            assistant_session_id=payload.get("assistant_session_id"),
            updated_at=payload.get("updated_at", ""),
            busy=lock.locked(),
        )

    def _require(self, name: str) -> Dict[str, Any]:
        payload = self._sessions.get(name)
        if payload is None:
            raise ValueError(f"Assistant session '{name}' does not exist")
        return payload

    def _load(self) -> None:
        if not self.settings.assistant_state_path.exists():
            self._sessions = {}
            return
        payload = json.loads(self.settings.assistant_state_path.read_text())
        sessions = payload.get("sessions", {})
        self._sessions = sessions if isinstance(sessions, dict) else {}

    def _persist(self) -> None:
        payload = json.dumps({"sessions": self._sessions}, ensure_ascii=False, indent=2)
        temp_path = self.settings.assistant_state_path.with_suffix(".tmp")
        temp_path.write_text(payload)
        temp_path.replace(self.settings.assistant_state_path)

    def _append_transcript(self, session: Dict[str, Any], role: str, content: str) -> None:
        transcript = session.setdefault("transcript", [])
        transcript.append(
            {
                "role": role,
                "time": _now_iso(),
                "content": _truncate_content(content),
            }
        )
        if len(transcript) > MAX_TRANSCRIPT_ENTRIES:
            del transcript[:-MAX_TRANSCRIPT_ENTRIES]

    def _build_subprocess_env(self, provider: str) -> Dict[str, str]:
        env = dict(os.environ)
        if provider != "codex":
            return env

        source_codex_home = Path.home() / ".codex"
        target_codex_home = self.settings.assistant_state_path.parent / "sidecar-codex-home"
        target_codex_home.mkdir(parents=True, exist_ok=True)

        auth_path = source_codex_home / "auth.json"
        if auth_path.exists():
            (target_codex_home / "auth.json").write_text(auth_path.read_text())

        config_path = source_codex_home / "config.toml"
        if config_path.exists():
            config_text = config_path.read_text()
            config_text = config_text.replace(
                'model_reasoning_effort = "xhigh"',
                'model_reasoning_effort = "high"',
            )
            (target_codex_home / "config.toml").write_text(config_text)

        env["CODEX_HOME"] = str(target_codex_home)
        return env


async def _read_stream(stream: asyncio.StreamReader | None) -> str:
    if stream is None:
        return ""
    payload = await stream.read()
    return payload.decode("utf-8", errors="replace")


def _truncate_content(content: str) -> str:
    if len(content) <= MAX_TRANSCRIPT_CHARS:
        return content
    return content[:MAX_TRANSCRIPT_CHARS] + "\n...[truncated]..."


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()
