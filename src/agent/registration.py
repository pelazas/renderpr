import json
import logging
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

TASKS_PARAM_PREFIX = "/renderpr/tasks"


def _param_name(pr_number: str) -> str:
    return f"{TASKS_PARAM_PREFIX}/{pr_number}"


def _ssm():
    return boto3.client("ssm")


def register_task(
    pr_number: str, task_arn: str, public_ip: str, head_sha: str | None = None
) -> None:
    """Write the task's identity to SSM so the webhook Lambda can find it.

    ``head_sha`` records the commit this task is reviewing so the Lambda can tell
    a still-relevant task (same SHA → reuse) from a stale one (older SHA → replace).
    """
    payload = {
        "task_arn": task_arn,
        "public_ip": public_ip,
        "head_sha": head_sha,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        _ssm().put_parameter(
            Name=_param_name(pr_number),
            Value=json.dumps(payload),
            Type="String",
            Overwrite=True,
        )
        logger.info("Registered task for PR #%s in SSM", pr_number)
    except ClientError:
        logger.exception("Failed to register task for PR #%s in SSM", pr_number)


def deregister_task(pr_number: str, task_arn: str | None = None) -> None:
    """Remove the task's identity from SSM on shutdown.

    When ``task_arn`` is given, the param is only deleted if it still belongs to
    this task. This prevents a replaced task's delayed SIGTERM from clobbering
    the registration a newer task has since written for the same PR.
    """
    ssm = _ssm()
    if task_arn is not None:
        try:
            resp = ssm.get_parameter(Name=_param_name(pr_number))
            payload = json.loads(resp["Parameter"]["Value"])
        except ClientError as e:
            if e.response["Error"]["Code"] == "ParameterNotFound":
                return
            logger.exception("Failed to read task for PR #%s before deregister", pr_number)
            return
        except (json.JSONDecodeError, KeyError):
            payload = {}
        if payload.get("task_arn") not in (None, task_arn):
            logger.info(
                "Skipping deregister for PR #%s: param belongs to a different task", pr_number
            )
            return
    try:
        ssm.delete_parameter(Name=_param_name(pr_number))
        logger.info("Deregistered task for PR #%s from SSM", pr_number)
    except ClientError as e:
        if e.response["Error"]["Code"] == "ParameterNotFound":
            return
        logger.exception("Failed to deregister task for PR #%s from SSM", pr_number)


def lookup_task_record(pr_number: str) -> dict | None:
    """Read the full registration payload from SSM. Returns None if not registered."""
    try:
        resp = _ssm().get_parameter(Name=_param_name(pr_number))
    except ClientError as e:
        if e.response["Error"]["Code"] == "ParameterNotFound":
            return None
        logger.exception("Failed to look up task for PR #%s in SSM", pr_number)
        return None

    try:
        return json.loads(resp["Parameter"]["Value"])
    except (json.JSONDecodeError, KeyError):
        logger.warning("Malformed SSM payload for PR #%s", pr_number)
        return None


def lookup_task(pr_number: str) -> str | None:
    """Read the task's public IP from SSM. Returns None if not registered."""
    payload = lookup_task_record(pr_number)
    if not payload:
        return None
    return payload.get("public_ip") or None
