from __future__ import annotations

import unittest

from agentic_os.core import ValidationError, topological_tasks, validate_goal


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

