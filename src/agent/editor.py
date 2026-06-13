import logging
import time
from pathlib import Path

from src.agent.config import (
    DEV_SERVER_HEALTH_POLL_INTERVAL,
    DEV_SERVER_HEALTH_TIMEOUT,
    REPO_DIR,
)
from src.agent.routing import get_strategy

logger = logging.getLogger(__name__)

# Build-error overlay markers by framework family. The dev server returns HTTP
# 200 with one of these in the body when a hot edit fails to compile, so we
# treat that as "not ready" instead of screenshotting a broken page.
_OVERLAY_MARKERS: dict[str, tuple[str, ...]] = {
    "next": (
        "nextjs__container_errors",
        "__next_error__",
        "__nextjs_original-stack-frame",
    ),
    "vite": (
        "vite-error-overlay",
        "[plugin:vite",
    ),
}
_GENERIC_OVERLAY_MARKERS: tuple[str, ...] = (
    "Application error",
    "Build Error",
    "Failed to compile",
    "Internal Server Error",
)


def _find_occurrence(content: str, old_string: str, line_hint: int | None) -> int | None:
    """Find the index in content where old_string occurs, preferring the occurrence
    closest to the given line_hint. Returns the byte offset or None if not found.
    """
    occurrences: list[int] = []
    start = 0
    while True:
        idx = content.find(old_string, start)
        if idx == -1:
            break
        occurrences.append(idx)
        start = idx + 1
    if not occurrences:
        return None
    if len(occurrences) == 1 or line_hint is None:
        return occurrences[0]

    line_offsets: list[int] = [0]
    for i, ch in enumerate(content):
        if ch == "\n":
            line_offsets.append(i + 1)
    target_offset = line_offsets[line_hint - 1] if 0 < line_hint <= len(line_offsets) else 0

    return min(occurrences, key=lambda o: abs(o - target_offset))


def apply_edit(edit: dict) -> bool:
    filepath = Path(REPO_DIR) / edit["file"]
    if not filepath.exists():
        return False
    content = filepath.read_text()
    line_hint = edit.get("line")
    pos = _find_occurrence(content, edit["oldString"], line_hint)
    if pos is None:
        return False
    new_content = content[:pos] + edit["newString"] + content[pos + len(edit["oldString"]):]
    filepath.write_text(new_content)
    return True


def wait_for_dev_server(
    url: str,
    timeout: int | None = None,
    interval: float | None = None,
    framework: str = "next",
) -> bool:
    import httpx

    timeout = timeout or DEV_SERVER_HEALTH_TIMEOUT
    interval = interval or DEV_SERVER_HEALTH_POLL_INTERVAL
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = httpx.get(url, timeout=5)
            if resp.status_code == 200:
                body = resp.text
                if _has_dev_error_overlay(body, framework):
                    logger.warning("Dev server returned 200 but contains error overlay")
                    time.sleep(interval)
                    continue
                return True
        except (httpx.HTTPError, ConnectionError):
            pass
        time.sleep(interval)
    return False


def _has_dev_error_overlay(body: str, framework: str = "next") -> bool:
    markers = _GENERIC_OVERLAY_MARKERS + _OVERLAY_MARKERS.get(framework, ())
    return any(m in body for m in markers)


def revert_edit(edit: dict) -> None:
    import subprocess
    subprocess.run(
        ["git", "checkout", edit["file"]],
        cwd=REPO_DIR,
        capture_output=True,
        check=True,
        timeout=30,
    )


def execute_change(
    query: str,
    openrouter_api_key: str,
    dev_server_url: str,
    diff: str,
    bucket: str,
    pr_number: str,
    frontend_root: str | None = None,
    framework: str = "next",
) -> dict:
    from src.agent.code_edit import EditGenerationError, request_edit, validate_edit
    from src.agent.routes import build_repo_tree, infer_routes, _validate_routes
    from src.agent.visual import capture_screenshots, upload_screenshots

    try:
        edit = request_edit(query, openrouter_api_key, frontend_root)
    except EditGenerationError as e:
        return {"status": "error", "message": str(e)}

    if not validate_edit(edit):
        return {"status": "error", "message": "Could not validate the generated edit."}

    if not apply_edit(edit):
        return {"status": "error", "message": f"Could not apply edit to {edit['file']}."}

    if not wait_for_dev_server(dev_server_url, framework=framework):
        revert_edit(edit)
        return {"status": "error", "message": "The edit broke the build and was reverted. Try rephrasing your request."}

    try:
        repo_tree = build_repo_tree()
        routes, mocks = infer_routes(diff, repo_tree, openrouter_api_key, framework)
    except Exception:
        routes = [{"path": "/", "actions": [], "reason": "fallback"}]
        mocks = {}

    edit_actions = edit.get("actions", [])
    if edit_actions:
        source_path = Path(REPO_DIR) / edit["file"]
        source_contents = {edit["file"]: source_path.read_text(errors="replace")} if source_path.exists() else {}
        routes_with_actions = [
            {**route, "actions": edit_actions}
            for route in routes
        ]
        routes = _validate_routes(routes_with_actions, source_contents)
        logger.info(
            "Using validated edit-provided actions for all routes: %s",
            [route.get("actions", []) for route in routes],
        )

    edit_route = get_strategy(framework, Path(REPO_DIR)).file_to_route(edit["file"])
    logger.info("Edit route for %s: %s", edit["file"], edit_route)

    if edit_route and not any(r["path"] == edit_route for r in routes):
        routes.append({"path": edit_route, "actions": [], "reason": "edit-target"})
        logger.info("Added edit route %s to capture list (was missing from inferred routes)", edit_route)

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
        "edit_route": edit_route,
        "screenshot_paths": [p for p, _ in results],
        "screenshot_urls": screenshot_urls,
    }
