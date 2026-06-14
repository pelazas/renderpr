"""Scheduled orphan-task reaper.

Runs on an EventBridge schedule and bounds Fargate cost two ways:

1. Force-stops any running review task older than ``MAX_TASK_AGE_SECONDS``. The
   in-container idle timeout (15 min) handles the normal case; this is the
   backstop for tasks that wedge, keep resetting their idle timer, or otherwise
   never self-terminate — the "containers I have to close manually" problem.
2. Sweeps ``/renderpr/tasks/*`` SSM registrations whose task is no longer
   running, so stale entries don't accumulate or misroute command dispatch.
"""
import json
import logging
import os
from datetime import datetime, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ECS_CLUSTER = os.environ["ECS_CLUSTER_ARN"]
# Hard lifetime ceiling. Idle timeout is 900s; default leaves headroom for a slow
# review before the backstop fires.
MAX_TASK_AGE_SECONDS = int(os.environ.get("MAX_TASK_AGE_SECONDS", "1500"))
TASKS_PARAM_PREFIX = os.environ.get("TASKS_PARAM_PREFIX", "/renderpr/tasks")


def _task_started_at(task: dict) -> datetime | None:
    """When the task began. Prefer startedAt; fall back to createdAt for tasks
    still provisioning (which can also wedge and should still age out)."""
    return task.get("startedAt") or task.get("createdAt")


def _age_seconds(task: dict, now: datetime) -> float | None:
    started = _task_started_at(task)
    if started is None:
        return None
    return (now - started).total_seconds()


def _list_running_task_arns(ecs) -> list[str]:
    arns: list[str] = []
    token: str | None = None
    while True:
        kwargs: dict = {"cluster": ECS_CLUSTER, "desiredStatus": "RUNNING"}
        if token:
            kwargs["nextToken"] = token
        resp = ecs.list_tasks(**kwargs)
        arns.extend(resp.get("taskArns", []))
        token = resp.get("nextToken")
        if not token:
            return arns


def _reap_stale_tasks(now: datetime) -> list[str]:
    ecs = boto3.client("ecs")
    arns = _list_running_task_arns(ecs)
    stopped: list[str] = []
    for i in range(0, len(arns), 100):
        chunk = arns[i : i + 100]
        desc = ecs.describe_tasks(cluster=ECS_CLUSTER, tasks=chunk)
        for task in desc.get("tasks", []):
            age = _age_seconds(task, now)
            if age is None or age < MAX_TASK_AGE_SECONDS:
                continue
            arn = task["taskArn"]
            try:
                ecs.stop_task(
                    cluster=ECS_CLUSTER,
                    task=arn,
                    reason=f"RenderPR reaper: exceeded max lifetime {MAX_TASK_AGE_SECONDS}s",
                )
                stopped.append(arn)
                logger.info("Reaped stale task %s (age %.0fs)", arn, age)
            except Exception:
                logger.exception("Failed to stop stale task %s", arn)
    return stopped


def _all_task_params(ssm) -> list[dict]:
    params: list[dict] = []
    token: str | None = None
    while True:
        kwargs: dict = {"Path": TASKS_PARAM_PREFIX, "Recursive": True, "MaxResults": 10}
        if token:
            kwargs["NextToken"] = token
        resp = ssm.get_parameters_by_path(**kwargs)
        params.extend(resp.get("Parameters", []))
        token = resp.get("NextToken")
        if not token:
            return params


def _sweep_orphan_params() -> list[str]:
    ecs = boto3.client("ecs")
    ssm = boto3.client("ssm")
    running = set(_list_running_task_arns(ecs))

    try:
        params = _all_task_params(ssm)
    except Exception:
        logger.exception("Failed to list task registration params")
        return []

    deleted: list[str] = []
    for p in params:
        try:
            payload = json.loads(p.get("Value", "{}"))
        except (json.JSONDecodeError, TypeError):
            payload = {}
        arn = payload.get("task_arn")
        # Delete a registration only when its task is provably gone. A missing or
        # unknown arn is left alone (don't race a task that hasn't registered).
        if not arn or arn in running:
            continue
        try:
            ssm.delete_parameter(Name=p["Name"])
            deleted.append(p["Name"])
            logger.info("Deleted orphan task registration %s", p["Name"])
        except Exception:
            logger.exception("Failed to delete orphan param %s", p["Name"])
    return deleted


def handler(event: dict, context: object) -> dict:
    now = datetime.now(timezone.utc)
    stopped = _reap_stale_tasks(now)
    deleted = _sweep_orphan_params()
    logger.info(
        "Reaper run complete: stopped=%d orphan_params_deleted=%d",
        len(stopped), len(deleted),
    )
    return {"stopped": stopped, "orphan_params_deleted": deleted}
