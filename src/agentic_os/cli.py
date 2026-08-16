from __future__ import annotations

import argparse
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from agentic_os import __version__
from agentic_os.core import (
    ValidationError,
    goal_digest,
    load_yaml,
    validate_config,
    validate_goal,
)
from agentic_os.engine import GoalRunner, RunnerError, run_mock_goal
from agentic_os.gitops import GitError, GitOperations, run_command
from agentic_os.state import StateStore
from agentic_os.watcher import VerificationWatcher


def find_project_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "agentic.yaml").is_file():
            return candidate
    raise ValidationError("No agentic.yaml found in this directory or its parents")


def project_context() -> tuple[Path, dict, StateStore, GitOperations]:
    root = find_project_root()
    config = load_yaml(root / "agentic.yaml")
    validate_config(config)
    state_path = root / config["runtime"]["state"]
    worktree_root = root / config["runtime"]["worktrees"]
    return root, config, StateStore(state_path), GitOperations(root, worktree_root)


def command_doctor(_: argparse.Namespace) -> int:
    root, config, state, git = project_context()
    git.ensure_repository()
    checks = [
        ("project root", str(root)),
        ("configuration", "valid"),
        ("git", shutil.which("git") or "missing"),
        ("git head", git.head()),
        ("runtime database", str(state.path)),
        ("default provider", config["models"]["default"]["provider"]),
        ("main merge", config["autonomy"]["main_merge"]),
    ]
    git_ok = shutil.which("git") is not None
    for label, value in checks:
        marker = "OK" if value != "missing" else "FAIL"
        print(f"[{marker}] {label}: {value}")
    return 0 if git_ok else 1


def command_worktree_create(args: argparse.Namespace) -> int:
    _, config, state, git = project_context()
    profile = config.get("models", {}).get(args.model)
    if not isinstance(profile, dict):
        raise ValidationError(f"Unknown model profile: {args.model}")
    if not git.is_clean(git.root):
        raise ValidationError("Main checkout must be clean before creating a worktree")
    existing = state.get_worktree(args.name)
    if existing:
        raise ValidationError(f"Worktree already registered: {args.name}")
    path, base_sha = git.create_worktree(args.name, args.branch, base_ref=args.base)
    try:
        state.create_worktree(
            name=args.name,
            path=str(path),
            branch=args.branch,
            model=args.model,
            mode=args.mode,
            base_sha=base_sha,
        )
    except Exception:
        if git.is_clean(path):
            git.remove_worktree(path)
        raise
    state.append_event(
        "worktree.created",
        payload={
            "name": args.name,
            "path": str(path),
            "branch": args.branch,
            "model": args.model,
            "mode": args.mode,
            "base_sha": base_sha,
        },
    )
    print(f"Worktree created: {args.name}")
    print(f"Path: {path}")
    print(f"Branch: {args.branch}")
    print(f"Model: {args.model}")
    print(f"Mode: {args.mode}")
    return 0


def command_worktree_list(_: argparse.Namespace) -> int:
    _, _, state, _ = project_context()
    worktrees = state.list_worktrees()
    if not worktrees:
        print("No managed worktrees.")
        return 0
    for item in worktrees:
        print(
            f"{item['name']}  {item['branch']}  {item['model']}  "
            f"{item['mode']}  {item['status']}"
        )
    return 0


def command_worktree_inspect(args: argparse.Namespace) -> int:
    _, _, state, git = project_context()
    item = _require_worktree(state, args.name)
    path = Path(item["path"])
    lease = state.get_lease(args.name)
    for key in ("name", "path", "branch", "model", "mode", "status", "base_sha"):
        print(f"{key}: {item[key]}")
    print(f"head_sha: {git.head(path)}")
    print(f"clean: {str(git.is_clean(path)).lower()}")
    print(f"lease: {lease['holder_run_id'] if lease else 'none'}")
    return 0


def command_worktree_remove(args: argparse.Namespace) -> int:
    if not args.yes:
        raise ValidationError("Worktree removal requires --yes")
    _, _, state, git = project_context()
    item = _require_worktree(state, args.name)
    if state.get_lease(args.name):
        raise ValidationError("Cannot remove a leased worktree")
    git.remove_worktree(Path(item["path"]))
    state.update_worktree(args.name, status="REMOVED")
    state.append_event(
        "worktree.removed", payload={"name": args.name, "branch_preserved": True}
    )
    print(f"Worktree removed: {args.name}")
    print(f"Branch preserved: {item['branch']}")
    return 0


