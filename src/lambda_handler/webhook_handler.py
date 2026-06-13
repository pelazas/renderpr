import json
import os
import hmac
import hashlib
import logging
import urllib.request

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ECS_CLUSTER = os.environ["ECS_CLUSTER_ARN"]
ECS_TASK_DEF = os.environ["ECS_TASK_DEFINITION_ARN"]
SUBNET_IDS = os.environ["SUBNET_IDS"].split(",")
SECURITY_GROUP_ID = os.environ["SECURITY_GROUP_ID"]
GITHUB_PARAM_NAME = os.environ["GITHUB_PARAM_NAME"]

_ssm = boto3.client("ssm")
try:
    param = _ssm.get_parameter(Name=GITHUB_PARAM_NAME, WithDecryption=True)
    _secrets = json.loads(param["Parameter"]["Value"])
    WEBHOOK_SECRET: bytes = _secrets["webhook_secret"].encode("utf-8")
except Exception:
    logger.exception("Failed to load webhook secret from SSM at cold start")
    WEBHOOK_SECRET = b""

COMMAND_TOKEN_PARAM_NAME = os.environ.get("COMMAND_TOKEN_PARAM_NAME", "/renderpr/renderpr-command-token")
try:
    param = _ssm.get_parameter(Name=COMMAND_TOKEN_PARAM_NAME, WithDecryption=True)
    COMMAND_TOKEN: bytes = param["Parameter"]["Value"].encode("utf-8")
except Exception:
    logger.exception("Failed to load command token from SSM at cold start")
    COMMAND_TOKEN = b""


def _verify_signature(body: bytes, signature_header: str) -> bool:
    if not WEBHOOK_SECRET:
        return False
    expected = "sha256=" + hmac.new(WEBHOOK_SECRET, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def _run_fargate_task(overrides: list[dict], pr_number: str) -> None:
    client = boto3.client("ecs")
    family = ECS_TASK_DEF.split("/")[-1].split(":")[0]
    task_tags = [
        {"key": "Project", "value": "renderpr"},
        {"key": "PRNumber", "value": pr_number},
    ]
    logger.info("Starting run_task with tags=%s", task_tags)
    resp = client.run_task(
        cluster=ECS_CLUSTER,
        taskDefinition=ECS_TASK_DEF,
        launchType="FARGATE",
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": SUBNET_IDS,
                "securityGroups": [SECURITY_GROUP_ID],
                "assignPublicIp": "ENABLED",
            }
        },
        overrides={
            "containerOverrides": [
                {
                    "name": "ReviewContainer",
                    "environment": overrides,
                }
            ]
        },
        tags=task_tags,
    )
    for t in resp.get("tasks", []):
        logger.info("Spawned task %s with tags=%s", t.get("taskArn"), t.get("tags"))
    failures = resp.get("failures", [])
    if failures:
        logger.error("run_task failures: %s", failures)


def _lookup_running_task(pr_number: str) -> str | None:
    """Return the public IP of the running task for this PR, or None."""
    param_name = f"/renderpr/tasks/{pr_number}"
    ssm = boto3.client("ssm")
    try:
        resp = ssm.get_parameter(Name=param_name)
    except ssm.exceptions.ParameterNotFound:
        logger.info("No running task found in SSM for PR #%s", pr_number)
        return None
    except Exception:
        logger.exception("Failed to look up task for PR #%s in SSM", pr_number)
        return None

    try:
        payload = json.loads(resp["Parameter"]["Value"])
    except (json.JSONDecodeError, KeyError):
        logger.warning("Malformed SSM payload for PR #%s", pr_number)
        return None

    public_ip = payload.get("public_ip")
    if public_ip:
        logger.info("Found running task for PR #%s via SSM at %s", pr_number, public_ip)
    return public_ip


