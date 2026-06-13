"""Per-repo user secrets, loaded from SSM Parameter Store (BYOC).

Secrets (env-var values, auth signing secrets, provider keys) are stored one
parameter per secret under::

    /renderpr/secrets/{installation_id}/{repo_full_name}/{KEY}

written by the user with their own AWS credentials (see scripts/renderpr-secrets.sh).

Security invariant: secrets are **never** loaded for fork PRs — fork code is
untrusted and could exfiltrate them. The fork gate is enforced here in code,
independent of IAM, and returns before any SSM call.
"""
import logging
from typing import Iterable

import boto3

from src.agent.config import SECRETS_SSM_PREFIX

logger = logging.getLogger(__name__)


def secret_path_prefix(installation_id: str, repo_full_name: str) -> str:
    return f"{SECRETS_SSM_PREFIX}/{installation_id}/{repo_full_name}".rstrip("/") + "/"


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

    ssm = ssm_client or boto3.client("ssm")
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