def command_context(args: argparse.Namespace) -> int:
    _, _, state, git = project_context()
    current = _require_worktree(state, args.worktree)
    print(f"Worktree: {current['name']}")
    print(f"Branch: {current['branch']}")
    print(f"Model: {current['model']}")
    print(f"Mode: {current['mode']}")
    print(f"Status: {current['status']}")
    print(f"Head: {git.head(Path(current['path']))}")
    print("\nGoals:")
    matched = [goal for goal in state.list_goals() if goal["worktree_name"] == args.worktree]
    if not matched:
        print("  none")
    for goal in matched:
        print(f"  {goal['goal_id']}@{goal['version']}  {goal['status']}")
        print(f"    objective: {goal['snapshot']['objective']}")
        run = state.latest_run_for_goal(goal["goal_id"])
        task_statuses = (
            {task["task_id"]: task["status"] for task in state.list_tasks(run["id"])}
            if run
            else {}
        )
        for task in goal["snapshot"]["tasks"]:
            dependencies = ",".join(task.get("depends_on", [])) or "-"
            print(
                f"    {task['id']}  {task_statuses.get(task['id'], 'PLANNED')}  "
                f"depends={dependencies}  {task['objective']}"
            )
    print("\nOther agents/worktrees:")
    siblings = [item for item in state.list_worktrees() if item["name"] != args.worktree]
    if not siblings:
        print("  none")
    for item in siblings:
        path = Path(item["path"])
        head = git.head(path) if path.exists() else "missing"
        print(
            f"  {item['name']}  {item['branch']}  {item['model']}  "
            f"{item['status']}  head={head}"
        )
    return 0


def command_chat(args: argparse.Namespace) -> int:
    """Open a user-controlled model session inside a managed worktree."""
    _, config, state, _ = project_context()
    worktree = _require_worktree(state, args.worktree)
    if state.get_lease(args.worktree):
        raise ValidationError("Cannot open an interactive session while a goal owns the worktree")
    profile = config.get("models", {}).get(worktree["model"])
    if not isinstance(profile, dict):
        raise ValidationError(f"Unknown model profile: {worktree['model']}")
    command = profile.get("interactive_command")
    print(f"Worktree: {worktree['path']}")
    print(f"Model profile: {worktree['model']}")
    print("Autonomy: disabled; approve and arm a goal explicitly when ready.")
    if not command:
        if profile.get("provider") == "mock":
            print("Mock profile: no external interactive process was started.")
            return 0
        raise ValidationError(
            f"Model profile {worktree['model']} has no interactive_command"
        )
    if not isinstance(command, list) or not command or not all(
        isinstance(part, str) and part for part in command
    ):
        raise ValidationError("interactive_command must be a non-empty string array")
    result = subprocess.run(command, cwd=worktree["path"], check=False)
    return result.returncode


def command_goal_validate(args: argparse.Namespace) -> int:
    root, _, _, _ = project_context()
    path = _resolve_path(root, args.path)
    goal = load_yaml(path)
    errors = validate_goal(goal)
    if errors:
        print(f"Goal is invalid: {path}")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"Goal is valid: {goal['id']} ({len(goal['tasks'])} tasks)")
    return 0


def command_goal_approve(args: argparse.Namespace) -> int:
    root, _, state, _ = project_context()
    worktree = _require_worktree(state, args.worktree)
    if worktree["mode"] == "goal" and worktree["status"] == "RUNNING":
        raise ValidationError("Cannot approve a new goal while the worktree is running")
    path = _resolve_path(root, args.path)
    goal = load_yaml(path)
    errors = validate_goal(goal)
    if errors:
        raise ValidationError("Cannot approve invalid goal: " + "; ".join(errors))
    version = state.approve_goal(
        goal_id=goal["id"],
        path=str(path.relative_to(root)),
        digest=goal_digest(goal),
        worktree_name=args.worktree,
        snapshot=goal,
    )
    state.append_event(
        "goal.approved",
        goal_id=goal["id"],
        payload={"version": version, "worktree": args.worktree},
    )
    print(f"Goal approved: {goal['id']}@{version}")
    print(f"Worktree: {args.worktree}")
    print("Autonomous execution has not started.")
    return 0


