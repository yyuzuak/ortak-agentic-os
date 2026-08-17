from __future__ import annotations

from collections import deque
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

import yaml


class ValidationError(ValueError):
    """Raised when configuration or goal data is invalid."""


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValidationError(f"File not found: {path}")

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValidationError(f"Invalid YAML in {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ValidationError(f"Expected a mapping in {path}")
    return data


def validate_config(config: dict[str, Any]) -> None:
    if config.get("version") != 0:
        raise ValidationError("agentic.yaml must declare version: 0")

    runtime = config.get("runtime")
    if not isinstance(runtime, dict):
        raise ValidationError("agentic.yaml must define runtime")
    for key in ("state", "worktrees"):
        if not _safe_relative_path(runtime.get(key)):
            raise ValidationError(f"runtime.{key} must be a safe relative path")
    if "integrations" in runtime and not _safe_relative_path(runtime["integrations"]):
        raise ValidationError("runtime.integrations must be a safe relative path")

    models = config.get("models")
    if not isinstance(models, dict) or not models:
        raise ValidationError("agentic.yaml must define at least one model profile")
    for name, profile in models.items():
        if not _non_empty_string(name) or not isinstance(profile, dict):
            raise ValidationError("model profiles must be named mappings")
        provider = profile.get("provider")
        if provider not in {"mock", "command"}:
            raise ValidationError(f"models.{name}.provider is unsupported: {provider}")
        provider_defaults = config.get("providers", {}).get(provider, {})
        if provider == "command" and not _command_array(
            profile.get("command", provider_defaults.get("command"))
        ):
            raise ValidationError(f"models.{name}.command must be a command array")
        interactive = profile.get("interactive_command")
        if interactive is not None and not _command_array(interactive):
            raise ValidationError(
                f"models.{name}.interactive_command must be a command array"
            )

    if config.get("autonomy", {}).get("main_merge") != "manual":
        raise ValidationError("autonomy.main_merge must be manual")

    limits = config.get("limits", {})
    for key in ("parallel_agents", "repair_attempts"):
        value = limits.get(key)
        if value is not None and (not isinstance(value, int) or value < 1):
            raise ValidationError(f"limits.{key} must be a positive integer")
    budget = limits.get("budget_usd")
    if budget is not None and (
        not isinstance(budget, (int, float)) or isinstance(budget, bool) or budget < 0
    ):
        raise ValidationError("limits.budget_usd must be a non-negative number")

    loops = config.get("loops", {})
    if not isinstance(loops, dict) or not all(
        _non_empty_string(name) and isinstance(profile, dict)
        for name, profile in loops.items()
    ):
        raise ValidationError("loops must be a mapping of named profiles")
    skills = config.get("skills", {})
    if not isinstance(skills, dict) or not all(
        _non_empty_string(name) and isinstance(value, (str, dict))
        for name, value in skills.items()
    ):
        raise ValidationError("skills must be a mapping of instructions")
    commands = config.get("verification", {}).get("commands", [])
    if not isinstance(commands, list) or not all(_command_array(item) for item in commands):
        raise ValidationError("verification.commands must be a list of command arrays")


def validate_goal(goal: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    if not _non_empty_string(goal.get("id")):
        errors.append("goal.id must be a non-empty string")
    if not _non_empty_string(goal.get("objective")):
        errors.append("goal.objective must be a non-empty string")

    requires = goal.get("requires", [])
    if not isinstance(requires, list) or not all(
        isinstance(item, dict)
        and _non_empty_string(item.get("goal"))
        and _non_empty_string(item.get("status"))
        for item in requires
    ):
        errors.append("goal.requires must contain goal/status mappings")

    skills = goal.get("skills", [])
    if not isinstance(skills, list) or not all(isinstance(item, str) for item in skills):
        errors.append("goal.skills must be a list of skill names")

    loop = goal.get("loop", "default")
    if not isinstance(loop, (str, dict)):
        errors.append("goal.loop must be a profile name or mapping")

    integration_checks = goal.get("integration_checks", [])
    if not isinstance(integration_checks, list) or not all(
        _command_array(command) for command in integration_checks
    ):
        errors.append("goal.integration_checks must be a list of command arrays")

    tasks = goal.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        errors.append("goal.tasks must be a non-empty list")
        return errors

    task_ids: list[str] = []
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            errors.append(f"tasks[{index}] must be a mapping")
            continue
        task_id = task.get("id")
        if not _non_empty_string(task_id):
            errors.append(f"tasks[{index}].id must be a non-empty string")
        else:
            task_ids.append(task_id)
        if not _non_empty_string(task.get("objective")):
            errors.append(f"tasks[{index}].objective must be a non-empty string")

        dependencies = task.get("depends_on", [])
        if not isinstance(dependencies, list) or not all(
            isinstance(item, str) for item in dependencies
        ):
            errors.append(f"tasks[{index}].depends_on must be a list of task IDs")

        checks = task.get("checks", [])
        if not isinstance(checks, list) or not all(
            isinstance(command, list)
            and command
            and all(isinstance(part, str) for part in command)
            for command in checks
        ):
            errors.append(f"tasks[{index}].checks must be a list of command arrays")

        owned_paths = task.get("owned_paths", [])
        if not isinstance(owned_paths, list) or not all(
            isinstance(pattern, str) for pattern in owned_paths
        ):
            errors.append(f"tasks[{index}].owned_paths must be a list of patterns")

        task_skills = task.get("skills", [])
        if not isinstance(task_skills, list) or not all(
            isinstance(skill, str) for skill in task_skills
        ):
            errors.append(f"tasks[{index}].skills must be a list of skill names")

    duplicates = sorted({task_id for task_id in task_ids if task_ids.count(task_id) > 1})
    if duplicates:
        errors.append(f"duplicate task IDs: {', '.join(duplicates)}")

    known = set(task_ids)
    for task in tasks:
        if not isinstance(task, dict):
            continue
        task_id = task.get("id", "<unknown>")
        dependencies = task.get("depends_on", [])
        if not isinstance(dependencies, list):
            continue
        missing = sorted(dep for dep in dependencies if dep not in known)
        if missing:
            errors.append(f"{task_id} has unknown dependencies: {', '.join(missing)}")

    if not errors:
        try:
            topological_tasks(goal)
        except ValidationError as exc:
            errors.append(str(exc))

    return errors


def topological_tasks(goal: dict[str, Any]) -> list[dict[str, Any]]:
    tasks = goal["tasks"]
    by_id = {task["id"]: task for task in tasks}
    indegree = {task_id: 0 for task_id in by_id}
    dependents: dict[str, list[str]] = {task_id: [] for task_id in by_id}

    for task in tasks:
        for dependency in task.get("depends_on", []):
            indegree[task["id"]] += 1
            dependents[dependency].append(task["id"])

    ready = deque(task_id for task_id, degree in indegree.items() if degree == 0)
    ordered: list[dict[str, Any]] = []
    while ready:
        task_id = ready.popleft()
        ordered.append(by_id[task_id])
        for dependent in dependents[task_id]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)

    if len(ordered) != len(tasks):
        raise ValidationError("task dependency graph contains a cycle")
    return ordered


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _safe_relative_path(value: Any) -> bool:
    if not _non_empty_string(value):
        return False
    path = Path(value)
    return not path.is_absolute() and ".." not in path.parts


def _command_array(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(_non_empty_string(part) for part in value)
    )


def goal_digest(goal: dict[str, Any]) -> str:
    canonical = json.dumps(goal, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()
