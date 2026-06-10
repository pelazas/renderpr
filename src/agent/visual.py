import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

import boto3
from playwright.sync_api import Page, TimeoutError, sync_playwright

from src.agent.config import PLAYWRIGHT_NAVIGATION_TIMEOUT, REPO_DIR, RETRY_MAX_ATTEMPTS, SETTLE_AFTER_NAVIGATION_MS, VIEWPORTS, VIEWPORT_LABELS

VIEWPORT_ORDER: Final[list[int]] = [vp["width"] for vp in VIEWPORTS]

logger = logging.getLogger(__name__)


def _screenshot_route(
    page: "Page",
    base_url: str,
    route: dict,
    screenshot_dir: Path,
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

        for action in actions:
            try:
                if action["type"] == "click":
                    page.click(action["selector"])
                elif action["type"] == "wait":
                    page.wait_for_timeout(action.get("ms", 1000))
            except Exception:
                logger.warning("Action %s failed for %s", action.get("type"), path, exc_info=True)

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        vp_label = VIEWPORT_LABELS.get(width, f"{width}w")
        label = f"{vp_label} - {path}"
        filename = screenshot_dir / f"{vp_label}-{route_slug}-{timestamp}.png"

        try:
            page.screenshot(path=str(filename), full_page=True)
            logger.info("Screenshot saved: %s", filename)
            results.append((filename, label))
        except Exception:
            logger.warning("Screenshot failed for %s at viewport %d", path, width, exc_info=True)

    return results


def capture_screenshots(
    dev_server_url: str,
    screenshot_dir: Path | None = None,
    routes: list[dict] | None = None,
    mocks: dict | None = None,
) -> list[tuple[Path, str]]:
    if screenshot_dir is None:
        screenshot_dir = Path(REPO_DIR) / ".renderpr" / "screenshots"

    screenshot_dir.mkdir(parents=True, exist_ok=True)
    results: list[tuple[Path, str]] = []

    if not routes:
        routes = [{"path": "/", "actions": [], "reason": "default"}]

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context()
            page = context.new_page()

            if mocks:
                mock_count = 0
                for domain, endpoints in mocks.items():
                    for path, mock_data in endpoints.items():
                        pattern = f"**{path}**"
                        body = json.dumps(mock_data["body"])
                        status = mock_data.get("status", 200)
                        def make_handler(p, bd, st):
                            def handler(route):
                                logger.info("Mock intercepted: %s -> %d", p, st)
                                route.fulfill(
                                    status=st,
                                    content_type="application/json",
                                    body=bd,
                                )
                            return handler
                        page.route(pattern, make_handler(path, body, status))
                        mock_count += 1
                logger.info("Registered %d mock endpoint(s)", mock_count)

            for route in routes:
                route_results = _screenshot_route(page, dev_server_url, route, screenshot_dir)
                results.extend(route_results)

            browser.close()
    except Exception:
        logger.exception("Failed to initialize Playwright")
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
