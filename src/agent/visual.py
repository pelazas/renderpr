import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Final
from urllib.parse import urlparse

import boto3
from playwright.sync_api import Page, TimeoutError, sync_playwright

from src.agent.config import LOGIN_WALL_URL_MARKERS, PLAYWRIGHT_CLICK_TIMEOUT, PLAYWRIGHT_NAVIGATION_TIMEOUT, REPO_DIR, RETRY_MAX_ATTEMPTS, SETTLE_AFTER_NAVIGATION_MS, VIEWPORTS, VIEWPORT_LABELS
from src.agent.mock_server import API_FALLBACK_HEADER

VIEWPORT_ORDER: Final[list[int]] = [vp["width"] for vp in VIEWPORTS]

logger = logging.getLogger(__name__)


def _flatten_mocks(mocks: dict | None) -> dict[str, dict]:
    if not mocks:
        return {}

    flattened: dict[str, dict] = {}
    for endpoints in mocks.values():
        if not isinstance(endpoints, dict):
            continue
        for path, mock_data in endpoints.items():
            if not isinstance(path, str) or not path.startswith("/"):
                continue
            if not isinstance(mock_data, dict) or "body" not in mock_data:
                continue
            flattened[path] = {
                "body": mock_data["body"],
                "status": mock_data.get("status", 200),
            }
    return flattened


def _url_is_login_wall(url: str) -> bool:
    parsed_path = urlparse(url).path or ""
    return any(parsed_path.startswith(marker) for marker in LOGIN_WALL_URL_MARKERS)


def _screenshot_route(
    page: "Page",
    base_url: str,
    route: dict,
    screenshot_dir: Path,
    login_signals: list | None = None,
) -> list[tuple[Path, str]]:
    results: list[tuple[Path, str]] = []
    path = route["path"]
    actions = route.get("actions", [])
    url = f"{base_url.rstrip('/')}{path}"
    route_slug = path.strip("/").replace("/", "-") or "home"

    for vp in VIEWPORTS:
        width = vp["width"]
        page.set_viewport_size({"width": width, "height": vp["height"]})

        try:
            page.goto(url, wait_until="networkidle", timeout=PLAYWRIGHT_NAVIGATION_TIMEOUT)
            page.wait_for_timeout(SETTLE_AFTER_NAVIGATION_MS)
        except TimeoutError:
            logger.warning("Navigation timeout for %s at viewport %d, skipping", path, width)
            continue

        if login_signals is not None and width == VIEWPORT_ORDER[0] and _url_is_login_wall(page.url):
            login_signals.append({"path": path, "url": page.url})
            logger.info("Login wall detected at %s -> %s", path, page.url)

        try:
            ready_state = page.evaluate("document.readyState")
            logger.info("PAGE STATE [%s] readyState=%s", path, ready_state)
        except Exception:
            logger.warning("Failed to read document.readyState for %s", path, exc_info=True)

        page.wait_for_timeout(SETTLE_AFTER_NAVIGATION_MS)
        has_click = _has_click_action(actions)
        if actions and not has_click:
            _perform_actions(page, path, actions)

        baseline = _save_screenshot(page, screenshot_dir, path, route_slug, width, "")
        if baseline:
            results.append(baseline)

        if has_click and _perform_actions(page, path, actions):
            page.wait_for_timeout(SETTLE_AFTER_NAVIGATION_MS)
            interacted = _save_screenshot(page, screenshot_dir, path, route_slug, width, " after interaction")
            if interacted:
                results.append(interacted)


    return results


def _has_click_action(actions: list[dict]) -> bool:
    return any(action.get("type") == "click" for action in actions)


def _perform_actions(page: "Page", path: str, actions: list[dict]) -> bool:
    try:
        _wait_for_document_ready(page)
        for action in actions:
            if action["type"] == "click":
                selector = action["selector"]
                page.wait_for_selector(selector, state="visible", timeout=PLAYWRIGHT_CLICK_TIMEOUT)
                page.click(selector, timeout=PLAYWRIGHT_CLICK_TIMEOUT)
            elif action["type"] == "wait":
                page.wait_for_timeout(action.get("ms", 1000))
        return True
    except Exception as exc:
        logger.warning("Skipping interacted screenshot for %s after action failure: %s", path, exc)
        return False


def _wait_for_document_ready(page: "Page") -> None:
    if hasattr(page, "wait_for_function"):
        page.wait_for_function("document.readyState === 'complete'", timeout=PLAYWRIGHT_NAVIGATION_TIMEOUT)


def _save_screenshot(
    page: "Page",
    screenshot_dir: Path,
    path: str,
    route_slug: str,
    width: int,
    label_suffix: str,
) -> tuple[Path, str] | None:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    vp_label = VIEWPORT_LABELS.get(width, f"{width}w")
    label = f"{vp_label} - {path}{label_suffix}"
    suffix = "-interacted" if label_suffix else ""
    filename = screenshot_dir / f"{vp_label}-{route_slug}{suffix}-{timestamp}.png"

    try:
        page.screenshot(path=str(filename), full_page=True)
        logger.info("Screenshot saved: %s", filename)
        return filename, label
    except Exception:
        logger.warning("Screenshot failed for %s at viewport %d", path, width, exc_info=True)
        return None


