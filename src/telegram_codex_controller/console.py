from __future__ import annotations

import asyncio
from collections import deque
import contextlib
import hashlib
import json
import logging
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List

from telegram import Bot, ForumTopic, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import Application

from .config import Settings
from .reply_routes import ReplyRoute, remember_reply_route, send_chunked_message
from .utils import tail_lines


LOG = logging.getLogger(__name__)

INDEX_TOPIC_NAME = "INDEX"
ALERTS_TOPIC_NAME = "ALERTS"


@dataclass(frozen=True)
class SessionStatusSpec:
    route: ReplyRoute
    kind: str
    label: str
    title: str
    state: str
    step: str
    summary: str
    event: str | None = None
    alert: str | None = None
    force: bool = False


class TelegramConsoleManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.state_path = settings.assistant_state_path.parent / "console_state.json"
        self._application: Application | None = None
        self._stuck_task: asyncio.Task | None = None
        self._pin_task: asyncio.Task | None = None
        self._flush_task: asyncio.Task | None = None
        self._topic_bump_task: asyncio.Task | None = None
        self._state = self._load_state()
        self._last_status_render_monotonic: Dict[str, float] = {}
        self._last_index_render_monotonic = 0.0
        self._pin_queue: deque[tuple[int, int]] = deque()
        self._pin_queue_seen: set[tuple[int, int]] = set()
        self._write_backoff_until_monotonic = 0.0
        self._next_write_allowed_monotonic = 0.0
        self._next_running_write_allowed_monotonic = 0.0
        self._dirty_session_routes: set[str] = set()
        self._pending_status_due_monotonic: Dict[str, float] = {}
        self._topic_bump_queue: deque[str] = deque()
        self._topic_bump_queue_seen: set[str] = set()
        self._topic_bump_reason_by_route: Dict[str, str] = {}
        self._index_dirty = True
        self._index_due_monotonic = 0.0
        self._index_urgent = True

    async def start(self, application: Application) -> None:
        self._application = application
        self._prune_expired_records()
        self._restore_reply_routes()
        self._queue_existing_stopped_session_cleanups()
        if self._stuck_task is None:
            self._stuck_task = asyncio.create_task(self._stuck_watcher())
        if self._pin_task is None:
            self._pin_task = asyncio.create_task(self._pin_worker())
        if self._flush_task is None:
            self._flush_task = asyncio.create_task(self._flush_worker())
        if self._topic_bump_task is None:
            self._topic_bump_task = asyncio.create_task(self._topic_bump_worker())

    async def stop(self) -> None:
        task = self._stuck_task
        self._stuck_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        pin_task = self._pin_task
        self._pin_task = None
        if pin_task is not None:
            pin_task.cancel()
            try:
                await pin_task
            except asyncio.CancelledError:
                pass
        flush_task = self._flush_task
        self._flush_task = None
        if flush_task is not None:
            flush_task.cancel()
            try:
                await flush_task
            except asyncio.CancelledError:
                pass
        topic_bump_task = self._topic_bump_task
        self._topic_bump_task = None
        if topic_bump_task is not None:
            topic_bump_task.cancel()
            try:
                await topic_bump_task
            except asyncio.CancelledError:
                pass
        self._persist()

    async def update_status(self, spec: SessionStatusSpec) -> None:
        if self._application is None:
            return

        record = self._state["sessions"].setdefault(spec.route.label, {})
        now_iso = _now_iso()
        previous_last_error = str(record.get("last_error", ""))
        previous_state = str(record.get("state", ""))
        previous_step = str(record.get("step", ""))
        previous_summary = str(record.get("summary", ""))
        previous_title = str(record.get("title", ""))
        previous_label = str(record.get("label", ""))
        previous_kind = str(record.get("kind", ""))
        state_changed = previous_state != spec.state

        record.update(
            {
                "route_kind": spec.route.kind,
                "route_target": spec.route.target,
                "callback_key": record.get("callback_key") or _callback_key(spec.route),
                "kind": spec.kind,
                "label": spec.label,
                "title": spec.title,
                "state": spec.state,
                "step": spec.step,
                "summary": spec.summary,
                "last_seen_at": now_iso,
            }
        )
        desired_topic_title = _topic_title(record)
        if desired_topic_title != record.get("topic_title"):
            record["_pending_topic_title"] = desired_topic_title
        meaningful_change = (
            spec.force
            or previous_state != spec.state
            or previous_step != spec.step
            or previous_summary != spec.summary
            or previous_title != spec.title
            or previous_label != spec.label
            or previous_kind != spec.kind
        )
        if spec.alert and spec.state in {"error", "waiting"}:
            record["last_error"] = spec.alert
            record["last_error_at"] = now_iso
            record["stuck_alert_sent_at"] = None
            meaningful_change = meaningful_change or previous_last_error != spec.alert
        elif spec.state in {"running", "idle", "done", "stopped"}:
            record["last_error"] = ""
            record["last_error_at"] = ""
            record["last_alert_text"] = ""
            record["stuck_alert_sent_at"] = None
            record["last_alert_signature"] = None
            meaningful_change = meaningful_change or bool(previous_last_error)

        if meaningful_change or not record.get("updated_at"):
            record["updated_at"] = now_iso

        status_text = self._render_status_text(record)
        last_text = record.get("status_text")
        record["_pending_status_text"] = status_text
        if spec.state == "stopped":
            self._dirty_session_routes.discard(spec.route.label)
            self._pending_status_due_monotonic.pop(spec.route.label, None)
            self._queue_stopped_session_cleanup(record)
        elif self._session_has_pending_work(record):
            self._mark_session_dirty(
                spec.route.label,
                record,
                immediate=spec.force
                or state_changed
                or spec.state in {"error", "waiting", "done", "stopped"}
                or self._pending_topic_title_changed(record),
            )

        if spec.event:
            await self._send_session_event(record, spec.route, spec.event)
        if spec.alert:
            await self._send_alert(record, spec.route, spec.alert)

        if state_changed:
            if spec.state in {"running", "waiting", "error", "done", "stopped"}:
                self.request_topic_bump(spec.route, reason=spec.state)
        elif spec.state in {"waiting", "error"} and (
            previous_step != spec.step or previous_summary != spec.summary or spec.force
        ):
            self.request_topic_bump(spec.route, reason=spec.state)

        self._persist()
        if self._should_refresh_index(
            previous_state=previous_state,
            previous_step=previous_step,
            previous_summary=previous_summary,
            new_state=spec.state,
            new_step=spec.step,
            new_summary=spec.summary,
            force=spec.force,
        ):
            urgent_index = (
                spec.state in {"error", "waiting", "done", "stopped"}
                or previous_state in {"error", "waiting"}
            )
            immediate_index = spec.force or urgent_index or (
                previous_state != spec.state and spec.state != "running"
            )
            self._mark_index_dirty(
                immediate=immediate_index,
                urgent=urgent_index or spec.force,
            )

    async def update_index(self) -> None:
        self._prune_expired_records()
        self._mark_index_dirty(immediate=True, urgent=True)
        self._persist()

    async def send_log_document(
        self,
        route: ReplyRoute,
        filename: str,
        content: str,
        caption: str,
    ) -> None:
        if self._application is None:
            return

        record = self._state["sessions"].setdefault(
            route.label,
            {
                "route_kind": route.kind,
                "route_target": route.target,
                "label": route.target,
                "kind": route.kind,
            },
        )

        chat_id, thread_id = await self._ensure_session_target(record)
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=Path(filename).suffix or ".log") as handle:
            handle.write(content)
            temp_path = Path(handle.name)

        try:
            sent = await self._application.bot.send_document(
                chat_id=chat_id,
                document=temp_path,
                filename=filename,
                caption=caption,
                message_thread_id=thread_id,
            )
            remember_reply_route(self._application, chat_id, sent.message_id, route)
        finally:
            temp_path.unlink(missing_ok=True)

    async def send_text(
        self,
        route: ReplyRoute,
        text: str,
    ) -> None:
        if self._application is None:
            return
        record = self._state["sessions"].setdefault(
            route.label,
            {
                "route_kind": route.kind,
                "route_target": route.target,
                "label": route.target,
                "kind": route.kind,
            },
        )
        chat_id, thread_id = await self._ensure_session_target(record)
        await send_chunked_message(
            self._application,
            chat_id,
            text,
            message_thread_id=thread_id,
            route=route,
        )

    def recent_errors(self, limit: int = 10) -> List[dict[str, Any]]:
        rows = [
            record
            for record in self._state.get("sessions", {}).values()
            if record.get("last_error") and record.get("last_error_at")
        ]
        rows.sort(key=lambda item: item["last_error_at"], reverse=True)
        return rows[:limit]

    def resolve_callback_key(self, key: str) -> ReplyRoute | None:
        for record in self._state.get("sessions", {}).values():
            if record.get("callback_key") == key:
                return ReplyRoute(kind=record["route_kind"], target=record["route_target"])
        return None

    def should_emit_artifact(
        self,
        route: ReplyRoute,
        artifact_key: str,
        signature: str,
        *,
        cooldown_minutes: int = 15,
    ) -> bool:
        record = self._state["sessions"].setdefault(
            route.label,
            {
                "route_kind": route.kind,
                "route_target": route.target,
                "label": route.target,
                "kind": route.kind,
            },
        )
        artifact_state = record.setdefault("artifacts", {}).setdefault(artifact_key, {})
        last_signature = artifact_state.get("signature")
        last_at = _parse_iso(artifact_state.get("at", ""))
        if (
            last_signature == signature
            and last_at is not None
            and datetime.now(UTC) - last_at < timedelta(minutes=cooldown_minutes)
        ):
            return False
        artifact_state["signature"] = signature
        artifact_state["at"] = _now_iso()
        self._persist()
        return True

    def open_record(self, identifier: str) -> dict[str, Any]:
        record = self._resolve_record(identifier)
        if record is None:
            raise ValueError(f"Session '{identifier}' is not tracked")
        return record

    def render_record(self, identifier: str) -> str:
        return self._render_status_text(self.open_record(identifier))

    def session_labels(self) -> List[str]:
        self._prune_expired_records()
        return sorted(self._state.get("sessions", {}))

    def clear_session(self, route: ReplyRoute) -> None:
        self._state.get("sessions", {}).pop(route.label, None)
        self._dirty_session_routes.discard(route.label)
        self._pending_status_due_monotonic.pop(route.label, None)
        self._topic_bump_reason_by_route.pop(route.label, None)
        self._topic_bump_queue_seen.discard(route.label)
        self._last_status_render_monotonic.pop(route.label, None)
        self._mark_index_dirty(immediate=True, urgent=True)
        self._persist()

    def lookup_route_by_message(self, chat_id: int, message_id: int) -> ReplyRoute | None:
        for record in self._state.get("sessions", {}).values():
            route_kind = record.get("route_kind")
            route_target = record.get("route_target")
            if not route_kind or not route_target:
                continue
            if (
                record.get("status_chat_id") is not None
                and record.get("status_message_id") is not None
                and int(record["status_chat_id"]) == int(chat_id)
                and int(record["status_message_id"]) == int(message_id)
            ):
                return ReplyRoute(kind=route_kind, target=route_target)
            event_message_id = record.get("last_event_message_id")
            event_chat_id = record.get("last_event_chat_id") or record.get("status_chat_id") or self._console_chat_id()
            if (
                event_message_id is not None
                and int(event_chat_id) == int(chat_id)
                and int(event_message_id) == int(message_id)
            ):
                return ReplyRoute(kind=route_kind, target=route_target)
            alert_message_id = record.get("last_alert_message_id")
            alert_chat_id = record.get("last_alert_chat_id") or self._console_chat_id()
            if (
                alert_message_id is not None
                and int(alert_chat_id) == int(chat_id)
                and int(alert_message_id) == int(message_id)
            ):
                return ReplyRoute(kind=route_kind, target=route_target)
            topic_bump_message_id = record.get("topic_bump_message_id")
            topic_bump_chat_id = record.get("topic_bump_chat_id") or record.get("status_chat_id") or self._console_chat_id()
            if (
                topic_bump_message_id is not None
                and int(topic_bump_chat_id) == int(chat_id)
                and int(topic_bump_message_id) == int(message_id)
            ):
                return ReplyRoute(kind=route_kind, target=route_target)
        return None

    def resolve_route_by_topic(self, chat_id: int, message_thread_id: int | None) -> ReplyRoute | None:
        if message_thread_id is None:
            return None
        for record in self._state.get("sessions", {}).values():
            route_kind = record.get("route_kind")
            route_target = record.get("route_target")
            topic_id = record.get("topic_id")
            if not route_kind or not route_target or topic_id is None:
                continue
            record_chat_id = record.get("status_chat_id") or self._console_chat_id()
            if int(record_chat_id) != int(chat_id):
                continue
            if int(topic_id) == int(message_thread_id):
                return ReplyRoute(kind=route_kind, target=route_target)
        return None

    def request_topic_bump(self, route: ReplyRoute, *, reason: str = "activity") -> None:
        self._enqueue_topic_bump(route.label, reason=reason)

    def resolve_identifier(self, identifier: str) -> ReplyRoute | None:
        record = self._resolve_record(identifier)
        if record is None:
            return None
        return ReplyRoute(kind=record["route_kind"], target=record["route_target"])

    def set_console_chat(self, chat_id: int) -> None:
        self._state.setdefault("meta", {})["console_chat_id"] = int(chat_id)
        self._persist()

    def set_forum_enabled(self, enabled: bool) -> None:
        self._state.setdefault("meta", {})["forum_enabled_override"] = bool(enabled)
        self._persist()

    def forum_enabled(self) -> bool:
        meta = self._state.setdefault("meta", {})
        override = meta.get("forum_enabled_override")
        if override is not None:
            return bool(override)
        return self.settings.console_forum_enabled

    def set_special_topic(self, kind: str, message_thread_id: int | None) -> None:
        meta = self._state.setdefault("meta", {})
        meta[f"{kind}_topic_id"] = message_thread_id
        if kind == "index":
            meta["index_message_id"] = None
        self._persist()

    def set_session_topic(self, route: ReplyRoute, message_thread_id: int | None) -> None:
        record = self._state.get("sessions", {}).setdefault(route.label, {})
        record["route_kind"] = route.kind
        record["route_target"] = route.target
        self._queue_stale_topic_cleanup(record, new_topic_id=message_thread_id)
        record["topic_id"] = message_thread_id
        record["status_message_id"] = None
        record["topic_bump_message_id"] = None
        record["topic_bump_chat_id"] = None
        record["last_topic_bump_at"] = None
        self._persist()

    async def ensure_alerts_ready(self) -> int | None:
        _chat_id, thread_id = await self._ensure_special_target("alerts")
        return thread_id

    async def ensure_session_topic(self, route: ReplyRoute, *, topic_name: str | None = None) -> int | None:
        if not self.forum_enabled():
            return None
        record = self._state.get("sessions", {}).setdefault(
            route.label,
            {
                "route_kind": route.kind,
                "route_target": route.target,
                "label": route.target,
                "kind": route.kind,
            },
        )
        if record.get("topic_id"):
            return int(record["topic_id"])
        return await self.create_session_topic(route, topic_name=topic_name)

    def routes(self) -> List[ReplyRoute]:
        result: List[ReplyRoute] = []
        for record in self._state.get("sessions", {}).values():
            route_kind = record.get("route_kind")
            route_target = record.get("route_target")
            if route_kind and route_target:
                result.append(ReplyRoute(kind=route_kind, target=route_target))
        return result

    async def create_session_topic(self, route: ReplyRoute, *, topic_name: str | None = None) -> int:
        if self._application is None:
            raise RuntimeError("Console manager is not attached to the Telegram application.")
        if not self.forum_enabled():
            raise ValueError("Forum mode is disabled. Set CONSOLE_FORUM_ENABLED=true first.")

        record = self._state.get("sessions", {}).setdefault(
            route.label,
            {
                "route_kind": route.kind,
                "route_target": route.target,
                "label": route.target,
                "kind": route.kind,
            },
        )
        if record.get("topic_id"):
            if topic_name:
                normalized_topic_name = topic_name[:128]
                if normalized_topic_name != record.get("topic_title"):
                    record["_pending_topic_title"] = normalized_topic_name
                    self._mark_session_dirty(route.label, record, immediate=True)
                    self._persist()
            return int(record["topic_id"])
        topic = await self._create_topic(self._console_chat_id(), topic_name or self._session_topic_name(record))
        record["topic_id"] = topic.message_thread_id
        record["topic_title"] = topic_name or self._session_topic_name(record)
        record["status_message_id"] = None
        record["topic_bump_message_id"] = None
        record["topic_bump_chat_id"] = None
        record["last_topic_bump_at"] = None
        self._persist()
        return topic.message_thread_id

    async def _send_session_event(self, record: dict[str, Any], route: ReplyRoute, text: str) -> None:
        if self._application is None:
            return
        chat_id, thread_id = await self._ensure_session_target(record)
        sent = await self._application.bot.send_message(
            chat_id=chat_id,
            text=text,
            message_thread_id=thread_id,
        )
        remember_reply_route(self._application, chat_id, sent.message_id, route)
        record["last_event_message_id"] = sent.message_id
        record["last_event_chat_id"] = chat_id
        self._persist()

    async def _send_alert(self, record: dict[str, Any], route: ReplyRoute, text: str) -> None:
        if self._application is None:
            return
        last_alert_signature = record.get("last_alert_signature")
        current_signature = f"{record.get('state')}:{record.get('step')}"
        last_alert_at = _parse_iso(record.get("last_alert_at", ""))
        if (
            last_alert_signature == current_signature
            and last_alert_at is not None
            and datetime.now(UTC) - last_alert_at < timedelta(minutes=15)
        ):
            return

        chat_id, thread_id = await self._ensure_special_target("alerts")
        sent = await self._application.bot.send_message(
            chat_id=chat_id,
            text=text,
            message_thread_id=thread_id,
            reply_markup=self._status_reply_markup(record),
        )
        remember_reply_route(self._application, chat_id, sent.message_id, route)
        record["last_alert_message_id"] = sent.message_id
        record["last_alert_chat_id"] = chat_id
        record["last_alert_text"] = text
        record["last_alert_signature"] = current_signature
        record["last_alert_at"] = _now_iso()
        self._persist()

    async def _stuck_watcher(self) -> None:
        while True:
            try:
                await self._check_stuck_sessions()
            except Exception:
                LOG.exception("Stuck-session watcher failed")
            await asyncio.sleep(60)

    async def _topic_bump_worker(self) -> None:
        while True:
            try:
                self._queue_periodic_topic_bumps()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Topic bump worker failed")
            await asyncio.sleep(60)

    async def _check_stuck_sessions(self) -> None:
        if self._application is None:
            return
        threshold = timedelta(minutes=self.settings.console_stuck_minutes)
        now = datetime.now(UTC)
        for record in self._state.get("sessions", {}).values():
            if record.get("state") != "running":
                continue
            last_seen_at = record.get("last_seen_at") or record.get("updated_at")
            if not last_seen_at:
                continue
            seen_at = _parse_iso(last_seen_at)
            if seen_at is None or now - seen_at < threshold:
                continue
            if record.get("stuck_alert_sent_at"):
                continue

            route = ReplyRoute(kind=record["route_kind"], target=record["route_target"])
            alert = f"⏱ {record['label']} appears stuck for more than {self.settings.console_stuck_minutes}m.\nStep: {record.get('step', '-')}"
            await self._send_alert(record, route, alert)
            self.request_topic_bump(route, reason="stuck")
            record["stuck_alert_sent_at"] = _now_iso()
            self._persist()

    def _queue_periodic_topic_bumps(self) -> None:
        if self._application is None or not self.forum_enabled() or not self.settings.console_topic_bump_enabled:
            return
        now = datetime.now(UTC)
        interval = timedelta(minutes=self.settings.console_topic_bump_minutes)
        for route_label, record in self._state.get("sessions", {}).items():
            if record.get("state") != "running":
                continue
            topic_id = record.get("topic_id")
            chat_id = record.get("status_chat_id") or self._console_chat_id()
            if not topic_id or not chat_id:
                continue
            last_bump = _parse_iso(record.get("last_topic_bump_at", ""))
            if last_bump is not None and now - last_bump < interval:
                continue
            self._enqueue_topic_bump(route_label, reason="activity")

    async def _flush_topic_bump(self, *, urgent_only: bool = False) -> bool:
        if self._application is None or not self.forum_enabled() or not self.settings.console_topic_bump_enabled:
            return False
        if self._in_write_backoff() or time.monotonic() < self._next_write_allowed_monotonic:
            return False

        queue_length = len(self._topic_bump_queue)
        for _ in range(queue_length):
            route_label = self._topic_bump_queue.popleft()
            reason = self._topic_bump_reason_by_route.get(route_label, "activity")
            if urgent_only and _topic_bump_priority(reason) < _topic_bump_priority("done"):
                self._topic_bump_queue.append(route_label)
                continue

            self._topic_bump_queue_seen.discard(route_label)
            record = self._state.get("sessions", {}).get(route_label)
            if record is None:
                self._topic_bump_reason_by_route.pop(route_label, None)
                continue
            topic_id = record.get("topic_id")
            chat_id = record.get("status_chat_id") or self._console_chat_id()
            if not topic_id or not chat_id:
                self._topic_bump_reason_by_route.pop(route_label, None)
                continue

            reason = self._topic_bump_reason_by_route.pop(route_label, reason)
            route = ReplyRoute(kind=record["route_kind"], target=record["route_target"])
            now_iso = _now_iso()
            text = _topic_bump_text(record, reason=reason, when_iso=now_iso)
            old_bump_id = record.get("topic_bump_message_id")
            try:
                sent = await self._application.bot.send_message(
                    chat_id=int(chat_id),
                    message_thread_id=int(topic_id),
                    text=text,
                    disable_notification=True,
                )
                remember_reply_route(self._application, int(chat_id), sent.message_id, route)
                record["topic_bump_message_id"] = sent.message_id
                record["topic_bump_chat_id"] = int(chat_id)
                record["last_topic_bump_at"] = now_iso
                self._record_write(time.monotonic(), state=str(record.get("state", "")), urgent=True)
                self._persist()
                if old_bump_id:
                    with contextlib.suppress(Exception):
                        await self._application.bot.delete_message(
                            chat_id=int(chat_id),
                            message_id=int(old_bump_id),
                        )
                return True
            except RetryAfter as exc:
                self._topic_bump_reason_by_route[route_label] = reason
                self._topic_bump_queue.appendleft(route_label)
                self._topic_bump_queue_seen.add(route_label)
                self._enter_write_backoff(float(exc.retry_after) + 1)
                LOG.info("Topic bump backed off for %.1fs", float(exc.retry_after))
                return False
            except TelegramError:
                self._topic_bump_reason_by_route.pop(route_label, None)
                LOG.exception("Failed to bump topic for %s", route_label)
                return False
        return False

    async def _ensure_session_target(self, record: dict[str, Any]) -> tuple[int, int | None]:
        chat_id = self._console_chat_id()
        if not self.forum_enabled():
            return chat_id, None

        if record.get("topic_id"):
            return chat_id, int(record["topic_id"])

        if not self.settings.console_auto_create_topics:
            return chat_id, None

        topic = await self._create_topic(chat_id, self._session_topic_name(record))
        record["topic_id"] = topic.message_thread_id
        record["topic_title"] = self._session_topic_name(record)
        self._persist()
        return chat_id, topic.message_thread_id

    async def _ensure_special_target(self, kind: str) -> tuple[int, int | None]:
        chat_id = self._console_chat_id()
        if not self.forum_enabled():
            return chat_id, None

        meta = self._state.setdefault("meta", {})
        config_topic_id = (
            self.settings.console_index_topic_id
            if kind == "index"
            else self.settings.console_alerts_topic_id
        )
        stored_topic_id = meta.get(f"{kind}_topic_id")
        topic_id = config_topic_id or stored_topic_id
        if topic_id:
            meta[f"{kind}_topic_id"] = topic_id
            return chat_id, int(topic_id)

        if not self.settings.console_auto_create_topics:
            return chat_id, None

        topic_name = INDEX_TOPIC_NAME if kind == "index" else ALERTS_TOPIC_NAME
        topic = await self._create_topic(chat_id, topic_name)
        meta[f"{kind}_topic_id"] = topic.message_thread_id
        self._persist()
        return chat_id, topic.message_thread_id

    async def _create_topic(self, chat_id: int, name: str) -> ForumTopic:
        assert self._application is not None
        return await self._application.bot.create_forum_topic(chat_id=chat_id, name=name)

    def _render_status_text(self, record: dict[str, Any]) -> str:
        emoji = _state_emoji(record.get("state", "unknown"))
        summary = _render_summary_lines(
            record.get("summary", ""),
            self.settings.console_status_summary_lines,
        )
        lookup = record.get("label") or record.get("route_target")
        header_parts = [str(lookup), str(record.get("kind", "?"))]
        route_target = record.get("route_target")
        if route_target and route_target != lookup:
            header_parts.append(str(route_target))

        lines = [
            f"{emoji} {' | '.join(header_parts)}",
            f"State: {record.get('state', '-')}",
            f"Step: {record.get('step', '-')}",
            f"Updated: {_hhmmss(record.get('updated_at', ''))}",
            "Summary:",
            summary or "-",
            (
                "Reply here or send text directly in this topic to route input."
                if self.forum_enabled() and record.get("topic_id")
                else "Reply to this message to send input back to this session."
            ),
        ]
        return "\n".join(lines)

    def _render_index_text(self) -> str:
        order = {"error": 0, "waiting": 1, "running": 2, "idle": 3, "done": 4, "stopped": 5}
        rows = sorted(
            self._state.get("sessions", {}).values(),
            key=lambda item: (
                order.get(item.get("state", ""), 9),
                -(
                    _parse_iso(str(item.get("updated_at", ""))).timestamp()
                    if _parse_iso(str(item.get("updated_at", ""))) is not None
                    else 0.0
                ),
            ),
        )
        newest_row_update = ""
        newest_row_dt: datetime | None = None
        for record in rows:
            updated_at = str(record.get("updated_at", "")).strip()
            updated_dt = _parse_iso(updated_at)
            if updated_dt is None:
                continue
            if newest_row_dt is None or updated_dt > newest_row_dt:
                newest_row_dt = updated_dt
                newest_row_update = updated_at
        lines = [
            "Console INDEX",
            f"Updated: {_hhmmss(newest_row_update)}",
            "Legend: 🟢 running | 🟡 waiting | 🔴 error | ✅ done | ⚫ stopped",
            "",
        ]
        if not rows:
            lines.append("No tracked sessions yet.")
        else:
            for record in rows:
                emoji = _state_emoji(record.get("state", "unknown"))
                detail = _index_detail(record)
                lines.append(
                    f"{emoji} {record.get('label', '?')} | {record.get('state', '?')} | {detail} | {_hhmmss(record.get('updated_at', ''))}"
                )
        lines.extend(
            [
                "",
                "Use the buttons below to refresh or open a session.",
            ]
        )
        return "\n".join(lines)

    def _index_reply_markup(self) -> InlineKeyboardMarkup | None:
        rows = []
        rows.append(
            [
                InlineKeyboardButton("Refresh INDEX", callback_data="i|r|0"),
                InlineKeyboardButton("Recent Errors", callback_data="i|e|0"),
            ]
        )

        order = {"error": 0, "waiting": 1, "running": 2, "idle": 3, "done": 4, "stopped": 5}
        sessions = sorted(
            self._state.get("sessions", {}).values(),
            key=lambda item: (
                order.get(item.get("state", ""), 9),
                -(
                    _parse_iso(str(item.get("updated_at", ""))).timestamp()
                    if _parse_iso(str(item.get("updated_at", ""))) is not None
                    else 0.0
                ),
            ),
        )
        buttons = []
        for record in sessions[:6]:
            key = record.get("callback_key")
            if not key:
                route = ReplyRoute(kind=record.get("route_kind", "unknown"), target=record.get("route_target", "unknown"))
                key = _callback_key(route)
                record["callback_key"] = key
            label = str(record.get("label") or record.get("route_target") or "?")[:24]
            buttons.append(InlineKeyboardButton(label, callback_data=f"i|o|{key}"))

        for index in range(0, len(buttons), 2):
            rows.append(buttons[index:index + 2])
        return InlineKeyboardMarkup(rows)

    def _session_topic_name(self, record: dict[str, Any]) -> str:
        return _topic_title(record)

    def _status_reply_markup(self, record: dict[str, Any]) -> InlineKeyboardMarkup | None:
        key = record.get("callback_key")
        if not key:
            return None

        buttons = [
            [
                InlineKeyboardButton("Continue", callback_data=f"c|c|{key}"),
                InlineKeyboardButton("Refresh", callback_data=f"c|r|{key}"),
                InlineKeyboardButton("Recent", callback_data=f"c|t|{key}"),
            ]
        ]
        second_row: list[InlineKeyboardButton] = []
        if record.get("route_kind") == "mirror":
            second_row.append(InlineKeyboardButton("Focus", callback_data=f"c|f|{key}"))
        elif record.get("route_kind") in {"tmux", "agent"}:
            second_row.append(InlineKeyboardButton("Stop", callback_data=f"c|x|{key}"))
        if second_row:
            buttons.append(second_row)
        return InlineKeyboardMarkup(buttons)

    def _console_chat_id(self) -> int:
        meta_chat_id = self._state.setdefault("meta", {}).get("console_chat_id")
        if meta_chat_id is not None:
            return int(meta_chat_id)
        if self.settings.console_chat_id is not None:
            return self.settings.console_chat_id
        if self.settings.mirror_chat_ids:
            return sorted(self.settings.mirror_chat_ids)[0]
        return sorted(self.settings.authorized_user_ids)[0]

    async def _safe_pin(self, chat_id: int, message_id: int) -> None:
        if self._application is None:
            return
        try:
            await self._application.bot.pin_chat_message(chat_id=chat_id, message_id=message_id)
        except TelegramError:
            LOG.debug("Unable to pin message %s in chat %s", message_id, chat_id)

    def _queue_pin(self, chat_id: int, message_id: int) -> None:
        item = (chat_id, message_id)
        if item in self._pin_queue_seen:
            return
        self._pin_queue.append(item)
        self._pin_queue_seen.add(item)

    async def _pin_worker(self) -> None:
        while True:
            try:
                if not self._pin_queue:
                    await asyncio.sleep(2)
                    continue
                chat_id, message_id = self._pin_queue.popleft()
                self._pin_queue_seen.discard((chat_id, message_id))
                if self._application is None:
                    continue
                try:
                    await self._application.bot.pin_chat_message(chat_id=chat_id, message_id=message_id)
                    await asyncio.sleep(3)
                except RetryAfter as exc:
                    self._queue_pin(chat_id, message_id)
                    await asyncio.sleep(float(exc.retry_after) + 1)
                except TelegramError:
                    LOG.debug("Unable to pin message %s in chat %s", message_id, chat_id)
                    await asyncio.sleep(2)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Pin worker failed")
                await asyncio.sleep(5)

    async def _flush_worker(self) -> None:
        while True:
            try:
                await self._flush_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Flush worker failed")
                await asyncio.sleep(5)
            await asyncio.sleep(1)

    async def _flush_once(self) -> None:
        if self._application is None or self._in_write_backoff():
            return
        if time.monotonic() < self._next_write_allowed_monotonic:
            return

        route_label = self._next_dirty_session_route(include_background_running=False)
        if route_label is not None:
            handled = await self._flush_session(route_label)
            if handled:
                return

        if self._has_urgent_topic_bump():
            handled = await self._flush_topic_bump(urgent_only=True)
            if handled:
                return

        if self._should_flush_index(urgent_only=True):
            handled = await self._flush_index()
            if handled:
                return

        if self._should_flush_index(urgent_only=False):
            handled = await self._flush_index()
            if handled:
                return

        route_label = self._next_dirty_session_route(include_background_running=True)
        if route_label is not None:
            handled = await self._flush_session(route_label)
            if handled:
                return

        handled = await self._flush_stopped_session_cleanup()
        if handled:
            return

        handled = await self._flush_stale_topic_cleanup()
        if handled:
            return

        await self._flush_topic_bump()

    async def _flush_session(self, route_label: str) -> bool:
        record = self._state.get("sessions", {}).get(route_label)
        if record is None:
            self._dirty_session_routes.discard(route_label)
            self._pending_status_due_monotonic.pop(route_label, None)
            return False

        pending_text = record.get("_pending_status_text")
        if not pending_text or not self._session_has_pending_work(record):
            self._dirty_session_routes.discard(route_label)
            self._pending_status_due_monotonic.pop(route_label, None)
            return False

        chat_id, thread_id = await self._ensure_session_target(record)
        reply_markup = self._status_reply_markup(record)
        current_text = record.get("status_text")
        now = time.monotonic()
        urgent = self._session_has_urgent_work(record)
        if self.forum_enabled() and record.get("topic_id") and record.get("_pending_topic_title"):
            pending_topic_title = str(record["_pending_topic_title"])[:128]
            if pending_topic_title != record.get("topic_title"):
                try:
                    await self._application.bot.edit_forum_topic(
                        chat_id=chat_id,
                        message_thread_id=int(record["topic_id"]),
                        name=pending_topic_title,
                    )
                    record["topic_title"] = pending_topic_title
                    record.pop("_pending_topic_title", None)
                    self._record_write(now, state=str(record.get("state", "")), urgent=True)
                    self._persist()
                    return True
                except RetryAfter as exc:
                    self._enter_write_backoff(float(exc.retry_after) + 1)
                    LOG.info("Topic title edit backed off for %.1fs", float(exc.retry_after))
                    return False
                except BadRequest as exc:
                    message = str(exc).lower()
                    if "not modified" in message:
                        record["topic_title"] = pending_topic_title
                        record.pop("_pending_topic_title", None)
                        self._record_write(now, state=str(record.get("state", "")), urgent=True)
                        if not self._session_has_pending_work(record):
                            self._dirty_session_routes.discard(route_label)
                            self._pending_status_due_monotonic.pop(route_label, None)
                        self._persist()
                        return True
                except TelegramError:
                    self._enter_write_backoff(5)
                    LOG.exception("Failed to edit topic title for %s", route_label)
                    return False

        if record.get("status_message_id"):
            if pending_text == current_text:
                self._dirty_session_routes.discard(route_label)
                self._pending_status_due_monotonic.pop(route_label, None)
                return False
            try:
                await self._application.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=record["status_message_id"],
                    text=pending_text,
                    reply_markup=reply_markup,
                )
                record["status_text"] = pending_text
                self._last_status_render_monotonic[route_label] = now
                self._dirty_session_routes.discard(route_label)
                self._pending_status_due_monotonic.pop(route_label, None)
                self._record_write(now, state=str(record.get("state", "")), urgent=urgent)
                self._persist()
                return True
            except RetryAfter as exc:
                self._enter_write_backoff(float(exc.retry_after) + 1)
                LOG.info("Status edit backed off for %.1fs", float(exc.retry_after))
                return False
            except BadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    record["status_text"] = pending_text
                    self._dirty_session_routes.discard(route_label)
                    self._pending_status_due_monotonic.pop(route_label, None)
                    self._record_write(now, state=str(record.get("state", "")), urgent=urgent)
                    self._persist()
                    return True
                record["status_message_id"] = None
            except TelegramError:
                self._enter_write_backoff(5)
                LOG.exception("Failed to edit status message for %s", route_label)
                return False

        try:
            sent = await self._application.bot.send_message(
                chat_id=chat_id,
                text=pending_text,
                message_thread_id=thread_id,
                reply_markup=reply_markup,
            )
            record["status_message_id"] = sent.message_id
            record["status_chat_id"] = chat_id
            record["status_thread_id"] = thread_id
            record["status_text"] = pending_text
            remember_reply_route(
                self._application,
                chat_id,
                sent.message_id,
                ReplyRoute(kind=record["route_kind"], target=record["route_target"]),
            )
            if self.settings.console_pin_status_messages:
                self._queue_pin(chat_id, sent.message_id)
            self._last_status_render_monotonic[route_label] = now
            self._dirty_session_routes.discard(route_label)
            self._pending_status_due_monotonic.pop(route_label, None)
            self._record_write(now, state=str(record.get("state", "")), urgent=urgent)
            self._persist()
            return True
        except RetryAfter as exc:
            self._enter_write_backoff(float(exc.retry_after) + 1)
            LOG.info("Status send backed off for %.1fs", float(exc.retry_after))
            return False
        except TelegramError:
            self._enter_write_backoff(5)
            LOG.exception("Failed to send status message for %s", route_label)
            return False

    async def _flush_index(self) -> bool:
        meta = self._state.setdefault("meta", {})
        chat_id, thread_id = await self._ensure_special_target("index")
        text = self._render_index_text()
        reply_markup = self._index_reply_markup()
        message_id = meta.get("index_message_id")
        current_text = meta.get("index_text")
        now = time.monotonic()

        if message_id:
            if text == current_text:
                self._index_dirty = False
                self._index_due_monotonic = 0.0
                self._index_urgent = False
                return False
            try:
                await self._application.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    reply_markup=reply_markup,
                )
                meta["index_text"] = text
                self._last_index_render_monotonic = now
                self._index_dirty = False
                self._index_due_monotonic = 0.0
                self._index_urgent = False
                self._bump_next_write(now)
                self._persist()
                return True
            except RetryAfter as exc:
                self._enter_write_backoff(float(exc.retry_after) + 1)
                LOG.info("INDEX edit backed off for %.1fs", float(exc.retry_after))
                return False
            except BadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    meta["index_text"] = text
                    self._index_dirty = False
                    self._index_due_monotonic = 0.0
                    self._index_urgent = False
                    self._bump_next_write(now)
                    self._persist()
                    return True
                meta["index_message_id"] = None
            except TelegramError:
                self._enter_write_backoff(5)
                LOG.exception("Failed to edit index message")
                return False

        try:
            sent = await self._application.bot.send_message(
                chat_id=chat_id,
                text=text,
                message_thread_id=thread_id,
                reply_markup=reply_markup,
            )
            meta["index_message_id"] = sent.message_id
            meta["index_text"] = text
            meta["index_chat_id"] = chat_id
            meta["index_thread_id"] = thread_id
            if self.settings.console_pin_status_messages:
                self._queue_pin(chat_id, sent.message_id)
            self._last_index_render_monotonic = now
            self._index_dirty = False
            self._index_due_monotonic = 0.0
            self._index_urgent = False
            self._bump_next_write(now)
            self._persist()
            return True
        except RetryAfter as exc:
            self._enter_write_backoff(float(exc.retry_after) + 1)
            LOG.info("INDEX send backed off for %.1fs", float(exc.retry_after))
            return False
        except TelegramError:
            self._enter_write_backoff(5)
            LOG.exception("Failed to send index message")
            return False

    def _next_dirty_session_route(self, *, include_background_running: bool) -> str | None:
        now = time.monotonic()
        candidates: list[tuple[int, int, float, float, str]] = []
        for route_label in list(self._dirty_session_routes):
            record = self._state.get("sessions", {}).get(route_label)
            if record is None:
                self._dirty_session_routes.discard(route_label)
                self._pending_status_due_monotonic.pop(route_label, None)
                continue
            if not self._session_has_pending_work(record):
                self._dirty_session_routes.discard(route_label)
                self._pending_status_due_monotonic.pop(route_label, None)
                continue
            priority = _state_priority(str(record.get("state", "")))
            updated = _parse_iso(str(record.get("updated_at", "")))
            timestamp = updated.timestamp() if updated is not None else 0.0
            due = self._pending_status_due_monotonic.get(route_label, 0.0)
            if self._pending_topic_title_changed(record):
                due = 0.0
            if due > now:
                continue
            urgent = self._session_has_urgent_work(record)
            if (
                record.get("state") == "running"
                and not urgent
                and (
                    not include_background_running
                    or now < self._next_running_write_allowed_monotonic
                )
            ):
                continue
            last_render = self._last_status_render_monotonic.get(route_label, 0.0)
            candidates.append((priority, 0 if urgent else 1, last_render, -timestamp, route_label))
        if not candidates:
            return None
        candidates.sort()
        return candidates[0][4]

    def _resolve_record(self, identifier: str) -> dict[str, Any] | None:
        self._prune_expired_records()
        clean = identifier.strip()
        sessions = self._state.get("sessions", {})
        if clean in sessions:
            return sessions[clean]
        for record in sessions.values():
            if record.get("label") == clean:
                return record
            if record.get("route_target") == clean:
                return record
        return None

    def _restore_reply_routes(self) -> None:
        if self._application is None:
            return
        for record in self._state.get("sessions", {}).values():
            message_id = record.get("status_message_id")
            chat_id = record.get("status_chat_id")
            if not message_id or not chat_id:
                message_id = None
            route = ReplyRoute(kind=record["route_kind"], target=record["route_target"])
            if message_id and chat_id:
                remember_reply_route(
                    self._application,
                    int(chat_id),
                    int(message_id),
                    route,
                )
            topic_bump_message_id = record.get("topic_bump_message_id")
            topic_bump_chat_id = record.get("topic_bump_chat_id") or chat_id
            if topic_bump_message_id and topic_bump_chat_id:
                remember_reply_route(
                    self._application,
                    int(topic_bump_chat_id),
                    int(topic_bump_message_id),
                    route,
                )
            event_message_id = record.get("last_event_message_id")
            event_chat_id = record.get("last_event_chat_id") or chat_id
            if event_message_id and event_chat_id:
                remember_reply_route(
                    self._application,
                    int(event_chat_id),
                    int(event_message_id),
                    route,
                )
            alert_message_id = record.get("last_alert_message_id")
            alert_chat_id = record.get("last_alert_chat_id")
            if alert_message_id and alert_chat_id:
                remember_reply_route(
                    self._application,
                    int(alert_chat_id),
                    int(alert_message_id),
                    route,
                )

    def _restore_pin_queue(self) -> None:
        if not self.settings.console_pin_status_messages:
            return
        meta = self._state.get("meta", {})
        index_chat_id = meta.get("index_chat_id")
        index_message_id = meta.get("index_message_id")
        if index_chat_id and index_message_id:
            self._queue_pin(int(index_chat_id), int(index_message_id))
        for record in self._state.get("sessions", {}).values():
            chat_id = record.get("status_chat_id")
            message_id = record.get("status_message_id")
            if chat_id and message_id:
                self._queue_pin(int(chat_id), int(message_id))

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"meta": {}, "sessions": {}}
        try:
            payload = json.loads(self.state_path.read_text())
        except Exception:
            LOG.warning("Failed to load console state from %s", self.state_path)
            return {"meta": {}, "sessions": {}}
        if not isinstance(payload, dict):
            return {"meta": {}, "sessions": {}}
        payload.setdefault("meta", {})
        payload.setdefault("sessions", {})
        return payload

    def _persist(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.state_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(self._state, ensure_ascii=False, indent=2))
        temp_path.replace(self.state_path)

    def _prune_expired_records(self) -> None:
        now = datetime.now(UTC)
        retention = timedelta(minutes=self.settings.console_completed_retention_minutes)
        sessions = self._state.get("sessions", {})
        expired = [
            route_label
            for route_label, record in sessions.items()
            if _is_expired_completed_record(record, now, retention)
        ]
        for route_label in expired:
            sessions.pop(route_label, None)
            self._dirty_session_routes.discard(route_label)
            self._pending_status_due_monotonic.pop(route_label, None)
            self._topic_bump_reason_by_route.pop(route_label, None)
            self._topic_bump_queue_seen.discard(route_label)
            self._last_status_render_monotonic.pop(route_label, None)

    def _should_refresh_index(
        self,
        *,
        previous_state: str,
        previous_step: str,
        previous_summary: str,
        new_state: str,
        new_step: str,
        new_summary: str,
        force: bool,
    ) -> bool:
        if force:
            return True
        if previous_state != new_state or previous_step != new_step:
            return True
        if new_state in {"error", "waiting"} and previous_summary != new_summary:
            return True
        return False

    def _session_has_pending_work(self, record: dict[str, Any]) -> bool:
        pending_text = record.get("_pending_status_text")
        status_changed = bool(pending_text) and (
            pending_text != record.get("status_text") or not record.get("status_message_id")
        )
        return status_changed or self._pending_topic_title_changed(record)

    def _mark_index_dirty(self, *, immediate: bool, urgent: bool) -> None:
        now = time.monotonic()
        due = now if immediate else max(
            now,
            self._last_index_render_monotonic + self._index_min_render_interval(),
        )
        if self._index_dirty:
            due = min(self._index_due_monotonic, due)
        self._index_dirty = True
        self._index_due_monotonic = due
        self._index_urgent = self._index_urgent or urgent

    def _should_flush_index(self, *, urgent_only: bool) -> bool:
        if not self._index_dirty:
            return False
        if urgent_only and not self._index_urgent:
            return False
        return self._index_due_monotonic <= time.monotonic()

    def _pending_topic_title_changed(self, record: dict[str, Any]) -> bool:
        pending_topic_title = record.get("_pending_topic_title")
        return bool(pending_topic_title) and pending_topic_title != record.get("topic_title")

    def _session_has_urgent_work(self, record: dict[str, Any]) -> bool:
        if self._pending_topic_title_changed(record):
            return True
        if not record.get("status_message_id"):
            return True
        if str(record.get("step", "")).strip().lower() == "input sent":
            return True
        return str(record.get("state", "")) in {"error", "waiting", "done", "stopped"}

    def _session_min_render_interval(self, record: dict[str, Any]) -> float:
        min_interval = float(self.settings.console_status_update_min_interval_seconds)
        if str(record.get("state", "")) == "running":
            min_interval = max(
                min_interval,
                float(self.settings.console_running_update_min_interval_seconds),
            )
        return min_interval

    def _index_min_render_interval(self) -> float:
        return max(
            float(self.settings.console_status_update_min_interval_seconds),
            float(self.settings.console_global_write_spacing_seconds) * 2.0,
        )

    def _mark_session_dirty(
        self,
        route_label: str,
        record: dict[str, Any],
        *,
        immediate: bool,
    ) -> None:
        now = time.monotonic()
        due = now if immediate else max(
            now,
            self._last_status_render_monotonic.get(route_label, 0.0) + self._session_min_render_interval(record),
        )
        existing_due = self._pending_status_due_monotonic.get(route_label)
        if existing_due is not None:
            due = now if immediate else min(existing_due, due)
        self._pending_status_due_monotonic[route_label] = due
        self._dirty_session_routes.add(route_label)

    def _queue_existing_stopped_session_cleanups(self) -> None:
        for record in self._state.get("sessions", {}).values():
            if record.get("state") == "stopped":
                self._queue_stopped_session_cleanup(record)

    def _queue_stopped_session_cleanup(self, record: dict[str, Any]) -> None:
        if record.get("_pending_topic_delete"):
            return
        route_kind = record.get("route_kind")
        route_target = record.get("route_target")
        if not route_kind or not route_target:
            return
        record["_pending_topic_delete"] = {
            "route_kind": route_kind,
            "route_target": route_target,
            "topic_id": record.get("topic_id"),
            "status_thread_id": record.get("status_thread_id"),
            "topic_managed": bool(record.get("topic_managed", True)),
            "chat_id": record.get("status_chat_id") or self._console_chat_id(),
            "status_message_id": record.get("status_message_id"),
            "topic_bump_message_id": record.get("topic_bump_message_id"),
            "last_event_message_id": record.get("last_event_message_id"),
            "label": record.get("label") or route_target,
        }

    async def _flush_stopped_session_cleanup(self) -> bool:
        if self._application is None:
            return False
        for route_label, record in list(self._state.get("sessions", {}).items()):
            pending = record.get("_pending_topic_delete")
            if not pending or record.get("state") != "stopped":
                continue

            route = ReplyRoute(kind=pending["route_kind"], target=pending["route_target"])
            chat_id = int(pending.get("chat_id") or self._console_chat_id())
            status_message_id = pending.get("status_message_id")
            topic_bump_message_id = pending.get("topic_bump_message_id")
            event_message_id = pending.get("last_event_message_id")
            topic_ids = []
            for candidate in [pending.get("topic_id"), pending.get("status_thread_id")]:
                if candidate and candidate not in topic_ids:
                    topic_ids.append(candidate)
            topic_managed = bool(pending.get("topic_managed", True))
            now = time.monotonic()

            try:
                for message_id in [status_message_id, topic_bump_message_id, event_message_id]:
                    if not message_id:
                        continue
                    with contextlib.suppress(BadRequest):
                        await self._application.bot.delete_message(
                            chat_id=chat_id,
                            message_id=int(message_id),
                        )
                if topic_ids and self.forum_enabled():
                    if topic_managed:
                        for topic_id in topic_ids:
                            with contextlib.suppress(BadRequest):
                                await self._application.bot.delete_forum_topic(
                                    chat_id=chat_id,
                                    message_thread_id=int(topic_id),
                                )
                    else:
                        for topic_id in topic_ids:
                            with contextlib.suppress(BadRequest):
                                await self._application.bot.edit_forum_topic(
                                    chat_id=chat_id,
                                    message_thread_id=int(topic_id),
                                    name=_archived_topic_title(
                                        {
                                            "label": pending.get("label") or pending.get("route_target") or "?",
                                            "topic_title": record.get("topic_title") or "",
                                        }
                                    ),
                                )
                            with contextlib.suppress(BadRequest):
                                await self._application.bot.close_forum_topic(
                                    chat_id=chat_id,
                                    message_thread_id=int(topic_id),
                                )
                self._record_write(now, state="stopped", urgent=True)
                self.clear_session(route)
                LOG.info("Deleted stopped session topic for %s", route_label)
                return True
            except RetryAfter as exc:
                self._enter_write_backoff(float(exc.retry_after) + 1)
                LOG.info("Stopped session cleanup backed off for %.1fs", float(exc.retry_after))
                return False
            except TelegramError:
                LOG.exception("Failed to delete stopped session topic for %s", route_label)
                self.clear_session(route)
                return False
        return False

    def _queue_stale_topic_cleanup(self, record: dict[str, Any], *, new_topic_id: int | None) -> None:
        old_topic_id = record.get("topic_id")
        if old_topic_id is None or old_topic_id == new_topic_id:
            return
        stale_topics = record.setdefault("_stale_topics", [])
        for item in stale_topics:
            if item.get("topic_id") == old_topic_id:
                return
        stale_topics.append(
            {
                "topic_id": old_topic_id,
                "chat_id": record.get("status_chat_id") or self._console_chat_id(),
                "status_message_id": record.get("status_message_id"),
                "topic_bump_message_id": record.get("topic_bump_message_id"),
                "topic_title": record.get("topic_title") or "",
                "label": record.get("label") or record.get("route_target") or "?",
            }
        )

    async def _flush_stale_topic_cleanup(self) -> bool:
        if self._application is None or not self.forum_enabled():
            return False
        for route_label, record in self._state.get("sessions", {}).items():
            stale_topics = record.get("_stale_topics")
            if not stale_topics:
                continue
            stale = stale_topics[0]
            topic_id = stale.get("topic_id")
            if topic_id is None:
                stale_topics.pop(0)
                if not stale_topics:
                    record.pop("_stale_topics", None)
                self._persist()
                return False

            chat_id = int(stale.get("chat_id") or self._console_chat_id())
            status_message_id = stale.get("status_message_id")
            topic_bump_message_id = stale.get("topic_bump_message_id")
            archived_title = _archived_topic_title(stale)
            now = time.monotonic()
            try:
                if status_message_id:
                    with contextlib.suppress(BadRequest):
                        await self._application.bot.delete_message(
                            chat_id=chat_id,
                            message_id=int(status_message_id),
                        )
                if topic_bump_message_id and topic_bump_message_id != status_message_id:
                    with contextlib.suppress(BadRequest):
                        await self._application.bot.delete_message(
                            chat_id=chat_id,
                            message_id=int(topic_bump_message_id),
                        )
                with contextlib.suppress(BadRequest):
                    await self._application.bot.edit_forum_topic(
                        chat_id=chat_id,
                        message_thread_id=int(topic_id),
                        name=archived_title,
                    )
                with contextlib.suppress(BadRequest):
                    await self._application.bot.close_forum_topic(
                        chat_id=chat_id,
                        message_thread_id=int(topic_id),
                    )
                stale_topics.pop(0)
                if not stale_topics:
                    record.pop("_stale_topics", None)
                self._record_write(now, state=str(record.get("state", "")), urgent=True)
                self._persist()
                LOG.info("Archived stale topic %s for %s", topic_id, route_label)
                return True
            except RetryAfter as exc:
                self._enter_write_backoff(float(exc.retry_after) + 1)
                LOG.info("Stale topic cleanup backed off for %.1fs", float(exc.retry_after))
                return False
            except TelegramError:
                LOG.exception("Failed to archive stale topic %s for %s", topic_id, route_label)
                stale_topics.pop(0)
                if not stale_topics:
                    record.pop("_stale_topics", None)
                self._persist()
                return False
        return False

    def _has_urgent_topic_bump(self) -> bool:
        return any(
            _topic_bump_priority(self._topic_bump_reason_by_route.get(route_label, "activity"))
            >= _topic_bump_priority("done")
            for route_label in self._topic_bump_queue
        )

    def _enqueue_topic_bump(self, route_label: str, *, reason: str) -> None:
        if not self.settings.console_topic_bump_enabled or not self.forum_enabled():
            return
        record = self._state.get("sessions", {}).get(route_label)
        if record is None or not record.get("topic_id"):
            return
        if not self._topic_bump_due(record, reason):
            return
        current_reason = self._topic_bump_reason_by_route.get(route_label)
        if current_reason is None or _topic_bump_priority(reason) >= _topic_bump_priority(current_reason):
            self._topic_bump_reason_by_route[route_label] = reason
        if route_label in self._topic_bump_queue_seen:
            return
        if _topic_bump_priority(reason) >= _topic_bump_priority("done"):
            self._topic_bump_queue.appendleft(route_label)
        else:
            self._topic_bump_queue.append(route_label)
        self._topic_bump_queue_seen.add(route_label)

    def _topic_bump_due(self, record: dict[str, Any], reason: str) -> bool:
        last_bump = _parse_iso(str(record.get("last_topic_bump_at", "")))
        if last_bump is None:
            return True
        seconds = _topic_bump_min_interval_seconds(
            reason,
            self.settings.console_topic_bump_minutes,
        )
        return datetime.now(UTC) - last_bump >= timedelta(seconds=seconds)

    def _enter_write_backoff(self, seconds: float) -> None:
        self._write_backoff_until_monotonic = max(
            self._write_backoff_until_monotonic,
            time.monotonic() + seconds,
        )

    def _in_write_backoff(self) -> bool:
        return time.monotonic() < self._write_backoff_until_monotonic

    def _bump_next_write(self, now: float) -> None:
        self._next_write_allowed_monotonic = max(
            self._next_write_allowed_monotonic,
            now + float(self.settings.console_global_write_spacing_seconds),
        )

    def _record_write(self, now: float, *, state: str, urgent: bool) -> None:
        self._bump_next_write(now)
        if state == "running" and not urgent:
            self._next_running_write_allowed_monotonic = max(
                self._next_running_write_allowed_monotonic,
                now + self._running_write_spacing_seconds(),
            )

    def _running_write_spacing_seconds(self) -> float:
        return max(
            float(self.settings.console_global_write_spacing_seconds),
            float(self.settings.console_running_update_min_interval_seconds) / 2.0,
        )


