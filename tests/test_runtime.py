from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from agentic_os.engine import run_mock_goal
from agentic_os.state import StateStore


class RuntimeTests(unittest.TestCase):
    def test_mock_goal_reaches_integration_ready(self) -> None:
        goal = {
            "id": "G-1",
            "objective": "Demo",
            "tasks": [
                {"id": "T-1", "objective": "First"},
                {"id": "T-2", "objective": "Second", "depends_on": ["T-1"]},
            ],
        }

        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite")
            run_id = run_mock_goal(goal, state)

            runs = state.list_runs()
            self.assertEqual(runs[0]["id"], run_id)
            self.assertEqual(runs[0]["status"], "READY_FOR_INTEGRATION")

            event_types = [event["event_type"] for event in state.list_events()]
            self.assertEqual(event_types[0], "goal.run.started")
            self.assertEqual(event_types[-1], "goal.ready_for_integration")
            self.assertEqual(event_types.count("task.completed"), 2)

    def test_parallel_initialization_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            errors: list[Exception] = []

            def initialize() -> None:
                try:
                    StateStore(path).list_runs()
                except Exception as exc:  # pragma: no cover - assertion records it
                    errors.append(exc)

            threads = [threading.Thread(target=initialize) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
