import json
import os
import logging
from pathlib import Path
import shutil
import subprocess
import sys
import time

import boto3
import httpx
import jwt

from src.agent.config import (
    DEV_SERVER_HOST,
    DEV_SERVER_PORT,
    DEV_SERVER_START_TIMEOUT,
    DEV_SERVER_POLL_INTERVAL,
    REPO_DIR,
    RETRY_MAX_ATTEMPTS,
)

logger = logging.getLogger(__name__)


def _fetch_secrets() -> dict:
    github_param = os.environ.get("GITHUB_PARAM_NAME")
    openrouter_param = os.environ.get("OPENROUTER_PARAM_NAME")

    if not github_param or not openrouter_param:
        logger.error("Missing SSM parameter names in environment")
        sys.exit(1)

    ssm = boto3.client("ssm")

    try:
        github_resp = ssm.get_parameter(Name=github_param, WithDecryption=True)
        openrouter_resp = ssm.get_parameter(Name=openrouter_param, WithDecryption=True)
    except Exception:
        logger.exception("Failed to fetch secrets from SSM")
        sys.exit(1)

    github_data = json.loads(github_resp["Parameter"]["Value"])
    openrouter_data = json.loads(openrouter_resp["Parameter"]["Value"])

    return {
        "app_id": github_data["app_id"],
        "private_key": github_data["private_key"],
        "openrouter_api_key": openrouter_data["openrouter_api_key"],
    }


def _clone_repo(repo_full_name: str, pr_number: str, token: str) -> None:
    clone_url = f"https://x-access-token:{token}@github.com/{repo_full_name}.git"

    shutil.rmtree(REPO_DIR, ignore_errors=True)
    for attempt in range(RETRY_MAX_ATTEMPTS):
        try:
            subprocess.run(
                ["git", "clone", clone_url, REPO_DIR],
                capture_output=True, text=True, check=True,
            )
            break
        except Exception:
            if attempt == RETRY_MAX_ATTEMPTS - 1:
                logger.exception("git clone failed after %d attempts", RETRY_MAX_ATTEMPTS)
                sys.exit(1)
            shutil.rmtree(REPO_DIR, ignore_errors=True)
            logger.warning("git clone failed (attempt %d/%d), retrying...", attempt + 1, RETRY_MAX_ATTEMPTS)

    for cmd_name, cmd in [
        ("fetch", ["git", "-C", REPO_DIR, "fetch", "origin", f"pull/{pr_number}/head:review-pr"]),
        ("checkout", ["git", "-C", REPO_DIR, "checkout", "review-pr"]),
    ]:
        for attempt in range(RETRY_MAX_ATTEMPTS):
            try:
                subprocess.run(cmd, capture_output=True, text=True, check=True)
                break
            except Exception:
                if attempt == RETRY_MAX_ATTEMPTS - 1:
                    logger.exception("git %s failed after %d attempts", cmd_name, RETRY_MAX_ATTEMPTS)
                    sys.exit(1)
                logger.warning("git %s failed (attempt %d/%d), retrying...", cmd_name, attempt + 1, RETRY_MAX_ATTEMPTS)


_dev_server_proc: subprocess.Popen | None = None
_dev_server_url: str = ""


def _start_dev_server() -> None:
    global _dev_server_proc, _dev_server_url

    pkg_json = os.path.join(REPO_DIR, "package.json")
    if not os.path.exists(pkg_json):
        logger.error("No package.json found at %s", pkg_json)
        sys.exit(1)

    try:
        subprocess.run(
            ["npm", "ci"],
            cwd=REPO_DIR,
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )
    except Exception:
        logger.exception("npm ci failed")
        sys.exit(1)

    _dev_server_proc = subprocess.Popen(["npm", "run", "dev"], cwd=REPO_DIR)

    _dev_server_url = f"http://{DEV_SERVER_HOST}:{DEV_SERVER_PORT}/"
    url = _dev_server_url
    deadline = time.time() + DEV_SERVER_START_TIMEOUT
    while time.time() < deadline:
        try:
            with httpx.Client(timeout=5) as client:
                resp = client.get(url)
            logger.info("Dev server ready (status %d)", resp.status_code)
            return
        except httpx.ConnectError:
            time.sleep(DEV_SERVER_POLL_INTERVAL)

    logger.error("Dev server did not start within %ds", DEV_SERVER_START_TIMEOUT)
    if _dev_server_proc:
        _dev_server_proc.kill()
    sys.exit(1)


