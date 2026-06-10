import logging
import time
from pathlib import Path

from src.agent.config import (
    DEV_SERVER_HEALTH_POLL_INTERVAL,
    DEV_SERVER_HEALTH_TIMEOUT,
    REPO_DIR,
)

logger = logging.getLogger(__name__)


def apply_edit(edit: dict) -> bool:
    filepath = Path(REPO_DIR) / edit["file"]
    if not filepath.exists():
        return False
    content = filepath.read_text()
    if edit["oldString"] not in content:
        return False
    new_content = content.replace(edit["oldString"], edit["newString"], 1)
    filepath.write_text(new_content)
    return True


def wait_for_dev_server(url: str, timeout: int | None = None, interval: float | None = None) -> bool:
    import httpx

    timeout = timeout or DEV_SERVER_HEALTH_TIMEOUT
    interval = interval or DEV_SERVER_HEALTH_POLL_INTERVAL
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = httpx.get(url, timeout=5)
            if resp.status_code == 200:
                return True
        except (httpx.HTTPError, ConnectionError):
            pass
        time.sleep(interval)
    return False


def revert_edit(edit: dict) -> None:
    import subprocess
    subprocess.run(
        ["git", "checkout", edit["file"]],
        cwd=REPO_DIR,
        capture_output=True,
        check=True,
    )


def execute_change(
    query: str,
    openrouter_api_key: str,
    dev_server_url: str,
    diff: str,
    bucket: str,
    pr_number: str,
    frontend_root: str | None = None,
) -> dict:
    from src.agent.code_edit import EditGenerationError, request_edit, validate_edit
    from src.agent.routes import build_repo_tree, infer_routes
    from src.agent.visual import capture_screenshots, upload_screenshots

    try:
        edit = request_edit(query, openrouter_api_key, frontend_root)
    except EditGenerationError as e:
        return {"status": "error", "message": str(e)}

    if not validate_edit(edit):
        return {"status": "error", "message": "Could not validate the generated edit."}

    if not apply_edit(edit):
        return {"status": "error", "message": f"Could not apply edit to {edit['file']}."}

    if not wait_for_dev_server(dev_server_url):
        revert_edit(edit)
        return {"status": "error", "message": "The edit broke the build and was reverted. Try rephrasing your request."}

    try:
        repo_tree = build_repo_tree()
        routes, mocks = infer_routes(diff, repo_tree, openrouter_api_key)
    except Exception:
        routes = [{"path": "/", "actions": [], "reason": "fallback"}]
        mocks = {}

    screenshot_dir = Path(REPO_DIR) / ".renderpr" / "screenshots"
    results = capture_screenshots(
        dev_server_url,
        screenshot_dir=screenshot_dir,
        routes=routes,
        mocks=mocks,
    )
    screenshot_urls = upload_screenshots(bucket, pr_number, results) if bucket else []

    return {
        "status": "success",
        "edit": edit,
        "screenshot_paths": [p for p, _ in results],
        "screenshot_urls": screenshot_urls,
    }
