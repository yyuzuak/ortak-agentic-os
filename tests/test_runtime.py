from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agentic_os.engine import run_mock_goal
from agentic_os.state import StateStore


class RuntimeTests(unittest.TestCase):
    @staticmethod
    def journal_mode(path: Path) -> str:
        connection = sqlite3.connect(path)
        try:
            return connection.execute("PRAGMA journal_mode").fetchone()[0].lower()
        finally:
            connection.close()

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

    def test_state_uses_wal_and_migrates_older_databases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            StateStore(path)
            self.assertEqual(self.journal_mode(path), "wal")

            # A database created before WAL was adopted is converted on open.
            legacy = Path(directory) / "legacy.sqlite"
            connection = sqlite3.connect(legacy)
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.close()
            self.assertEqual(self.journal_mode(legacy), "delete")

            StateStore(legacy)
            self.assertEqual(self.journal_mode(legacy), "wal")

    def test_an_open_reader_does_not_block_writes(self) -> None:
        """Under a rollback journal this write would wait out busy_timeout."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            state = StateStore(path)
            state.append_event("seed")

            reader = sqlite3.connect(path, timeout=5)
            reader.execute("BEGIN")
            reader.execute("SELECT * FROM events").fetchall()
            try:
                started = time.monotonic()
                state.append_event("written-while-read-open")
                elapsed = time.monotonic() - started
            finally:
                reader.rollback()
                reader.close()

            self.assertLess(elapsed, 5)
            self.assertEqual(
                [event["event_type"] for event in state.list_events()],
                ["seed", "written-while-read-open"],
            )

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
