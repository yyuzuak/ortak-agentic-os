from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentic_os.gitops import GitOperations, run_command
from agentic_os.state import StateStore


@dataclass(frozen=True)
class VerificationResult:
    worktree: str
    head_sha: str
    status: str
    details: str = ""


class VerificationWatcher:
    """Event-friendly, model-free verification of newly published worktree heads."""

    def __init__(
        self,
        *,
        config: dict[str, Any],
        state: StateStore,
        git: GitOperations,
    ):
        self.config = config
        self.state = state
        self.git = git

    def run_once(self) -> list[VerificationResult]:
        results: list[VerificationResult] = []
        commands = self.config.get("verification", {}).get("commands", [])
        for worktree in self.state.list_worktrees():
            if worktree["status"] == "RUNNING" or self.state.get_lease(worktree["name"]):
                continue
            path = Path(worktree["path"])
            if not path.exists():
                continue
            head = self.git.head(path)
            if self.state.get_verification(worktree["name"], head):
                continue
            try:
                for command in commands:
                    if not isinstance(command, list) or not command:
                        raise ValueError("verification.commands entries must be command arrays")
                    run_command(command, cwd=path, timeout=600)
                result = VerificationResult(worktree["name"], head, "PASSED")
            except Exception as exc:
                result = VerificationResult(worktree["name"], head, "FAILED", str(exc))
            self.state.record_verification(
                result.worktree, result.head_sha, result.status, result.details
            )
            self.state.append_event(
                f"background.verification.{result.status.lower()}",
                payload={
                    "worktree": result.worktree,
                    "head_sha": result.head_sha,
                    "details": result.details,
                },
            )
            results.append(result)
        return results

    def run_for(self, duration_seconds: float, interval_seconds: float) -> list[VerificationResult]:
        if duration_seconds <= 0 or interval_seconds <= 0:
            raise ValueError("duration and interval must be positive")
        deadline = time.monotonic() + duration_seconds
        results: list[VerificationResult] = []
        while True:
            results.extend(self.run_once())
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(interval_seconds, remaining))
        return results