def _launch_session(p, storage_state: dict | None, mocks: dict | None, entry_url: str | None):
    """Launch a fresh browser + page wired with mocks, diagnostics, and auth entry.

    Returns (browser, page). Called on first launch and on every crash-relaunch so
    a recovered session is configured identically to the original.
    """
    browser = p.chromium.launch()
    # A synthetic auth session (forged cookies / localStorage) is loaded into
    # the context so the app treats the preview user as logged in.
    context = browser.new_context(storage_state=storage_state) if storage_state else browser.new_context()  # type: ignore[arg-type]

    mock_entries = _flatten_mocks(mocks)

    def handle_mock_route(route):
        request = route.request
        parsed = urlparse(request.url)
        mock = mock_entries.get(parsed.path)

        if mock is not None:
            logger.info("Mock matched %s %s -> %d", request.method, parsed.path, mock["status"])
            return route.fulfill(
                status=mock["status"],
                content_type="application/json",
                body=json.dumps(mock["body"]),
            )

        # Catch-all: an unmocked data endpoint would otherwise hit the real
        # (often broken) handler and crash the page. Return empty data plus the
        # fallback header so the route renders and the banner can explain why.
        if parsed.path.startswith("/api/") and not parsed.path.startswith("/api/auth"):
            logger.info("Unmocked API fallback (screenshot) for %s", parsed.path)
            return route.fulfill(
                status=200,
                content_type="application/json",
                body="[]",
                headers={API_FALLBACK_HEADER: parsed.path},
            )

        return route.continue_()

    context.route("**/*", handle_mock_route)
    if mock_entries:
        logger.info("Mock routes registered for %d endpoint(s): %s", len(mock_entries), list(mock_entries.keys()))

    page = context.new_page()

    page.on("console", lambda msg: logger.info("PAGE CONSOLE [%s]: %s", msg.type, msg.text))
    page.on("pageerror", lambda exc: logger.warning("PAGE ERROR (hydration?): %s", exc))
    page.on("requestfailed", lambda req: logger.warning("PAGE REQUEST FAILED: %s (%s)", req.url, req.failure))

    # Some providers (e.g. Clerk) establish the session by consuming a ticket
    # at an entry URL before the app is browsed.
    if entry_url:
        try:
            page.goto(entry_url, wait_until="networkidle", timeout=PLAYWRIGHT_NAVIGATION_TIMEOUT)
            logger.info("Loaded auth entry URL")
        except Exception:
            logger.warning("Failed to load auth entry URL", exc_info=True)

    return browser, page


def capture_screenshots(
    dev_server_url: str,
    screenshot_dir: Path | None = None,
    routes: list[dict] | None = None,
    mocks: dict | None = None,
    storage_state: dict | None = None,
    entry_url: str | None = None,
    login_signals: list | None = None,
) -> list[tuple[Path, str]]:
    if screenshot_dir is None:
        screenshot_dir = Path(REPO_DIR) / ".renderpr" / "screenshots"

    screenshot_dir.mkdir(parents=True, exist_ok=True)
    results: list[tuple[Path, str]] = []

    if not routes:
        routes = [{"path": "/", "actions": [], "reason": "default"}]

    try:
        with sync_playwright() as p:
            browser, page = _launch_session(p, storage_state, mocks, entry_url)

            # A renderer can segfault mid-capture (signal 11 under SwiftShader on
            # Fargate, typically memory pressure during a full-page screenshot).
            # Treat that as recoverable: relaunch the browser and retry the route
            # rather than aborting the entire review.
            for route in routes:
                for attempt in range(RETRY_MAX_ATTEMPTS):
                    try:
                        results.extend(_screenshot_route(page, dev_server_url, route, screenshot_dir, login_signals))
                        break
                    except Exception:
                        logger.warning(
                            "Browser crashed capturing %s (attempt %d/%d); relaunching",
                            route["path"], attempt + 1, RETRY_MAX_ATTEMPTS, exc_info=True,
                        )
                        try:
                            browser.close()
                        except Exception:
                            pass
                        if attempt + 1 < RETRY_MAX_ATTEMPTS:
                            browser, page = _launch_session(p, storage_state, mocks, entry_url)
                        else:
                            logger.error("Giving up on route %s after %d attempts", route["path"], RETRY_MAX_ATTEMPTS)

            try:
                browser.close()
            except Exception:
                pass
    except Exception:
        logger.exception("Failed to initialize Playwright")
        sys.exit(1)

    if not results:
        logger.error("Captured 0 screenshots across %d route(s); failing", len(routes))
        sys.exit(1)

    logger.info("Captured %d screenshots across %d route(s)", len(results), len(routes))
    return results


def upload_screenshots(
    bucket: str,
    pr_number: str,
    pairs: list[tuple[Path, str]],
) -> list[tuple[str, str]]:
    client = boto3.client("s3")
    results: list[tuple[str, str]] = []

    for path, label in pairs:
        key = f"screenshots/{pr_number}/{uuid.uuid4().hex}.png"
        body = path.read_bytes()
        for attempt in range(RETRY_MAX_ATTEMPTS):
            try:
                client.put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=body,
                    ContentType="image/png",
                )
                url = f"https://{bucket}.s3.amazonaws.com/{key}"
                results.append((url, label))
                logger.info("Uploaded screenshot: %s (%s)", url, label)
                break
            except Exception:
                if attempt == RETRY_MAX_ATTEMPTS - 1:
                    logger.warning("Failed to upload screenshot %s after %d attempts", path, RETRY_MAX_ATTEMPTS, exc_info=True)
                else:
                    logger.warning("S3 upload attempt %d/%d failed for %s", attempt + 1, RETRY_MAX_ATTEMPTS, path)

    logger.info("Uploaded %d/%d screenshots", len(results), len(pairs))
    return results
