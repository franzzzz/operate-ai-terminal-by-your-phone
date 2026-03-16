from __future__ import annotations

import re
import shlex
from typing import Iterable, List


_NOISE_PATTERNS = [
    re.compile(r"^gpt-[\w\.-]+\s+.*·.*left\s+·"),
    re.compile(r"^\d+\s+background terminals?\s+running"),
    re.compile(r"^Working\s+\("),
    re.compile(r"^Thread renamed to "),
    re.compile(r"^Completed execution of SCM Script"),
    re.compile(r"^SSH agent already running"),
    re.compile(r"^Last login:"),
    re.compile(r"^Tip:\s"),
    re.compile(r"^\[Restored"),
    re.compile(r"^https?://"),
]


def chunk_text(text: str, max_chars: int) -> List[str]:
    if len(text) <= max_chars:
        return [text]

    chunks: List[str] = []
    current: List[str] = []
    current_len = 0

    for line in text.splitlines(keepends=True):
        if len(line) > max_chars:
            if current:
                chunks.append("".join(current))
                current = []
                current_len = 0

            start = 0
            while start < len(line):
                chunks.append(line[start:start + max_chars])
                start += max_chars
            continue

        if current_len + len(line) > max_chars and current:
            chunks.append("".join(current))
            current = [line]
            current_len = len(line)
            continue

        current.append(line)
        current_len += len(line)

    if current:
        chunks.append("".join(current))

    return chunks


def coalesce_args(args: Iterable[str]) -> str:
    return " ".join(arg for arg in args).strip()


def parse_shell_like_args(text: str) -> List[str]:
    return shlex.split(text)


def tail_lines(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    lines = text.splitlines()
    if len(lines) <= limit:
        return "\n".join(lines)
    return "\n".join(lines[-limit:])


def compact_summary_lines(
    text: str,
    *,
    max_lines: int = 5,
    max_line_length: int = 120,
) -> List[str]:
    if max_lines <= 0:
        return []

    compacted: List[str] = []
    for raw_line in text.splitlines():
        line = _normalize_summary_line(raw_line)
        if not line:
            continue
        if compacted and compacted[-1] == line:
            continue
        compacted.append(_shorten_line(line, max_line_length))

    if not compacted:
        fallback = _normalize_summary_line(tail_lines(text, 1))
        return [_shorten_line(fallback or "<no output yet>", max_line_length)]
    return compacted[-max_lines:]


def compact_summary_text(
    text: str,
    *,
    max_lines: int = 5,
    max_line_length: int = 120,
) -> str:
    return "\n".join(
        compact_summary_lines(
            text,
            max_lines=max_lines,
            max_line_length=max_line_length,
        )
    )


def _normalize_summary_line(raw_line: str) -> str:
    line = raw_line.strip()
    if not line:
        return ""

    if "❯ " in line:
        line = line.split("❯ ", 1)[1].strip()
    elif line == "❯":
        return ""

    if line.startswith("› "):
        return line

    if _looks_like_shell_prompt(line):
        return ""
    if _looks_like_noise(line):
        return ""
    if line.startswith(("╭", "╰", "│", "─")):
        return ""

    return line


def _looks_like_noise(line: str) -> bool:
    lowered = line.lower()
    if "command not found: docker" in lowered:
        return True
    if "__pycache__" in lowered:
        return True
    return any(pattern.search(line) for pattern in _NOISE_PATTERNS)


def _looks_like_shell_prompt(line: str) -> bool:
    return (
        " via " in line
        and (" on " in line or line.startswith("~/") or line.startswith("/Users/"))
        and "❯" not in line
    )


def _shorten_line(line: str, max_line_length: int) -> str:
    if max_line_length <= 0 or len(line) <= max_line_length:
        return line
    return line[: max_line_length - 1] + "…"
