import json
import hmac
import hashlib
import os

import boto3
import pytest
from moto import mock_aws

GITHUB_PARAM_NAME = "/renderpr/github-app"
WEBHOOK_SECRET = "test-webhook-secret"
CLUSTER_NAME = "renderpr-cluster"
CLUSTER_ARN = f"arn:aws:ecs:us-east-1:123456789012:cluster/{CLUSTER_NAME}"


def _sign(body: str) -> str:
    return "sha256=" + hmac.new(
        WEBHOOK_SECRET.encode(), body.encode(), hashlib.sha256
    ).hexdigest()


@pytest.fixture(autouse=True)
def aws_setup():
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
    os.environ["ECS_CLUSTER_ARN"] = CLUSTER_ARN
    os.environ["SUBNET_IDS"] = "subnet-abc,subnet-def"
    os.environ["SECURITY_GROUP_ID"] = "sg-123"
    os.environ["GITHUB_PARAM_NAME"] = GITHUB_PARAM_NAME

    with mock_aws():
        ssm = boto3.client("ssm", region_name="us-east-1")
        ssm.put_parameter(
            Name=GITHUB_PARAM_NAME,
            Type="SecureString",
            Value=json.dumps(
                {
                    "app_id": "123456",
                    "private_key": "fake-private-key",
                    "webhook_secret": WEBHOOK_SECRET,
                }
            ),
        )

        ecs = boto3.client("ecs", region_name="us-east-1")
        ecs.create_cluster(clusterName=CLUSTER_NAME)
        task_def = ecs.register_task_definition(
            family="renderpr-review",
            containerDefinitions=[
                {
                    "name": "ReviewContainer",
                    "image": "test-image",
                    "memory": 512,
                }
            ],
        )
        os.environ["ECS_TASK_DEFINITION_ARN"] = task_def["taskDefinition"][
            "taskDefinitionArn"
        ]
        yield


def _create_running_task(ecs_client, eni_id="eni-test"):
    """Helper to create a running task with ENI attachment for testing."""
    return ecs_client.run_task(
        cluster=CLUSTER_ARN,
        taskDefinition=os.environ["ECS_TASK_DEFINITION_ARN"],
        launchType="FARGATE",
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": ["subnet-abc"],
                "securityGroups": ["sg-123"],
                "assignPublicIp": "ENABLED",
            }
        },
        overrides={
            "containerOverrides": [
                {
                    "name": "ReviewContainer",
                    "environment": [{"name": "PR_NUMBER", "value": "42"}],
                }
            ]
        },
    )


def test_valid_hmac_returns_200():
    from src.lambda_handler.webhook_handler import handler

    body = json.dumps(
        {
            "action": "opened",
            "installation": {"id": 456},
            "repository": {"full_name": "owner/repo"},
            "pull_request": {"number": 42},
        }
    )
    event = {
        "headers": {"x-hub-signature-256": _sign(body)},
        "body": body,
    }
    result = handler(event, {})
    assert result["statusCode"] == 200
    data = json.loads(result["body"])
    assert data["ok"] is True

    ecs = boto3.client("ecs", region_name="us-east-1")
    tasks = ecs.list_tasks(cluster=CLUSTER_ARN)
    assert len(tasks["taskArns"]) > 0


def test_invalid_hmac_returns_401():
    from src.lambda_handler.webhook_handler import handler

    event = {
        "headers": {"x-hub-signature-256": "sha256=invalidsignature"},
        "body": json.dumps({"installation": {"id": 456}}),
    }
    result = handler(event, {})
    assert result["statusCode"] == 401


def test_missing_signature_header_returns_401():
    from src.lambda_handler.webhook_handler import handler

    event = {
        "headers": {},
        "body": json.dumps({"installation": {"id": 456}}),
    }
    result = handler(event, {})
    assert result["statusCode"] == 401


def test_issue_comment_with_renderpr_triggers():
    from src.lambda_handler.webhook_handler import handler

    body = json.dumps(
        {
            "action": "created",
            "installation": {"id": 456},
            "repository": {"full_name": "owner/repo"},
            "issue": {"number": 77, "pull_request": {}},
            "comment": {"body": "@renderpr review the changes"},
        }
    )
    event = {
        "headers": {"x-hub-signature-256": _sign(body)},
        "body": body,
    }
    result = handler(event, {})
    assert result["statusCode"] == 200
    data = json.loads(result["body"])
    assert data["ok"] is True

    ecs = boto3.client("ecs", region_name="us-east-1")
    tasks = ecs.list_tasks(cluster=CLUSTER_ARN)
    assert len(tasks["taskArns"]) > 0


def test_issue_comment_without_renderpr_ignored():
    from src.lambda_handler.webhook_handler import handler

    body = json.dumps(
        {
            "action": "created",
            "installation": {"id": 456},
            "repository": {"full_name": "owner/repo"},
            "issue": {"number": 77, "pull_request": {}},
            "comment": {"body": "just a regular comment"},
        }
    )
    event = {
        "headers": {"x-hub-signature-256": _sign(body)},
        "body": body,
    }
    result = handler(event, {})
    assert result["statusCode"] == 200
    data = json.loads(result["body"])
    assert data.get("ignored") is True


