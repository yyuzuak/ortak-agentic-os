from __future__ import annotations

from uuid import uuid4

from agentic_os.core import topological_tasks
from agentic_os.state import StateStore


def run_mock_goal(goal: dict, state: StateStore) -> str:
    goal_id = goal["id"]
    run_id = f"RUN-{uuid4().hex[:10].upper()}"

    state.create_run(run_id, goal_id)
    state.append_event("goal.run.started", goal_id=goal_id, run_id=run_id)

    for task in topological_tasks(goal):
        task_id = task["id"]
        common = {"task_id": task_id, "provider": "mock"}
        state.append_event(
            "task.claimed",
            goal_id=goal_id,
            run_id=run_id,
            payload=common,
        )
        state.append_event(
            "checkpoint.created",
            goal_id=goal_id,
            run_id=run_id,
            payload={**common, "checkpoint": f"MOCK-{task_id}"},
        )
        state.append_event(
            "verification.passed",
            goal_id=goal_id,
            run_id=run_id,
            payload={**common, "suite": "mock"},
        )
        state.append_event(
            "task.completed",
            goal_id=goal_id,
            run_id=run_id,
            payload=common,
        )

    state.set_run_status(run_id, "READY_FOR_INTEGRATION")
    state.append_event(
        "goal.ready_for_integration",
        goal_id=goal_id,
        run_id=run_id,
        payload={"main_merge": "manual"},
    )
    return run_id