def _state_emoji(state: str) -> str:
    return {
        "running": "🟢",
        "idle": "⚪",
        "waiting": "🟡",
        "error": "🔴",
        "done": "✅",
        "stopped": "⚫",
    }.get(state, "🔵")


def _state_priority(state: str) -> int:
    return {
        "error": 0,
        "waiting": 1,
        "stopped": 2,
        "done": 3,
        "idle": 4,
        "running": 5,
    }.get(state, 9)


def _render_summary_lines(text: str, line_limit: int) -> str:
    if not text.strip():
        return ""
    summary = tail_lines(text.strip(), line_limit)
    return "\n".join(f"  {line}" for line in summary.splitlines())


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _hhmmss(value: str) -> str:
    dt = _parse_iso(value)
    if dt is None:
        return "--:--:--"
    return dt.astimezone().strftime("%H:%M:%S")


def _callback_key(route: ReplyRoute) -> str:
    return hashlib.sha1(route.label.encode("utf-8")).hexdigest()[:10]


def _is_expired_completed_record(record: dict[str, Any], now: datetime, retention: timedelta) -> bool:
    state = str(record.get("state", ""))
    if state not in {"done", "stopped"}:
        return False
    updated = _parse_iso(str(record.get("updated_at", "")))
    if updated is None:
        return False
    return now - updated > retention


