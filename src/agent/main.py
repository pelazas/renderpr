import hashlib
import json
import os
import logging
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

import boto3
from botocore.exceptions import ClientError as BotoClientError
import httpx
import jwt

from src.agent.config import (
    NPM_CACHE_ENABLED,
    NPM_CACHE_HASH_ALGO,
    NPM_CACHE_PREFIX,
    NPM_CI_TIMEOUT_SECONDS,
    DEV_SERVER_HOST,
    DEV_SERVER_PORT,
    DEV_SERVER_START_TIMEOUT,
    DEV_SERVER_POLL_INTERVAL,
    REPO_DIR,
    RETRY_MAX_ATTEMPTS,
)
from src.agent.discovery import discover_frontend
from src.agent.polling import ChangeSession

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

    return {
        "app_id": github_data["app_id"],
        "private_key": github_data["private_key"],
        "openrouter_api_key": openrouter_resp["Parameter"]["Value"],
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


def _npm_cache_key(install_cwd: Path) -> str | None:
    try:
        if not install_cwd:
            return None
        lockfile = install_cwd / "package-lock.json"
        if not lockfile.exists():
            return None
        return hashlib.new(NPM_CACHE_HASH_ALGO, lockfile.read_bytes()).hexdigest()
    except OSError:
        return None


def _try_npm_cache_restore(install_cwd: Path, cache_key: str, s3) -> bool:
    bucket = os.environ.get("SCREENSHOT_BUCKET", "")
    if not bucket:
        return False
    key = f"{NPM_CACHE_PREFIX}/{cache_key}.tar.gz"
    tarball = Path("/tmp") / f"{cache_key}.tar.gz"
    try:
        s3.head_object(Bucket=bucket, Key=key)
        logger.info("npm cache HIT for %s, downloading...", cache_key[:12])
        s3.download_file(bucket, key, str(tarball))
        try:
            extract_dir = Path("/tmp") / f"{cache_key}_extracted"
            shutil.rmtree(extract_dir, ignore_errors=True)
            extract_dir.mkdir(parents=True, exist_ok=True)
            subprocess.run(["tar", "xzf", str(tarball), "-C", str(extract_dir)], check=True)
            node_modules_dst = install_cwd / "node_modules"
            shutil.rmtree(node_modules_dst, ignore_errors=True)
            shutil.move(str(extract_dir / "node_modules"), str(node_modules_dst))
            tarball.unlink()
            shutil.rmtree(extract_dir, ignore_errors=True)
            logger.info("npm cache restored")
            return True
        except (subprocess.CalledProcessError, OSError):
            logger.warning("Corrupted tarball, deleting and falling back")
            try:
                s3.delete_object(Bucket=bucket, Key=key)
            except Exception:
                pass
            tarball.unlink(missing_ok=True)
            return False
    except BotoClientError as e:
        if e.response["Error"]["Code"] == "NotFound":
            logger.info("npm cache MISS for %s", cache_key[:12])
        else:
            logger.warning("npm cache head_object error: %s", e)
    except Exception:
        logger.warning("npm cache restore failed unexpectedly", exc_info=True)
    return False


def _try_npm_cache_store(install_cwd: Path, cache_key: str | None) -> None:
    if not cache_key:
        return
    bucket = os.environ.get("SCREENSHOT_BUCKET", "")
    if not bucket:
        return
    key = f"{NPM_CACHE_PREFIX}/{cache_key}.tar.gz"
    try:
        tarball = Path("/tmp") / f"{cache_key}.tar.gz"
        subprocess.run(["tar", "czf", str(tarball), "-C", str(install_cwd), "node_modules"], check=True)
        s3 = boto3.client("s3")
        s3.upload_file(str(tarball), bucket, key)
        tarball.unlink()
        logger.info("npm cache stored for %s", cache_key[:12])
    except Exception:
        logger.warning("npm cache upload failed", exc_info=True)


_dev_server_proc: subprocess.Popen | None = None
_dev_server_url: str = ""


def _start_dev_server(
    package_dir: str | None = None,
    install_dir: str | None = None,
) -> None:
    global _dev_server_proc, _dev_server_url

    dev_cwd = Path(package_dir).parent if package_dir else Path(REPO_DIR)
    install_cwd = Path(install_dir).parent if install_dir else dev_cwd

    logger.info("Dev server cwd: %s (package_dir=%s)", dev_cwd, package_dir)
    logger.info("Install cwd: %s (install_dir=%s)", install_cwd, install_dir)

    pkg_json = os.path.join(dev_cwd, "package.json")
    if not os.path.exists(pkg_json):
        logger.error("No package.json found at %s", pkg_json)
        sys.exit(1)

    cache_restored = False
    cache_key = _npm_cache_key(install_cwd) if NPM_CACHE_ENABLED else None
    if cache_key is not None:
        try:
            s3 = boto3.client("s3")
            cache_restored = _try_npm_cache_restore(install_cwd, cache_key, s3)
        except Exception:
            logger.warning("npm cache check failed", exc_info=True)

    if not cache_restored:
        try:
            proc = subprocess.Popen(
                ["npm", "ci"],
                cwd=str(install_cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            out_lines: list[str] = []
            for line in iter(proc.stdout.readline, ""):
                out_lines.append(line)
                if len(out_lines) % 50 == 0:
                    logger.info("npm ci progress: ... %s", line.rstrip()[:200])
            proc.wait(timeout=NPM_CI_TIMEOUT_SECONDS)
            if proc.returncode != 0:
                logger.error("npm ci failed (exit %d) in %s", proc.returncode, install_cwd)
                logger.error("Last 20 lines:\n%s", "\n".join(out_lines[-20:]))
                sys.exit(1)
        except subprocess.TimeoutExpired:
            proc.kill()
            logger.error("npm ci timed out after %ds in %s. Last 20 lines:\n%s", NPM_CI_TIMEOUT_SECONDS, install_cwd, "\n".join(out_lines[-20:]))
            sys.exit(1)
        except Exception:
            logger.exception("npm ci failed unexpectedly")
            sys.exit(1)

        threading.Thread(target=_try_npm_cache_store, args=(install_cwd, cache_key), daemon=True).start()

    _dev_server_env = {**os.environ, "HOST": "0.0.0.0"}
    _dev_server_proc = subprocess.Popen(
        ["npm", "run", "dev"],
        cwd=str(dev_cwd),
        env=_dev_server_env,
    )

    _dev_server_url = f"http://{DEV_SERVER_HOST}:{DEV_SERVER_PORT}/"
    url = _dev_server_url
    deadline = time.time() + DEV_SERVER_START_TIMEOUT
    while time.time() < deadline:
        try:
            with httpx.Client(timeout=30) as client:
                resp = client.get(url)
            logger.info("Dev server ready (status %d)", resp.status_code)
            return
        except httpx.HTTPError:
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


def _fetch_pr_meta(token: str, repo_full_name: str, pr_number: str) -> dict:
    url = f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "RenderPR/1.0",
    }

    with httpx.Client(timeout=30) as client:
        resp = client.get(url, headers=headers)

    if resp.status_code != 200:
        logger.error("Failed to fetch PR meta: %d %s", resp.status_code, resp.text)
        sys.exit(1)

    data = resp.json()
    return {
        "head_ref": data["head"]["ref"],
        "is_fork": data["head"]["repo"]["fork"],
    }


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


def _capture_screenshots(
    diff: str,
    secrets: dict,
) -> tuple[list[Path], list[tuple[str, str]]]:
    from src.agent.routes import build_repo_tree, infer_routes
    from src.agent.visual import capture_screenshots, upload_screenshots

    repo_tree = build_repo_tree()
    routes, mocks = infer_routes(diff, repo_tree, secrets["openrouter_api_key"])
    logger.info("Routes to screenshot: %s", [r["path"] for r in routes])
    if mocks:
        logger.info("Mocks configured for %d domain(s): %s", len(mocks), list(mocks.keys()))

    screenshot_dir = Path(REPO_DIR) / ".renderpr" / "screenshots"
    results = capture_screenshots(_dev_server_url, screenshot_dir=screenshot_dir, routes=routes, mocks=mocks)

    bucket = os.environ.get("SCREENSHOT_BUCKET", "")
    pr_number = os.environ.get("PR_NUMBER", "0")
    if bucket:
        pairs = upload_screenshots(bucket, pr_number, results)
    else:
        logger.warning("SCREENSHOT_BUCKET not set, skipping upload")
        pairs = []

    return [p for p, _ in results], pairs


def _build_screenshot_grid(pairs: list[tuple[str, str]]) -> str:
    if not pairs:
        return ""

    rows: list[str] = []
    for i in range(0, len(pairs), 2):
        cells = ""
        for j in range(2):
            idx = i + j
            if idx < len(pairs):
                url, label = pairs[idx]
                cells += f'<td><img width="400" src="{url}" alt="{label}"><br><em>{label}</em></td>'
            else:
                cells += "<td></td>"
        rows.append(f"<tr>{cells}</tr>")

    return f"""<table>{''.join(rows)}</table>

---"""


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

    diff = _fetch_diff(
        token=token,
        repo_full_name=repo_full_name,
        pr_number=pr_number,
    )
    logger.info("Fetched diff for PR #%s (%d bytes)", pr_number, len(diff))
    logger.info("Changes: %s", _parse_diff_summary(diff))

    discovery = discover_frontend(diff)
    logger.info("Frontend discovery result: %s", {k: v for k, v in discovery.items() if k != "reason" or v is not None})
    if not discovery["has_frontend"]:
        _post_comment(
            token=token,
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            body=f"## RenderPR\n\n{discovery['reason']}\n\nSkipping review.",
        )
        logger.info("No frontend changes detected. Exiting gracefully.")
        return

    if discovery["package_json_path"] is None or discovery["dev_command"] is None:
        _post_comment(
            token=token,
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            body=f"## RenderPR\n\n{discovery['reason']}\n\nSkipping review.",
        )
        logger.info("Cannot start dev server. Exiting gracefully.")
        return

    _start_dev_server(
        package_dir=discovery["package_json_path"],
        install_dir=discovery.get("workspace_root"),
    )

    logger.info("Dev server ready. Proceeding to review...")

    screenshot_paths, screenshot_urls = _capture_screenshots(diff, secrets)
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
            screenshot_urls=screenshot_urls,
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

    logger.info("Initial review posted. Starting command server...")

    pr_meta = _fetch_pr_meta(
        token=token,
        repo_full_name=repo_full_name,
        pr_number=pr_number,
    )
    head_ref = pr_meta["head_ref"]
    is_fork = pr_meta["is_fork"]
    bucket = os.environ.get("SCREENSHOT_BUCKET", "")
    change_session = ChangeSession()

    from src.agent.command_server import CommandServer
    from src.agent.editor import execute_change

    def on_change(query: str) -> dict:
        frontend_root = discovery.get("package_json_path")
        if frontend_root:
            frontend_root = str(Path(frontend_root).parent)

        result = execute_change(
            query=query,
            openrouter_api_key=secrets["openrouter_api_key"],
            dev_server_url=_dev_server_url,
            diff=diff,
            bucket=bucket,
            pr_number=pr_number,
            frontend_root=frontend_root,
        )

        if result["status"] == "success":
            edit = result.get("edit", {})
            change_session.add_edit(edit.get("file", ""))
            screenshot_url = result["screenshot_urls"][0][0] if result.get("screenshot_urls") else ""
            public_ip = os.environ.get("RENDERPR_PUBLIC_IP", "localhost")

            body = f"""Here's the updated app with the change applied:

<img width="400" src="{screenshot_url}" alt="After change">

Live app: http://{public_ip}:3000

*Type @renderpr apply to accept, or @renderpr reject to reject the change.*"""
            _post_comment(
                token=token,
                repo_full_name=repo_full_name,
                pr_number=pr_number,
                body=body,
            )

        return result

    def on_apply() -> dict:
        if not change_session.edited_files:
            return {"status": "error", "message": "No pending changes to apply."}

        try:
            for file_path in change_session.edited_files:
                subprocess.run(
                    ["git", "add", "--", file_path],
                    cwd=REPO_DIR,
                    capture_output=True,
                    check=True,
                    timeout=30,
                )
            subprocess.run(
                ["git", "commit", "-m", "renderpr: apply suggested changes"],
                cwd=REPO_DIR,
                capture_output=True,
                check=True,
                timeout=30,
            )

            if is_fork:
                branch = f"renderpr/suggestion-{pr_number}"
                subprocess.run(
                    ["git", "push", "origin", f"HEAD:{branch}"],
                    cwd=REPO_DIR,
                    capture_output=True,
                    check=True,
                    timeout=60,
                )
                msg = f"Changes pushed to `{branch}` on the base repo."
            else:
                subprocess.run(
                    ["git", "push", "origin", f"HEAD:{head_ref}"],
                    cwd=REPO_DIR,
                    capture_output=True,
                    check=True,
                    timeout=60,
                )
                msg = "Changes applied and pushed to the PR."

            _post_comment(token, repo_full_name, pr_number, msg)
            change_session.clear()
            return {"status": "success", "message": msg}
        except subprocess.CalledProcessError as e:
            logger.exception("Apply failed: %s", e.stderr)
            return {"status": "error", "message": "Failed to apply changes. Check git state."}
        except subprocess.TimeoutExpired:
            logger.exception("Apply timed out")
            return {"status": "error", "message": "Apply timed out. Check git state."}

    def on_reject() -> dict:
        if not change_session.edited_files:
            return {"status": "error", "message": "No pending changes to reject."}

        try:
            for file_path in change_session.edited_files:
                subprocess.run(
                    ["git", "checkout", "--", file_path],
                    cwd=REPO_DIR,
                    capture_output=True,
                    check=True,
                    timeout=30,
                )
            change_session.clear()
            msg = "Changes reverted. The app is back to its original state."
            _post_comment(token, repo_full_name, pr_number, msg)
            return {"status": "success", "message": msg}
        except subprocess.CalledProcessError as e:
            logger.exception("Reject failed: %s", e.stderr)
            return {"status": "error", "message": "Failed to revert changes."}
        except subprocess.TimeoutExpired:
            logger.exception("Reject timed out")
            return {"status": "error", "message": "Reject timed out."}

    server = CommandServer(
        handle_change_fn=on_change,
        handle_apply_fn=on_apply,
        handle_reject_fn=on_reject,
    )
    server.start()

    from src.agent.network import get_public_ip
    public_ip = get_public_ip()
    os.environ["RENDERPR_PUBLIC_IP"] = public_ip
    logger.info("Public IP: %s", public_ip)

    boot_cmd = os.environ.get("COMMAND", "")
    if boot_cmd:
        logger.info("Received boot command: %s", boot_cmd)
        if boot_cmd.startswith("code_change::"):
            query = boot_cmd.removeprefix("code_change::")
            on_change(query)
        elif boot_cmd == "apply":
            on_apply()
        elif boot_cmd == "reject":
            on_reject()

    logger.info("RenderPR agent entering idle loop")
    server.wait_for_command()


if __name__ == "__main__":
    run()
