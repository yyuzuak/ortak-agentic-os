from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CliEndToEndTests(unittest.TestCase):
    def test_controlled_goal_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.run_cmd(["git", "init", "-b", "main"], root)
            self.run_cmd(["git", "config", "user.name", "Agentic Test"], root)
            self.run_cmd(["git", "config", "user.email", "agentic@example.test"], root)
            (root / ".gitignore").write_text(".agentic/\n", encoding="utf-8")
            (root / "agentic.yaml").write_text(
                """version: 0
runtime:
  state: .agentic/state.sqlite
  worktrees: .agentic/worktrees
  integrations: .agentic/integrations
models:
  default:
    provider: mock
    model: deterministic-worker
autonomy:
  main_merge: manual
limits:
  repair_attempts: 2
  budget_usd: 1
verification:
  commands: []
""",
                encoding="utf-8",
            )
            goals = root / "goals"
            goals.mkdir()
            (goals / "demo.yaml").write_text(
                """id: E2E-001
objective: Exercise the controlled workflow.
tasks:
  - id: TASK-001
    objective: Create an artifact.
    output: generated/result.txt
    owned_paths:
      - generated/**
""",
                encoding="utf-8",
            )
            (goals / "second.yaml").write_text(
                """id: E2E-002
objective: Exercise a second parallel worktree.
tasks:
  - id: TASK-002
    objective: Create a second artifact.
    output: generated/second.txt
    owned_paths:
      - generated/**
""",
                encoding="utf-8",
            )
            self.run_cmd(["git", "add", "-A"], root)
            self.run_cmd(["git", "commit", "-m", "fixture"], root)
            main_before = self.output(["git", "rev-parse", "HEAD"], root)

            self.cli(["doctor"], root)
            self.cli(
                [
                    "worktree",
                    "create",
                    "feature",
                    "--branch",
                    "task/feature",
                    "--model",
                    "default",
                ],
                root,
            )
            self.cli(
                [
                    "worktree",
                    "create",
                    "parallel",
                    "--branch",
                    "task/parallel",
                    "--model",
                    "default",
                ],
                root,
            )
            chat = self.cli(["chat", "feature"], root)
            self.assertIn("Autonomy: disabled", chat)
            approved = self.cli(
                ["goal", "approve", "goals/demo.yaml", "--worktree", "feature"], root
            )
            self.assertIn("Goal approved: E2E-001@1", approved)
            self.cli(["goal", "arm", "E2E-001"], root)
            completed = self.cli(["goal", "run", "E2E-001"], root)
            self.assertIn("Status: READY_FOR_INTEGRATION", completed)
            self.cli(
                ["goal", "approve", "goals/second.yaml", "--worktree", "parallel"],
                root,
            )
            self.cli(["goal", "arm", "E2E-002"], root)
            second_completed = self.cli(["goal", "run", "E2E-002"], root)
            self.assertIn("Status: READY_FOR_INTEGRATION", second_completed)
            context = self.cli(["context", "feature"], root)
            self.assertIn("E2E-001@1  READY_FOR_INTEGRATION", context)
            watched = self.cli(["watch", "--once"], root)
            self.assertIn("PASSED", watched)
            integrated = self.cli(
                [
                    "goal",
                    "integrate",
                    "E2E-001",
                    "--branch",
                    "integration/sprint-e2e",
                ],
                root,
            )
            self.assertIn("Main branch was not modified.", integrated)
            second_integrated = self.cli(
                [
                    "goal",
                    "integrate",
                    "E2E-002",
                    "--branch",
                    "integration/sprint-e2e",
                ],
                root,
            )
            self.assertIn("Main branch was not modified.", second_integrated)
            integration_path = root / ".agentic/integrations/integration-sprint-e2e"
            self.assertTrue((integration_path / "generated/result.txt").is_file())
            self.assertTrue((integration_path / "generated/second.txt").is_file())
            self.assertEqual(self.output(["git", "rev-parse", "HEAD"], root), main_before)
            self.cli(["worktree", "remove", "feature", "--yes"], root)
            self.cli(["worktree", "remove", "parallel", "--yes"], root)
            self.assertEqual(self.output(["git", "status", "--porcelain"], root), "")

    def cli(self, args: list[str], cwd: Path) -> str:
        return self.output([sys.executable, "-m", "agentic_os", *args], cwd)

    def run_cmd(self, args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False)
        if result.returncode != 0:
            self.fail(
                f"Command failed: {' '.join(args)}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result

    def output(self, args: list[str], cwd: Path) -> str:
        return self.run_cmd(args, cwd).stdout.strip()


if __name__ == "__main__":
    unittest.main()
