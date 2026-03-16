from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from telegram_codex_controller.assistant_sessions import AssistantSessionManager
from telegram_codex_controller.config import Settings


class AssistantSessionManagerTests(unittest.TestCase):
    def _settings(self, root: Path) -> Settings:
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
            mirror_enabled=False,
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

    def test_create_reset_and_reload_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = self._settings(root)
            manager = AssistantSessionManager(settings)
            session = manager.create_session("demo", "codex", temp_dir)
            self.assertEqual(session.name, "demo")
            self.assertEqual(session.provider, "codex")

            manager._sessions["demo"]["assistant_session_id"] = "session-123"
            manager._persist()

            reloaded = AssistantSessionManager(settings)
            loaded = reloaded.list_sessions()[0]
            self.assertEqual(loaded.assistant_session_id, "session-123")

            reset = reloaded.set_cwd("demo", temp_dir)
            self.assertIsNone(reset.assistant_session_id)

    def test_rejects_duplicate_session_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = self._settings(Path(temp_dir))
            manager = AssistantSessionManager(settings)
            manager.create_session("demo", "codex", temp_dir)

            with self.assertRaises(ValueError):
                manager.create_session("demo", "claude", temp_dir)


if __name__ == "__main__":
    unittest.main()
