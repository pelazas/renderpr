"""Per-repo user secrets, loaded from SSM Parameter Store (BYOC).

Secrets (env-var values, auth signing secrets, provider keys) are stored one
parameter per secret under::

    /renderpr/secrets/{installation_id}/{repo_full_name}/{KEY}

written by the user with their own AWS credentials (see scripts/renderpr-secrets.sh).

Security invariant: secrets are **never** loaded for fork PRs — fork code is
untrusted and could exfiltrate them. The fork gate is enforced here in code,
independent of IAM, and returns before any SSM call.

Least-privilege (threat-model F-2): the task role does not hold a broad SSM read.
Instead each load assumes SECRETS_ACCESS_ROLE_ARN with an inline session policy
scoped to *this* repo's secrets path, so a task can only ever read its own repo's
secrets — never another installation's or repo's. See ``_scoped_ssm_client``.
"""
import json
import logging
import os
from typing import Iterable

import boto3

from src.agent.config import SECRETS_SSM_PREFIX

logger = logging.getLogger(__name__)


def secret_path_prefix(installation_id: str, repo_full_name: str) -> str:
    return f"{SECRETS_SSM_PREFIX}/{installation_id}/{repo_full_name}".rstrip("/") + "/"


def _scoped_ssm_client(installation_id: str, repo_full_name: str):
    """Return an SSM client backed by repo-scoped temporary credentials, or None.

    Assumes ``SECRETS_ACCESS_ROLE_ARN`` with an inline session policy that allows
    SSM reads only under this repo's secrets path (threat-model F-2). Returns:

    * a plain ``boto3.client("ssm")`` when the role ARN or region/account can't be
      derived (local/non-container runs) — in prod that client has no secrets
      permission and simply yields ``{}``;
    * a credential-scoped client on success;
    * ``None`` when the AssumeRole call fails.
    """
    role_arn = os.environ.get("SECRETS_ACCESS_ROLE_ARN")
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    # Role ARN: arn:aws:iam::<account>:role/... — account is field index 4.
    account = role_arn.split(":")[4] if role_arn and len(role_arn.split(":")) > 4 else None

    if not role_arn or not region or not account:
        logger.warning(
            "SECRETS_ACCESS_ROLE_ARN/region/account unavailable; using default SSM client "
            "(no scoped secrets access)"
        )
        return boto3.client("ssm")

    scoped_path = f"{SECRETS_SSM_PREFIX}/{installation_id}/{repo_full_name}"
    # SSM param ARNs concatenate the leading-slash name directly after "parameter".
    root_arn = f"arn:aws:ssm:{region}:{account}:parameter{scoped_path}"
    child_arn = f"arn:aws:ssm:{region}:{account}:parameter{scoped_path}/*"
    session_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["ssm:GetParametersByPath", "ssm:GetParameter", "ssm:GetParameters"],
                "Resource": [root_arn, child_arn],
            }
        ],
    }

    try:
        sts = boto3.client("sts")
        resp = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName="renderpr-secrets"[:64],
            Policy=json.dumps(session_policy),
            DurationSeconds=900,
        )
        creds = resp["Credentials"]
        return boto3.client(
            "ssm",
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
        )
    except Exception:
        # Only the boto error is logged; no secret values are ever in scope here.
        logger.warning("Failed to assume scoped secrets role; continuing without injection", exc_info=True)
        return None


def load_repo_secrets(
    installation_id: str,
    repo_full_name: str,
    is_fork: bool,
    ssm_client=None,
) -> dict[str, str]:
    """Return ``{KEY: value}`` for this repo's secrets, or ``{}``.

    Returns ``{}`` (never raising) when: the PR is from a fork, identifiers are
    missing, nothing is stored, or SSM access fails — so a misconfigured repo
    degrades to "no injection" rather than crashing the review.
    """
    if is_fork:
        logger.info("Fork PR detected — skipping secret injection (secrets never load on forks)")
        return {}
    if not installation_id or not repo_full_name:
        return {}

    ssm = ssm_client
    if ssm is None:
        ssm = _scoped_ssm_client(installation_id, repo_full_name)
        if ssm is None:
            return {}
    prefix = secret_path_prefix(installation_id, repo_full_name)
    secrets: dict[str, str] = {}
    try:
        paginator = ssm.get_paginator("get_parameters_by_path")
        for page in paginator.paginate(Path=prefix, Recursive=True, WithDecryption=True):
            for param in page.get("Parameters", []):
                key = param["Name"].rsplit("/", 1)[-1]
                secrets[key] = param["Value"]
    except Exception:
        # Values must never reach the logs; exc_info here carries only the boto error.
        logger.warning("Failed to load repo secrets from SSM; continuing without injection", exc_info=True)
        return {}

    # Names are not secret; values are never logged.
    logger.info("Loaded %d repo secret(s): %s", len(secrets), sorted(secrets.keys()))
    return secrets


def redact(text: str, secret_values: Iterable[str], placeholder: str = "***") -> str:
    """Replace any occurrence of a secret value in ``text`` with ``placeholder``.

    Values shorter than 4 chars are skipped to avoid mangling unrelated text.
    Use before writing any user-influenced string (errors, diffs) to logs/comments.
    """
    redacted = text
    for value in sorted({v for v in secret_values if v and len(v) >= 4}, key=len, reverse=True):
        if value in redacted:
            redacted = redacted.replace(value, placeholder)
    return redacted
