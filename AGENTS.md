# Ortak Agentic OS

This repository is a minimal, model-agnostic runtime for goal-driven software agents.

## Working rules

- Keep the core deterministic and model-independent.
- Prefer a small number of durable files over generated coordination documents.
- Store transient runs, events, leases, and status in `.agentic/state.sqlite`.
- Never modify or commit pre-existing user changes without explicit approval.
- One worktree may have only one active writer.
- Do not start an autonomous loop unless its goal is explicitly approved and armed.
- Do not commit directly to `main` from an agent task.
- Treat commits, versioned contracts, and structured events as coordination boundaries.
- Run the relevant tests before reporting completion.
- Keep provider-specific behavior behind adapters; goals and policies must not depend on one model vendor.

## Project commands

- Install/sync: `uv sync`
- Health check: `uv run agentic doctor`
- Create an isolated branch: `uv run agentic worktree create NAME --branch task/NAME --model default`
- Open controlled interaction: `uv run agentic chat NAME`
- Inspect shared context: `uv run agentic context NAME`
- Validate/approve/arm/run: validate a goal, then use the explicit goal lifecycle commands
- Verify new heads once: `uv run agentic watch --once`
- Inspect runtime state: `uv run agentic status` or `uv run agentic events`
- Tests: `uv run python -m unittest discover -s tests -v`
