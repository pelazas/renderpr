import json
import os

import boto3
import pytest
from moto import mock_aws

GITHUB_PARAM_NAME = "/renderpr/github-app"
WEBHOOK_SECRET = "test-webhook-secret"
CLUSTER_NAME = "renderpr-cluster"
CLUSTER_ARN = f"arn:aws:ecs:us-east-1:123456789012:cluster/{CLUSTER_NAME}"


@pytest.fixture(autouse=True)
def aws_setup():
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
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
        yield


def test_register_and_lookup_task():
    from src.agent.registration import register_task, lookup_task

    register_task("42", "arn:aws:ecs:us-east-1:123456789012:task/cluster/abc", "1.2.3.4")
    assert lookup_task("42") == "1.2.3.4"


def test_lookup_task_returns_none_when_not_registered():
    from src.agent.registration import lookup_task

    assert lookup_task("99") is None


def test_register_overwrites_existing():
    from src.agent.registration import register_task, lookup_task

    register_task("42", "arn:old", "1.1.1.1")
    register_task("42", "arn:new", "2.2.2.2")
    assert lookup_task("42") == "2.2.2.2"


def test_deregister_task_removes_entry():
    from src.agent.registration import register_task, deregister_task, lookup_task

    register_task("42", "arn:abc", "1.2.3.4")
    assert lookup_task("42") == "1.2.3.4"
    deregister_task("42")
    assert lookup_task("42") is None


def test_deregister_missing_task_is_noop():
    from src.agent.registration import deregister_task

    deregister_task("never-existed")


def test_deregister_with_matching_arn_deletes():
    from src.agent.registration import register_task, deregister_task, lookup_task

    register_task("42", "arn:mine", "1.2.3.4")
    deregister_task("42", task_arn="arn:mine")
    assert lookup_task("42") is None


def test_deregister_with_mismatched_arn_is_noop():
    """A replaced task's late SIGTERM must not delete a newer task's registration."""
    from src.agent.registration import register_task, deregister_task, lookup_task

    register_task("42", "arn:new-task", "9.9.9.9")
    # The old task (arn:old-task) tries to deregister after being replaced.
    deregister_task("42", task_arn="arn:old-task")
    assert lookup_task("42") == "9.9.9.9"
