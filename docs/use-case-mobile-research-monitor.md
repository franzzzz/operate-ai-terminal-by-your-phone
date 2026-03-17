# Use Case: Mobile Research Monitor

## Who This Is For

You run a long Codex or Claude research turn locally, but you still want to supervise it while you are away from the machine.

## Problem

Research sessions are usually long, iterative, and interruption-prone:

- they may ask for clarification
- they may get stuck on authentication or data access
- they may drift and need a steering prompt

## Why This Project Helps

This project lets you treat Telegram as the mobile control plane for a research session:

- mirror the session into its own topic
- inspect recent output without wading through full logs
- continue the work from your phone
- keep the exact local context alive

## Example Flow

1. Start or attach a session.
2. Open its topic in Telegram.
3. Watch for `🟡 waiting` or `🔴 error`.
4. Use `Continue` or reply manually when the session needs direction.

## Example Operator Prompt

Use the fixed `Continue` action or send a targeted reply such as:

```text
Focus only on the unresolved blockers. Keep going until the plan is fully completed.
```

## Why It Works Better Than Starting a New Chat

- the same local workspace stays active
- the same terminal or SDK context stays alive
- you intervene in place instead of recreating context from scratch
