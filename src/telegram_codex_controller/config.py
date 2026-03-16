from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Set

from dotenv import load_dotenv


load_dotenv()


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_int(value: str | None, default: int) -> int:
    if value is None or not value.strip():
        return default
    return int(value)


def _parse_user_ids(value: str | None) -> Set[int]:
    if not value:
        return set()
    result: Set[int] = set()
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        result.add(int(item))
    return result


def _parse_optional_int(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    return int(value)


def _resolve_path(project_root: Path, raw_path: str) -> Path:
    candidate = Path(raw_path).expanduser()
    if candidate.is_absolute():
        return candidate
    return (project_root / candidate).resolve()


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    authorized_user_ids: Set[int]
    tmux_bin: str
    log_lines_default: int
    poll_interval_seconds: int
    allow_shell: bool
    shell_timeout_seconds: int
    codex_command_template: str
    max_message_chars: int
    session_name_prefix: str
    mirror_enabled: bool
    mirror_chat_ids: Set[int]
    mirror_poll_interval_seconds: int
    mirror_initial_lines: int
    node_bin: str
    assistant_state_path: Path
    assistant_sidecar_script: Path
    assistant_sidecar_workdir: Path
    console_chat_id: int | None
    console_forum_enabled: bool
    console_auto_create_topics: bool
    console_index_topic_id: int | None
    console_alerts_topic_id: int | None
    console_status_summary_lines: int
    console_status_update_min_interval_seconds: int
    console_running_update_min_interval_seconds: int
    console_global_write_spacing_seconds: int
    console_stuck_minutes: int
    console_completed_retention_minutes: int
    console_topic_bump_enabled: bool
    console_topic_bump_minutes: int
    console_send_log_documents: bool
    console_pin_status_messages: bool

    @classmethod
    def load(cls) -> "Settings":
        project_root = Path(__file__).resolve().parents[2]
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        user_ids = _parse_user_ids(os.getenv("AUTHORIZED_USER_IDS"))
        mirror_chat_ids = _parse_user_ids(os.getenv("MIRROR_CHAT_IDS")) or user_ids
        assistant_state_path = _resolve_path(
            project_root,
            os.getenv(
                "ASSISTANT_STATE_PATH",
                str(project_root / ".state" / "assistant_sessions.json"),
            ),
        )
        assistant_sidecar_script = _resolve_path(
            project_root,
            os.getenv(
                "ASSISTANT_SIDECAR_SCRIPT",
                str(project_root / "sidecar" / "runner.mjs"),
            ),
        )
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required")
        if not user_ids:
            raise ValueError("AUTHORIZED_USER_IDS must contain at least one Telegram user ID")

        return cls(
            telegram_bot_token=token,
            authorized_user_ids=user_ids,
            tmux_bin=os.getenv("TMUX_BIN", "tmux").strip() or "tmux",
            log_lines_default=_parse_int(os.getenv("LOG_LINES_DEFAULT"), 80),
            poll_interval_seconds=_parse_int(os.getenv("POLL_INTERVAL_SECONDS"), 3),
            allow_shell=_parse_bool(os.getenv("ALLOW_SHELL"), False),
            shell_timeout_seconds=_parse_int(os.getenv("SHELL_TIMEOUT_SECONDS"), 20),
            codex_command_template=os.getenv("CODEX_COMMAND_TEMPLATE", 'codex exec "{prompt}"'),
            max_message_chars=_parse_int(os.getenv("MAX_MESSAGE_CHARS"), 3500),
            session_name_prefix=os.getenv("SESSION_NAME_PREFIX", "tgc_").strip() or "tgc_",
            mirror_enabled=_parse_bool(os.getenv("MIRROR_ENABLED"), True),
            mirror_chat_ids=mirror_chat_ids,
            mirror_poll_interval_seconds=_parse_int(os.getenv("MIRROR_POLL_INTERVAL_SECONDS"), 3),
            mirror_initial_lines=_parse_int(os.getenv("MIRROR_INITIAL_LINES"), 24),
            node_bin=os.getenv("NODE_BIN", "node").strip() or "node",
            assistant_state_path=assistant_state_path,
            assistant_sidecar_script=assistant_sidecar_script,
            assistant_sidecar_workdir=assistant_sidecar_script.parent,
            console_chat_id=_parse_optional_int(os.getenv("CONSOLE_CHAT_ID")),
            console_forum_enabled=_parse_bool(os.getenv("CONSOLE_FORUM_ENABLED"), False),
            console_auto_create_topics=_parse_bool(os.getenv("CONSOLE_AUTO_CREATE_TOPICS"), True),
            console_index_topic_id=_parse_optional_int(os.getenv("CONSOLE_INDEX_TOPIC_ID")),
            console_alerts_topic_id=_parse_optional_int(os.getenv("CONSOLE_ALERTS_TOPIC_ID")),
            console_status_summary_lines=_parse_int(os.getenv("CONSOLE_STATUS_SUMMARY_LINES"), 15),
            console_status_update_min_interval_seconds=_parse_int(
                os.getenv("CONSOLE_STATUS_UPDATE_MIN_INTERVAL_SECONDS"),
                5,
            ),
            console_running_update_min_interval_seconds=_parse_int(
                os.getenv("CONSOLE_RUNNING_UPDATE_MIN_INTERVAL_SECONDS"),
                12,
            ),
            console_global_write_spacing_seconds=_parse_int(
                os.getenv("CONSOLE_GLOBAL_WRITE_SPACING_SECONDS"),
                2,
            ),
            console_stuck_minutes=_parse_int(os.getenv("CONSOLE_STUCK_MINUTES"), 15),
            console_completed_retention_minutes=_parse_int(
                os.getenv("CONSOLE_COMPLETED_RETENTION_MINUTES"),
                60,
            ),
            console_topic_bump_enabled=_parse_bool(os.getenv("CONSOLE_TOPIC_BUMP_ENABLED"), True),
            console_topic_bump_minutes=_parse_int(os.getenv("CONSOLE_TOPIC_BUMP_MINUTES"), 10),
            console_send_log_documents=_parse_bool(os.getenv("CONSOLE_SEND_LOG_DOCUMENTS"), True),
            console_pin_status_messages=_parse_bool(os.getenv("CONSOLE_PIN_STATUS_MESSAGES"), False),
        )
