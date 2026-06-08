import json
import os
import logging

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ECS_CLUSTER = os.environ["ECS_CLUSTER_ARN"]
ECS_TASK_DEF = os.environ["ECS_TASK_DEFINITION_ARN"]
SUBNET_IDS = os.environ["SUBNET_IDS"].split(",")
SECURITY_GROUP_ID = os.environ["SECURITY_GROUP_ID"]
GITHUB_PARAM_NAME = os.environ["GITHUB_PARAM_NAME"]


def handler(event: dict, context: object) -> dict:
    logger.info("Webhook received")

    # TODO: Validate HMAC signature
    # TODO: Parse installation.id, repo, PR number from event body

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
        body.get("pull_request", {}).get("number", "0")
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
