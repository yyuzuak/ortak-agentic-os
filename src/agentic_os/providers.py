from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from agentic_os.gitops import run_command


class RecoverableProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderResult:
    summary: str
    cost_usd: float = 0


class Provider(Protocol):
    def execute(
        self,
        *,
        goal: dict[str, Any],
        task: dict[str, Any],
        worktree: Path,
        attempt: int,
    ) -> ProviderResult: ...


class MockProvider:
    """Deterministic provider used for demos, tests, and installation checks."""

    def __init__(self, cost_per_task: float = 0):
        self.cost_per_task = float(cost_per_task)

    def execute(
        self,
        *,
        goal: dict[str, Any],
        task: dict[str, Any],
        worktree: Path,
        attempt: int,
    ) -> ProviderResult:
        failures = int(task.get("mock_failures", 0))
        delay = float(task.get("mock_delay_seconds", 0))
        if delay > 0:
            time.sleep(delay)
        if attempt <= failures:
            raise RecoverableProviderError(
                f"deterministic mock failure {attempt}/{failures}"
            )

        relative = Path(task.get("output", f"agentic-demo/{task['id']}.txt"))
        target = (worktree / relative).resolve()
        if worktree.resolve() not in target.parents:
            raise RuntimeError(f"Provider output escapes worktree: {relative}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "goal": goal["id"],
                    "task": task["id"],
                    "objective": task["objective"],
                    "attempt": attempt,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return ProviderResult(
            summary=f"created {relative.as_posix()}", cost_usd=self.cost_per_task
        )


class CommandProvider:
    """Generic adapter for Codex, Claude, GLM, DeepSeek, or another local CLI."""

    def __init__(self, command: list[str], timeout_seconds: int = 1800, fixed_cost: float = 0):
        if not command or not all(isinstance(item, str) for item in command):
            raise ValueError("Command provider requires a non-empty command array")
        self.command = command
        self.timeout_seconds = int(timeout_seconds)
        self.fixed_cost = float(fixed_cost)

    def execute(
        self,
        *,
        goal: dict[str, Any],
        task: dict[str, Any],
        worktree: Path,
        attempt: int,
    ) -> ProviderResult:
        prompt = json.dumps(
            {
                "goal": goal,
                "task": task,
                "attempt": attempt,
                "instruction": "Modify only this worktree. Do not commit or merge.",
            },
            sort_keys=True,
        )
        result = run_command(
            self.command,
            cwd=worktree,
            input_text=prompt,
            timeout=self.timeout_seconds,
        )
        summary = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "completed"
        return ProviderResult(summary=summary, cost_usd=self.fixed_cost)


def build_provider(config: dict[str, Any], model_name: str) -> tuple[str, Provider]:
    models = config.get("models", {})
    profile = models.get(model_name)
    if profile is None and model_name == "mock":
        profile = {"provider": "mock", "model": "deterministic-worker"}
    if not isinstance(profile, dict):
        raise ValueError(f"Unknown model profile: {model_name}")

    provider_name = profile.get("provider")
    provider_config = config.get("providers", {}).get(provider_name, {})
    if provider_name == "mock":
        cost = profile.get("cost_per_task", provider_config.get("cost_per_task", 0))
        return "mock", MockProvider(cost_per_task=cost)
    if provider_name == "command":
        command = profile.get("command", provider_config.get("command"))
        return "command", CommandProvider(
            command=command,
            timeout_seconds=profile.get(
                "timeout_seconds", provider_config.get("timeout_seconds", 1800)
            ),
            fixed_cost=profile.get("fixed_cost", provider_config.get("fixed_cost", 0)),
        )
    raise ValueError(f"Unsupported provider type: {provider_name}")
