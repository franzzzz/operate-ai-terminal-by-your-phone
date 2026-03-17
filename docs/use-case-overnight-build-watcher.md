# Use Case: Overnight Build Watcher

## Who This Is For

You kick off a long-running build, migration, test suite, or indexing job before stepping away from your desk.

## Problem

Without a mobile console, you usually have two bad options:

- ignore the job until you get back to the machine
- keep a noisy terminal log open and hope you notice the important part later

## Why This Project Helps

With `Pocket Operator`, you can:

- launch the job in `tmux`
- keep one topic dedicated to that build
- watch `INDEX` for overall progress
- watch `ALERTS` for waiting, error, done, or stuck signals
- send a follow-up command from Telegram if the build needs a human step

## Example Flow

1. Start a build session from Telegram:

```bash
/run nightly-build make build
```

2. Open or create its topic:

```bash
/open nightly-build
```

3. Leave your desk.
4. If the job blocks or fails, check `ALERTS`.
5. If the job needs intervention, reply directly inside the session topic.

## Why It Works Better Than Raw Logs

- one session card stays up to date instead of flooding chat
- important state changes surface separately from ordinary progress
- you stay attached to the original job instead of starting over
