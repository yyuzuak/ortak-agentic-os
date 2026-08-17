from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class ClosingConnection(sqlite3.Connection):
    """Commit or roll back like sqlite3.Connection, then always close."""

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        result = super().__exit__(exc_type, exc, traceback)
        self.close()
        return result


class StateStore:
    """SQLite-backed source of truth for runtime state and append-only events."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path, timeout=30.0, factory=ClosingConnection
        )
        connection.row_factory = sqlite3.Row
        # busy_timeout first: the journal_mode switch needs a brief exclusive
        # lock, and every other process may already be reading.
        connection.execute("PRAGMA busy_timeout = 30000")
        # Several processes share this database - a goal run per worktree, chat
        # sessions, the watcher. Under WAL a writer no longer blocks readers,
        # so `agentic status` and `events` stay responsive during a run. This
        # is a no-op once the database file is already in WAL mode, and it
        # migrates databases created before WAL was adopted.
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS worktrees (
                    name TEXT PRIMARY KEY,
                    path TEXT NOT NULL,
                    branch TEXT NOT NULL UNIQUE,
                    model TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    base_sha TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS goals (
                    goal_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    worktree_name TEXT NOT NULL,
                    snapshot TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(goal_id, version),
                    FOREIGN KEY(worktree_name) REFERENCES worktrees(name)
                );

                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    goal_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    objective TEXT NOT NULL,
                    status TEXT NOT NULL,
                    depends_on TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    head_sha TEXT,
                    PRIMARY KEY(run_id, task_id),
                    FOREIGN KEY(run_id) REFERENCES runs(id)
                );

                CREATE TABLE IF NOT EXISTS leases (
                    worktree_name TEXT PRIMARY KEY,
                    holder_run_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS verifications (
                    worktree_name TEXT NOT NULL,
                    head_sha TEXT NOT NULL,
                    status TEXT NOT NULL,
                    details TEXT NOT NULL,
                    checked_at TEXT NOT NULL,
                    PRIMARY KEY(worktree_name, head_sha)
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
            self._migrate_runs(connection)

    @staticmethod
    def _migrate_runs(connection: sqlite3.Connection) -> None:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
        additions = {
            "goal_version": "INTEGER NOT NULL DEFAULT 1",
            "worktree_name": "TEXT",
            "provider": "TEXT NOT NULL DEFAULT 'mock'",
            "base_sha": "TEXT",
            "budget_usd": "REAL NOT NULL DEFAULT 0",
            "cost_usd": "REAL NOT NULL DEFAULT 0",
            "current_task": "TEXT",
            "error": "TEXT",
        }
        for column, definition in additions.items():
            if column not in columns:
                try:
                    connection.execute(f"ALTER TABLE runs ADD COLUMN {column} {definition}")
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise

    def create_worktree(
        self,
        *,
        name: str,
        path: str,
        branch: str,
        model: str,
        mode: str,
        base_sha: str,
    ) -> None:
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO worktrees(name, path, branch, model, mode, status, base_sha, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'READY', ?, ?, ?)
                """,
                (name, path, branch, model, mode, base_sha, now, now),
            )

    def get_worktree(self, name: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM worktrees WHERE name = ?", (name,)
            ).fetchone()
        return dict(row) if row else None

    def list_worktrees(self, include_removed: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM worktrees"
        params: tuple[Any, ...] = ()
        if not include_removed:
            query += " WHERE status != ?"
            params = ("REMOVED",)
        query += " ORDER BY created_at"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def update_worktree(self, name: str, **fields: Any) -> None:
        allowed = {"model", "mode", "status", "base_sha", "path"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        updates["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = [*updates.values(), name]
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE worktrees SET {assignments} WHERE name = ?", values
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown worktree: {name}")

    def approve_goal(
        self,
        *,
        goal_id: str,
        path: str,
        digest: str,
        worktree_name: str,
        snapshot: dict[str, Any],
    ) -> int:
        now = utc_now()
        encoded = json.dumps(snapshot, sort_keys=True)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM goals WHERE goal_id = ?",
                (goal_id,),
            ).fetchone()
            version = int(row["version"]) + 1
            connection.execute(
                """
                INSERT INTO goals(goal_id, version, path, digest, status, worktree_name, snapshot, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'APPROVED', ?, ?, ?, ?)
                """,
                (goal_id, version, path, digest, worktree_name, encoded, now, now),
            )
        return version

    def get_goal(self, goal_id: str, version: int | None = None) -> dict[str, Any] | None:
        query = "SELECT * FROM goals WHERE goal_id = ?"
        params: list[Any] = [goal_id]
        if version is None:
            query += " ORDER BY version DESC LIMIT 1"
        else:
            query += " AND version = ?"
            params.append(version)
        with self._connect() as connection:
            row = connection.execute(query, params).fetchone()
        if not row:
            return None
        goal = dict(row)
        goal["snapshot"] = json.loads(goal["snapshot"])
        return goal

    def set_goal_status(self, goal_id: str, version: int, status: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE goals SET status = ?, updated_at = ? WHERE goal_id = ? AND version = ?",
                (status, utc_now(), goal_id, version),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown goal: {goal_id}@{version}")

    def list_goals(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM goals ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        goals = [dict(row) for row in rows]
        for goal in goals:
            goal["snapshot"] = json.loads(goal["snapshot"])
        return goals

    def create_run(
        self,
        run_id: str,
        goal_id: str,
        status: str = "RUNNING",
        *,
        goal_version: int = 1,
        worktree_name: str | None = None,
        provider: str = "mock",
        base_sha: str | None = None,
        budget_usd: float = 0,
    ) -> None:
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO runs(
                    id, goal_id, status, created_at, updated_at, goal_version,
                    worktree_name, provider, base_sha, budget_usd, cost_usd
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    run_id,
                    goal_id,
                    status,
                    now,
                    now,
                    goal_version,
                    worktree_name,
                    provider,
                    base_sha,
                    budget_usd,
                ),
            )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def latest_run_for_goal(
        self, goal_id: str, statuses: tuple[str, ...] | None = None
    ) -> dict[str, Any] | None:
        query = "SELECT * FROM runs WHERE goal_id = ?"
        params: list[Any] = [goal_id]
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            query += f" AND status IN ({placeholders})"
            params.extend(statuses)
        query += " ORDER BY created_at DESC LIMIT 1"
        with self._connect() as connection:
            row = connection.execute(query, params).fetchone()
        return dict(row) if row else None

    def set_run_status(self, run_id: str, status: str, error: str | None = None) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE runs SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                (status, error, utc_now(), run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown run: {run_id}")

    def set_current_task(self, run_id: str, task_id: str | None) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE runs SET current_task = ?, updated_at = ? WHERE id = ?",
                (task_id, utc_now(), run_id),
            )

    def add_run_cost(self, run_id: str, amount: float) -> float:
        with self._connect() as connection:
            connection.execute(
                "UPDATE runs SET cost_usd = cost_usd + ?, updated_at = ? WHERE id = ?",
                (amount, utc_now(), run_id),
            )
            row = connection.execute(
                "SELECT cost_usd FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
        if not row:
            raise KeyError(f"Unknown run: {run_id}")
        return float(row["cost_usd"])

    def set_run_budget(self, run_id: str, budget_usd: float) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE runs SET budget_usd = ?, updated_at = ? WHERE id = ?",
                (budget_usd, utc_now(), run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown run: {run_id}")

    def create_tasks(self, run_id: str, tasks: list[dict[str, Any]]) -> None:
        with self._connect() as connection:
            for sequence, task in enumerate(tasks):
                connection.execute(
                    """
                    INSERT OR IGNORE INTO tasks(
                        run_id, task_id, sequence, objective, status, depends_on, payload
                    ) VALUES (?, ?, ?, ?, 'PENDING', ?, ?)
                    """,
                    (
                        run_id,
                        task["id"],
                        sequence,
                        task["objective"],
                        json.dumps(task.get("depends_on", []), sort_keys=True),
                        json.dumps(task, sort_keys=True),
                    ),
                )

    def list_tasks(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE run_id = ? ORDER BY sequence", (run_id,)
            ).fetchall()
        tasks = [dict(row) for row in rows]
        for task in tasks:
            task["depends_on"] = json.loads(task["depends_on"])
            task["payload"] = json.loads(task["payload"])
        return tasks

    def update_task(
        self,
        run_id: str,
        task_id: str,
        *,
        status: str | None = None,
        attempts: int | None = None,
        head_sha: str | None = None,
    ) -> None:
        updates: dict[str, Any] = {}
        if status is not None:
            updates["status"] = status
        if attempts is not None:
            updates["attempts"] = attempts
        if head_sha is not None:
            updates["head_sha"] = head_sha
        if not updates:
            return
        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = [*updates.values(), run_id, task_id]
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE tasks SET {assignments} WHERE run_id = ? AND task_id = ?", values
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown task: {run_id}/{task_id}")

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def acquire_lease(
        self,
        worktree_name: str,
        run_id: str,
        ttl_seconds: int = 300,
        max_parallel: int | None = None,
    ) -> None:
        now = datetime.now(UTC)
        expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM leases WHERE expires_at <= ?",
                (now.isoformat(timespec="seconds"),),
            )
            row = connection.execute(
                "SELECT * FROM leases WHERE worktree_name = ?", (worktree_name,)
            ).fetchone()
            if row and row["holder_run_id"] != run_id:
                expiry = datetime.fromisoformat(row["expires_at"])
                if expiry > now:
                    raise RuntimeError(
                        f"Worktree {worktree_name} is leased by {row['holder_run_id']}"
                    )
            if max_parallel is not None and (not row or row["holder_run_id"] != run_id):
                active = connection.execute(
                    "SELECT COUNT(*) FROM leases WHERE holder_run_id != ?", (run_id,)
                ).fetchone()[0]
                if active >= max_parallel:
                    raise RuntimeError(
                        f"Parallel agent limit reached: {active}/{max_parallel}"
                    )
            connection.execute(
                """
                INSERT INTO leases(worktree_name, holder_run_id, expires_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(worktree_name) DO UPDATE SET
                  holder_run_id = excluded.holder_run_id,
                  expires_at = excluded.expires_at,
                  updated_at = excluded.updated_at
                """,
                (worktree_name, run_id, expires_at, utc_now()),
            )

    def renew_lease(self, worktree_name: str, run_id: str, ttl_seconds: int = 300) -> None:
        expires_at = (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat(
            timespec="seconds"
        )
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE leases SET expires_at = ?, updated_at = ?
                WHERE worktree_name = ? AND holder_run_id = ?
                """,
                (expires_at, utc_now(), worktree_name, run_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"Lease lost for worktree {worktree_name}")

    def release_lease(self, worktree_name: str, run_id: str | None = None) -> None:
        query = "DELETE FROM leases WHERE worktree_name = ?"
        params: list[Any] = [worktree_name]
        if run_id is not None:
            query += " AND holder_run_id = ?"
            params.append(run_id)
        with self._connect() as connection:
            connection.execute(query, params)

    def get_lease(self, worktree_name: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM leases WHERE worktree_name = ?", (worktree_name,)
            ).fetchone()
        return dict(row) if row else None

    def release_expired_leases(self) -> list[str]:
        now = datetime.now(UTC)
        released: list[str] = []
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM leases").fetchall()
            for row in rows:
                if datetime.fromisoformat(row["expires_at"]) <= now:
                    connection.execute(
                        "DELETE FROM leases WHERE worktree_name = ?", (row["worktree_name"],)
                    )
                    released.append(row["holder_run_id"])
        return released

    def record_verification(
        self, worktree_name: str, head_sha: str, status: str, details: str = ""
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO verifications(worktree_name, head_sha, status, details, checked_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(worktree_name, head_sha) DO UPDATE SET
                  status = excluded.status,
                  details = excluded.details,
                  checked_at = excluded.checked_at
                """,
                (worktree_name, head_sha, status, details, utc_now()),
            )

    def get_verification(self, worktree_name: str, head_sha: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM verifications
                WHERE worktree_name = ? AND head_sha = ?
                """,
                (worktree_name, head_sha),
            ).fetchone()
        return dict(row) if row else None

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
