import hashlib
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


def apply_edits(edits: list[dict]) -> bool:
    """Apply every edit atomically. If any edit fails to apply, restore all
    touched files to their pre-edit contents and return False.
    """
    snapshots: dict[Path, str] = {}
    for edit in edits:
        fp = Path(REPO_DIR) / edit["file"]
        if fp not in snapshots and fp.exists():
            snapshots[fp] = fp.read_text()

    for edit in edits:
        if not apply_edit(edit):
            for fp, original in snapshots.items():
                fp.write_text(original)
            return False
    return True


def revert_edits(edits: list[dict]) -> None:
    for file_path in {edit["file"] for edit in edits}:
        revert_edit({"file": file_path})


def _screenshots_identical(
    before: list[tuple[Path, str]],
    after: list[tuple[Path, str]],
) -> bool:
    """True when the after-edit screenshots are pixel-for-pixel identical to the
    before-edit ones (same labels, same bytes) — i.e. the edit changed nothing
    visible. Empty input is treated as "not identical" so a capture failure never
    masquerades as a no-op.
    """
    def by_label(results: list[tuple[Path, str]]) -> dict[str, str]:
        digests: dict[str, str] = {}
        for path, label in results:
            digests[label] = hashlib.md5(Path(path).read_bytes()).hexdigest()
        return digests

    before_digests = by_label(before)
    after_digests = by_label(after)
    return bool(before_digests) and before_digests == after_digests


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
        change = request_edit(query, openrouter_api_key, frontend_root)
    except EditGenerationError as e:
        return {"status": "error", "message": str(e)}

    edits = change["edits"]
    actions = change.get("actions", [])

    if not all(validate_edit(edit) for edit in edits):
        return {"status": "error", "message": "Could not validate the generated edit(s)."}

    # Resolve the routes to screenshot. This is independent of whether the edits
    # are applied, so it can be computed once and reused for the before/after pass.
    try:
        repo_tree = build_repo_tree()
        routes, mocks = infer_routes(diff, repo_tree, openrouter_api_key, framework)
    except Exception:
        routes = [{"path": "/", "actions": [], "reason": "fallback"}]
        mocks = {}

    if actions:
        source_contents = {
            edit["file"]: (Path(REPO_DIR) / edit["file"]).read_text(errors="replace")
            for edit in edits
            if (Path(REPO_DIR) / edit["file"]).exists()
        }
        routes = _validate_routes([{**route, "actions": actions} for route in routes], source_contents)
        logger.info(
            "Using validated edit-provided actions for all routes: %s",
            [route.get("actions", []) for route in routes],
        )

    strategy = get_strategy(framework, Path(REPO_DIR))
    edit_route: str | None = None
    for edit in edits:
        route = strategy.file_to_route(edit["file"])
        if route and not any(r["path"] == route for r in routes):
            routes.append({"path": route, "actions": [], "reason": "edit-target"})
            logger.info("Added edit route %s to capture list (was missing from inferred routes)", route)
        if route and edit_route is None:
            edit_route = route
    logger.info("Edit route: %s (from %d edit(s))", edit_route, len(edits))

    screenshot_dir = Path(REPO_DIR) / ".renderpr" / "screenshots"

    # Capture the current state first so we can prove the edit actually changes
    # something visible — a no-op edit must not be reported as a success.
    before = capture_screenshots(dev_server_url, screenshot_dir=screenshot_dir, routes=routes, mocks=mocks)

    if not apply_edits(edits):
        return {"status": "error", "message": "Could not apply the generated edit(s)."}

    if not wait_for_dev_server(dev_server_url, framework=framework):
        revert_edits(edits)
        return {"status": "error", "message": "The edit broke the build and was reverted. Try rephrasing your request."}

    after = capture_screenshots(dev_server_url, screenshot_dir=screenshot_dir, routes=routes, mocks=mocks)

    if _screenshots_identical(before, after):
        revert_edits(edits)
        logger.info("Edit produced no visible change across %d route(s); reverted", len(routes))
        return {"status": "no_visible_change", "edits": edits, "edit_route": edit_route}

    screenshot_urls = upload_screenshots(bucket, pr_number, after) if bucket else []

    return {
        "status": "success",
        "edits": edits,
        "edit_route": edit_route,
        "screenshot_paths": [p for p, _ in after],
        "screenshot_urls": screenshot_urls,
    }