def _index_detail(record: dict[str, Any]) -> str:
    state = str(record.get("state", ""))
    step = str(record.get("step", "")).strip()
    summary = str(record.get("summary", "")).strip()
    if state in {"error", "waiting"} and summary:
        return tail_lines(summary, 1).strip() or step or state
    return step or state or "-"


def _topic_bump_priority(reason: str) -> int:
    return {
        "activity": 0,
        "input": 1,
        "running": 1,
        "done": 2,
        "stopped": 2,
        "stuck": 3,
        "waiting": 3,
        "error": 4,
    }.get(reason, 0)


def _topic_bump_min_interval_seconds(reason: str, periodic_minutes: int) -> int:
    if reason == "activity":
        return max(60, periodic_minutes * 60)
    if reason in {"input", "running"}:
        return 30
    if reason in {"done", "stopped"}:
        return 20
    if reason in {"stuck", "waiting", "error"}:
        return 15
    return 30


def _topic_bump_text(record: dict[str, Any], *, reason: str, when_iso: str) -> str:
    label = str(record.get("label") or record.get("route_target") or "?")
    state = str(record.get("state", "unknown"))
    detail = _index_detail(record)
    timestamp = _hhmmss(when_iso)
    if reason == "input":
        return f"✍ {label} | input routed | {timestamp}"
    if reason == "activity":
        return f"↻ {label} | active | {timestamp}"
    if reason == "stuck":
        return f"⏱ {label} | stuck | {timestamp}"
    return f"{_state_emoji(state)} {label} | {state} | {detail} | {timestamp}"


def _archived_topic_title(stale: dict[str, Any]) -> str:
    label = str(stale.get("label") or stale.get("topic_title") or "?").strip()
    if label.startswith(("🟢 ", "🟡 ", "🔴 ", "✅ ", "⚫ ", "⚪ ", "🔵 ")):
        label = label[2:].strip()
    return f"⚫ archived | {label}"[:128]


def _topic_title(record: dict[str, Any]) -> str:
    emoji = _state_emoji(str(record.get("state", "unknown")))
    label = str(record.get("label") or record.get("route_target") or "?").strip()
    title = f"{emoji} {label}".strip()
    return title[:128]
