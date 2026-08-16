from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

from agentic_os import __version__
from agentic_os.core import ValidationError, load_yaml, validate_config, validate_goal
from agentic_os.engine import run_mock_goal
from agentic_os.state import StateStore


def find_project_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "agentic.yaml").is_file():
            return candidate
    raise ValidationError("No agentic.yaml found in this directory or its parents")


def project_context() -> tuple[Path, dict, StateStore]:
    root = find_project_root()
    config = load_yaml(root / "agentic.yaml")
    validate_config(config)
    state_path = root / config["runtime"]["state"]
    return root, config, StateStore(state_path)


def command_doctor(_: argparse.Namespace) -> int:
    root, config, state = project_context()
    checks = [
        ("project root", str(root)),
        ("configuration", "valid"),
        ("git", shutil.which("git") or "missing"),
        ("runtime database", str(state.path)),
        ("default provider", config["models"]["default"]["provider"]),
        ("main merge", config["autonomy"]["main_merge"]),
    ]

    git_ok = shutil.which("git") is not None
    for label, value in checks:
        marker = "OK" if value != "missing" else "FAIL"
        print(f"[{marker}] {label}: {value}")
    return 0 if git_ok else 1


def command_goal_validate(args: argparse.Namespace) -> int:
    root, _, _ = project_context()
    path = Path(args.path)
    if not path.is_absolute():
        path = root / path
    goal = load_yaml(path)
    errors = validate_goal(goal)
    if errors:
        print(f"Goal is invalid: {path}")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"Goal is valid: {goal['id']} ({len(goal['tasks'])} tasks)")
    return 0


def command_demo(args: argparse.Namespace) -> int:
    root, _, state = project_context()
    path = Path(args.goal)
    if not path.is_absolute():
        path = root / path
    goal = load_yaml(path)
    errors = validate_goal(goal)
    if errors:
        raise ValidationError("Cannot run invalid goal: " + "; ".join(errors))
    if goal.get("status") != "approved":
        raise ValidationError("Autonomous demo requires goal.status: approved")

    run_id = run_mock_goal(goal, state)
    print(f"Mock run completed: {run_id}")
    print(f"Goal: {goal['id']}")
    print("Status: READY_FOR_INTEGRATION")
    print("Main branch was not modified.")
    return 0


def command_status(args: argparse.Namespace) -> int:
    _, _, state = project_context()
    runs = state.list_runs(args.limit)
    if not runs:
        print("No runs recorded.")
        return 0
    for run in runs:
        print(f"{run['id']}  {run['goal_id']}  {run['status']}  {run['updated_at']}")
    return 0


def command_version(_: argparse.Namespace) -> int:
    print(__version__)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentic", description="Ortak Agentic OS")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Validate the local setup")
    doctor.set_defaults(handler=command_doctor)

    goal = subparsers.add_parser("goal", help="Manage goals")
    goal_subparsers = goal.add_subparsers(dest="goal_command", required=True)
    validate = goal_subparsers.add_parser("validate", help="Validate a goal file")
    validate.add_argument("path")
    validate.set_defaults(handler=command_goal_validate)

    demo = subparsers.add_parser("demo", help="Run the deterministic mock loop")
    demo.add_argument("--goal", default="goals/demo.yaml")
    demo.set_defaults(handler=command_demo)

    status = subparsers.add_parser("status", help="Show recent runs")
    status.add_argument("--limit", type=int, default=20)
    status.set_defaults(handler=command_status)

    version = subparsers.add_parser("version", help="Show the CLI version")
    version.set_defaults(handler=command_version)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        raise_code = args.handler(args)
    except (ValidationError, KeyError, OSError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise_code = 1
    raise SystemExit(raise_code)
