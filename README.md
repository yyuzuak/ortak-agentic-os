# Ortak Agentic OS

A minimal template for controlled, goal-driven, multi-model software development.

The user chooses a worktree and model, collaborates interactively with the agent, approves a versioned goal, and only then starts an autonomous loop. Runtime coordination is kept in one ignored SQLite database instead of a directory full of status and message files.

## V0 principles

- Human-controlled transition from interactive work to autonomy
- Model-independent goals and runtime protocols
- Commit and event based coordination
- One active writer per worktree
- Manual merge to `main`
- Event-driven verification
- Small tracked surface: instructions, one config, and goals

## Quick start

```bash
uv sync
uv run agentic doctor
uv run agentic goal validate goals/demo.yaml
uv run agentic demo
uv run agentic status
```

Run the tests:

```bash
uv run python -m unittest discover -s tests -v
```

## Current V0 skeleton

The initial executable skeleton provides:

- YAML configuration and goal loading
- Goal validation, dependency validation, and cycle detection
- SQLite-backed runs and append-only events
- A deterministic mock execution loop
- Health and status commands

The mock loop intentionally does not call a model or modify application source. Provider adapters, real worktree execution, leases, and integration gates are the next implementation slices.

## Repository surface

```text
AGENTS.md          shared agent rules
CLAUDE.md          Claude adapter importing AGENTS.md
agentic.yaml       project configuration
goals/             versioned goals
.agentic/          ignored SQLite runtime and worktrees
src/agentic_os/    CLI/runtime package
tests/             deterministic core tests
```

