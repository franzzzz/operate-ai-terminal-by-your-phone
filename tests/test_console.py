from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from telegram_codex_controller.config import Settings
from telegram_codex_controller.console import SessionStatusSpec, TelegramConsoleManager
from telegram_codex_controller.reply_routes import ReplyRoute, parse_reply_route
from telegram_codex_controller.terminal_mirror import TerminalMirrorManager


class FakeApplication:
    def __init__(self, bot: object | None = None) -> None:
        self.bot_data: dict[object, object] = {}
        self.bot = bot


class FakeBot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def delete_message(self, **kwargs: object) -> None:
        self.calls.append(("delete_message", kwargs))

    async def edit_forum_topic(self, **kwargs: object) -> None:
        self.calls.append(("edit_forum_topic", kwargs))

    async def close_forum_topic(self, **kwargs: object) -> None:
        self.calls.append(("close_forum_topic", kwargs))


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


class ConsoleRenderTests(unittest.TestCase):
    def test_parse_reply_route_understands_status_event_prefixes(self) -> None:
        self.assertEqual(parse_reply_route("[tail:research]\nhello"), ReplyRoute("tmux", "research"))
        self.assertEqual(parse_reply_route("[agent:triage]\nhello"), ReplyRoute("agent", "triage"))
        self.assertEqual(parse_reply_route("[mirror ttys002] test"), ReplyRoute("mirror", "ttys002"))

    def test_render_record_uses_label_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = make_settings(Path(temp_dir))
            manager = TelegramConsoleManager(settings)
            manager._state["sessions"]["mirror:ttys002"] = {
                "route_kind": "mirror",
                "route_target": "ttys002",
                "kind": "mirror",
                "label": "oracle-chat-link",
                "title": "Terminal",
                "state": "running",
                "step": "mirroring",
                "summary": "line1\nline2",
                "updated_at": "2026-03-15T21:00:00+00:00",
            }
            payload = manager.render_record("oracle-chat-link")
            self.assertIn("oracle-chat-link", payload)
            self.assertIn("mirroring", payload)

    def test_terminal_alias_persists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = make_settings(Path(temp_dir))
            manager = TerminalMirrorManager(settings)
            manager.set_alias("ttys002", "oracle-chat-link")
            self.assertEqual(manager.alias_for("ttys002"), "oracle-chat-link")
            reloaded = TerminalMirrorManager(settings)
            self.assertEqual(reloaded.alias_for("ttys002"), "oracle-chat-link")

    def test_index_sorts_errors_before_running(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = make_settings(Path(temp_dir))
            manager = TelegramConsoleManager(settings)
            manager._state["sessions"] = {
                "tmux:build": {
                    "route_kind": "tmux",
                    "route_target": "build",
                    "kind": "tmux",
                    "label": "build",
                    "state": "running",
                    "step": "building",
                    "updated_at": "2026-03-15T21:00:00+00:00",
                },
                "mirror:ttys002": {
                    "route_kind": "mirror",
                    "route_target": "ttys002",
                    "kind": "mirror",
                    "label": "oracle-chat-link",
                    "state": "error",
                    "step": "traceback",
                    "updated_at": "2026-03-15T21:00:01+00:00",
                },
            }
            payload = manager._render_index_text().splitlines()
            error_index = next(i for i, line in enumerate(payload) if "oracle-chat-link" in line)
            running_index = next(i for i, line in enumerate(payload) if "build" in line)
            self.assertLess(error_index, running_index)

    def test_index_sorts_newer_sessions_first_with_same_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = make_settings(Path(temp_dir))
            manager = TelegramConsoleManager(settings)
            manager._state["sessions"] = {
                "tmux:older": {
                    "route_kind": "tmux",
                    "route_target": "older",
                    "kind": "tmux",
                    "label": "older",
                    "state": "running",
                    "step": "building",
                    "updated_at": "2026-03-15T21:00:00+00:00",
                },
                "tmux:newer": {
                    "route_kind": "tmux",
                    "route_target": "newer",
                    "kind": "tmux",
                    "label": "newer",
                    "state": "running",
                    "step": "building",
                    "updated_at": "2026-03-15T21:00:05+00:00",
                },
            }
            payload = manager._render_index_text().splitlines()
            newer_index = next(i for i, line in enumerate(payload) if "newer" in line)
            older_index = next(i for i, line in enumerate(payload) if "older" in line)
            self.assertLess(newer_index, older_index)

    def test_lookup_route_by_message_covers_status_alert_event_and_bump(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = make_settings(Path(temp_dir))
            manager = TelegramConsoleManager(settings)
            manager._state["sessions"]["mirror:ttys002"] = {
                "route_kind": "mirror",
                "route_target": "ttys002",
                "kind": "mirror",
                "label": "oracle-chat-link",
                "status_chat_id": -1001,
                "status_message_id": 11,
                "last_event_chat_id": -1001,
                "last_event_message_id": 12,
                "last_alert_chat_id": -1001,
                "last_alert_message_id": 13,
                "topic_bump_chat_id": -1001,
                "topic_bump_message_id": 14,
            }
            expected = ReplyRoute("mirror", "ttys002")
            self.assertEqual(manager.lookup_route_by_message(-1001, 11), expected)
            self.assertEqual(manager.lookup_route_by_message(-1001, 12), expected)
            self.assertEqual(manager.lookup_route_by_message(-1001, 13), expected)
            self.assertEqual(manager.lookup_route_by_message(-1001, 14), expected)

    def test_waiting_transition_is_marked_dirty_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = make_settings(Path(temp_dir))
            manager = TelegramConsoleManager(settings)
            manager._application = FakeApplication()
            manager.set_forum_enabled(True)
            route = ReplyRoute("mirror", "ttys002")
            manager._state["sessions"][route.label] = {
                "route_kind": "mirror",
                "route_target": "ttys002",
                "kind": "mirror",
                "label": "oracle-chat-link",
                "title": "Terminal",
                "state": "running",
                "step": "mirroring",
                "summary": "still working",
                "updated_at": "2026-03-15T21:00:00+00:00",
                "status_message_id": 11,
                "status_text": "old",
                "topic_id": 42,
                "topic_title": "🟢 oracle-chat-link",
            }
            manager._last_status_render_monotonic[route.label] = time.monotonic()

            asyncio.run(
                manager.update_status(
                    SessionStatusSpec(
                        route=route,
                        kind="mirror",
                        label="oracle-chat-link",
                        title="Terminal",
                        state="waiting",
                        step="waiting for input",
                        summary="ready for input",
                    )
                )
            )

            self.assertIn(route.label, manager._dirty_session_routes)
            self.assertLessEqual(
                manager._pending_status_due_monotonic[route.label],
                time.monotonic() + 0.1,
            )
            self.assertEqual(manager._topic_bump_reason_by_route[route.label], "waiting")

    def test_render_record_mentions_direct_topic_input_when_forum_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = make_settings(Path(temp_dir))
            manager = TelegramConsoleManager(settings)
            manager.set_forum_enabled(True)
            manager._state["sessions"]["mirror:ttys002"] = {
                "route_kind": "mirror",
                "route_target": "ttys002",
                "kind": "mirror",
                "label": "oracle-chat-link",
                "title": "Terminal",
                "state": "running",
                "step": "mirroring",
                "summary": "line1\nline2",
                "updated_at": "2026-03-15T21:00:00+00:00",
                "topic_id": 42,
            }
            payload = manager.render_record("oracle-chat-link")
            self.assertIn("send text directly in this topic", payload)

    def test_resolve_route_by_topic_uses_bound_topic_and_chat(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = make_settings(Path(temp_dir))
            manager = TelegramConsoleManager(settings)
            manager.set_console_chat(-1001)
            manager._state["sessions"]["mirror:ttys002"] = {
                "route_kind": "mirror",
                "route_target": "ttys002",
                "kind": "mirror",
                "label": "oracle-chat-link",
                "topic_id": 42,
                "status_chat_id": -1001,
            }
            self.assertEqual(
                manager.resolve_route_by_topic(-1001, 42),
                ReplyRoute("mirror", "ttys002"),
            )
            self.assertIsNone(manager.resolve_route_by_topic(-1002, 42))

    def test_set_session_topic_queues_stale_topic_cleanup_on_rebind(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = make_settings(Path(temp_dir))
            manager = TelegramConsoleManager(settings)
            route = ReplyRoute("mirror", "ttys035")
            manager._state["sessions"][route.label] = {
                "route_kind": "mirror",
                "route_target": "ttys035",
                "label": "ttys035",
                "topic_id": 41,
                "topic_title": "🟢 ttys035",
                "status_chat_id": -1001,
                "status_message_id": 11,
                "topic_bump_message_id": 12,
            }

            manager.set_session_topic(route, 42)

            record = manager._state["sessions"][route.label]
            self.assertEqual(record["topic_id"], 42)
            self.assertIsNone(record["status_message_id"])
            stale_topics = record.get("_stale_topics") or []
            self.assertEqual(len(stale_topics), 1)
            self.assertEqual(stale_topics[0]["topic_id"], 41)
            self.assertEqual(stale_topics[0]["status_message_id"], 11)

    def test_create_session_topic_reuses_existing_topic_instead_of_duplication(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = make_settings(Path(temp_dir))
            manager = TelegramConsoleManager(settings)
            manager._application = FakeApplication(bot=FakeBot())
            manager.set_forum_enabled(True)
            route = ReplyRoute("mirror", "ttys035")
            manager._state["sessions"][route.label] = {
                "route_kind": "mirror",
                "route_target": "ttys035",
                "label": "ttys035",
                "kind": "mirror",
                "topic_id": 41,
                "topic_title": "🟢 ttys035",
            }

            topic_id = asyncio.run(manager.create_session_topic(route, topic_name="Custom Title"))

            self.assertEqual(topic_id, 41)
            self.assertEqual(
                manager._state["sessions"][route.label]["_pending_topic_title"],
                "Custom Title",
            )

    def test_flush_stale_topic_cleanup_archives_previous_topic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = make_settings(Path(temp_dir))
            bot = FakeBot()
            manager = TelegramConsoleManager(settings)
            manager._application = FakeApplication(bot=bot)
            manager.set_forum_enabled(True)
            manager.set_console_chat(-1001)
            manager._state["sessions"]["mirror:ttys035"] = {
                "route_kind": "mirror",
                "route_target": "ttys035",
                "label": "ttys035",
                "kind": "mirror",
                "state": "running",
                "_stale_topics": [
                    {
                        "topic_id": 41,
                        "chat_id": -1001,
                        "status_message_id": 11,
                        "topic_bump_message_id": 12,
                        "topic_title": "🟢 ttys035",
                        "label": "ttys035",
                    }
                ],
            }

            handled = asyncio.run(manager._flush_stale_topic_cleanup())

            self.assertTrue(handled)
            self.assertNotIn("_stale_topics", manager._state["sessions"]["mirror:ttys035"])
            self.assertEqual(
                bot.calls,
                [
                    ("delete_message", {"chat_id": -1001, "message_id": 11}),
                    ("delete_message", {"chat_id": -1001, "message_id": 12}),
                    (
                        "edit_forum_topic",
                        {"chat_id": -1001, "message_thread_id": 41, "name": "⚫ archived | ttys035"},
                    ),
                    (
                        "close_forum_topic",
                        {"chat_id": -1001, "message_thread_id": 41},
                    ),
                ],
            )


if __name__ == "__main__":
    unittest.main()