def _dispatch_to_task(public_ip: str, command: str, query: str | None) -> bool:
    if not COMMAND_TOKEN:
        logger.error("Cannot dispatch: command token not loaded from SSM")
        return False
    body = {"command": command}
    if query:
        body["query"] = query
    data = json.dumps(body).encode()
    request_headers = {
        "Content-Type": "application/json",
        "X-RenderPR-Token": COMMAND_TOKEN.decode("utf-8"),
    }
    try:
        req = urllib.request.Request(
            f"http://{public_ip}:3001/__renderpr/command",
            data=data,
            headers=request_headers,
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception:
        logger.exception("Failed to dispatch command to task at %s", public_ip)
        return False


def _parse_renderpr_command(comment_body: str) -> dict | None:
    if "@renderpr" not in comment_body:
        return None

    after = comment_body.split("@renderpr", 1)[1].strip()

    if not after or after.startswith("review") or after.startswith("help"):
        return {"command": "review"}

    if after.startswith("code change"):
        query = after.removeprefix("code change").lstrip(": ").strip()
        if query:
            return {"command": "change", "query": query}

    if after.startswith("change "):
        return {"command": "change", "query": after.removeprefix("change ").strip()}

    if after == "apply":
        return {"command": "apply"}

    # "@renderpr reject" is a no-op: an unwanted change is simply left
    # uncommitted, so there is nothing to do. Return None so it is ignored
    # rather than falling through to the review catch-all below.
    if after == "reject":
        return None

    return {"command": "review"}


def handler(event: dict, context: object) -> dict:
    logger.info("Webhook received")

    headers = event.get("headers", {}) or {}
    signature = headers.get(
        "x-hub-signature-256",
        headers.get("X-Hub-Signature-256", ""),
    )
    if not signature or not _verify_signature(
        (event.get("body") or "").encode("utf-8"), signature
    ):
        return {"statusCode": 401, "body": json.dumps({"error": "Invalid signature"})}

    try:
        body = json.loads(event.get("body", "{}"))
    except json.JSONDecodeError:
        logger.error("Invalid JSON in webhook body")
        return {"statusCode": 400, "body": json.dumps({"error": "Invalid JSON"})}

    installation_id = str(
        body.get("installation", {}).get("id", "000000")
    )
    repo_full_name = str(
        body.get("repository", {}).get("full_name", "owner/repo")
    )
    pr_number = str(
        body.get("pull_request", {}).get("number")
        or body.get("issue", {}).get("number")
        or body.get("number", "0")
    )

    base_env = [
        {"name": "INSTALLATION_ID", "value": installation_id},
        {"name": "REPO_FULL_NAME", "value": repo_full_name},
        {"name": "PR_NUMBER", "value": pr_number},
    ]

    action = body.get("action", "")
    comment_body = body.get("comment", {}).get("body", "")
    comment_author = body.get("comment", {}).get("user", {}).get("login", "")
    sender_type = body.get("sender", {}).get("type", "")
    sender_login = body.get("sender", {}).get("login", "")

    if action == "created" and "@renderpr" in comment_body:
        if comment_author.endswith("[bot]") or sender_type == "Bot":
            logger.info("Ignoring comment from bot account: %s", comment_author)
            return {"statusCode": 200, "body": json.dumps({"ok": True, "ignored": "self"})}
        cmd = _parse_renderpr_command(comment_body)
        logger.info("Parsed command: %s", cmd)

        if cmd is None:
            logger.info("Ignoring no-op @renderpr command")
            return {"statusCode": 200, "body": json.dumps({"ok": True, "ignored": "noop"})}

        if cmd["command"] in ("change", "apply", "review"):
            public_ip = _lookup_running_task(pr_number)
            if public_ip:
                success = _dispatch_to_task(public_ip, cmd["command"], cmd.get("query"))
                if success:
                    return {"statusCode": 200, "body": json.dumps({"ok": True, "dispatched": True})}
                logger.warning("Dispatch failed, falling through to cold-start")
            else:
                logger.info("No running task found for PR #%s", pr_number)

            if cmd["command"] == "apply":
                return {
                    "statusCode": 409,
                    "body": json.dumps({
                        "ok": False,
                        "error": f"No active review session for PR #{pr_number}. Run `@renderpr review` first.",
                    }),
                }

            if cmd["command"] == "review":
                _run_fargate_task(base_env, pr_number)
                return {"statusCode": 200, "body": json.dumps({"ok": True, "cold_start": True})}

            command_str = cmd["command"]
            if cmd["command"] == "change" and cmd.get("query"):
                command_str = f"code_change::{cmd['query']}"

            overrides = base_env + [
                {"name": "COMMAND", "value": command_str},
                {"name": "SKIP_REVIEW", "value": "true"},
            ]
            _run_fargate_task(overrides, pr_number)
            return {"statusCode": 200, "body": json.dumps({"ok": True, "cold_start": True})}

        # Default: full review (no @renderpr command, just a plain comment)
        _run_fargate_task(base_env, pr_number)
        return {"statusCode": 200, "body": json.dumps({"ok": True})}

    elif action not in ("opened", "synchronize"):
        logger.info("Ignoring non-trigger action: %s", action)
        return {"statusCode": 200, "body": json.dumps({"ok": True, "ignored": True})}

    # Skip events caused by the bot itself (e.g. an `apply` commit pushed to the
    # PR branch fires a `synchronize` event whose sender is renderpr[bot]).
    # Without this, every apply would recursively trigger a fresh review.
    if sender_login.endswith("[bot]") or sender_type == "Bot":
        logger.info("Ignoring %s event from bot account: %s", action, sender_login or sender_type)
        return {"statusCode": 200, "body": json.dumps({"ok": True, "ignored": "self"})}

    _run_fargate_task(base_env, pr_number)
    return {"statusCode": 200, "body": json.dumps({"ok": True})}
