import hashlib
import json
import os
import logging
import re
import signal
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
    DEV_SERVER_CANDIDATE_PORTS,
    DEV_SERVER_URL_REGEX,
    LOCKFILES,
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
from src.agent.env_inject import build_injected_env, write_env_local
from src.agent.mock_server import BACKUP_SUFFIX, write_dev_origin_allowlist, write_server_mocks
from src.agent.polling import ChangeSession
from src.agent.renderpr_config import ConfigError, load_config
from src.agent.secrets import load_repo_secrets
from src.agent.stack import LaunchProfile

_DEV_URL_RE = re.compile(DEV_SERVER_URL_REGEX)

logger = logging.getLogger(__name__)


def _fetch_command_token() -> str:
    param_name = os.environ.get("COMMAND_TOKEN_PARAM_NAME", "/renderpr/renderpr-command-token")
    ssm = boto3.client("ssm")
    try:
        resp = ssm.get_parameter(Name=param_name, WithDecryption=True)
    except Exception:
        logger.exception("Failed to fetch command token from SSM")
        sys.exit(1)
    return resp["Parameter"]["Value"]


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


def _npm_cache_key(install_cwd: Path, package_manager: str = "npm") -> str | None:
    """Cache key from whichever lockfile this package manager uses.

    Keyed as ``{pm}-{lockfile-hash}`` so different dependency trees — and
    different package managers — never collide. Returns None when there's no
    lockfile (nothing stable to key on).
    """
    try:
        if not install_cwd:
            return None
        lockfiles = [name for name, pm in LOCKFILES.items() if pm == package_manager]
        for name in lockfiles:
            lockfile = install_cwd / name
            if lockfile.exists():
                digest = hashlib.new(NPM_CACHE_HASH_ALGO, lockfile.read_bytes()).hexdigest()
                return f"{package_manager}-{digest}"
        return None
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
        # head_object on a missing key surfaces as "404"; other clients/ops may
        # use "NotFound"/"NoSuchKey". Treat all as a normal cache miss.
        if e.response["Error"]["Code"] in ("404", "NotFound", "NoSuchKey"):
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
    if not (install_cwd / "node_modules").is_dir():
        logger.warning("npm cache store skipped: no node_modules in %s", install_cwd)
        return
    key = f"{NPM_CACHE_PREFIX}/{cache_key}.tar.gz"
    tarball = Path("/tmp") / f"{cache_key}.tar.gz"
    try:
        # Archive node_modules for any package manager (npm, yarn, pnpm, bun).
        # Exclude derived/volatile dirs (e.g. Vite's dep cache) that the dev
        # server rewrites concurrently — they regenerate on startup and are the
        # usual cause of "file changed as we read it" churn. tar's exit codes:
        # 0 = ok, 1 = non-fatal warnings (changed/vanished files), 2 = fatal.
        # bun/yarn/npm all produced exit 1 here when node_modules churned, which
        # the old check=True turned into a hard failure (no cache stored).
        result = subprocess.run(
            [
                "tar",
                "--ignore-failed-read",
                "--warning=no-file-changed",
                "--exclude=node_modules/.vite",
                "--exclude=node_modules/.cache",
                "-czf", str(tarball),
                "-C", str(install_cwd),
                "node_modules",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode >= 2:
            logger.warning(
                "npm cache store: tar failed (exit %d) for %s: %s",
                result.returncode, cache_key[:12], (result.stderr or "")[-500:],
            )
            return
        if result.returncode == 1:
            logger.info("npm cache store: tar reported non-fatal warnings; archive still usable")
        s3 = boto3.client("s3")
        s3.upload_file(str(tarball), bucket, key)
        logger.info("npm cache stored for %s", cache_key[:12])
    except Exception:
        logger.warning("npm cache upload failed", exc_info=True)
    finally:
        tarball.unlink(missing_ok=True)


_dev_server_proc: subprocess.Popen | None = None
_dev_server_url: str = ""
_dev_server_port: int = DEV_SERVER_PORT
_framework: str = "next"
_runtime_generated_files: set[str] = set()


def _ensure_pnpm_hoisted(install_cwd: Path) -> list[str]:
    """Force pnpm into a flat (hoisted) node_modules so the tar-to-S3 cache
    captures real files, not dangling symlinks into pnpm's global store.

    Returns the runtime-generated paths (relative to REPO_DIR) to exclude from
    any later apply commit.
    """
    npmrc = install_cwd / ".npmrc"
    repo_path = Path(REPO_DIR)
    generated: list[str] = []
    try:
        if npmrc.exists():
            content = npmrc.read_text()
            if "node-linker" in content:
                return []
            backup = npmrc.with_name(f".npmrc{BACKUP_SUFFIX}")
            if not backup.exists():
                shutil.copy2(npmrc, backup)
                generated.append(str(backup.relative_to(repo_path)))
            npmrc.write_text(content.rstrip() + "\nnode-linker=hoisted\n")
        else:
            npmrc.write_text("node-linker=hoisted\n")
        generated.append(str(npmrc.relative_to(repo_path)))
        logger.info("Forced pnpm node-linker=hoisted in %s", npmrc)
    except (OSError, ValueError):
        logger.warning("Could not write pnpm .npmrc in %s", install_cwd, exc_info=True)
        return []
    return generated


def _run_install(install_command: list[str], install_cwd: Path) -> None:
    label = " ".join(install_command)
    try:
        proc = subprocess.Popen(
            install_command,
            cwd=str(install_cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert proc.stdout is not None
        out_lines: list[str] = []
        for line in iter(proc.stdout.readline, ""):
            out_lines.append(line)
            if len(out_lines) % 50 == 0:
                logger.info("%s progress: ... %s", label, line.rstrip()[:200])
        proc.wait(timeout=NPM_CI_TIMEOUT_SECONDS)
        if proc.returncode != 0:
            logger.error("%s failed (exit %d) in %s", label, proc.returncode, install_cwd)
            logger.error("Last 20 lines:\n%s", "\n".join(out_lines[-20:]))
            sys.exit(1)
    except subprocess.TimeoutExpired:
        proc.kill()
        logger.error("%s timed out after %ds in %s. Last 20 lines:\n%s", label, NPM_CI_TIMEOUT_SECONDS, install_cwd, "\n".join(out_lines[-20:]))
        sys.exit(1)
    except Exception:
        logger.exception("%s failed unexpectedly", label)
        sys.exit(1)


def _http_ok(port: int) -> bool:
    url = f"http://{DEV_SERVER_HOST}:{port}/"
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.get(url)
        logger.info("Dev server responded on port %d (status %d)", port, resp.status_code)
        return True
    except httpx.HTTPError:
        return False


def _start_dev_process(
    dev_command: list[str],
    dev_cwd: Path,
    dev_env: dict[str, str],
) -> tuple[subprocess.Popen, dict]:
    """Launch the dev server and drain its stdout in a thread, sniffing the
    first printed "http://...:PORT" banner into the returned holder.
    """
    proc = subprocess.Popen(
        dev_command,
        cwd=str(dev_cwd),
        env=dev_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert proc.stdout is not None
    sniffed: dict = {"port": None}

    def _drain() -> None:
        assert proc.stdout is not None
        for line in iter(proc.stdout.readline, ""):
            logger.info("dev server: %s", line.rstrip()[:200])
            if sniffed["port"] is None:
                match = _DEV_URL_RE.search(line)
                if match:
                    sniffed["port"] = int(match.group(1))
                    logger.info("Sniffed dev server port: %d", sniffed["port"])

    threading.Thread(target=_drain, daemon=True).start()
    return proc, sniffed


def _resolve_ready_port(proc: subprocess.Popen, sniffed: dict, default_port: int) -> int | None:
    """Poll until the dev server answers HTTP. Prefer the sniffed port once
    known; otherwise probe the framework default and candidate ports.
    """
    deadline = time.time() + DEV_SERVER_START_TIMEOUT
    while time.time() < deadline:
        if proc.poll() is not None:
            logger.error("Dev server exited early (code %s) before becoming ready", proc.returncode)
            return None
        if sniffed["port"] is not None:
            if _http_ok(sniffed["port"]):
                return sniffed["port"]
        else:
            for port in (default_port, *DEV_SERVER_CANDIDATE_PORTS):
                if _http_ok(port):
                    return port
        time.sleep(DEV_SERVER_POLL_INTERVAL)
    return None


def _start_dev_server(
    profile: LaunchProfile,
    package_dir: str | None = None,
    install_dir: str | None = None,
    injected_env: dict[str, str] | None = None,
) -> None:
    global _dev_server_proc, _dev_server_url, _dev_server_port, _framework

    _framework = profile.framework
    dev_cwd = Path(package_dir).parent if package_dir else Path(REPO_DIR)
    install_cwd = Path(install_dir).parent if install_dir else dev_cwd

    logger.info("Dev server cwd: %s (package_dir=%s)", dev_cwd, package_dir)
    logger.info("Install cwd: %s (install_dir=%s)", install_cwd, install_dir)

    pkg_json = os.path.join(dev_cwd, "package.json")
    if not os.path.exists(pkg_json):
        logger.error("No package.json found at %s", pkg_json)
        sys.exit(1)

    if profile.package_manager == "pnpm":
        _runtime_generated_files.update(_ensure_pnpm_hoisted(install_cwd))

    cache_restored = False
    cache_key = _npm_cache_key(install_cwd, profile.package_manager) if NPM_CACHE_ENABLED else None
    if cache_key is not None:
        try:
            s3 = boto3.client("s3")
            cache_restored = _try_npm_cache_restore(install_cwd, cache_key, s3)
        except Exception:
            logger.warning("dependency cache check failed", exc_info=True)

    if not cache_restored:
        _run_install(profile.install_command, install_cwd)
        threading.Thread(target=_try_npm_cache_store, args=(install_cwd, cache_key), daemon=True).start()

    # Layer precedence: ambient env, then user-provided secrets/vars, then the
    # framework launch profile's env (host flags etc.) which must win for a correct boot.
    dev_env = {**os.environ, **(injected_env or {}), **profile.dev_env}
    _dev_server_proc, sniffed = _start_dev_process(profile.dev_command, dev_cwd, dev_env)

    port = _resolve_ready_port(_dev_server_proc, sniffed, profile.default_port)
    if port is None:
        logger.error("Dev server did not start within %ds", DEV_SERVER_START_TIMEOUT)
        if _dev_server_proc:
            _dev_server_proc.kill()
        sys.exit(1)

    _dev_server_port = port
    _dev_server_url = f"http://{DEV_SERVER_HOST}:{port}/"
    logger.info("Dev server ready at %s", _dev_server_url)


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
    auth_session=None,
) -> tuple[list[Path], list[tuple[str, str]], list[dict]]:
    from src.agent.routes import build_repo_tree, infer_routes
    from src.agent.visual import capture_screenshots, upload_screenshots

    repo_tree = build_repo_tree()
    routes, mocks = infer_routes(diff, repo_tree, secrets["openrouter_api_key"], _framework)
    logger.info("Routes to screenshot: %s", [r["path"] for r in routes])
    if mocks:
        logger.info("Mocks configured for %d domain(s): %s", len(mocks), list(mocks.keys()))
        generated = write_server_mocks(Path(REPO_DIR), mocks, _framework)
        _runtime_generated_files.update(generated)

    screenshot_dir = Path(REPO_DIR) / ".renderpr" / "screenshots"
    login_signals: list[dict] = []
    results = capture_screenshots(
        _dev_server_url, screenshot_dir=screenshot_dir, routes=routes, mocks=mocks,
        storage_state=auth_session.storage_state if auth_session else None,
        entry_url=auth_session.entry_url if auth_session else None,
        login_signals=login_signals,
    )

    bucket = os.environ.get("SCREENSHOT_BUCKET", "")
    pr_number = os.environ.get("PR_NUMBER", "0")
    if bucket:
        pairs = upload_screenshots(bucket, pr_number, results)
    else:
        logger.warning("SCREENSHOT_BUCKET not set, skipping upload")
        pairs = []

    return [p for p, _ in results], pairs, login_signals


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


def _post_comment(token: str, repo_full_name: str, pr_number: str, body: str) -> int | None:
    """Post a new PR comment. Returns the new comment's id, or None on failure."""
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
        return None

    logger.info("Comment posted to PR #%s", pr_number)
    try:
        return resp.json().get("id")
    except Exception:
        logger.warning("Comment posted but could not parse its id from the response")
        return None


def _update_comment(token: str, repo_full_name: str, comment_id: int, body: str) -> bool:
    """Edit an existing PR comment in place. Best-effort: never raises, returns success."""
    url = f"https://api.github.com/repos/{repo_full_name}/issues/comments/{comment_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "RenderPR/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        with httpx.Client(timeout=30) as client:
            resp = client.patch(url, headers=headers, json={"body": body})
    except Exception:
        logger.exception("Failed to update comment %s", comment_id)
        return False

    if resp.status_code != 200:
        logger.error("Failed to update comment %s: %d %s", comment_id, resp.status_code, resp.text)
        return False

    return True


# Ordered stages shown in the live progress comment while a review runs.
PROGRESS_STAGES = [
    "Setting up preview environment",
    "Installing dependencies & starting dev server",
    "Capturing screenshots",
    "Generating review",
]


def _render_progress(current: int, failed_at: int | None = None) -> str:
    """Render the progress checklist. Marks `current` as in-progress, earlier as done.

    When `failed_at` is set, that stage is marked failed and later stages stay pending.
    """
    lines = []
    for i, label in enumerate(PROGRESS_STAGES):
        if failed_at is not None and i == failed_at:
            marker = "❌"
        elif failed_at is not None and i > failed_at:
            marker = "⬜"
        elif i < current:
            marker = "✅"
        elif i == current:
            marker = "🔄"
        else:
            marker = "⬜"
        lines.append(f"- {marker} {label}")
    checklist = "\n".join(lines)

    if failed_at is not None:
        header = (
            "## ❌ RenderPR review failed\n\n"
            f"Something went wrong while **{PROGRESS_STAGES[failed_at].lower()}**. "
            "Check the task logs, or comment `@renderpr review` to try again."
        )
    else:
        header = (
            "## 🔄 RenderPR is reviewing this PR\n\n"
            "This usually takes **5–7 minutes**. I'll update this comment as I go."
        )
    return f"{header}\n\n{checklist}"


def _append_live_preview_link(body: str, public_ip: str) -> str:
    if not public_ip:
        return body
    return f"{body}\n\n---\n\nLive app: http://{public_ip}:{_dev_server_port}"


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
    command_token = _fetch_command_token()
    os.environ["RENDERPR_COMMAND_TOKEN"] = command_token
    token = _get_installation_token(
        installation_id=installation_id,
        app_id=secrets["app_id"],
        private_key=secrets["private_key"],
    )
    skip_review = os.environ.get("SKIP_REVIEW", "false").lower() == "true"

    # Post an immediate placeholder so the PR shows the review is underway, then edit
    # this same comment as stages complete (and into the final review or an error).
    # Conversational re-runs (change/apply) set SKIP_REVIEW and get no placeholder.
    progress_comment_id = (
        None
        if skip_review
        else _post_comment(token, repo_full_name, pr_number, body=_render_progress(0))
    )
    current_stage = 0

    def update_progress(stage: int) -> None:
        nonlocal current_stage
        current_stage = stage
        if progress_comment_id is not None:
            _update_comment(token, repo_full_name, progress_comment_id, body=_render_progress(stage))

    def finalize(body: str) -> None:
        """Replace the progress comment with a final message, or post fresh if none exists."""
        if progress_comment_id is not None and _update_comment(
            token, repo_full_name, progress_comment_id, body=body
        ):
            return
        _post_comment(token, repo_full_name, pr_number, body=body)

    try:
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
            finalize(f"## RenderPR\n\n{discovery['reason']}\n\nSkipping review.")
            logger.info("No frontend changes detected. Exiting gracefully.")
            return

        if discovery["package_json_path"] is None or discovery["dev_command"] is None:
            finalize(f"## RenderPR\n\n{discovery['reason']}\n\nSkipping review.")
            logger.info("Cannot start dev server. Exiting gracefully.")
            return

        # Fork status gates env/secret injection (secrets never touch fork PRs).
        # Fetched once here, before the dev server starts, and reused for apply.
        pr_meta = _fetch_pr_meta(token=token, repo_full_name=repo_full_name, pr_number=pr_number)
        is_fork = pr_meta["is_fork"]

        try:
            repo_config = load_config(REPO_DIR)
        except ConfigError as exc:
            finalize(
                "## RenderPR\n\n"
                f"Invalid `.renderpr.yml`: {exc}\n\n"
                "Fix the config and comment `@renderpr review` to retry."
            )
            logger.error("Invalid .renderpr.yml: %s", exc)
            return

        repo_secrets = load_repo_secrets(installation_id, repo_full_name, is_fork)
        frontend_root = str(Path(discovery["package_json_path"]).parent)
        injected_env, missing_env = build_injected_env(frontend_root, repo_config["env"], repo_secrets)
        env_local_rel = write_env_local(frontend_root, injected_env, REPO_DIR)
        if env_local_rel:
            _runtime_generated_files.add(env_local_rel)

        from src.agent.network import get_public_ip, get_task_arn
        from src.agent.registration import register_task, deregister_task
        public_ip = get_public_ip()
        task_arn = get_task_arn()
        os.environ["RENDERPR_PUBLIC_IP"] = public_ip
        logger.info("Public IP: %s", public_ip)
        logger.info("Task ARN: %s", task_arn)

        def _shutdown(_signum, _frame):
            deregister_task(pr_number)
            sys.exit(0)

        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)

        _runtime_generated_files.update(
            write_dev_origin_allowlist(Path(REPO_DIR), public_ip, discovery["launch_profile"].framework)
        )

        update_progress(1)
        _start_dev_server(
            profile=discovery["launch_profile"],
            package_dir=discovery["package_json_path"],
            install_dir=discovery.get("workspace_root"),
            injected_env=injected_env,
        )

        logger.info("Dev server ready. Proceeding to review...")

        # Build a synthetic auth session (forged/minted) to get past login walls.
        # On forks repo_secrets is empty, so this is None and nothing is injected.
        from src.agent.auth import build_session
        auth_session = build_session(repo_config["auth"], repo_secrets, _dev_server_url)

        def do_review(use_progress: bool = False) -> None:
            from src.agent.review import ReviewError, run_review

            # Only the initial review edits the progress comment; re-runs post fresh.
            pid = progress_comment_id if use_progress else None

            if pid is not None:
                update_progress(2)
            screenshot_paths, screenshot_urls, login_walls = _capture_screenshots(diff, secrets, auth_session)
            logger.info(
                "Captured %d screenshots: %s",
                len(screenshot_paths),
                ", ".join(p.name for p in screenshot_paths),
            )

            # Don't review a login screen: if pages redirected to a login wall and no
            # auth was configured, degrade with guidance instead of a bogus review.
            if login_walls and auth_session is None:
                walled = ", ".join(sorted({w["path"] for w in login_walls}))
                guidance = (
                    "## RenderPR\n\n"
                    f"This app appears to require **login** (redirected to a login page on: {walled}).\n\n"
                    "Configure auth in `.renderpr.yml` (and store the secret) so RenderPR can "
                    "preview it as a signed-in user — see the `auth` block in the docs. "
                    "Skipping the review rather than capturing a login screen."
                )
                if pid is not None and _update_comment(token, repo_full_name, pid, body=guidance):
                    return
                _post_comment(token, repo_full_name, pr_number, body=guidance)
                return
            if login_walls:
                logger.warning("Login wall hit despite configured auth on: %s",
                               [w["path"] for w in login_walls])

            if pid is not None:
                update_progress(3)
            try:
                review_body = run_review(
                    diff=diff,
                    screenshot_paths=screenshot_paths,
                    openrouter_api_key=secrets["openrouter_api_key"],
                    screenshot_urls=screenshot_urls,
                )
            except ReviewError:
                logger.exception("Review failed")
                if pid is not None:
                    _update_comment(token, repo_full_name, pid, body=_render_progress(3, failed_at=3))
                return

            review_body = _append_live_preview_link(review_body, public_ip)

            if pid is not None and _update_comment(token, repo_full_name, pid, body=review_body):
                logger.info("Review posted (edited progress comment) to PR #%s", pr_number)
            else:
                _post_comment(token, repo_full_name, pr_number, body=review_body)
                logger.info("Review posted to PR #%s", pr_number)

        if skip_review:
            logger.info("SKIP_REVIEW=true, skipping initial review")
        else:
            do_review(use_progress=True)
            logger.info("Initial review posted. Starting command server...")
    except BaseException:
        logger.exception("Review pipeline failed at stage %d", current_stage)
        if progress_comment_id is not None:
            _update_comment(
                token,
                repo_full_name,
                progress_comment_id,
                body=_render_progress(current_stage, failed_at=current_stage),
            )
        raise

    # pr_meta / is_fork were fetched before the dev server started (for the fork gate).
    head_ref = pr_meta["head_ref"]
    bucket = os.environ.get("SCREENSHOT_BUCKET", "")
    change_session = ChangeSession()
    for runtime_file in _runtime_generated_files:
        change_session.add_runtime_file(runtime_file)

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
            framework=discovery["launch_profile"].framework,
        )

        if result["status"] == "no_visible_change":
            _post_comment(
                token=token,
                repo_full_name=repo_full_name,
                pr_number=pr_number,
                body=(
                    "I made an edit, but it produced **no visible change** in the screenshots, "
                    "so I reverted it. Could you point me to the specific element and the change "
                    "you want? (e.g. _\"make the main headline orange\"_)."
                ),
            )
            return result

        if result["status"] == "error":
            _post_comment(
                token=token,
                repo_full_name=repo_full_name,
                pr_number=pr_number,
                body=f"⚠️ I couldn't make that change: {result.get('message', 'unknown error')}",
            )
            return result

        if result["status"] == "success":
            edits = result.get("edits", [])
            for edit in edits:
                change_session.add_edit(edit.get("file", ""))
            edit_route = result.get("edit_route")
            screenshot_urls = result.get("screenshot_urls", [])

            if edit_route:
                prefix = f"Desktop - {edit_route}"
                screenshot_url = next(
                    (url for url, label in screenshot_urls if label == prefix),
                    next(
                        (url for url, label in screenshot_urls if label.startswith("Desktop -")),
                        screenshot_urls[0][0] if screenshot_urls else "",
                    ),
                )
            else:
                screenshot_url = next(
                    (url for url, label in screenshot_urls if label.startswith("Desktop -")),
                    screenshot_urls[0][0] if screenshot_urls else "",
                )
            public_ip = os.environ.get("RENDERPR_PUBLIC_IP", "localhost")

            body = f"""Here's the updated app with the change applied:

<img width="400" src="{screenshot_url}" alt="After change">

Live app: http://{public_ip}:{_dev_server_port}

*Type @renderpr apply to accept the change, or leave it uncommitted to discard it.*"""
            _post_comment(
                token=token,
                repo_full_name=repo_full_name,
                pr_number=pr_number,
                body=body,
            )

        return result

    def on_apply() -> dict:
        stageable_files = change_session.stageable_edits()
        if not stageable_files:
            return {"status": "error", "message": "No pending changes to apply."}

        try:
            for file_path in stageable_files:
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

    def on_review() -> dict:
        do_review()
        return {"status": "success", "message": "Review re-posted."}

    server = CommandServer(
        handle_change_fn=on_change,
        handle_apply_fn=on_apply,
        handle_review_fn=on_review,
    )
    server.start()

    if task_arn and public_ip != "localhost":
        register_task(pr_number, task_arn, public_ip)

    boot_cmd = os.environ.get("COMMAND", "")
    if boot_cmd:
        logger.info("Received boot command: %s", boot_cmd)
        if boot_cmd.startswith("code_change::"):
            query = boot_cmd.removeprefix("code_change::")
            on_change(query)
        elif boot_cmd == "apply":
            on_apply()

    logger.info("RenderPR agent entering idle loop")
    server.wait_for_command()


if __name__ == "__main__":
    run()
