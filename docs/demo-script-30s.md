# 30-Second Demo Script

## Goal

Show the core promise in under 30 seconds:

> start with a running local Codex session, leave the desk, continue it from Telegram, and keep the same session alive.

## Fastest Recording Setup

If you want a deterministic recording instead of waiting for a real Codex task to pause at the right moment, run this script inside `Terminal.app`:

```bash
./scripts/demo_phone_takeover.sh
```

That gives you a local session that:

- appears active
- pauses for operator input
- resumes when Telegram sends the next instruction
- clearly proves that the same terminal session kept going

## Demo Outline

### Scene 1: The local session is already running

On desktop:

- show either:
  - a real Codex session already active in `Terminal.app`, or
  - `./scripts/demo_phone_takeover.sh` running in `Terminal.app`

### Scene 2: Open Telegram

On Telegram:

- open the project supergroup
- show `INDEX`
- show the dedicated topic for that running session

### Scene 3: The session needs intervention

- show `🟡 waiting` or a session that clearly needs a follow-up prompt
- open the session topic

### Scene 4: Continue from the phone

- tap `Continue`
- or reply with one short instruction

### Scene 5: Prove that the original session kept going

Back on desktop:

- show the same terminal session receiving the prompt
- show it continuing instead of restarting from scratch

## Recording Notes

- keep the whole clip under 30 seconds
- do not show setup steps
- do not show .env or tokens
- do not show raw logs longer than needed
- make the “same session continues” moment obvious
