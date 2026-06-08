import json
import os
import logging
import sys
import time

import boto3
import httpx
import jwt

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


REPO_DIR = "/app/repo"


def _clone_repo(repo_full_name: str, pr_number: str, token: str) -> None:
    clone_url = f"https://x-access-token:{token}@github.com/{repo_full_name}.git"
    commands = [
        ["git", "clone", clone_url, REPO_DIR],
        ["git", "-C", REPO_DIR, "fetch", "origin", f"pull/{pr_number}/head:review-pr"],
        ["git", "-C", REPO_DIR, "checkout", "review-pr"],
    ]

    import subprocess

    for cmd in commands:
        for attempt in range(2):
            try:
                subprocess.run(cmd, capture_output=True, text=True, check=True)
                break
            except Exception:
                if attempt == 1:
                    logger.exception("Command failed after retry: %s", " ".join(cmd))
                    sys.exit(1)
                logger.warning("Command failed, retrying: %s", " ".join(cmd))


def _start_dev_server() -> None:
    import os
    import subprocess
    import time

    from src.agent.config import DEV_SERVER_START_TIMEOUT, DEV_SERVER_POLL_INTERVAL, DEV_SERVER_PORT, DEV_SERVER_HOST

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

    proc = subprocess.Popen(["npm", "run", "dev"], cwd=REPO_DIR)

    url = f"http://{DEV_SERVER_HOST}:{DEV_SERVER_PORT}/"
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
    proc.kill()
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

    for attempt in range(2):
        with httpx.Client() as client:
            resp = client.post(url, headers=headers)

        if resp.status_code == 201:
            return resp.json()["token"]

        logger.error(
            "GitHub API error (attempt %d/2): %d %s",
            attempt + 1, resp.status_code, resp.text,
        )

        if 400 <= resp.status_code < 500:
            break

        if attempt == 0:
            time.sleep(3)

    sys.exit(1)


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


if __name__ == "__main__":
    run()
