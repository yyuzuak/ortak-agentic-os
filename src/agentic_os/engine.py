from __future__ import annotations

import fnmatch
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

from agentic_os.core import topological_tasks
from agentic_os.gitops import GitOperations, run_command
from agentic_os.providers import RecoverableProviderError, build_provider
from agentic_os.state import StateStore


class RunnerError(RuntimeError):
    pass


class ApprovalRequired(RunnerError):
    pass


def run_mock_goal(goal: dict, state: StateStore) -> str:
    """Compatibility smoke loop that does not require a Git worktree."""
    goal_id = goal["id"]
    run_id = f"RUN-{uuid4().hex[:10].upper()}"

    state.create_run(run_id, goal_id)
    state.append_event("goal.run.started", goal_id=goal_id, run_id=run_id)

    for task in topological_tasks(goal):
        task_id = task["id"]
        common = {"task_id": task_id, "provider": "mock"}
        state.append_event("task.claimed", goal_id=goal_id, run_id=run_id, payload=common)
        state.append_event(
            "checkpoint.created",
            goal_id=goal_id,
            run_id=run_id,
            payload={**common, "checkpoint": f"MOCK-{task_id}"},
        )
        state.append_event(
            "verification.passed",
            goal_id=goal_id,
            run_id=run_id,
            payload={**common, "suite": "mock"},
        )
        state.append_event("task.completed", goal_id=goal_id, run_id=run_id, payload=common)

    state.set_run_status(run_id, "READY_FOR_INTEGRATION")
    state.append_event(
        "goal.ready_for_integration",
        goal_id=goal_id,
        run_id=run_id,
        payload={"main_merge": "manual"},
    )
    return run_id


class _LeaseHeartbeat:
    """Keep a worktree lease alive for as long as this process is working.

    A task may run far longer than the lease TTL, so renewing only at task
    boundaries would let the lease expire mid-task and allow a second writer
    into the same worktree. With a heartbeat the TTL measures process liveness
    instead of task duration: if this process dies, renewals stop and the lease
    expires on schedule.
    """

    def __init__(
        self, state: StateStore, worktree_name: str, run_id: str, ttl_seconds: int
    ):
        self.state = state
        self.worktree_name = worktree_name
        self.run_id = run_id
        self.ttl_seconds = ttl_seconds
        self.interval = max(0.1, ttl_seconds / 3)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> _LeaseHeartbeat:
        self._thread = threading.Thread(
            target=self._renew_until_stopped,
            name=f"lease-heartbeat-{self.worktree_name}",
            daemon=True,
        )
        self._thread.start()
        return self

    def _renew_until_stopped(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.state.renew_lease(
                    self.worktree_name, self.run_id, ttl_seconds=self.ttl_seconds
                )
            except Exception:
                # The lease is gone. Stop renewing; the synchronous renewal at
                # the next task boundary raises the same failure on the main
                # thread, where the run can be failed properly.
                return

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 5)
        return False


