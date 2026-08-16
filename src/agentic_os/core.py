from __future__ import annotations

from collections import deque
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
    if not isinstance(runtime, dict) or not runtime.get("state"):
        raise ValidationError("agentic.yaml must define runtime.state")


def validate_goal(goal: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    if not _non_empty_string(goal.get("id")):
        errors.append("goal.id must be a non-empty string")
    if not _non_empty_string(goal.get("objective")):
        errors.append("goal.objective must be a non-empty string")

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

