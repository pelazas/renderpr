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


def register_task(pr_number: str, task_arn: str, public_ip: str) -> None:
    """Write the task's identity to SSM so the webhook Lambda can find it."""
    payload = {
        "task_arn": task_arn,
        "public_ip": public_ip,
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


def deregister_task(pr_number: str) -> None:
    """Remove the task's identity from SSM on graceful shutdown."""
    try:
        _ssm().delete_parameter(Name=_param_name(pr_number))
        logger.info("Deregistered task for PR #%s from SSM", pr_number)
    except ClientError as e:
        if e.response["Error"]["Code"] == "ParameterNotFound":
            return
        logger.exception("Failed to deregister task for PR #%s from SSM", pr_number)


def lookup_task(pr_number: str) -> str | None:
    """Read the task's public IP from SSM. Returns None if not registered."""
    try:
        resp = _ssm().get_parameter(Name=_param_name(pr_number))
    except ClientError as e:
        if e.response["Error"]["Code"] == "ParameterNotFound":
            return None
        logger.exception("Failed to look up task for PR #%s in SSM", pr_number)
        return None

    try:
        payload = json.loads(resp["Parameter"]["Value"])
    except (json.JSONDecodeError, KeyError):
        logger.warning("Malformed SSM payload for PR #%s", pr_number)
        return None

    public_ip = payload.get("public_ip")
    if not public_ip:
        return None
    return public_ip
