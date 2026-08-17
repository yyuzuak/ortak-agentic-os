from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agentic_os.core import goal_digest
from agentic_os.engine import GoalRunner
from agentic_os.gitops import GitError, GitOperations, run_command
from agentic_os.state import StateStore
from agentic_os.watcher import VerificationWatcher


class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        run_command(["git", "init", "-b", "main"], cwd=self.root)
        run_command(["git", "config", "user.name", "Agentic Test"], cwd=self.root)
        run_command(["git", "config", "user.email", "agentic@example.test"], cwd=self.root)
        (self.root / ".gitignore").write_text(".agentic/\n", encoding="utf-8")
        (self.root / "README.md").write_text("# Fixture\n", encoding="utf-8")
        run_command(["git", "add", "-A"], cwd=self.root)
        run_command(["git", "commit", "-m", "initial"], cwd=self.root)
        self.state = StateStore(self.root / ".agentic/state.sqlite")
        self.git = GitOperations(self.root, self.root / ".agentic/worktrees")
        self.config = {
            "models": {
                "default": {
                    "provider": "mock",
                    "model": "deterministic-worker",
                    "cost_per_task": 0.01,
                }
            },
            "limits": {"repair_attempts": 2, "budget_usd": 1},
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def create_managed_worktree(self, name: str = "feature") -> dict:
        path, base_sha = self.git.create_worktree(
            name, f"task/{name}", base_ref="main"
        )
        self.state.create_worktree(
            name=name,
            path=str(path),
            branch=f"task/{name}",
            model="default",
            mode="interactive",
            base_sha=base_sha,
        )
        return self.state.get_worktree(name)

    def approve_and_arm(self, goal: dict, worktree_name: str = "feature") -> dict:
        version = self.state.approve_goal(
            goal_id=goal["id"],
            path=f"goals/{goal['id']}.yaml",
            digest=goal_digest(goal),
            worktree_name=worktree_name,
            snapshot=goal,
        )
        self.state.set_goal_status(goal["id"], version, "ARMED")
        self.state.update_worktree(worktree_name, mode="goal", status="ARMED")
        return self.state.get_goal(goal["id"], version)

    def test_worktree_refuses_dirty_removal(self) -> None:
        worktree = self.create_managed_worktree()
        path = Path(worktree["path"])
        (path / "dirty.txt").write_text("dirty\n", encoding="utf-8")

        with self.assertRaisesRegex(GitError, "uncommitted changes"):
            self.git.remove_worktree(path)

        (path / "dirty.txt").unlink()
        self.git.remove_worktree(path)
        self.assertFalse(path.exists())

    def test_goal_run_repairs_commits_and_integrates(self) -> None:
        worktree = self.create_managed_worktree()
        goal = {
            "id": "GOAL-1",
            "objective": "Create two deterministic artifacts",
            "tasks": [
                {
                    "id": "TASK-1",
                    "objective": "Create first artifact",
                    "output": "agentic-demo/first.txt",
                    "owned_paths": ["agentic-demo/**"],
                    "checks": [
                        [
                            sys.executable,
                            "-c",
                            "from pathlib import Path; assert Path('agentic-demo/first.txt').is_file()",
                        ]
                    ],
                },
                {
                    "id": "TASK-2",
                    "objective": "Create second artifact",
                    "depends_on": ["TASK-1"],
                    "output": "agentic-demo/second.txt",
                    "owned_paths": ["agentic-demo/**"],
                    "mock_failures": 1,
                },
            ],
            "limits": {"repair_attempts": 2, "budget_usd": 1},
        }
        goal_record = self.approve_and_arm(goal)
        main_before = self.git.head(self.root)
        runner = GoalRunner(
            root=self.root,
            config=self.config,
            state=self.state,
            git=self.git,
        )

        run_id = runner.start(goal_record)

        run = self.state.get_run(run_id)
        self.assertEqual(run["status"], "READY_FOR_INTEGRATION")
        self.assertEqual(self.git.head(self.root), main_before)
        self.assertTrue(self.git.is_clean(Path(worktree["path"])))
        self.assertIsNone(self.state.get_lease("feature"))
        tasks = self.state.list_tasks(run_id)
        self.assertEqual([task["status"] for task in tasks], ["COMPLETED", "COMPLETED"])
        self.assertEqual(tasks[1]["attempts"], 2)
        event_types = [event["event_type"] for event in self.state.list_events(100)]
        self.assertIn("repair.requested", event_types)
        self.assertIn("review.passed", event_types)
        self.assertIn("verification.passed", event_types)

        integration_path, integration_head, _ = self.git.create_integration(
            integration_root=self.root / ".agentic/integrations",
            integration_branch="integration/goal-1",
            base_sha=run["base_sha"],
            source_branch=worktree["branch"],
        )
        self.assertTrue((integration_path / "agentic-demo/first.txt").is_file())
        self.assertEqual(self.git.head(integration_path), integration_head)
        self.assertEqual(self.git.head(self.root), main_before)

    def test_budget_requires_approval_then_resumes(self) -> None:
        self.create_managed_worktree()
        self.config["models"]["default"]["cost_per_task"] = 0.5
        goal = {
            "id": "GOAL-BUDGET",
            "objective": "Exercise budget control",
            "tasks": [
                {
                    "id": "TASK-1",
                    "objective": "Create artifact",
                    "owned_paths": ["agentic-demo/**"],
                }
            ],
            "limits": {"repair_attempts": 1, "budget_usd": 0.1},
        }
        goal_record = self.approve_and_arm(goal)
        runner = GoalRunner(
            root=self.root,
            config=self.config,
            state=self.state,
            git=self.git,
        )

        run_id = runner.start(goal_record)
        self.assertEqual(self.state.get_run(run_id)["status"], "APPROVAL_REQUIRED")
        self.state.set_run_budget(run_id, 1.0)
        runner.resume(run_id)
        self.assertEqual(self.state.get_run(run_id)["status"], "READY_FOR_INTEGRATION")

    def test_write_lease_rejects_second_writer(self) -> None:
        self.state.acquire_lease("feature", "RUN-1", ttl_seconds=60)
        with self.assertRaisesRegex(RuntimeError, "leased by RUN-1"):
            self.state.acquire_lease("feature", "RUN-2", ttl_seconds=60)

    def test_global_parallel_agent_limit_is_enforced(self) -> None:
        self.state.acquire_lease(
            "first", "RUN-1", ttl_seconds=60, max_parallel=1
        )
        with self.assertRaisesRegex(RuntimeError, "Parallel agent limit reached"):
            self.state.acquire_lease(
                "second", "RUN-2", ttl_seconds=60, max_parallel=1
            )

    def slow_goal(self, goal_id: str, delay_seconds: float) -> dict:
        return {
            "id": goal_id,
            "objective": "Work for longer than the lease TTL",
            "loop": {"lease_ttl_seconds": 2, "repair_attempts": 0},
            "tasks": [
                {
                    "id": "TASK-SLOW",
                    "objective": "Outlive the lease TTL",
                    "mock_delay_seconds": delay_seconds,
                },
                # A second task is required to reach the boundary renewal that
                # fails with "Lease lost" once the lease has expired mid-task.
                {
                    "id": "TASK-NEXT",
                    "objective": "Continue after the slow task",
                    "depends_on": ["TASK-SLOW"],
                },
            ],
        }

    def start_goal_in_thread(
        self, record: dict, worktree_name: str, errors: list[Exception]
    ) -> threading.Thread:
        """Start a run in the background and wait until it holds the lease."""

        def execute() -> None:
            try:
                GoalRunner(
                    root=self.root,
                    config=self.config,
                    state=self.state,
                    git=self.git,
                ).start(record)
            except Exception as exc:  # pragma: no cover - assertion records it
                errors.append(exc)

        thread = threading.Thread(target=execute)
        thread.start()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if self.state.get_lease(worktree_name):
                break
            time.sleep(0.01)
        return thread

    def test_competing_writer_cannot_steal_lease_during_long_task(self) -> None:
        self.create_managed_worktree("guarded")
        record = self.approve_and_arm(self.slow_goal("GOAL-GUARDED", 4.0), "guarded")
        errors: list[Exception] = []
        thread = self.start_goal_in_thread(record, "guarded", errors)
        # Stay inside the still-running task, but past the original TTL.
        time.sleep(2.6)

        with self.assertRaisesRegex(RuntimeError, "leased by"):
            self.state.acquire_lease("guarded", "RUN-OTHER", ttl_seconds=60)

        thread.join(timeout=15)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(
            self.state.get_goal("GOAL-GUARDED")["status"], "READY_FOR_INTEGRATION"
        )
        self.assertIsNone(self.state.get_lease("guarded"))

    def test_recover_does_not_reclaim_the_lease_of_a_live_run(self) -> None:
        self.create_managed_worktree("live")
        record = self.approve_and_arm(self.slow_goal("GOAL-LIVE", 4.0), "live")
        errors: list[Exception] = []
        thread = self.start_goal_in_thread(record, "live", errors)
        # A concurrent `agentic recover` while the task is still working.
        time.sleep(2.6)

        self.assertEqual(self.state.release_expired_leases(), [])

        thread.join(timeout=15)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(
            self.state.get_goal("GOAL-LIVE")["status"], "READY_FOR_INTEGRATION"
        )

    def test_two_worktrees_can_run_concurrently_within_limit(self) -> None:
        self.config["limits"]["parallel_agents"] = 2
        records = []
        for index, name in enumerate(("parallel-one", "parallel-two"), start=1):
            self.create_managed_worktree(name)
            goal = {
                "id": f"GOAL-PARALLEL-{index}",
                "objective": f"Run parallel slice {index}",
                "tasks": [
                    {
                        "id": f"TASK-{index}",
                        "objective": f"Slow slice {index}",
                        "mock_delay_seconds": 0.25,
                    }
                ],
            }
            records.append(self.approve_and_arm(goal, name))
        errors: list[Exception] = []

        def execute(record: dict) -> None:
            try:
                GoalRunner(
                    root=self.root,
                    config=self.config,
                    state=self.state,
                    git=self.git,
                ).start(record)
            except Exception as exc:  # pragma: no cover - assertion records it
                errors.append(exc)

        threads = [threading.Thread(target=execute, args=(record,)) for record in records]
        for thread in threads:
            thread.start()
        both_leased = False
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if self.state.get_lease("parallel-one") and self.state.get_lease(
                "parallel-two"
            ):
                both_leased = True
                break
            time.sleep(0.01)
        for thread in threads:
            thread.join(timeout=3)

        self.assertTrue(both_leased)
        self.assertEqual(errors, [])
        self.assertEqual(
            [self.state.get_goal(record["goal_id"])["status"] for record in records],
            ["READY_FOR_INTEGRATION", "READY_FOR_INTEGRATION"],
        )

    def test_run_waits_for_capacity_and_can_resume(self) -> None:
        self.config["limits"]["parallel_agents"] = 1
        first_worktree = self.create_managed_worktree("capacity-one")
        self.create_managed_worktree("capacity-two")
        first = self.approve_and_arm(
            {
                "id": "GOAL-CAPACITY-1",
                "objective": "Hold the only runtime slot",
                "tasks": [
                    {
                        "id": "TASK-1",
                        "objective": "Slow task",
                        "mock_delay_seconds": 0.25,
                    }
                ],
            },
            "capacity-one",
        )
        second = self.approve_and_arm(
            {
                "id": "GOAL-CAPACITY-2",
                "objective": "Wait for the runtime slot",
                "tasks": [{"id": "TASK-2", "objective": "Queued task"}],
            },
            "capacity-two",
        )
        runner = GoalRunner(
            root=self.root, config=self.config, state=self.state, git=self.git
        )
        first_thread = threading.Thread(target=lambda: runner.start(first))
        first_thread.start()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not self.state.get_lease(
            first_worktree["name"]
        ):
            time.sleep(0.01)

        second_run = runner.start(second)
        self.assertEqual(self.state.get_run(second_run)["status"], "PAUSED")
        self.assertIn(
            "Parallel agent limit reached", self.state.get_run(second_run)["error"]
        )
        first_thread.join(timeout=3)
        runner.resume(second_run)

        self.assertEqual(
            self.state.get_run(second_run)["status"], "READY_FOR_INTEGRATION"
        )

    def test_pause_then_resume_between_tasks(self) -> None:
        self.create_managed_worktree()
        goal = {
            "id": "GOAL-PAUSE",
            "objective": "Pause safely between checkpoints",
            "tasks": [
                {
                    "id": "TASK-1",
                    "objective": "Slow first task",
                    "mock_delay_seconds": 0.2,
                },
                {
                    "id": "TASK-2",
                    "objective": "Second task",
                    "depends_on": ["TASK-1"],
                },
            ],
        }
        goal_record = self.approve_and_arm(goal)
        runner = GoalRunner(
            root=self.root, config=self.config, state=self.state, git=self.git
        )
        errors: list[Exception] = []

        def execute() -> None:
            try:
                runner.start(goal_record)
            except Exception as exc:  # pragma: no cover - assertion records it
                errors.append(exc)

        thread = threading.Thread(target=execute)
        thread.start()
        run = None
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            runs = self.state.list_runs()
            if runs and runs[0]["status"] == "RUNNING" and runs[0]["current_task"] == "TASK-1":
                run = runs[0]
                break
            time.sleep(0.01)
        self.assertIsNotNone(run)
        self.state.set_run_status(run["id"], "PAUSE_REQUESTED")
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.state.get_run(run["id"])["status"], "PAUSED")

        runner.resume(run["id"])
        self.assertEqual(
            self.state.get_run(run["id"])["status"], "READY_FOR_INTEGRATION"
        )

    def test_stop_finishes_at_checkpoint_boundary(self) -> None:
        self.create_managed_worktree()
        goal = {
            "id": "GOAL-STOP",
            "objective": "Stop safely between checkpoints",
            "tasks": [
                {
                    "id": "TASK-1",
                    "objective": "Slow first task",
                    "mock_delay_seconds": 0.2,
                },
                {
                    "id": "TASK-2",
                    "objective": "Task that must not start",
                    "depends_on": ["TASK-1"],
                },
            ],
        }
        goal_record = self.approve_and_arm(goal)
        runner = GoalRunner(
            root=self.root, config=self.config, state=self.state, git=self.git
        )

        thread = threading.Thread(target=lambda: runner.start(goal_record))
        thread.start()
        run = None
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            runs = self.state.list_runs()
            if runs and runs[0]["current_task"] == "TASK-1":
                run = runs[0]
                break
            time.sleep(0.01)
        self.assertIsNotNone(run)
        self.state.set_run_status(run["id"], "STOP_REQUESTED")
        thread.join(timeout=3)

        self.assertFalse(thread.is_alive())
        self.assertEqual(self.state.get_run(run["id"])["status"], "STOPPED")
        self.assertEqual(
            [task["status"] for task in self.state.list_tasks(run["id"])],
            ["COMPLETED", "PENDING"],
        )
        self.assertEqual(self.state.get_worktree("feature")["mode"], "interactive")

    def test_background_watcher_only_verifies_new_head_once(self) -> None:
        worktree = self.create_managed_worktree()
        watcher = VerificationWatcher(config=self.config, state=self.state, git=self.git)

        first = watcher.run_once()
        second = watcher.run_once()

        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].worktree, worktree["name"])
        self.assertEqual(first[0].status, "PASSED")
        self.assertEqual(second, [])

    def test_background_watcher_observes_new_commits_during_window(self) -> None:
        worktree = self.create_managed_worktree("watched")
        path = Path(worktree["path"])
        watcher = VerificationWatcher(config=self.config, state=self.state, git=self.git)
        results = []
        thread = threading.Thread(
            target=lambda: results.extend(watcher.run_for(0.4, 0.03))
        )
        thread.start()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if self.state.get_verification("watched", self.git.head(path)):
                break
            time.sleep(0.01)
        first_head = self.git.head(path)
        (path / "published.txt").write_text("new head\n", encoding="utf-8")
        second_head, _ = self.git.commit_checkpoint(path, "fixture: publish new head")
        thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertNotEqual(first_head, second_head)
        self.assertEqual(
            {result.head_sha for result in results}, {first_head, second_head}
        )

    def test_command_provider_receives_resolved_skills(self) -> None:
        worktree = self.create_managed_worktree("command")
        sibling = self.create_managed_worktree("sibling")
        self.config["models"]["command-model"] = {
            "provider": "command",
            "model": "fixture",
            "command": [
                sys.executable,
                "-c",
                "import pathlib,sys; pathlib.Path('generated').mkdir(exist_ok=True); "
                "pathlib.Path('generated/prompt.json').write_text(sys.stdin.read())",
            ],
        }
        self.config["skills"] = {"focused": "Only make the requested focused change."}
        self.state.update_worktree("command", model="command-model")
        goal = {
            "id": "GOAL-COMMAND",
            "objective": "Exercise a generic command adapter",
            "skills": ["focused"],
            "tasks": [
                {
                    "id": "TASK-1",
                    "objective": "Write the received task packet",
                    "owned_paths": ["generated/**"],
                }
            ],
        }
        goal_record = self.approve_and_arm(goal, "command")
        runner = GoalRunner(
            root=self.root, config=self.config, state=self.state, git=self.git
        )

        runner.start(goal_record)

        prompt = (Path(worktree["path"]) / "generated/prompt.json").read_text(
            encoding="utf-8"
        )
        self.assertIn("Only make the requested focused change.", prompt)
        self.assertIn(sibling["branch"], prompt)
        self.assertIn("coordination_context", prompt)

    def test_repair_attempt_receives_the_previous_failure(self) -> None:
        worktree = self.create_managed_worktree("repair")
        self.config["models"]["failing-once"] = {
            "provider": "command",
            "model": "fixture",
            "command": [
                sys.executable,
                "-c",
                "import json,pathlib,sys\n"
                "payload = sys.stdin.read()\n"
                "attempt = json.loads(payload)['attempt']\n"
                "pathlib.Path('generated').mkdir(exist_ok=True)\n"
                "pathlib.Path('generated/prompt-%d.json' % attempt).write_text(payload)\n"
                "if attempt == 1:\n"
                "    sys.stderr.write('boom: contract check failed\\n')\n"
                "    sys.exit(1)\n",
            ],
        }
        self.state.update_worktree("repair", model="failing-once")
        goal = {
            "id": "GOAL-REPAIR",
            "objective": "Feed the failure back into the repair attempt",
            "tasks": [
                {
                    "id": "TASK-1",
                    "objective": "Fail once, then succeed",
                    "owned_paths": ["generated/**"],
                }
            ],
        }
        record = self.approve_and_arm(goal, "repair")

        GoalRunner(
            root=self.root, config=self.config, state=self.state, git=self.git
        ).start(record)

        generated = Path(worktree["path"]) / "generated"
        first = json.loads((generated / "prompt-1.json").read_text(encoding="utf-8"))
        second = json.loads((generated / "prompt-2.json").read_text(encoding="utf-8"))

        self.assertNotIn("previous_attempt", first["task"])
        previous = second["task"]["previous_attempt"]
        self.assertEqual(previous["attempt"], 1)
        self.assertIn("boom: contract check failed", previous["error"])
        self.assertEqual(
            self.state.get_goal("GOAL-REPAIR")["status"], "READY_FOR_INTEGRATION"
        )

    def test_provider_commit_is_detected_and_blocks_goal(self) -> None:
        worktree = self.create_managed_worktree("rogue")
        self.config["models"]["rogue-model"] = {
            "provider": "command",
            "model": "fixture",
            "command": [
                sys.executable,
                "-c",
                "import pathlib,subprocess; pathlib.Path('rogue.txt').write_text('x'); "
                "subprocess.run(['git','add','rogue.txt'],check=True); "
                "subprocess.run(['git','commit','-m','rogue commit'],check=True)",
            ],
        }
        self.state.update_worktree("rogue", model="rogue-model")
        goal = {
            "id": "GOAL-ROGUE",
            "objective": "Reject provider-owned commits",
            "tasks": [{"id": "TASK-1", "objective": "Attempt a direct commit"}],
            "limits": {"repair_attempts": 0},
        }
        record = self.approve_and_arm(goal, "rogue")
        main_before = self.git.head(self.root)

        with self.assertRaisesRegex(Exception, "Provider created a commit"):
            GoalRunner(
                root=self.root,
                config=self.config,
                state=self.state,
                git=self.git,
            ).start(record)

        self.assertEqual(self.git.head(self.root), main_before)
        self.assertNotEqual(self.git.head(Path(worktree["path"])), main_before)
        self.assertEqual(self.state.get_goal("GOAL-ROGUE")["status"], "BLOCKED")

    def test_multiple_goals_merge_into_one_sprint_integration(self) -> None:
        first_worktree = self.create_managed_worktree("first")
        second_worktree = self.create_managed_worktree("second")
        runner = GoalRunner(
            root=self.root, config=self.config, state=self.state, git=self.git
        )
        records = []
        for index, name in enumerate(("first", "second"), start=1):
            goal = {
                "id": f"GOAL-SPRINT-{index}",
                "objective": f"Create sprint slice {index}",
                "tasks": [
                    {
                        "id": f"TASK-{index}",
                        "objective": f"Create slice {index}",
                        "output": f"sprint/slice-{index}.txt",
                        "owned_paths": ["sprint/**"],
                    }
                ],
            }
            records.append(self.approve_and_arm(goal, name))

        run_ids = [runner.start(record) for record in records]
        integration_root = self.root / ".agentic/integrations"
        integration_path, _, _ = self.git.create_integration(
            integration_root=integration_root,
            integration_branch="integration/sprint-1",
            base_sha=self.state.get_run(run_ids[0])["base_sha"],
            source_branch=first_worktree["branch"],
        )
        reused_path, _, _ = self.git.create_integration(
            integration_root=integration_root,
            integration_branch="integration/sprint-1",
            base_sha=self.state.get_run(run_ids[1])["base_sha"],
            source_branch=second_worktree["branch"],
        )

        self.assertEqual(reused_path, integration_path)
        self.assertTrue((integration_path / "sprint/slice-1.txt").is_file())
        self.assertTrue((integration_path / "sprint/slice-2.txt").is_file())
        self.assertEqual(self.git.branch(self.root), "main")

    def test_sprint_integration_stops_on_merge_conflict(self) -> None:
        first = self.create_managed_worktree("conflict-one")
        second = self.create_managed_worktree("conflict-two")
        for worktree, value in ((first, "one\n"), (second, "two\n")):
            path = Path(worktree["path"])
            (path / "shared.txt").write_text(value, encoding="utf-8")
            self.git.commit_checkpoint(path, f"fixture: {worktree['name']}")
        main_before = self.git.head(self.root)
        integration_root = self.root / ".agentic/integrations"
        integration_path, _, _ = self.git.create_integration(
            integration_root=integration_root,
            integration_branch="integration/conflict",
            base_sha=main_before,
            source_branch=first["branch"],
        )

        head_before_conflict = self.git.head(integration_path)

        with self.assertRaisesRegex(GitError, "Merge failed"):
            self.git.create_integration(
                integration_root=integration_root,
                integration_branch="integration/conflict",
                base_sha=main_before,
                source_branch=second["branch"],
            )

        # The conflict is rolled back, so the sprint branch stays usable for
        # the goals that follow instead of needing manual cleanup.
        self.assertTrue(self.git.is_clean(integration_path))
        self.assertEqual(self.git.head(integration_path), head_before_conflict)
        self.assertEqual(self.git.head(self.root), main_before)

        third = self.create_managed_worktree("conflict-three")
        third_path = Path(third["path"])
        (third_path / "unrelated.txt").write_text("ok\n", encoding="utf-8")
        self.git.commit_checkpoint(third_path, "fixture: unrelated change")
        reused, _, _ = self.git.create_integration(
            integration_root=integration_root,
            integration_branch="integration/conflict",
            base_sha=main_before,
            source_branch=third["branch"],
        )
        self.assertTrue((reused / "unrelated.txt").is_file())

    def test_external_goal_dependency_blocks_until_ready(self) -> None:
        self.create_managed_worktree("database")
        self.create_managed_worktree("ui")
        database_goal = {
            "id": "DATABASE-1",
            "objective": "Publish schema",
            "tasks": [{"id": "DB-TASK", "objective": "Schema"}],
        }
        database_version = self.state.approve_goal(
            goal_id="DATABASE-1",
            path="goals/database.yaml",
            digest=goal_digest(database_goal),
            worktree_name="database",
            snapshot=database_goal,
        )
        ui_goal = {
            "id": "UI-1",
            "objective": "Build UI after schema",
            "requires": [{"goal": "DATABASE-1", "status": "READY_FOR_INTEGRATION"}],
            "tasks": [{"id": "UI-TASK", "objective": "UI"}],
        }
        ui_record = self.approve_and_arm(ui_goal, "ui")
        runner = GoalRunner(
            root=self.root, config=self.config, state=self.state, git=self.git
        )

        with self.assertRaisesRegex(Exception, "dependencies are not ready"):
            runner.start(ui_record)

        self.state.set_goal_status(
            "DATABASE-1", database_version, "READY_FOR_INTEGRATION"
        )
        runner.start(ui_record)
        self.assertEqual(self.state.get_goal("UI-1")["status"], "READY_FOR_INTEGRATION")

    def test_expired_lease_is_recoverable(self) -> None:
        self.state.acquire_lease("feature", "RUN-OLD", ttl_seconds=-1)
        released = self.state.release_expired_leases()
        self.assertEqual(released, ["RUN-OLD"])
        self.assertIsNone(self.state.get_lease("feature"))


if __name__ == "__main__":
    unittest.main()
