from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import re
from typing import Deque, Dict, Tuple

from telegram.ext import Application

from .utils import chunk_text


MAX_ROUTE_HISTORY = 4000

MIRROR_ROUTE_RE = re.compile(r"^\[(?:mirror|mirror-start|mirror-reset|mirror-snapshot) ([^\]]+)\]")
TMUX_ROUTE_RE = re.compile(r"^\[tail:([^\]]+)\]")
AGENT_ROUTE_RE = re.compile(r"^\[(?:agent|agent-tool|agent-system):([^\]]+)\]")
ROUTED_ROUTE_RE = re.compile(r"^\[routed (mirror|tmux|agent):([^\]]+)\]")


@dataclass(frozen=True)
class ReplyRoute:
    kind: str
    target: str

    @property
    def label(self) -> str:
        return f"{self.kind}:{self.target}"


def remember_reply_route(
    application: Application,
    chat_id: int,
    message_id: int,
    route: ReplyRoute,
) -> None:
    routes: Dict[Tuple[int, int], ReplyRoute] = application.bot_data.setdefault("reply_routes", {})
    order: Deque[Tuple[int, int]] = application.bot_data.setdefault("reply_route_order", deque())

    key = (chat_id, message_id)
    routes[key] = route
    order.append(key)

    while len(order) > MAX_ROUTE_HISTORY:
        expired = order.popleft()
        routes.pop(expired, None)


def lookup_reply_route(
    application: Application,
    chat_id: int,
    message_id: int,
    fallback_text: str | None = None,
) -> ReplyRoute | None:
    routes: Dict[Tuple[int, int], ReplyRoute] = application.bot_data.setdefault("reply_routes", {})
    route = routes.get((chat_id, message_id))
    if route is not None:
        return route
    return parse_reply_route(fallback_text or "")


def parse_reply_route(text: str) -> ReplyRoute | None:
    stripped = text.strip()
    if not stripped:
        return None

    routed_match = ROUTED_ROUTE_RE.match(stripped)
    if routed_match:
        return ReplyRoute(kind=routed_match.group(1), target=routed_match.group(2))

    mirror_match = MIRROR_ROUTE_RE.match(stripped)
    if mirror_match:
        return ReplyRoute(kind="mirror", target=mirror_match.group(1))

    tmux_match = TMUX_ROUTE_RE.match(stripped)
    if tmux_match:
        return ReplyRoute(kind="tmux", target=tmux_match.group(1))

    agent_match = AGENT_ROUTE_RE.match(stripped)
    if agent_match:
        return ReplyRoute(kind="agent", target=agent_match.group(1))

    return None


async def send_chunked_message(
    application: Application,
    chat_id: int,
    text: str,
    *,
    message_thread_id: int | None = None,
    route: ReplyRoute | None = None,
) -> None:
    settings = application.bot_data["settings"]
    for chunk in chunk_text(text, settings.max_message_chars):
        sent = await application.bot.send_message(
            chat_id=chat_id,
            message_thread_id=message_thread_id,
            text=chunk,
        )
        if route is not None:
            remember_reply_route(application, chat_id, sent.message_id, route)
