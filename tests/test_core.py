from __future__ import annotations

import unittest

from agentic_os.core import (
    ValidationError,
    topological_tasks,
    validate_config,
    validate_goal,
)


class ConfigValidationTests(unittest.TestCase):
    def valid_config(self) -> dict:
        return {
            "version": 0,
            "runtime": {
                "state": ".agentic/state.sqlite",
                "worktrees": ".agentic/worktrees",
            },
            "models": {"default": {"provider": "mock"}},
            "autonomy": {"main_merge": "manual"},
            "limits": {"parallel_agents": 2, "budget_usd": 1},
            "verification": {"commands": []},
        }

    def test_accepts_safe_model_agnostic_config(self) -> None:
        validate_config(self.valid_config())

    def test_rejects_runtime_path_escape(self) -> None:
        config = self.valid_config()
        config["runtime"]["worktrees"] = "../outside"
        with self.assertRaisesRegex(ValidationError, "safe relative path"):
            validate_config(config)

    def test_rejects_automatic_main_merge(self) -> None:
        config = self.valid_config()
        config["autonomy"]["main_merge"] = "automatic"
        with self.assertRaisesRegex(ValidationError, "must be manual"):
            validate_config(config)

    def test_rejects_unknown_or_incomplete_provider(self) -> None:
        config = self.valid_config()
        config["models"]["default"] = {"provider": "command"}
        with self.assertRaisesRegex(ValidationError, "command array"):
            validate_config(config)


class GoalValidationTests(unittest.TestCase):
    def test_orders_dependencies(self) -> None:
        goal = {
            "id": "G-1",
            "objective": "Demo",
            "tasks": [
                {"id": "T-2", "objective": "Second", "depends_on": ["T-1"]},
                {"id": "T-1", "objective": "First"},
            ],
        }

        self.assertEqual(validate_goal(goal), [])
        self.assertEqual([task["id"] for task in topological_tasks(goal)], ["T-1", "T-2"])

    def test_rejects_unknown_dependency(self) -> None:
        goal = {
            "id": "G-1",
            "objective": "Demo",
            "tasks": [
                {"id": "T-1", "objective": "First", "depends_on": ["T-404"]},
            ],
        }

        self.assertEqual(validate_goal(goal), ["T-1 has unknown dependencies: T-404"])

    def test_rejects_cycle(self) -> None:
        goal = {
            "id": "G-1",
            "objective": "Demo",
            "tasks": [
                {"id": "T-1", "objective": "First", "depends_on": ["T-2"]},
                {"id": "T-2", "objective": "Second", "depends_on": ["T-1"]},
            ],
        }

        with self.assertRaisesRegex(ValidationError, "contains a cycle"):
            topological_tasks(goal)
        self.assertEqual(validate_goal(goal), ["task dependency graph contains a cycle"])


if __name__ == "__main__":
    unittest.main()
