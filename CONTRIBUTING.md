# Contributing

Thanks for contributing to `telegram-codex-controller`.

## What This Project Optimizes For

This project is not trying to be a generic chat bot. It is trying to be a strong mobile control console for long-running local agent workflows.

Changes are most valuable when they improve one or more of these properties:

- faster first-time setup
- clearer mobile information hierarchy
- safer defaults
- better reliability under multi-session load
- fewer surprises when routing input back into a live session

## Development Workflow

1. Create or update a virtual environment.
2. Install Python dependencies from `requirements.txt`.
3. Install sidecar dependencies from `sidecar/package.json`.
4. Run the test suite before submitting changes.

Useful commands:

```bash
python3 -m compileall src tests
PYTHONPATH=src ./.venv/bin/python -m unittest discover -s tests -v
```

## Scope Guidance

Before adding a feature, ask:

- does this strengthen the phone-first control-console workflow?
- does this make setup easier or safer?
- does this reduce noise or confusion in Telegram?

Prefer focused changes over broad feature expansion.

## Pull Request Guidance

Please keep pull requests:

- small enough to review
- explicit about behavior changes
- backed by tests when the change affects routing, state, or rendering
- clear about platform assumptions, especially for macOS-specific mirroring behavior

## Areas Where Contributions Are Especially Useful

- cross-platform session support
- safer setup automation
- better onboarding and demo assets
- more robust Telegram topic lifecycle handling
- packaging and distribution improvements

## Code Style

- keep changes ASCII unless the file already requires Unicode
- prefer small, direct code paths over over-engineered abstractions
- keep user-facing language concise and operational
- add tests when behavior changes
