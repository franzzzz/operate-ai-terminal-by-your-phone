# Security Policy

## Scope

`Pocket Operator` can control local sessions on your machine. That makes it operationally powerful and security-sensitive.

This document describes the trust model, data boundaries, recommended safeguards, and how to report vulnerabilities.

## Threat Model

This project is intended for:

- personal use on a developer machine you control
- internal use in a trusted team environment
- situations where the Telegram bot, Telegram users, and the local machine are all under your control

This project is **not** designed to expose a machine to untrusted operators.

## What the Bot Can Do

Depending on configuration, the bot can:

- read and summarize terminal or session output
- send input back into running sessions
- start new `tmux` tasks
- continue SDK-backed Codex or Claude sessions
- optionally run shell commands if `/shell` is enabled

Treat the bot as a remote control surface for your machine.

## Data Stored Locally

By default, the project stores operational state locally:

- `.env`: bot token and local configuration
- `.state/`: session metadata, aliases, and console state
- `logs/`: controller stdout/stderr logs

These files are intentionally ignored by Git through `.gitignore`.

## Data Sent to Third Parties

This project does **not** provide its own hosted backend.

However, some data necessarily goes to third-party systems you already chose to use:

- Telegram Bot API receives messages, replies, status-card updates, alerts, and log documents you send through the bot
- Codex or Claude SDK providers receive prompts and tool interactions for SDK-backed sessions

You should assume that anything you intentionally send into a Telegram conversation or an SDK-backed session may leave your local machine.

## Recommended Safeguards

1. Restrict `AUTHORIZED_USER_IDS` to trusted operators only.
2. Keep `/shell` disabled unless you have a strong reason to enable it.
3. Use a dedicated Telegram bot for this project, not a shared bot.
4. Prefer a private supergroup or a private direct chat.
5. Limit who can access the Telegram supergroup if you use forum topics.
6. Rotate your Telegram bot token immediately if it is ever exposed.
7. Run the controller under a normal user account, not a privileged system account.
8. Keep secrets out of interactive prompts whenever possible.
9. Review what appears in logs and terminal history if you handle sensitive material.
10. If you mirror existing `Terminal.app` sessions, remember that their visible history may be summarized into Telegram.

## Telegram-Specific Notes

- If you disable Telegram group privacy mode, plain text sent inside session topics may be routed to the bot. Only do this in trusted groups.
- If privacy mode stays enabled, reply-to-card and explicit `/send` still work, but arbitrary topic chatter is less likely to reach the bot.
- Forum topics improve clarity, but they also create more places where machine state may be visible. Keep the group private.

## SDK and Shell Risk

SDK-backed Codex or Claude sessions may invoke tools against your workspace.

If `/shell` is enabled, the bot becomes much more powerful. In that mode, a compromised Telegram account or leaked bot token can have much more impact.

If you do not need `/shell`, keep it off.

## Reporting a Vulnerability

If you discover a security issue:

1. Do **not** open a public issue with exploit details.
2. Contact the maintainer privately first.
3. Include:
   - affected version or commit
   - reproduction steps
   - impact assessment
   - whether the issue exposes secrets, session data, or arbitrary command execution

If a private disclosure channel is later added, update this document to point to it explicitly.
