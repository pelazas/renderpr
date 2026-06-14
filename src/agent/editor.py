import hashlib
import logging
import time
from pathlib import Path

from src.agent.config import (
    DEV_SERVER_HEALTH_POLL_INTERVAL,
    DEV_SERVER_HEALTH_TIMEOUT,
    EDIT_MAX_ATTEMPTS,
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


def _select_grounding_images(results: list[tuple[Path, str]], limit: int = 3) -> list[bytes]:
    """Pick a few representative screenshots (preferring the Desktop baseline of
    each route) to hand the edit model as visual grounding."""
    chosen = [
        Path(path).read_bytes()
        for path, label in results
        if "Desktop -" in label and "interaction" not in label
    ]
    if not chosen:
        chosen = [Path(path).read_bytes() for path, _ in results]
    return chosen[:limit]


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


def _describe_attempt(edits: list[dict], reason: str) -> str:
    changes = "; ".join(
        f"{edit.get('file')}: '{edit.get('oldString')}' -> '{edit.get('newString')}'" for edit in edits
    )
    return f"Edited [{changes}] but {reason}."


def _routes_for_edits(base_routes: list[dict], edits: list[dict], actions: list, framework: str) -> tuple[list[dict], str | None]:
    """Expand the diff-inferred routes with edit-provided actions and the route
    that renders each edited file, returning (routes, primary_edit_route)."""
    from src.agent.routes import _validate_routes

    if actions:
        source_contents = {
            edit["file"]: (Path(REPO_DIR) / edit["file"]).read_text(errors="replace")
            for edit in edits
            if (Path(REPO_DIR) / edit["file"]).exists()
        }
        routes = _validate_routes([{**route, "actions": actions} for route in base_routes], source_contents)
    else:
        routes = [dict(route) for route in base_routes]

    strategy = get_strategy(framework, Path(REPO_DIR))
    edit_route: str | None = None
    for edit in edits:
        route = strategy.file_to_route(edit["file"])
        if route and not any(r["path"] == route for r in routes):
            routes.append({"path": route, "actions": [], "reason": "edit-target"})
        if route and edit_route is None:
            edit_route = route
    return routes, edit_route


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
    from src.agent.routes import build_repo_tree, infer_routes
    from src.agent.visual import capture_screenshots, upload_screenshots

    try:
        repo_tree = build_repo_tree()
        base_routes, mocks = infer_routes(diff, repo_tree, openrouter_api_key, framework)
    except Exception:
        base_routes = [{"path": "/", "actions": [], "reason": "fallback"}]
        mocks = {}

    screenshot_dir = Path(REPO_DIR) / ".renderpr" / "screenshots"
    # Screenshot the current page once: it grounds the edit model visually and is
    # the "before" baseline. Each failed attempt reverts, so it stays valid.
    before = capture_screenshots(dev_server_url, screenshot_dir=screenshot_dir, routes=base_routes, mocks=mocks)
    grounding = _select_grounding_images(before)

    feedback: list[str] = []
    last_edits: list[dict] = []
    last_route: str | None = None

    for attempt in range(EDIT_MAX_ATTEMPTS):
        try:
            change = request_edit(query, openrouter_api_key, frontend_root, images=grounding, feedback=feedback)
        except EditGenerationError as e:
            return {"status": "error", "message": str(e)}

        edits = change["edits"]
        last_edits = edits
        logger.info("Edit attempt %d/%d: %s", attempt + 1, EDIT_MAX_ATTEMPTS,
                    [(e.get("file"), e.get("newString")) for e in edits])

        if not all(validate_edit(edit) for edit in edits):
            feedback.append(_describe_attempt(edits, "the oldString did not match the file exactly"))
            continue

        routes, last_route = _routes_for_edits(base_routes, edits, change.get("actions", []), framework)

        if not apply_edits(edits):
            feedback.append(_describe_attempt(edits, "the edit could not be applied"))
            continue

        if not wait_for_dev_server(dev_server_url, framework=framework):
            revert_edits(edits)
            feedback.append(_describe_attempt(edits, "it broke the build"))
            continue

        after = capture_screenshots(dev_server_url, screenshot_dir=screenshot_dir, routes=routes, mocks=mocks)

        if _screenshots_identical(before, after):
            revert_edits(edits)
            logger.info("Attempt %d produced no visible change; retrying with feedback", attempt + 1)
            feedback.append(_describe_attempt(edits, "it changed NOTHING visible — the target still looks the same in the screenshot"))
            continue

        screenshot_urls = upload_screenshots(bucket, pr_number, after) if bucket else []
        return {
            "status": "success",
            "edits": edits,
            "edit_route": last_route,
            "screenshot_paths": [p for p, _ in after],
            "screenshot_urls": screenshot_urls,
        }

    logger.info("No visible change after %d attempt(s); giving up", EDIT_MAX_ATTEMPTS)
    return {"status": "no_visible_change", "edits": last_edits, "edit_route": last_route}