def command_goal_arm(args: argparse.Namespace) -> int:
    _, _, state, git = project_context()
    goal = _require_goal(state, args.goal, args.version)
    if goal["status"] != "APPROVED":
        raise ValidationError(f"Goal cannot be armed from {goal['status']}")
    worktree = _require_worktree(state, goal["worktree_name"])
    path = Path(worktree["path"])
    if not git.is_clean(path):
        raise ValidationError("Worktree must be clean before arming a goal")
    if git.branch(path) != worktree["branch"]:
        raise ValidationError("Worktree branch does not match its registration")
    state.set_goal_status(goal["goal_id"], goal["version"], "ARMED")
    state.update_worktree(worktree["name"], mode="goal", status="ARMED")
    state.append_event(
        "goal.armed",
        goal_id=goal["goal_id"],
        payload={"version": goal["version"], "head_sha": git.head(path)},
    )
    print(f"Goal armed: {goal['goal_id']}@{goal['version']}")
    print("Run explicitly with: agentic goal run " + goal["goal_id"])
    return 0


def command_goal_run(args: argparse.Namespace) -> int:
    root, config, state, git = project_context()
    goal = _require_goal(state, args.goal, args.version)
    runner = GoalRunner(root=root, config=config, state=state, git=git)
    run_id = runner.start(goal)
    run = state.get_run(run_id)
    print(f"Run: {run_id}")
    print(f"Status: {run['status']}")
    print(f"Cost: ${run['cost_usd']:.4f}")
    return 0


def command_goal_pause(args: argparse.Namespace) -> int:
    _, _, state, _ = project_context()
    run = _require_active_run(state, args.goal)
    state.set_run_status(run["id"], "PAUSE_REQUESTED")
    state.append_event(
        "goal.pause.requested", goal_id=args.goal, run_id=run["id"]
    )
    print(f"Pause requested: {run['id']}")
    return 0


def command_goal_resume(args: argparse.Namespace) -> int:
    root, config, state, git = project_context()
    run = state.latest_run_for_goal(args.goal, ("PAUSED", "APPROVAL_REQUIRED"))
    if not run:
        raise ValidationError(f"No resumable run for goal {args.goal}")
    if args.budget_usd is not None:
        if args.budget_usd <= float(run["budget_usd"]):
            raise ValidationError("New budget must be greater than the current budget")
        state.set_run_budget(run["id"], args.budget_usd)
        state.append_event(
            "budget.approved",
            goal_id=args.goal,
            run_id=run["id"],
            payload={"budget_usd": args.budget_usd},
        )
    runner = GoalRunner(root=root, config=config, state=state, git=git)
    runner.resume(run["id"])
    updated = state.get_run(run["id"])
    print(f"Run resumed: {run['id']}")
    print(f"Status: {updated['status']}")
    return 0


def command_goal_stop(args: argparse.Namespace) -> int:
    _, _, state, _ = project_context()
    run = _require_active_run(state, args.goal)
    state.set_run_status(run["id"], "STOP_REQUESTED")
    state.append_event("goal.stop.requested", goal_id=args.goal, run_id=run["id"])
    print(f"Stop requested: {run['id']}")
    return 0


def command_goal_status(args: argparse.Namespace) -> int:
    _, _, state, _ = project_context()
    goal = _require_goal(state, args.goal, args.version)
    print(f"Goal: {goal['goal_id']}@{goal['version']}")
    print(f"Status: {goal['status']}")
    print(f"Worktree: {goal['worktree_name']}")
    print(f"Digest: {goal['digest']}")
    run = state.latest_run_for_goal(goal["goal_id"])
    if run:
        print(f"Run: {run['id']} ({run['status']})")
        print(f"Cost: ${run['cost_usd']:.4f} / ${run['budget_usd']:.4f}")
        for task in state.list_tasks(run["id"]):
            print(
                f"  {task['task_id']}  {task['status']}  attempts={task['attempts']}  "
                f"head={task['head_sha'] or '-'}"
            )
    return 0


