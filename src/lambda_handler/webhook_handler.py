import json
import os
import hmac
import hashlib
import logging

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


def _verify_signature(body: bytes, signature_header: str) -> bool:
    if not WEBHOOK_SECRET:
        return False
    expected = "sha256=" + hmac.new(WEBHOOK_SECRET, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


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

    action = body.get("action", "")
    comment_body = body.get("comment", {}).get("body", "")

    if action == "created" and "@renderpr" in comment_body:
        logger.info("Triggering on @renderpr comment")
    elif action not in ("opened", "synchronize"):
        logger.info("Ignoring non-trigger action: %s", action)
        return {"statusCode": 200, "body": json.dumps({"ok": True, "ignored": True})}

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

    client = boto3.client("ecs")

    try:
        client.run_task(
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
                        "environment": [
                            {"name": "INSTALLATION_ID", "value": installation_id},
                            {"name": "REPO_FULL_NAME", "value": repo_full_name},
                            {"name": "PR_NUMBER", "value": pr_number},
                        ],
                    }
                ]
            },
        )
    except Exception:
        logger.exception("Failed to start Fargate task")
        return {"statusCode": 500, "body": json.dumps({"error": "Failed to start review"})}

    return {"statusCode": 200, "body": json.dumps({"ok": True})}
