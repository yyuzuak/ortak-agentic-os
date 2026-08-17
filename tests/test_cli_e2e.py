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

    def test_init_scaffolds_a_usable_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.run_cmd(["git", "init", "-b", "main"], root)
            self.run_cmd(["git", "config", "user.name", "Agentic Test"], root)
            self.run_cmd(["git", "config", "user.email", "agentic@example.test"], root)
            (root / "README.md").write_text("# Consumer project\n", encoding="utf-8")
            (root / ".gitignore").write_text(".venv/\n", encoding="utf-8")
            self.run_cmd(["git", "add", "-A"], root)
            self.run_cmd(["git", "commit", "-m", "existing project"], root)

            scaffolded = self.cli(["init"], root)
            self.assertIn("created: agentic.yaml", scaffolded)
            self.assertIn("created: goals/example.yaml", scaffolded)

            # An existing .gitignore is appended to, never replaced.
            ignore = (root / ".gitignore").read_text(encoding="utf-8")
            self.assertIn(".venv/", ignore)
            self.assertIn(".agentic/", ignore)

            # The scaffold is immediately usable end to end.
            self.cli(["doctor"], root)
            self.assertIn(
                "Goal is valid: EXAMPLE-001",
                self.cli(["goal", "validate", "goals/example.yaml"], root),
            )
            self.run_cmd(["git", "add", "-A"], root)
            self.run_cmd(["git", "commit", "-m", "agentic scaffold"], root)
            self.cli(
                ["worktree", "create", "ui", "--branch", "task/ui", "--model", "default"],
                root,
            )
            self.cli(["goal", "approve", "goals/example.yaml", "--worktree", "ui"], root)
            self.cli(["goal", "arm", "EXAMPLE-001"], root)
            self.assertIn(
                "Status: READY_FOR_INTEGRATION",
                self.cli(["goal", "run", "EXAMPLE-001"], root),
            )

            # Re-running is safe and does not clobber existing files.
            again = self.cli(["init"], root)
            self.assertIn("Already initialized", again)
            self.assertIn("kept: agentic.yaml", again)
            self.assertEqual(
                self.output(["git", "status", "--porcelain"], root), ""
            )

    def test_doctor_resolves_command_provider_binaries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.run_cmd(["git", "init", "-b", "main"], root)
            self.run_cmd(["git", "config", "user.name", "Agentic Test"], root)
            self.run_cmd(["git", "config", "user.email", "agentic@example.test"], root)
            self.cli(["init"], root)

            # A repository with no commits is reported, not crashed on.
            fresh = self.cli(["doctor"], root)
            self.assertIn("[WARN] git head: no commits yet", fresh)

            self.run_cmd(["git", "add", "-A"], root)
            self.run_cmd(["git", "commit", "-m", "scaffold"], root)
            config = root / "agentic.yaml"
            base = config.read_text(encoding="utf-8")

            # A command profile whose binary does not exist must fail loudly,
            # and a missing `default` profile is only a warning.
            config.write_text(
                base.replace(
                    "  default:\n    provider: mock\n    model: deterministic-worker\n",
                    "  coder:\n"
                    "    provider: command\n"
                    "    model: fixture\n"
                    '    command: ["definitely-not-installed-agent", "run"]\n',
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, "-m", "agentic_os", "doctor"],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("[FAIL] model coder command", result.stdout)
            self.assertIn("not found on PATH", result.stdout)
            self.assertIn("[WARN] default model profile", result.stdout)

            # A command profile pointing at a real binary passes.
            config.write_text(
                base.replace(
                    "  default:\n    provider: mock\n    model: deterministic-worker\n",
                    "  default:\n"
                    "    provider: command\n"
                    "    model: fixture\n"
                    f'    command: ["{sys.executable}", "-c", "pass"]\n',
                ),
                encoding="utf-8",
            )
            healthy = self.cli(["doctor"], root)
            self.assertIn("[OK] model default command", healthy)
            self.assertNotIn("[FAIL]", healthy)
            self.assertNotIn("[WARN]", healthy)

    def test_init_refuses_outside_a_repository_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.run_cmd(["git", "init", "-b", "main"], root)
            nested = root / "packages" / "app"
            nested.mkdir(parents=True)

            result = subprocess.run(
                [sys.executable, "-m", "agentic_os", "init"],
                cwd=nested,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("Git repository root", result.stderr)
            self.assertFalse((nested / "agentic.yaml").exists())
            self.assertFalse((root / "agentic.yaml").exists())

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