def command_goal_integrate(args: argparse.Namespace) -> int:
    root, config, state, git = project_context()
    goal = _require_goal(state, args.goal, args.version)
    if goal["status"] != "READY_FOR_INTEGRATION":
        raise ValidationError(f"Goal is not integration-ready: {goal['status']}")
    run = state.latest_run_for_goal(goal["goal_id"], ("READY_FOR_INTEGRATION",))
    if not run:
        raise ValidationError("No integration-ready run found")
    worktree = _require_worktree(state, goal["worktree_name"])
    branch = args.branch or f"integration/{goal['goal_id'].lower()}-v{goal['version']}"
    if branch in {"main", "master"}:
        raise ValidationError("Direct integration into the main branch is forbidden")
    integration_root = root / config["runtime"].get(
        "integrations", ".agentic/integrations"
    )
    path, head = git.create_integration(
        integration_root=integration_root,
        integration_branch=branch,
        base_sha=run["base_sha"],
        source_branch=worktree["branch"],
    )
    try:
        checks = [
            *config.get("verification", {}).get("commands", []),
            *goal["snapshot"].get("integration_checks", []),
        ]
        for command in checks:
            run_command(command, cwd=path, timeout=600)
    except Exception as exc:
        state.set_goal_status(goal["goal_id"], goal["version"], "BLOCKED")
        state.append_event(
            "integration.failed",
            goal_id=goal["goal_id"],
            run_id=run["id"],
            payload={"branch": branch, "error": str(exc)},
        )
        raise
    state.set_goal_status(goal["goal_id"], goal["version"], "INTEGRATED")
    state.set_run_status(run["id"], "INTEGRATED")
    state.append_event(
        "integration.completed",
        goal_id=goal["goal_id"],
        run_id=run["id"],
        payload={"branch": branch, "head_sha": head, "path": str(path)},
    )
    print(f"Integrated: {goal['goal_id']}@{goal['version']}")
    print(f"Branch: {branch}")
    print(f"Head: {head}")
    print("Main branch was not modified.")
    return 0


def command_recover(args: argparse.Namespace) -> int:
    _, _, state, _ = project_context()
    released = set(state.release_expired_leases())
    recovered: list[str] = []
    for run in state.list_runs(limit=1000):
        if run["status"] not in {"RUNNING", "PAUSE_REQUESTED", "STOP_REQUESTED"}:
            continue
        if args.force or run["id"] in released:
            state.set_run_status(run["id"], "PAUSED", "recovered after interrupted process")
            goal = state.get_goal(run["goal_id"], int(run["goal_version"]))
            if goal:
                state.set_goal_status(goal["goal_id"], goal["version"], "PAUSED")
            if run["worktree_name"]:
                state.release_lease(run["worktree_name"], run["id"])
                state.update_worktree(run["worktree_name"], status="PAUSED")
            state.append_event(
                "run.recovered",
                goal_id=run["goal_id"],
                run_id=run["id"],
            )
            recovered.append(run["id"])
    print(f"Recovered runs: {len(recovered)}")
    for run_id in recovered:
        print(run_id)
    return 0


def command_watch(args: argparse.Namespace) -> int:
    _, config, state, git = project_context()
    watcher = VerificationWatcher(config=config, state=state, git=git)
    if args.once:
        results = watcher.run_once()
    else:
        if args.duration is None:
            raise ValidationError("watch requires --once or --duration")
        results = watcher.run_for(args.duration, args.interval)
    if not results:
        print("No new worktree heads to verify.")
    for result in results:
        print(
            f"{result.worktree}  {result.head_sha[:12]}  {result.status}"
            + (f"  {result.details}" if result.details else "")
        )
    return 1 if any(result.status == "FAILED" for result in results) else 0


def command_demo(args: argparse.Namespace) -> int:
    root, _, state, _ = project_context()
    path = _resolve_path(root, args.goal)
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
    _, _, state, _ = project_context()
    runs = state.list_runs(args.limit)
    if not runs:
        print("No runs recorded.")
        return 0
    for run in runs:
        print(
            f"{run['id']}  {run['goal_id']}@{run['goal_version']}  {run['status']}  "
            f"${run['cost_usd']:.4f}  {run['updated_at']}"
        )
    return 0


def command_events(args: argparse.Namespace) -> int:
    _, _, state, _ = project_context()
    for event in state.list_events(args.limit):
        print(
            f"{event['id']:04d} {event['occurred_at']} {event['event_type']} "
            f"goal={event['goal_id'] or '-'} run={event['run_id'] or '-'} "
            f"{event['payload']}"
        )
    return 0


