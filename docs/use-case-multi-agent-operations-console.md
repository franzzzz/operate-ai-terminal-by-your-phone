# Use Case: Multi-Agent Operations Console

## Who This Is For

You routinely run several Codex, `tmux`, or SDK sessions at the same time and need a compact mobile operations view.

## Problem

Multiple concurrent agents create four kinds of chaos:

- session identity becomes unclear
- important errors disappear into ordinary output
- mobile screens are too small for full terminal streams
- you do not know which session deserves attention first

## Why This Project Helps

The project’s recommended Telegram workspace solves this with hierarchy:

- `INDEX` for overview
- `ALERTS` for urgency
- one topic per session for detail and direct intervention

## Recommended Layout

- `INDEX`
- `ALERTS`
- `BUILD-2 | parser-upgrade`
- `RESEARCH-1 | retrieval-notes`
- `FIX-3 | mobile-layout`

## Example Flow

1. Let several sessions run in parallel.
2. Use `INDEX` to see which ones are healthy, waiting, or broken.
3. Enter the right topic only when needed.
4. Use `Continue`, `Refresh`, `Recent`, `Focus`, or `Stop` depending on session type.

## Why It Works Better Than Separate Chats

- one workspace for the whole operation
- lower message noise
- better prioritization on a phone
- stronger mapping between session and action
