import boto3
import pytest
from moto import mock_aws

from src.agent.secrets import load_repo_secrets, redact, secret_path_prefix

REGION = "eu-west-1"


def _put(ssm, name, value):
    ssm.put_parameter(Name=name, Value=value, Type="SecureString")


@mock_aws
def test_loads_only_this_repos_secrets():
    ssm = boto3.client("ssm", region_name=REGION)
    _put(ssm, "/renderpr/secrets/999/owner/repo/NEXT_PUBLIC_API_URL", "https://api.example.com")
    _put(ssm, "/renderpr/secrets/999/owner/repo/NEXTAUTH_SECRET", "s3cr3t-value")
    _put(ssm, "/renderpr/secrets/999/owner/other-repo/LEAK", "nope")  # different repo
    _put(ssm, "/renderpr/secrets/111/owner/repo/LEAK", "nope")        # different install

    out = load_repo_secrets("999", "owner/repo", is_fork=False, ssm_client=ssm)

    assert out == {
        "NEXT_PUBLIC_API_URL": "https://api.example.com",
        "NEXTAUTH_SECRET": "s3cr3t-value",
    }


@mock_aws
def test_fork_pr_never_loads_secrets():
    ssm = boto3.client("ssm", region_name=REGION)
    _put(ssm, "/renderpr/secrets/999/owner/repo/NEXTAUTH_SECRET", "s3cr3t-value")

    # Even though the secret exists and a working client is supplied, a fork PR
    # must get nothing back.
    assert load_repo_secrets("999", "owner/repo", is_fork=True, ssm_client=ssm) == {}


def test_fork_gate_runs_before_any_ssm_call():
    class ExplodingClient:
        def get_paginator(self, *a, **k):  # pragma: no cover - must never run
            raise AssertionError("SSM must not be touched for fork PRs")

    assert load_repo_secrets("999", "owner/repo", is_fork=True, ssm_client=ExplodingClient()) == {}


@mock_aws
def test_missing_identifiers_returns_empty():
    ssm = boto3.client("ssm", region_name=REGION)
    assert load_repo_secrets("", "owner/repo", is_fork=False, ssm_client=ssm) == {}
    assert load_repo_secrets("999", "", is_fork=False, ssm_client=ssm) == {}


@mock_aws
def test_no_secrets_stored_returns_empty():
    ssm = boto3.client("ssm", region_name=REGION)
    assert load_repo_secrets("999", "owner/repo", is_fork=False, ssm_client=ssm) == {}


def test_ssm_error_degrades_to_empty():
    class BrokenClient:
        def get_paginator(self, *a, **k):
            raise RuntimeError("boom")

    assert load_repo_secrets("999", "owner/repo", is_fork=False, ssm_client=BrokenClient()) == {}


def test_secret_path_prefix():
    assert secret_path_prefix("999", "owner/repo") == "/renderpr/secrets/999/owner/repo/"


def test_redact_masks_values_longest_first():
    text = "url=https://x.io key=supersecretvalue tail=supersecret"
    out = redact(text, {"supersecret", "supersecretvalue"})
    assert "supersecret" not in out
    assert out == "url=https://x.io key=*** tail=***"


def test_redact_skips_short_values():
    assert redact("a=1 b=22 c=cat", {"1", "22", "cat"}) == "a=1 b=22 c=cat"