class GoalRunner:
    def __init__(
        self,
        *,
        root: Path,
        config: dict[str, Any],
        state: StateStore,
        git: GitOperations,
    ):
        self.root = root
        self.config = config
        self.state = state
        self.git = git

    def start(self, goal_record: dict[str, Any]) -> str:
        if goal_record["status"] != "ARMED":
            raise RunnerError(
                f"Goal must be ARMED before run; current status is {goal_record['status']}"
            )
        worktree = self._worktree(goal_record["worktree_name"])
        worktree_path = Path(worktree["path"])
        self._assert_external_dependencies(goal_record["snapshot"])
        if not self.git.is_clean(worktree_path):
            raise RunnerError("Worktree must be clean before starting an autonomous run")

        provider_name, _ = build_provider(self.config, worktree["model"])
        goal = goal_record["snapshot"]
        budget = float(
            goal.get("limits", {}).get(
                "budget_usd", self.config.get("limits", {}).get("budget_usd", 0)
            )
        )
        run_id = f"RUN-{uuid4().hex[:10].upper()}"
        self.state.create_run(
            run_id,
            goal_record["goal_id"],
            status="QUEUED",
            goal_version=int(goal_record["version"]),
            worktree_name=worktree["name"],
            provider=provider_name,
            base_sha=self.git.head(worktree_path),
            budget_usd=budget,
        )
        self.state.create_tasks(run_id, topological_tasks(goal))
        self.state.append_event(
            "goal.run.started",
            goal_id=goal_record["goal_id"],
            run_id=run_id,
            payload={
                "goal_version": goal_record["version"],
                "worktree": worktree["name"],
                "model": worktree["model"],
                "provider": provider_name,
                "base_sha": self.git.head(worktree_path),
                "budget_usd": budget,
            },
        )
        return self.execute(run_id)

    def resume(self, run_id: str) -> str:
        run = self._run(run_id)
        if run["status"] not in {"PAUSED", "APPROVAL_REQUIRED"}:
            raise RunnerError(f"Run {run_id} cannot resume from {run['status']}")
        worktree = self._worktree(run["worktree_name"])
        if run["status"] == "PAUSED" and not self.git.is_clean(Path(worktree["path"])):
            raise RunnerError("Worktree must be clean before resuming")
        return self.execute(run_id)

    def execute(self, run_id: str) -> str:
        run = self._run(run_id)
        goal_record = self._goal(run["goal_id"], int(run["goal_version"]))
        goal = goal_record["snapshot"]
        worktree = self._worktree(run["worktree_name"])
        worktree_path = Path(worktree["path"])
        _, provider = build_provider(self.config, worktree["model"])
        loop = self._resolve_loop(goal)
        repair_limit = int(
            loop.get(
                "repair_attempts",
                goal.get("limits", {}).get(
                    "repair_attempts", self.config.get("limits", {}).get("repair_attempts", 3)
                ),
            )
        )
        lease_ttl = int(loop.get("lease_ttl_seconds", 300))
        parallel_limit = self.config.get("limits", {}).get("parallel_agents")
        if parallel_limit is not None:
            parallel_limit = int(parallel_limit)

        try:
            self.state.acquire_lease(
                worktree["name"],
                run_id,
                ttl_seconds=lease_ttl,
                max_parallel=parallel_limit,
            )
        except RuntimeError as exc:
            self.state.set_run_status(run_id, "PAUSED", str(exc))
            self.state.set_goal_status(
                goal_record["goal_id"], goal_record["version"], "PAUSED"
            )
            self.state.update_worktree(worktree["name"], status="PAUSED")
            self.state.append_event(
                "run.capacity.waiting",
                goal_id=goal_record["goal_id"],
                run_id=run_id,
                payload={"reason": str(exc)},
            )
            return run_id
        self.state.set_run_status(run_id, "RUNNING")
        self.state.set_goal_status(goal_record["goal_id"], goal_record["version"], "RUNNING")
        self.state.update_worktree(worktree["name"], status="RUNNING", mode="goal")

        try:
            with _LeaseHeartbeat(self.state, worktree["name"], run_id, lease_ttl):
                for task_row in self.state.list_tasks(run_id):
                    if task_row["status"] == "COMPLETED":
                        continue
                    control = self._run(run_id)["status"]
                    if control == "PAUSE_REQUESTED":
                        return self._pause(
                            run_id, goal_record, worktree, "user requested pause"
                        )
                    if control == "STOP_REQUESTED":
                        return self._stop(run_id, goal_record, worktree)
                    self._assert_dependencies_completed(run_id, task_row)
                    self.state.renew_lease(
                        worktree["name"], run_id, ttl_seconds=lease_ttl
                    )
                    self._execute_task(
                        run_id=run_id,
                        goal_record=goal_record,
                        goal=goal,
                        task_row=task_row,
                        worktree=worktree,
                        worktree_path=worktree_path,
                        provider=provider,
                        repair_limit=repair_limit,
                    )

            head = self.git.head(worktree_path)
            self.state.set_current_task(run_id, None)
            self.state.set_run_status(run_id, "READY_FOR_INTEGRATION")
            self.state.set_goal_status(
                goal_record["goal_id"], goal_record["version"], "READY_FOR_INTEGRATION"
            )
            self.state.update_worktree(worktree["name"], status="READY", base_sha=head)
            self.state.append_event(
                "goal.ready_for_integration",
                goal_id=goal_record["goal_id"],
                run_id=run_id,
                payload={"head_sha": head, "main_merge": "manual"},
            )
            return run_id
        except KeyboardInterrupt:
            self._pause(run_id, goal_record, worktree, "keyboard interrupt")
            raise
        except ApprovalRequired as exc:
            self.state.set_run_status(run_id, "APPROVAL_REQUIRED", str(exc))
            self.state.set_goal_status(
                goal_record["goal_id"], goal_record["version"], "APPROVAL_REQUIRED"
            )
            self.state.update_worktree(worktree["name"], status="PAUSED")
            self.state.append_event(
                "approval.required",
                goal_id=goal_record["goal_id"],
                run_id=run_id,
                payload={"reason": str(exc), "kind": "budget"},
            )
            return run_id
        except Exception as exc:
            self.state.set_run_status(run_id, "FAILED", str(exc))
            self.state.set_goal_status(
                goal_record["goal_id"], goal_record["version"], "BLOCKED"
            )
            self.state.update_worktree(worktree["name"], status="BLOCKED")
            self.state.append_event(
                "goal.run.failed",
                goal_id=goal_record["goal_id"],
                run_id=run_id,
                payload={"error": str(exc)},
            )
            raise
        finally:
            self.state.release_lease(worktree["name"], run_id)

    def _execute_task(
        self,
        *,
        run_id: str,
        goal_record: dict[str, Any],
        goal: dict[str, Any],
        task_row: dict[str, Any],
        worktree: dict[str, Any],
        worktree_path: Path,
        provider: Any,
        repair_limit: int,
    ) -> None:
        task = task_row["payload"]
        provider_task = self._resolve_skills(goal, task)
        provider_task["coordination_context"] = self._coordination_context(
            worktree, goal
        )
        task_id = task["id"]
        start_attempt = int(task_row["attempts"]) + 1
        max_attempts = repair_limit + 1
        self.state.set_current_task(run_id, task_id)
        self.state.append_event(
            "task.claimed",
            goal_id=goal_record["goal_id"],
            run_id=run_id,
            payload={"task_id": task_id},
        )

        last_error: Exception | None = None
        for attempt in range(start_attempt, max_attempts + 1):
            self.state.update_task(run_id, task_id, status="RUNNING", attempts=attempt)
            if last_error is not None:
                # A repair attempt is only a repair if the provider is told what
                # went wrong; a bare attempt counter just buys the same mistake
                # again. Check failures carry the failing command and its stderr.
                provider_task["previous_attempt"] = {
                    "attempt": attempt - 1,
                    "kind": type(last_error).__name__,
                    "error": str(last_error),
                }
            try:
                head_before_provider = self.git.head(worktree_path)
                branch_before_provider = self.git.branch(worktree_path)
                if branch_before_provider != worktree["branch"]:
                    raise RunnerError(
                        f"Worktree branch changed before task {task_id}: "
                        f"{branch_before_provider}"
                    )
                result = provider.execute(
                    goal=goal,
                    task=provider_task,
                    worktree=worktree_path,
                    attempt=attempt,
                )
                branch_after_provider = self.git.branch(worktree_path)
                if branch_after_provider != worktree["branch"]:
                    raise RunnerError(
                        f"Provider changed branch during task {task_id}: "
                        f"{branch_after_provider}"
                    )
                if self.git.head(worktree_path) != head_before_provider:
                    raise RunnerError(
                        f"Provider created a commit during task {task_id}; "
                        "only the runtime may create checkpoint commits"
                    )
                projected_cost = float(self._run(run_id)["cost_usd"]) + result.cost_usd
                budget = float(self._run(run_id)["budget_usd"])
                if budget > 0 and projected_cost > budget:
                    raise ApprovalRequired(
                        f"Run cost ${projected_cost:.4f} would exceed budget ${budget:.4f}"
                    )
                self.state.add_run_cost(run_id, result.cost_usd)

                changed = self.git.status_paths(worktree_path)
                self._review_paths(task, changed)
                self.state.append_event(
                    "review.passed",
                    goal_id=goal_record["goal_id"],
                    run_id=run_id,
                    payload={"task_id": task_id, "changed_paths": changed},
                )
                self._run_checks(task, worktree_path, goal_record, run_id)
                head, committed_paths = self.git.commit_checkpoint(
                    worktree_path, f"agentic({task_id}): checkpoint"
                )
                self.state.update_task(
                    run_id, task_id, status="COMPLETED", attempts=attempt, head_sha=head
                )
                self.state.append_event(
                    "checkpoint.created",
                    goal_id=goal_record["goal_id"],
                    run_id=run_id,
                    payload={
                        "task_id": task_id,
                        "head_sha": head,
                        "changed_paths": committed_paths,
                        "summary": result.summary,
                    },
                )
                self.state.append_event(
                    "task.completed",
                    goal_id=goal_record["goal_id"],
                    run_id=run_id,
                    payload={"task_id": task_id, "attempts": attempt},
                )
                return
            except RecoverableProviderError as exc:
                last_error = exc
            except ApprovalRequired:
                # A human budget gate is not a failed repair attempt. Preserve the
                # provider's worktree changes and retry the same logical attempt
                # after approval.
                self.state.update_task(
                    run_id, task_id, status="PENDING", attempts=attempt - 1
                )
                raise
            except Exception as exc:
                last_error = exc

            if attempt < max_attempts:
                self.state.append_event(
                    "repair.requested",
                    goal_id=goal_record["goal_id"],
                    run_id=run_id,
                    payload={"task_id": task_id, "attempt": attempt, "error": str(last_error)},
                )

        self.state.update_task(run_id, task_id, status="FAILED", attempts=max_attempts)
        raise RunnerError(
            f"Task {task_id} failed after {max_attempts} attempts: {last_error}"
        )

    def _run_checks(
        self,
        task: dict[str, Any],
        worktree_path: Path,
        goal_record: dict[str, Any],
        run_id: str,
    ) -> None:
        checks = task.get("checks", [])
        self.state.append_event(
            "verification.started",
            goal_id=goal_record["goal_id"],
            run_id=run_id,
            payload={"task_id": task["id"], "checks": len(checks)},
        )
        for command in checks:
            run_command(command, cwd=worktree_path, timeout=300)
        self.state.append_event(
            "verification.passed",
            goal_id=goal_record["goal_id"],
            run_id=run_id,
            payload={"task_id": task["id"], "checks": len(checks)},
        )

    @staticmethod
    def _review_paths(task: dict[str, Any], changed: list[str]) -> None:
        if not changed and not task.get("allow_no_changes", False):
            raise RunnerError(f"Task {task['id']} produced no changes")
        owned = task.get("owned_paths", [])
        if not owned:
            return
        unauthorized = [
            path for path in changed if not any(fnmatch.fnmatch(path, pattern) for pattern in owned)
        ]
        if unauthorized:
            raise RunnerError(
                f"Task {task['id']} modified paths outside ownership: {', '.join(unauthorized)}"
            )

    def _assert_dependencies_completed(self, run_id: str, task: dict[str, Any]) -> None:
        statuses = {item["task_id"]: item["status"] for item in self.state.list_tasks(run_id)}
        blocked = [dep for dep in task["depends_on"] if statuses.get(dep) != "COMPLETED"]
        if blocked:
            raise RunnerError(
                f"Task {task['task_id']} has incomplete dependencies: {', '.join(blocked)}"
            )

    def _assert_external_dependencies(self, goal: dict[str, Any]) -> None:
        blocked: list[str] = []
        for requirement in goal.get("requires", []):
            dependency = self.state.get_goal(requirement["goal"])
            if not dependency or dependency["status"] != requirement["status"]:
                actual = dependency["status"] if dependency else "MISSING"
                blocked.append(
                    f"{requirement['goal']} expected {requirement['status']} but is {actual}"
                )
        if blocked:
            raise RunnerError("Goal dependencies are not ready: " + "; ".join(blocked))

    def _resolve_loop(self, goal: dict[str, Any]) -> dict[str, Any]:
        selection = goal.get("loop", "default")
        if isinstance(selection, dict):
            return selection
        profile = self.config.get("loops", {}).get(selection)
        if selection == "default" and profile is None:
            return {}
        if not isinstance(profile, dict):
            raise RunnerError(f"Unknown loop profile: {selection}")
        return profile

    def _resolve_skills(
        self, goal: dict[str, Any], task: dict[str, Any]
    ) -> dict[str, Any]:
        names = [*goal.get("skills", []), *task.get("skills", [])]
        catalog = self.config.get("skills", {})
        missing = [name for name in names if name not in catalog]
        if missing:
            raise RunnerError(f"Unknown skills: {', '.join(missing)}")
        resolved = dict(task)
        resolved["skill_instructions"] = {
            name: catalog[name] for name in dict.fromkeys(names)
        }
        return resolved

    def _coordination_context(
        self, current: dict[str, Any], goal: dict[str, Any]
    ) -> dict[str, Any]:
        """Build the small live snapshot every provider receives before a task."""
        siblings: list[dict[str, Any]] = []
        for item in self.state.list_worktrees():
            if item["name"] == current["name"]:
                continue
            path = Path(item["path"])
            siblings.append(
                {
                    "name": item["name"],
                    "branch": item["branch"],
                    "model": item["model"],
                    "status": item["status"],
                    "head_sha": self.git.head(path) if path.exists() else None,
                }
            )
        goals = [
            {
                "goal_id": item["goal_id"],
                "version": item["version"],
                "worktree": item["worktree_name"],
                "status": item["status"],
            }
            for item in self.state.list_goals()
        ]
        return {
            "current_worktree": {
                "name": current["name"],
                "branch": current["branch"],
                "model": current["model"],
            },
            "other_worktrees": siblings,
            "goals": goals,
            "requires": goal.get("requires", []),
        }

    def _pause(
        self,
        run_id: str,
        goal_record: dict[str, Any],
        worktree: dict[str, Any],
        reason: str,
    ) -> str:
        self.state.set_run_status(run_id, "PAUSED", reason)
        self.state.set_goal_status(goal_record["goal_id"], goal_record["version"], "PAUSED")
        self.state.update_worktree(worktree["name"], status="PAUSED")
        self.state.append_event(
            "goal.run.paused",
            goal_id=goal_record["goal_id"],
            run_id=run_id,
            payload={"reason": reason},
        )
        return run_id

    def _stop(
        self, run_id: str, goal_record: dict[str, Any], worktree: dict[str, Any]
    ) -> str:
        self.state.set_run_status(run_id, "STOPPED", "user requested stop")
        self.state.set_goal_status(goal_record["goal_id"], goal_record["version"], "STOPPED")
        self.state.update_worktree(worktree["name"], status="READY", mode="interactive")
        self.state.append_event(
            "goal.run.stopped",
            goal_id=goal_record["goal_id"],
            run_id=run_id,
        )
        return run_id

    def _run(self, run_id: str) -> dict[str, Any]:
        run = self.state.get_run(run_id)
        if not run:
            raise RunnerError(f"Unknown run: {run_id}")
        return run

    def _goal(self, goal_id: str, version: int) -> dict[str, Any]:
        goal = self.state.get_goal(goal_id, version)
        if not goal:
            raise RunnerError(f"Unknown goal: {goal_id}@{version}")
        return goal

    def _worktree(self, name: str) -> dict[str, Any]:
        worktree = self.state.get_worktree(name)
        if not worktree or worktree["status"] == "REMOVED":
            raise RunnerError(f"Unknown worktree: {name}")
        return worktree