def _get_installation_token(installation_id: str, app_id: str, private_key: str) -> str:
    now = int(time.time())
    payload = {"iat": now, "exp": now + 600, "iss": app_id}
    jwt_token = jwt.encode(payload, private_key, algorithm="RS256")

    url = f"https://api.github.com/app/installations/{installation_id}/access_tokens"
    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "RenderPR/1.0",
    }

    for attempt in range(RETRY_MAX_ATTEMPTS):
        with httpx.Client() as client:
            resp = client.post(url, headers=headers)

        if resp.status_code == 201:
            return resp.json()["token"]

        logger.error(
            "GitHub API error (attempt %d/%d): %d %s",
            attempt + 1, RETRY_MAX_ATTEMPTS, resp.status_code, resp.text,
        )

        if 400 <= resp.status_code < 500:
            break

        if attempt < RETRY_MAX_ATTEMPTS - 1:
            time.sleep(3)

    sys.exit(1)


def _fetch_diff(token: str, repo_full_name: str, pr_number: str) -> str:
    url = f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3.diff",
        "User-Agent": "RenderPR/1.0",
    }

    for attempt in range(RETRY_MAX_ATTEMPTS):
        with httpx.Client(timeout=30) as client:
            resp = client.get(url, headers=headers)

        if resp.status_code == 200:
            return resp.text

        logger.error(
            "GitHub API error fetching diff (attempt %d/%d): %d %s",
            attempt + 1, RETRY_MAX_ATTEMPTS, resp.status_code, resp.text,
        )

        if 400 <= resp.status_code < 500:
            break

        if attempt < RETRY_MAX_ATTEMPTS - 1:
            time.sleep(3)

    sys.exit(1)


def _parse_diff_summary(diff: str) -> str:
    lines = diff.splitlines()
    current_file = ""
    file_stats: list[str] = []
    additions = 0
    deletions = 0

    for line in lines:
        if line.startswith("+++ b/"):
            if current_file:
                file_stats.append(f"{current_file} (+{additions}/-{deletions})")
            current_file = line[6:]
            additions = 0
            deletions = 0
        elif line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1

    if current_file:
        file_stats.append(f"{current_file} (+{additions}/-{deletions})")

    return ", ".join(file_stats) if file_stats else "(no file changes detected)"


def _capture_screenshots() -> list[Path]:
    from src.agent.visual import capture_screenshots

    screenshot_dir = Path(REPO_DIR) / ".renderpr" / "screenshots"
    return capture_screenshots(_dev_server_url, screenshot_dir=screenshot_dir)


def _post_comment(token: str, repo_full_name: str, pr_number: str, body: str) -> None:
    url = f"https://api.github.com/repos/{repo_full_name}/issues/{pr_number}/comments"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "RenderPR/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    with httpx.Client(timeout=30) as client:
        resp = client.post(url, headers=headers, json={"body": body})

    if resp.status_code != 201:
        logger.error("Failed to post comment: %d %s", resp.status_code, resp.text)
        sys.exit(1)

    logger.info("Review posted to PR #%s", pr_number)


def run() -> None:
    logging.basicConfig(level=logging.INFO)

    installation_id = os.environ.get("INSTALLATION_ID", "unknown")
    repo_full_name = os.environ.get("REPO_FULL_NAME", "unknown")
    pr_number = os.environ.get("PR_NUMBER", "unknown")

    logger.info("RenderPR agent started")
    logger.info("Installation ID: %s", installation_id)
    logger.info("Repository: %s", repo_full_name)
    logger.info("PR Number: %s", pr_number)

    secrets = _fetch_secrets()
    token = _get_installation_token(
        installation_id=installation_id,
        app_id=secrets["app_id"],
        private_key=secrets["private_key"],
    )
    _clone_repo(
        repo_full_name=repo_full_name,
        pr_number=pr_number,
        token=token,
    )
    _start_dev_server()

    logger.info("Dev server ready. Proceeding to review...")

    diff = _fetch_diff(
        token=token,
        repo_full_name=repo_full_name,
        pr_number=pr_number,
    )
    logger.info("Fetched diff for PR #%s (%d bytes)", pr_number, len(diff))
    logger.info("Changes: %s", _parse_diff_summary(diff))

    screenshot_paths = _capture_screenshots()
    logger.info(
        "Captured %d screenshots: %s",
        len(screenshot_paths),
        ", ".join(p.name for p in screenshot_paths),
    )

    from src.agent.review import ReviewError, run_review

    try:
        review_body = run_review(
            diff=diff,
            screenshot_paths=screenshot_paths,
            openrouter_api_key=secrets["openrouter_api_key"],
        )
    except ReviewError:
        logger.exception("Review failed")
        sys.exit(1)

    _post_comment(
        token=token,
        repo_full_name=repo_full_name,
        pr_number=pr_number,
        body=review_body,
    )

    logger.info("RenderPR agent finished")


if __name__ == "__main__":
    run()
