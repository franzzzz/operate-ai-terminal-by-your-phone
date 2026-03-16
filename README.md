# telegram-codex-controller

[![CI](https://github.com/franzzzz/operate-ai-terminal-by-your-phone/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/franzzzz/operate-ai-terminal-by-your-phone/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/franzzzz/operate-ai-terminal-by-your-phone)](https://github.com/franzzzz/operate-ai-terminal-by-your-phone/releases)
[![License](https://img.shields.io/github/license/franzzzz/operate-ai-terminal-by-your-phone)](LICENSE)
[![Last Commit](https://img.shields.io/github/last-commit/franzzzz/operate-ai-terminal-by-your-phone)](https://github.com/franzzzz/operate-ai-terminal-by-your-phone/commits/main)

Turn Telegram into a mobile control console for Codex, Claude, and other terminal-based agents running on your computer.

This is not a simple stdout forwarder. It packages multi-session status, alerts, human handoff, log export, and reply routing into a workflow that is practical to use from your phone:

- monitor multiple Codex or agent sessions in parallel
- start new Codex, `tmux`, or SDK-backed sessions from Telegram
- take over existing Codex windows already running in macOS `Terminal.app`
- keep one editable status card per session instead of flooding chat
- organize work with `INDEX + ALERTS + one topic per session`
- send input back to the correct session by replying to a card or typing inside its topic
- ship full logs as `.log` documents instead of pasting large text blocks
- restrict control to approved Telegram users only

## Product Overview

`telegram-codex-controller` is a remote-control layer for local agent workflows. It is designed for people who already run Codex, Claude, research scripts, or long-lived build tasks on their own machine and want a clean mobile control plane instead of raw logs.

It supports three complementary operating modes:

1. **Take over existing windows**
   Mirror Codex sessions that are already running in macOS `Terminal.app`, then send follow-up input from Telegram.
2. **Start new long-running jobs**
   Launch named `tmux` sessions from Telegram so tasks stay alive independently of your current shell.
3. **Drive new SDK-backed conversations**
   Start and continue Codex or Claude sessions through the sidecar runner while keeping them inside the same Telegram console.

The product goal is not to replace SSH. The goal is to make these common remote actions fast and structured:

- see which session is active, waiting, stuck, done, or broken
- intervene quickly when a task needs human input, login, approval, or clarification
- manage several sessions without losing context on a small phone screen
- keep existing desktop work intact instead of migrating everything into a new tool

## Why This Tool Is Useful

If you already run agent workflows locally, the hardest part is often not starting tasks. The hard part is everything that happens after launch:

- you leave your desk and lose visibility into progress
- multiple windows become hard to distinguish on a phone
- a task pauses for human input and you do not notice in time
- normal output and real problems are mixed together
- the only fallback is reading a stream of raw terminal text

This project turns that into a structured mobile console:

- **Clear status**: one session card per workflow, one supergroup for the whole console
- **Clear priority**: `INDEX` for overview, `ALERTS` for action, session topics for detail
- **Low noise**: cards are edited in place instead of creating endless new messages
- **Direct intervention**: waiting states, reply routing, and explicit `/send`
- **Preserved desktop context**: new tasks and already-running windows can live together

## Good Fit Scenarios

- you run multiple Codex, Claude, or research-agent sessions at the same time
- you have long jobs that may require login, approval, or a short human reply
- you already have valuable local terminal state and do not want to migrate all of it
- you want overnight or weekend monitoring from your phone
- you want to package agent operations as a usable product, not a pile of personal scripts

## Key Capabilities

### Multi-Session Agent Console

- track many sessions in parallel
- support three session sources: `tmux`, existing `Terminal.app` windows, and SDK sessions
- keep separate status, summary, log access, and reply routing per session

### Real-Time State Lights

- `🟢 running`: active and healthy
- `🟡 waiting`: waiting for human input, login, approval, or confirmation
- `🔴 error`: failed or detected as abnormal
- `✅ done`: completed
- `⚫ stopped`: intentionally stopped or no longer present
- `⚪ idle`: ready but inactive

### Mobile-First Information Architecture

- one editable `INDEX` card for the whole console
- one `ALERTS` stream or topic for waiting, error, done, and stuck states
- one topic per session for detailed inspection and direct interaction
- coalesced card updates to reduce Telegram noise
- full logs exported as documents when needed

### Remote Control

- `/codex` to start new Codex CLI work
- `/run` to launch long-running `tmux` tasks
- `/agent_*` to manage SDK-backed Codex or Claude sessions
- `/mirror` to attach existing `Terminal.app` Codex windows
- `/focus` to bring the corresponding Terminal tab to the foreground
- `/send`, reply-to-card, or topic message routing to push input back into the right session

### Clear Session Identity

- persistent aliases for `ttysNNN` sessions, for example `oracle-chat-link`
- topic titles that follow current state, such as `🟢 oracle-chat-link` or `🔴 billing-ui`
- `INDEX` ordering that highlights error and waiting states first
- compact summaries optimized for small screens

### Security and Control

- restrict access with `AUTHORIZED_USER_IDS`
- keep `/shell` disabled by default
- export logs without exposing a full remote shell workflow
- suitable for private developer machines or controlled internal environments

## Recommended Telegram Workspace

The best product shape is:

- one Telegram **supergroup**
- **Topics / Forum** enabled
- one `INDEX` topic
- one `ALERTS` topic
- one topic per session, agent, or tracked window

A Telegram supergroup is not a separate app. It is Telegram’s larger group type. When Topics are enabled, the group behaves like a multi-page control console.

Recommended topic layout:

- `INDEX`
- `ALERTS`
- `BUILD-2 | claim-validator`
- `RESEARCH-1 | ebm-rules`
- `FIX-3 | billing-ui`

Daily phone workflow:

1. Open `INDEX` for the global view.
2. Check `ALERTS` for anything that needs action now.
3. Enter a specific session topic for current summary, logs, and controls.
4. When a human reply is needed, either reply to the session card or send text directly in that topic.

If you want plain text sent directly inside a topic to be routed back to the session, disable the bot’s **group privacy mode** in `@BotFather`. If privacy mode stays enabled, reply-to-card and `/send` still work, but plain topic text may not be delivered to the bot.

## Architecture

```text
Phone (Telegram)
      │
      ▼
Telegram Bot
      │
      ▼
Command Router
      │
      ├── Telegram console manager
      │     ├── editable session cards
      │     ├── INDEX summary
      │     ├── ALERTS topic/feed
      │     └── optional forum topics
      ├── SDK assistant runner
      ├── Terminal mirror watcher
      ├── Codex CLI task launcher
      ├── Optional shell runner
      └── tmux session manager
              │
              ▼
         tmux sessions
```

## Commands and Interaction

The sections above describe the product. This section covers the operating surface you use in Telegram.

### Common commands

- `/start` — show help
- `/help` — show help
- `/ping` — health check
- `/forum_on` — enable forum/topic mode for the current console chat
- `/forum_off` — disable forum/topic mode for the current console chat
- `/forum_bootstrap` — create INDEX, ALERTS, and topics for current sessions
- `/index_here` — bind the current chat/topic as INDEX
- `/alerts_here` — bind the current chat/topic as ALERTS
- `/open <session>` — bind a tracked session to the current topic and refresh its status card
- `/focus <mirror-session>` — bring an existing Terminal Codex tab to the front
- `/topic_create <session> [topic name]` — create and bind a forum topic for a session
- `/send <session> <text>` — send text directly to a mirror/tmux/agent session
- `/find <session> <pattern>` — search recent logs/transcript/history
- `/recent_errors [limit]` — show recent error/waiting sessions
- `/sessions` — list tracked sessions
- `/run <name> <command...>` — start a named task in tmux
- `/codex <name> <prompt...>` — start a Codex task using the configured Codex command template
- `/logs <session>` — export full tmux logs, mirror history, or assistant transcripts, usually as a `.log` document
- `/tail <session> [lines]` — return a short on-demand summary instead of starting a stream
- `/stop <name>` — stop a session
- `/shell <command...>` — run a shell command when enabled
- `/agents` — list tracked SDK-backed Codex or Claude sessions
- `/agent_new <assistant> <name> [cwd]` — create a named SDK session
- `/agent <name> <prompt...>` — continue a named SDK session
- `/agent_log <name>` — show recent transcript for a named SDK session
- `/agent_cwd <name> <cwd>` — change SDK session working directory and clear resume state
- `/agent_stop <name>` — delete a tracked SDK session
- `/mirror [status|on|off|snapshot [tty-or-alias]|alias <tty-or-alias> <alias>|unalias <tty-or-alias>|aliases]` — manage Terminal mirroring
- `/mirrors` — alias for `/mirror`
- Reply to a status card or routed event, or just send a plain text message inside that session topic — automatically send text back to the mirrored Terminal tab, tmux session, or SDK assistant session

Status cards now include inline keyboard actions for the most common flows:
- `Refresh`
- `Tail`
- `Logs`
- `Find Error`
- `Focus` for mirrored Terminal sessions
- `Stop` for tmux and SDK assistant sessions

Examples:

```bash
/run research codex exec "Analyze ICD-10-GM validation edge cases"
/codex ontology Build an ontology plan for medical coding validation
/agent_new codex triage /Users/linfwang/Documents/research
/agent triage "Summarize the repo structure"
/agent_log triage
/forum_on
/forum_bootstrap
/index_here
/alerts_here
/open triage
/topic_create triage
/send oracle-chat-link Reply with the single word ok.
/focus oracle-chat-link
/mirror alias ttys003 ocna-vpn
/mirror snapshot ocna-vpn
/open ocna-vpn
/find ocna-vpn yubikey
/recent_errors
/mirror snapshot
/mirrors
/logs ontology
/tail ontology
/stop ontology
/sessions
```

Recommended mobile flow:

1. Watch the editable `INDEX` message for overall state.
2. Use the `INDEX` buttons to refresh the overview, surface recent errors, or open a specific session.
3. Watch `ALERTS` for errors, waiting-for-human-input, completion, and stuck sessions.
4. Use the inline buttons on a session card for refresh, tail, logs, focus, and stop.
5. Reply directly to that session card, or type inside that session topic, when you need to send text back into the session.

The bot also registers high-frequency commands through `setMyCommands`, so they appear directly in Telegram’s command picker.

Mirror sessions keep the real `ttysNNN` routing key internally, but you can attach a persistent alias like `ocna-vpn` so status cards, topics, and alerts are easier to recognize.

## Releases

This repository supports two release paths:

1. Push a version tag from your machine:

```bash
git tag v0.1.0
git push origin v0.1.0
```

2. Run the `Release` workflow manually from the GitHub Actions UI and provide:
   - a tag such as `v0.1.0`
   - an optional target ref such as `main` or a commit SHA
   - an optional custom title
   - optional release notes

If notes are left empty, GitHub release notes are generated automatically.

## Requirements

- Python 3.11+
- Node.js 20+
- `tmux`
- a Telegram bot token created through `@BotFather`
- your Telegram numeric user id for authorization
- Codex CLI installed locally if you want `/codex`
- macOS `Terminal.app` if you want to mirror existing local windows
- a Telegram supergroup with Topics enabled if you want the recommended one-topic-per-session layout

## Quick Deployment

These steps are written for first-time setup with the recommended product shape:

- the controller runs on your computer
- Telegram on your phone is the operator interface
- one supergroup hosts the full console
- `INDEX + ALERTS + one topic per session` is the default layout

### Step 0: Decide on the deployment shape

The recommended shape is not a one-on-one bot chat. It is:

1. one Telegram supergroup
2. Topics / Forum enabled
3. the bot added to that supergroup
4. `INDEX`, `ALERTS`, and session topics living inside the group

If you only want a fast local trial, you can start with a direct bot chat. For long-term use, multi-session visibility, and cleaner mobile navigation, supergroup + topics is the better shape.

### Step 1: Create the Telegram bot from your phone

1. Open `@BotFather` in Telegram
2. Send `/newbot`
3. Follow the prompts for bot name and username
4. Save the returned bot token

You will place this token in `.env` as `TELEGRAM_BOT_TOKEN`.

### Step 2: Decide whether to disable group privacy

If you want plain text typed directly inside a session topic to be routed back to that session, disable the bot’s group privacy mode:

1. Open `@BotFather`
2. Send `/setprivacy`
3. Select your bot
4. Choose `Disable`

Behavior difference:

- **Privacy disabled**: plain text sent inside a session topic can route back to the session
- **Privacy enabled**: `/send` and reply-to-card still work, but plain topic text may not reach the bot

### Step 3: Create the Telegram supergroup

1. Create a new Telegram group
2. Convert or configure it as a supergroup
3. Enable `Topics` / `Forum`
4. Add your bot to the group
5. Prefer granting the bot these admin capabilities:
   - `Manage Topics`
   - `Pin Messages`
   - `Delete Messages`

Without topic-management permissions, `/forum_bootstrap`, automatic topic creation, and message maintenance features may fail.

### Step 4: Get your Telegram user id

Open `@userinfobot` and record your numeric Telegram user id.

You will place it in `.env` as `AUTHORIZED_USER_IDS`.

### Step 5: Install dependencies on the computer

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
(cd sidecar && npm install)
```

Install `tmux`:

macOS:

```bash
brew install tmux
```

Ubuntu / Debian:

```bash
sudo apt-get update
sudo apt-get install -y tmux
```

### Step 6: Configure `.env`

```bash
cp .env.example .env
```

Minimum working configuration:

```env
TELEGRAM_BOT_TOKEN=your_bot_token_here
AUTHORIZED_USER_IDS=123456789
MIRROR_CHAT_IDS=123456789
```

Recommended product-style configuration:

```env
TELEGRAM_BOT_TOKEN=your_bot_token_here
AUTHORIZED_USER_IDS=123456789
MIRROR_CHAT_IDS=123456789

MIRROR_ENABLED=true
CODEX_COMMAND_TEMPLATE=codex exec "{prompt}"

CONSOLE_FORUM_ENABLED=true
CONSOLE_AUTO_CREATE_TOPICS=true
CONSOLE_SEND_LOG_DOCUMENTS=true
CONSOLE_TOPIC_BUMP_ENABLED=true
CONSOLE_STATUS_SUMMARY_LINES=5
CONSOLE_RUNNING_UPDATE_MIN_INTERVAL_SECONDS=12
CONSOLE_GLOBAL_WRITE_SPACING_SECONDS=2
```

Deployment notes:

- `CONSOLE_CHAT_ID` can be left empty for the first boot
  After startup, run `/forum_on` in the target supergroup and the controller will remember that chat as the main console
- `CONSOLE_INDEX_TOPIC_ID` and `CONSOLE_ALERTS_TOPIC_ID` can also be left empty
  They can be created automatically during bootstrap or bound later with `/index_here` and `/alerts_here`
- If you are the only operator, `MIRROR_CHAT_IDS` usually only needs your own user id

### Step 7: Start the service

Run in the foreground:

```bash
PYTHONPATH=src python -m telegram_codex_controller.main
```

Or use the helper script:

```bash
./scripts/run.sh
```

If dependencies are already installed, the lightweight long-running entrypoint is:

```bash
./scripts/run_service.sh
```

For macOS background execution, see `docs/launchd.plist.example`.

### Step 8: Complete first-time mobile binding

After the service is running, use this order:

1. Open a direct chat with the bot and send `/start`
   This confirms the bot is online and your `AUTHORIZED_USER_IDS` setting is correct
2. Enter your supergroup
3. Send `/forum_on`
4. Send `/forum_bootstrap`

After that, the recommended result is:

- one `INDEX` topic
- one `ALERTS` topic
- one topic per currently known session

If you want to bind things manually:

- enter the `INDEX` topic and run `/index_here`
- enter the `ALERTS` topic and run `/alerts_here`
- enter any session topic and run `/open <session>`

### Step 9: Run a real mobile smoke test

Recommended smoke test:

1. Make sure you already have a Codex window on the computer, or run:

```bash
/codex demo Explain this repo
```

2. In the supergroup, run:

```bash
/forum_bootstrap
```

3. Confirm that your phone shows:
   - `INDEX`
   - `ALERTS`
   - a `demo` topic, or a topic for the mirrored session

4. Enter that session topic and try both input paths:
   - reply to the status card
   - send a plain text message directly in the topic

5. If the target terminal receives the input, reply routing is working

If privacy mode is still enabled, the plain topic message in step 4 may not work. That is a Telegram limitation, not a controller bug.

### Step 10: Start with these commands

After deployment, these are usually the most useful commands:

- `/forum_on`
- `/forum_bootstrap`
- `/open <session>`
- `/mirror on`
- `/mirror alias <tty> <alias>`
- `/send <session> <text>`
- `/tail <session>`
- `/logs <session>`
- `/recent_errors`

For daily use, think of the Telegram workspace like this:

- `INDEX`: control overview
- `ALERTS`: action queue for waiting, errors, completions, and stuck work
- `session topic`: detailed status, logs, and direct reply path for one session

## Configuration

All configuration is done via environment variables.

| Variable | Required | Default | Description |
|---|---:|---:|---|
| `TELEGRAM_BOT_TOKEN` | yes | - | Telegram bot token |
| `AUTHORIZED_USER_IDS` | yes | - | Comma-separated Telegram user IDs allowed to use the bot |
| `MIRROR_CHAT_IDS` | no | `AUTHORIZED_USER_IDS` | Telegram chat IDs that receive automatic Terminal mirror events |
| `TMUX_BIN` | no | `tmux` | Path to tmux binary |
| `LOG_LINES_DEFAULT` | no | `80` | Default log lines returned by `/logs` |
| `POLL_INTERVAL_SECONDS` | no | `3` | Background poll interval for log streaming |
| `ALLOW_SHELL` | no | `false` | Enable `/shell` |
| `SHELL_TIMEOUT_SECONDS` | no | `20` | Timeout for `/shell` |
| `CODEX_COMMAND_TEMPLATE` | no | `codex exec "{prompt}"` | Command template used by `/codex` |
| `MAX_MESSAGE_CHARS` | no | `3500` | Chunk size for Telegram messages |
| `SESSION_NAME_PREFIX` | no | `tgc_` | Prefix added to tmux session names |
| `MIRROR_ENABLED` | no | `true` | Enable automatic mirroring of existing Terminal Codex tabs |
| `MIRROR_POLL_INTERVAL_SECONDS` | no | `3` | Poll interval for Terminal snapshot diffing |
| `MIRROR_INITIAL_LINES` | no | `24` | Initial line count sent when a new Terminal Codex tab is attached |
| `NODE_BIN` | no | `node` | Node executable used for the SDK sidecar |
| `ASSISTANT_STATE_PATH` | no | `./.state/assistant_sessions.json` | JSON file storing SDK session metadata and resume IDs |
| `ASSISTANT_SIDECAR_SCRIPT` | no | `./sidecar/runner.mjs` | Node entrypoint used for SDK-backed Codex or Claude sessions |
| `CONSOLE_CHAT_ID` | no | first chat in `MIRROR_CHAT_IDS` | Main chat or supergroup where the console lives |
| `CONSOLE_FORUM_ENABLED` | no | `false` | Enable one-topic-per-session delivery inside a forum-enabled supergroup |
| `CONSOLE_AUTO_CREATE_TOPICS` | no | `true` | Auto-create INDEX, ALERTS, and session topics when forum mode is enabled |
| `CONSOLE_INDEX_TOPIC_ID` | no | auto | Existing topic id to use for INDEX |
| `CONSOLE_ALERTS_TOPIC_ID` | no | auto | Existing topic id to use for ALERTS |
| `CONSOLE_STATUS_SUMMARY_LINES` | no | `5` | Number of important lines to keep in each status card summary |
| `CONSOLE_STATUS_UPDATE_MIN_INTERVAL_SECONDS` | no | `5` | Minimum interval between status-card edits for the same session |
| `CONSOLE_RUNNING_UPDATE_MIN_INTERVAL_SECONDS` | no | `12` | Minimum interval for sessions that are simply running normally |
| `CONSOLE_GLOBAL_WRITE_SPACING_SECONDS` | no | `2` | Global spacing between automatic Telegram card/index writes |
| `CONSOLE_STUCK_MINUTES` | no | `15` | How long a running session can go without updates before ALERTS marks it stuck |
| `CONSOLE_COMPLETED_RETENTION_MINUTES` | no | `60` | How long completed/stopped sessions stay visible in INDEX before dropping out |
| `CONSOLE_TOPIC_BUMP_ENABLED` | no | `true` | Periodically bump active session topics so they stay near the top of the forum list |
| `CONSOLE_TOPIC_BUMP_MINUTES` | no | `10` | Minimum interval between low-noise active-topic bumps |
| `CONSOLE_SEND_LOG_DOCUMENTS` | no | `true` | Send full logs/transcripts as `.log` documents instead of large text blobs |
| `CONSOLE_PIN_STATUS_MESSAGES` | no | `false` | Pin each session status card when created |

## Security notes

This bot can control your development machine. Use it carefully.

Recommended safeguards:

1. Only approve your own Telegram user ID.
2. Keep `/shell` disabled unless you truly need it.
3. Run the bot with a low-privilege user.
4. Avoid putting secrets directly into commands.
5. Prefer `/codex` or `/run` with known-safe commands.
6. Keep the machine protected behind your usual OS security controls.
7. Remember that `/agent_*` can invoke Codex or Claude with tool access in your local workspace.
8. If you move the console into a supergroup, keep forum topics restricted to trusted users only.

## How `/codex` works

The `/codex` command builds a shell command from `CODEX_COMMAND_TEMPLATE`.

Template example:

```env
CODEX_COMMAND_TEMPLATE=codex exec "{prompt}"
```

Then:

```bash
/codex research Investigate SOAP validation edge cases
```

becomes:

```bash
codex exec "Investigate SOAP validation edge cases"
```

If your local Codex workflow uses a different command, just change the template.

Examples:

```env
CODEX_COMMAND_TEMPLATE=codex exec --profile prod "{prompt}"
CODEX_COMMAND_TEMPLATE=codex run research_agent --input "{prompt}"
```

## Service Mode and Operations

For day-to-day use, the controller should run as a background service rather than inside a temporary interactive shell.

### macOS LaunchAgent behavior

On macOS, the recommended mode is a user `launchd` LaunchAgent.

That gives you:

- automatic start after login through `RunAtLoad`
- automatic restart through `KeepAlive`
- access to the logged-in GUI session, which is required for existing `Terminal.app` mirroring

To keep the Telegram console available after a machine restart, all of these conditions must hold:

- the Mac is powered on
- the macOS user session is logged in
- the machine has network access
- the machine is not asleep

Because existing `Terminal.app` mirroring depends on the GUI session, a LaunchAgent is usually the correct mode for this project. A LaunchDaemon can start earlier, but it does not have the same access to logged-in Terminal windows.

### Install or reload the LaunchAgent

Sample files:

- macOS: `docs/launchd.plist.example`
- Linux: `docs/systemd.service.example`

Typical macOS flow:

```bash
cp docs/launchd.plist.example ~/Library/LaunchAgents/com.linfwang.telegram-codex-controller.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.linfwang.telegram-codex-controller.plist
launchctl kickstart -k gui/$(id -u)/com.linfwang.telegram-codex-controller
```

### Service helper scripts

This repository now includes two operational helper scripts:

```bash
./scripts/service_status.sh
./scripts/service_restart.sh
```

They default to the LaunchAgent label `com.linfwang.telegram-codex-controller`.

If you use a different LaunchAgent label or plist path, override them with:

- `TGC_LAUNCHD_LABEL`
- `TGC_LAUNCHD_PLIST`
- `TGC_LOG_PATH`

Examples:

```bash
TGC_LAUNCHD_LABEL=com.example.telegram-codex-controller ./scripts/service_status.sh
TGC_LAUNCHD_PLIST=$HOME/Library/LaunchAgents/com.example.telegram-codex-controller.plist ./scripts/service_restart.sh
```

### Daily operations

Common commands:

```bash
./scripts/service_status.sh
./scripts/service_restart.sh
tail -f logs/stderr.log
```

### Optional shell alias

If you want short shell commands for everyday service operations, add aliases like these:

```bash
alias tgService="/path/to/repo/scripts/service_restart.sh"
alias tgStatus="/path/to/repo/scripts/service_status.sh"
alias tgLogs="tail -f /path/to/repo/logs/stderr.log"
```

### tmux and mirroring notes

- The bot process itself does not need to run inside `tmux`, though spawned tasks can.
- Existing terminal mirroring is macOS-specific because it reads `Terminal.app` tab contents through AppleScript.
- Sessions running in another terminal emulator are not mirrored automatically.

### Forum topics are optional but recommended

If `CONSOLE_FORUM_ENABLED=true` and `CONSOLE_CHAT_ID` points at a forum-enabled Telegram supergroup, the controller can keep:

- one editable INDEX card in an INDEX topic
- one ALERTS stream in an ALERTS topic
- one status card per session in its own topic
- topic titles that follow session state, e.g. `🔴 oracle-chat-link`

A practical setup flow is:

1. In the target supergroup, run `/forum_on`.
2. Run `/forum_bootstrap` once to create INDEX, ALERTS, and topics for currently active sessions.
3. Run `/index_here` inside the INDEX topic if you want to rebind it manually later.
4. Run `/alerts_here` inside the ALERTS topic if you want to rebind it manually later.
5. Run `/topic_create oracle-chat-link` or `/topic_create amasys-research` to create additional per-session topics.
6. Use `/open <session>` inside a topic if you want to rebind or refresh that session card there.
7. Inside a session topic, you can either reply to the status card or just send plain text in that topic to route input back to the bound session.

Without forum mode, the same architecture still works in a single chat, but session separation is weaker.

## Project structure

```text
telegram-codex-controller/
├── .env.example
├── README.md
├── requirements.txt
├── sidecar/
│   ├── package.json
│   └── runner.mjs
├── docs/
│   └── systemd.service.example
├── scripts/
│   ├── service_restart.sh
│   ├── service_status.sh
│   ├── run.sh
│   └── run_service.sh
└── src/
    └── telegram_codex_controller/
        ├── assistant_sessions.py
        ├── __init__.py
        ├── bot.py
        ├── console.py
        ├── config.py
        ├── main.py
        ├── security.py
        ├── session_manager.py
        ├── terminal_mirror.py
        └── utils.py
```

## Troubleshooting

### Bot starts but `/codex` fails

Check:

- Codex CLI is installed on the same machine
- The command works directly in your shell
- `CODEX_COMMAND_TEMPLATE` matches your real Codex CLI syntax

### `/agent_*` fails immediately

Check:

- `sidecar/node_modules` exists and `npm install` completed
- `node` is available to the launch environment
- Your local Codex or Claude authentication is already working outside Telegram

### Terminal mirroring shows nothing

Check:

- The Codex session is running inside macOS `Terminal.app`
- The bot process has permission to automate `Terminal.app`
- `MIRROR_ENABLED=true`
- The Telegram chat ID is present in `MIRROR_CHAT_IDS`

### `tmux not found`

Install tmux and ensure `TMUX_BIN` points to it if needed.

### No response to commands

Check:

- Your Telegram user ID is listed in `AUTHORIZED_USER_IDS`
- Your bot token is correct
- The bot process is running

### Long logs are cut off

Session cards keep only the most recent important lines. Use `/logs`, `/agent_log`, or `/find` when you need deeper detail. By default, full logs are sent as `.log` documents to avoid chat spam.

## License

MIT
