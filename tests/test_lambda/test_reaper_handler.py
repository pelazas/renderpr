import json
import os
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from moto import mock_aws

CLUSTER_NAME = "renderpr-cluster"
CLUSTER_ARN = f"arn:aws:ecs:us-east-1:123456789012:cluster/{CLUSTER_NAME}"


@pytest.fixture(autouse=True)
def aws_setup():
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
    os.environ["ECS_CLUSTER_ARN"] = CLUSTER_ARN

    with mock_aws():
        ecs = boto3.client("ecs", region_name="us-east-1")
        ecs.create_cluster(clusterName=CLUSTER_NAME)
        task_def = ecs.register_task_definition(
            family="renderpr-review",
            containerDefinitions=[{"name": "ReviewContainer", "image": "img", "memory": 512}],
        )
        os.environ["ECS_TASK_DEFINITION_ARN"] = task_def["taskDefinition"]["taskDefinitionArn"]
        yield


def _run_task(ecs):
    return ecs.run_task(
        cluster=CLUSTER_ARN,
        taskDefinition=os.environ["ECS_TASK_DEFINITION_ARN"],
        launchType="FARGATE",
    )["tasks"][0]["taskArn"]


def _register_param(pr_number: str, task_arn: str):
    ssm = boto3.client("ssm", region_name="us-east-1")
    ssm.put_parameter(
        Name=f"/renderpr/tasks/{pr_number}",
        Type="String",
        Value=json.dumps({"task_arn": task_arn, "public_ip": "1.2.3.4", "head_sha": "abc"}),
        Overwrite=True,
    )


class TestAgeHelpers:
    def test_age_seconds_from_started_at(self):
        from src.lambda_handler.reaper_handler import _age_seconds

        now = datetime(2026, 6, 14, 12, 0, 0, tzinfo=timezone.utc)
        task = {"startedAt": now - timedelta(minutes=30)}
        assert _age_seconds(task, now) == pytest.approx(1800)

    def test_age_seconds_none_when_no_timestamp(self):
        from src.lambda_handler.reaper_handler import _age_seconds

        now = datetime(2026, 6, 14, 12, 0, 0, tzinfo=timezone.utc)
        assert _age_seconds({}, now) is None


def test_reaper_stops_task_over_max_age(monkeypatch):
    from src.lambda_handler import reaper_handler

    ecs = boto3.client("ecs", region_name="us-east-1")
    arn = _run_task(ecs)

    # moto doesn't populate startedAt/createdAt (age math is unit-tested in
    # TestAgeHelpers); force the task over the ceiling to exercise stop wiring.
    monkeypatch.setattr(reaper_handler, "_age_seconds", lambda task, now: 9999)
    result = reaper_handler.handler({}, {})

    assert arn in result["stopped"]
    running = ecs.list_tasks(cluster=CLUSTER_ARN, desiredStatus="RUNNING")["taskArns"]
    assert arn not in running


def test_reaper_leaves_fresh_task_running():
    from src.lambda_handler import reaper_handler

    ecs = boto3.client("ecs", region_name="us-east-1")
    arn = _run_task(ecs)

    # Default 1500s ceiling: a just-started task must survive.
    result = reaper_handler.handler({}, {})

    assert arn not in result["stopped"]
    running = ecs.list_tasks(cluster=CLUSTER_ARN, desiredStatus="RUNNING")["taskArns"]
    assert arn in running


def test_reaper_deletes_orphan_param_keeps_live_one():
    from src.lambda_handler import reaper_handler

    ecs = boto3.client("ecs", region_name="us-east-1")
    live_arn = _run_task(ecs)
    _register_param("10", live_arn)  # backed by a running task -> keep
    _register_param("20", "arn:aws:ecs:us-east-1:123456789012:task/cluster/dead")  # orphan -> delete

    result = reaper_handler.handler({}, {})

    deleted = result["orphan_params_deleted"]
    assert "/renderpr/tasks/20" in deleted
    assert "/renderpr/tasks/10" not in deleted

    ssm = boto3.client("ssm", region_name="us-east-1")
    assert ssm.get_parameter(Name="/renderpr/tasks/10")  # still present
    with pytest.raises(ssm.exceptions.ParameterNotFound):
        ssm.get_parameter(Name="/renderpr/tasks/20")