def command_version(_: argparse.Namespace) -> int:
    print(__version__)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentic", description="Ortak Agentic OS")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Validate the local setup")
    doctor.set_defaults(handler=command_doctor)

    worktree = subparsers.add_parser("worktree", help="Manage isolated worktrees")
    worktree_sub = worktree.add_subparsers(dest="worktree_command", required=True)
    create = worktree_sub.add_parser("create")
    create.add_argument("name")
    create.add_argument("--branch", required=True)
    create.add_argument("--model", default="default")
    create.add_argument("--mode", choices=("plan", "interactive"), default="interactive")
    create.add_argument("--base", default="HEAD")
    create.set_defaults(handler=command_worktree_create)
    listing = worktree_sub.add_parser("list")
    listing.set_defaults(handler=command_worktree_list)
    inspect = worktree_sub.add_parser("inspect")
    inspect.add_argument("name")
    inspect.set_defaults(handler=command_worktree_inspect)
    remove = worktree_sub.add_parser("remove")
    remove.add_argument("name")
    remove.add_argument("--yes", action="store_true")
    remove.set_defaults(handler=command_worktree_remove)

    context = subparsers.add_parser("context", help="Show one agent's relevant shared state")
    context.add_argument("worktree")
    context.set_defaults(handler=command_context)

    chat = subparsers.add_parser(
        "chat", help="Open a user-controlled model session in a worktree"
    )
    chat.add_argument("worktree")
    chat.set_defaults(handler=command_chat)

    goal = subparsers.add_parser("goal", help="Manage versioned goals")
    goal_sub = goal.add_subparsers(dest="goal_command", required=True)
    validate = goal_sub.add_parser("validate")
    validate.add_argument("path")
    validate.set_defaults(handler=command_goal_validate)
    approve = goal_sub.add_parser("approve")
    approve.add_argument("path")
    approve.add_argument("--worktree", required=True)
    approve.set_defaults(handler=command_goal_approve)
    arm = goal_sub.add_parser("arm")
    arm.add_argument("goal")
    arm.add_argument("--version", type=int)
    arm.set_defaults(handler=command_goal_arm)
    run = goal_sub.add_parser("run")
    run.add_argument("goal")
    run.add_argument("--version", type=int)
    run.set_defaults(handler=command_goal_run)
    pause = goal_sub.add_parser("pause")
    pause.add_argument("goal")
    pause.set_defaults(handler=command_goal_pause)
    resume = goal_sub.add_parser("resume")
    resume.add_argument("goal")
    resume.add_argument("--budget-usd", type=float)
    resume.set_defaults(handler=command_goal_resume)
    stop = goal_sub.add_parser("stop")
    stop.add_argument("goal")
    stop.set_defaults(handler=command_goal_stop)
    goal_status = goal_sub.add_parser("status")
    goal_status.add_argument("goal")
    goal_status.add_argument("--version", type=int)
    goal_status.set_defaults(handler=command_goal_status)
    integrate = goal_sub.add_parser("integrate")
    integrate.add_argument("goal")
    integrate.add_argument("--version", type=int)
    integrate.add_argument("--branch")
    integrate.set_defaults(handler=command_goal_integrate)

    recover = subparsers.add_parser("recover", help="Recover interrupted runs")
    recover.add_argument("--force", action="store_true")
    recover.set_defaults(handler=command_recover)
    watch = subparsers.add_parser("watch", help="Verify newly published worktree heads")
    watch.add_argument("--once", action="store_true")
    watch.add_argument("--duration", type=float)
    watch.add_argument("--interval", type=float, default=30)
    watch.set_defaults(handler=command_watch)
    events = subparsers.add_parser("events", help="Show append-only runtime events")
    events.add_argument("--limit", type=int, default=30)
    events.set_defaults(handler=command_events)
    demo = subparsers.add_parser("demo", help="Run the logic-only mock loop")
    demo.add_argument("--goal", default="goals/demo.yaml")
    demo.set_defaults(handler=command_demo)
    status = subparsers.add_parser("status", help="Show recent runs")
    status.add_argument("--limit", type=int, default=20)
    status.set_defaults(handler=command_status)
    version = subparsers.add_parser("version", help="Show the CLI version")
    version.set_defaults(handler=command_version)
    return parser


def _resolve_path(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def _require_worktree(state: StateStore, name: str) -> dict:
    worktree = state.get_worktree(name)
    if not worktree or worktree["status"] == "REMOVED":
        raise ValidationError(f"Unknown worktree: {name}")
    return worktree


def _require_goal(state: StateStore, goal_id: str, version: int | None) -> dict:
    goal = state.get_goal(goal_id, version)
    if not goal:
        suffix = f"@{version}" if version is not None else ""
        raise ValidationError(f"Unknown goal: {goal_id}{suffix}")
    return goal


def _require_active_run(state: StateStore, goal_id: str) -> dict:
    run = state.latest_run_for_goal(
        goal_id, ("RUNNING", "PAUSE_REQUESTED", "STOP_REQUESTED")
    )
    if not run:
        raise ValidationError(f"No active run for goal {goal_id}")
    return run


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return_code = args.handler(args)
    except (
        ValidationError,
        RunnerError,
        GitError,
        KeyError,
        ValueError,
        RuntimeError,
        OSError,
        sqlite3.Error,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return_code = 1
    raise SystemExit(return_code)
