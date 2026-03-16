from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from telegram_codex_controller.config import Settings
from telegram_codex_controller.terminal_mirror import TerminalMirrorManager, TerminalTarget, _compute_delta


def make_settings(root: Path) -> Settings:
    sidecar = root / "sidecar" / "runner.mjs"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text("// test sidecar\n")
    return Settings(
        telegram_bot_token="token",
        authorized_user_ids={1},
        tmux_bin="tmux",
        log_lines_default=80,
        poll_interval_seconds=3,
        allow_shell=False,
        shell_timeout_seconds=20,
        codex_command_template='codex exec "{prompt}"',
        max_message_chars=3500,
        session_name_prefix="tgc_",
        mirror_enabled=True,
        mirror_chat_ids={1},
        mirror_poll_interval_seconds=3,
        mirror_initial_lines=24,
        node_bin="node",
        assistant_state_path=root / ".state" / "assistant_sessions.json",
        assistant_sidecar_script=sidecar,
        assistant_sidecar_workdir=sidecar.parent,
        console_chat_id=None,
        console_forum_enabled=False,
        console_auto_create_topics=True,
        console_index_topic_id=None,
        console_alerts_topic_id=None,
        console_status_summary_lines=5,
        console_status_update_min_interval_seconds=2,
        console_running_update_min_interval_seconds=6,
        console_global_write_spacing_seconds=2,
        console_stuck_minutes=15,
        console_completed_retention_minutes=60,
        console_topic_bump_enabled=True,
        console_topic_bump_minutes=10,
        console_send_log_documents=True,
        console_pin_status_messages=False,
    )


class StubMirrorManager(TerminalMirrorManager):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.contents_by_tty: dict[str, str] = {}
        self.published: list[dict[str, str | None | bool]] = []
        self._targets = [
            TerminalTarget(
                tty="ttys002",
                pid=100,
                title="Terminal",
                command="codex",
                alias="oracle-chat-link",
            )
        ]

    def _discover_targets(self):  # type: ignore[override]
        return list(self._targets)

    def _read_terminal_contents(self, tty: str) -> str:  # type: ignore[override]
        return self.contents_by_tty.get(tty, "")

    async def _publish_status(  # type: ignore[override]
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
        self.published.append(
            {
                "tty": target.tty,
                "state": state,
                "step": step,
                "summary": summary,
                "event": event,
                "alert": alert,
                "force": force,
            }
        )


class ClipboardMirrorManager(TerminalMirrorManager):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.clipboard_writes: list[bytes] = []
        self.osascript_calls: list[tuple[list[str], dict[str, str] | None]] = []
        self.next_osascript_results: list[str] = []

    def _read_clipboard_bytes(self) -> bytes:  # type: ignore[override]
        return b"original clipboard"

    def _write_clipboard_bytes(self, payload: bytes) -> None:  # type: ignore[override]
        self.clipboard_writes.append(payload)

    def _run_osascript(self, lines, env=None):  # type: ignore[override]
        self.osascript_calls.append((list(lines), dict(env) if env is not None else None))
        if self.next_osascript_results:
            return self.next_osascript_results.pop(0)
        return "ok"


class TerminalMirrorDeltaTests(unittest.TestCase):
    def test_returns_append_only_delta(self) -> None:
        delta, reset = _compute_delta("a\nb", "a\nb\nc", 10)
        self.assertEqual(delta, "c")
        self.assertFalse(reset)

    def test_returns_overlap_delta_after_scroll(self) -> None:
        previous = "1\n2\n3"
        current = "2\n3\n4\n5"
        delta, reset = _compute_delta(previous, current, 10)
        self.assertEqual(delta, "4\n5")
        self.assertFalse(reset)

    def test_returns_snapshot_when_screen_rewrites(self) -> None:
        previous = "old one\nold two"
        current = "new one\nnew two\nnew three"
        delta, reset = _compute_delta(previous, current, 2)
        self.assertEqual(delta, "new two\nnew three")
        self.assertTrue(reset)

    def test_poll_uses_current_snapshot_for_state_instead_of_stale_delta(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = StubMirrorManager(make_settings(Path(temp_dir)))
            manager.contents_by_tty["ttys002"] = "initial line\nstill running"
            asyncio.run(manager._poll_once())
            manager.published.clear()

            manager.contents_by_tty["ttys002"] = "all good\nprompt >"
            with patch(
                "telegram_codex_controller.terminal_mirror._compute_delta",
                return_value=("Traceback: stale failure text", True),
            ):
                asyncio.run(manager._poll_once())

            self.assertEqual(len(manager.published), 1)
            self.assertEqual(manager.published[0]["state"], "running")
            self.assertEqual(manager.published[0]["step"], "mirroring")

    def test_send_input_uses_utf8_clipboard_for_primary_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = ClipboardMirrorManager(make_settings(Path(temp_dir)))

            manager.send_input("ttys002", "分开配置")

            self.assertEqual(manager.clipboard_writes[0], "分开配置".encode("utf-8"))
            self.assertEqual(manager.clipboard_writes[-1], b"original clipboard")
            first_env = manager.osascript_calls[0][1] or {}
            self.assertIn("TGC_TTY", first_env)
            self.assertNotIn("TGC_PAYLOAD", first_env)

    def test_send_input_uses_temp_file_for_fallback_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = ClipboardMirrorManager(make_settings(Path(temp_dir)))
            manager.next_osascript_results = ["missing", "ok"]

            manager.send_input("ttys002", "分开配置")

            self.assertEqual(len(manager.osascript_calls), 2)
            fallback_env = manager.osascript_calls[1][1] or {}
            self.assertIn("TGC_PAYLOAD_PATH", fallback_env)
            payload_path = Path(fallback_env["TGC_PAYLOAD_PATH"])
            self.assertFalse(payload_path.exists())
            self.assertNotIn("TGC_PAYLOAD", fallback_env)


if __name__ == "__main__":
    unittest.main()