def test_synchronize_action_triggers():
    from src.lambda_handler.webhook_handler import handler

    body = json.dumps(
        {
            "action": "synchronize",
            "installation": {"id": 456},
            "repository": {"full_name": "owner/repo"},
            "pull_request": {"number": 99},
        }
    )
    event = {
        "headers": {"x-hub-signature-256": _sign(body)},
        "body": body,
    }
    result = handler(event, {})
    assert result["statusCode"] == 200
    data = json.loads(result["body"])
    assert data["ok"] is True

    ecs = boto3.client("ecs", region_name="us-east-1")
    tasks = ecs.list_tasks(cluster=CLUSTER_ARN)
    assert len(tasks["taskArns"]) > 0


def test_non_trigger_action_ignored():
    from src.lambda_handler.webhook_handler import handler

    body = json.dumps(
        {
            "action": "labeled",
            "installation": {"id": 456},
            "repository": {"full_name": "owner/repo"},
            "pull_request": {"number": 99},
        }
    )
    event = {
        "headers": {"x-hub-signature-256": _sign(body)},
        "body": body,
    }
    result = handler(event, {})
    assert result["statusCode"] == 200
    data = json.loads(result["body"])
    assert data.get("ignored") is True


def test_invalid_json_body_returns_400():
    from src.lambda_handler.webhook_handler import handler

    body = "not-valid-json"
    event = {
        "headers": {"x-hub-signature-256": _sign(body)},
        "body": body,
    }
    result = handler(event, {})
    assert result["statusCode"] == 400


class TestParseRenderprCommand:
    def test_no_at_renderpr(self):
        from src.lambda_handler.webhook_handler import _parse_renderpr_command
        assert _parse_renderpr_command("hello") is None

    def test_review_command(self):
        from src.lambda_handler.webhook_handler import _parse_renderpr_command
        assert _parse_renderpr_command("@renderpr review") == {"command": "review"}

    def test_code_change_with_space(self):
        from src.lambda_handler.webhook_handler import _parse_renderpr_command
        result = _parse_renderpr_command("@renderpr code change make button orange")
        assert result == {"command": "change", "query": "make button orange"}

    def test_code_change_with_colon(self):
        from src.lambda_handler.webhook_handler import _parse_renderpr_command
        result = _parse_renderpr_command("@renderpr code change: add a card")
        assert result == {"command": "change", "query": "add a card"}

    def test_apply_command(self):
        from src.lambda_handler.webhook_handler import _parse_renderpr_command
        assert _parse_renderpr_command("@renderpr apply") == {"command": "apply"}

    def test_reject_command(self):
        from src.lambda_handler.webhook_handler import _parse_renderpr_command
        assert _parse_renderpr_command("@renderpr reject") == {"command": "reject"}

    def test_unknown_falls_to_review(self):
        from src.lambda_handler.webhook_handler import _parse_renderpr_command
        assert _parse_renderpr_command("@renderpr something") == {"command": "review"}


def test_code_change_cold_start_spawns_task(monkeypatch):
    from src.lambda_handler.webhook_handler import handler

    # No running task, so it should cold-start spawn
    body = json.dumps(
        {
            "action": "created",
            "installation": {"id": 456},
            "repository": {"full_name": "owner/repo"},
            "issue": {"number": 77, "pull_request": {}},
            "comment": {"body": "@renderpr code change make button orange"},
        }
    )
    event = {
        "headers": {"x-hub-signature-256": _sign(body)},
        "body": body,
    }
    result = handler(event, {})
    assert result["statusCode"] == 200
    data = json.loads(result["body"])
    assert data.get("cold_start") is True

    ecs = boto3.client("ecs", region_name="us-east-1")
    tasks = ecs.list_tasks(cluster=CLUSTER_ARN)
    assert len(tasks["taskArns"]) > 0


def test_apply_cold_start_spawns_task(monkeypatch):
    from src.lambda_handler.webhook_handler import handler

    body = json.dumps(
        {
            "action": "created",
            "installation": {"id": 456},
            "repository": {"full_name": "owner/repo"},
            "issue": {"number": 77, "pull_request": {}},
            "comment": {"body": "@renderpr apply"},
        }
    )
    event = {
        "headers": {"x-hub-signature-256": _sign(body)},
        "body": body,
    }
    result = handler(event, {})
    assert result["statusCode"] == 200
    data = json.loads(result["body"])
    assert data.get("cold_start") is True


def test_code_change_with_running_task_dispatches(monkeypatch):
    from src.lambda_handler.webhook_handler import handler

    dispatch_called = {"args": None}

    def mock_dispatch(ip, cmd, query):
        dispatch_called["args"] = (ip, cmd, query)
        return True

    monkeypatch.setattr("src.lambda_handler.webhook_handler._get_running_task_eni", lambda pr: "eni-123")
    monkeypatch.setattr("src.lambda_handler.webhook_handler._get_public_ip", lambda eni: "54.1.2.3")
    monkeypatch.setattr("src.lambda_handler.webhook_handler._dispatch_to_task", mock_dispatch)

    body = json.dumps(
        {
            "action": "created",
            "installation": {"id": 456},
            "repository": {"full_name": "owner/repo"},
            "issue": {"number": 77, "pull_request": {}},
            "comment": {"body": "@renderpr code change make it blue"},
        }
    )
    event = {
        "headers": {"x-hub-signature-256": _sign(body)},
        "body": body,
    }
    result = handler(event, {})
    assert result["statusCode"] == 200
    data = json.loads(result["body"])
    assert data.get("dispatched") is True
    assert dispatch_called["args"] == ("54.1.2.3", "change", "make it blue")
