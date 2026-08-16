from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    goal_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    goal_id TEXT,
                    run_id TEXT,
                    payload TEXT NOT NULL
                );
                """
            )

    def create_run(self, run_id: str, goal_id: str, status: str = "RUNNING") -> None:
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO runs(id, goal_id, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (run_id, goal_id, status, now, now),
            )

    def set_run_status(self, run_id: str, status: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE runs SET status = ?, updated_at = ? WHERE id = ?",
                (status, utc_now(), run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown run: {run_id}")

    def append_event(
        self,
        event_type: str,
        *,
        goal_id: str | None = None,
        run_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO events(occurred_at, event_type, goal_id, run_id, payload)
                VALUES (?, ?, ?, ?, ?)
                """,
                (utc_now(), event_type, goal_id, run_id, json.dumps(payload or {}, sort_keys=True)),
            )

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, goal_id, status, created_at, updated_at FROM runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_events(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, occurred_at, event_type, goal_id, run_id, payload
                FROM events ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        events = [dict(row) for row in reversed(rows)]
        for event in events:
            event["payload"] = json.loads(event["payload"])
        return events
