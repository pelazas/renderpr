import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

import boto3
from playwright.sync_api import sync_playwright

from src.agent.config import PLAYWRIGHT_NAVIGATION_TIMEOUT, REPO_DIR, RETRY_MAX_ATTEMPTS, VIEWPORTS, VIEWPORT_LABELS

VIEWPORT_ORDER: Final[list[int]] = [vp["width"] for vp in VIEWPORTS]

logger = logging.getLogger(__name__)


def capture_screenshots(
    dev_server_url: str,
    screenshot_dir: Path | None = None,
) -> list[tuple[Path, str]]:
    if screenshot_dir is None:
        screenshot_dir = Path(REPO_DIR) / ".renderpr" / "screenshots"

    screenshot_dir.mkdir(parents=True, exist_ok=True)
    results: list[tuple[Path, str]] = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context()
            page = context.new_page()

            for vp in VIEWPORTS:
                width = vp["width"]
                page.set_viewport_size({"width": width, "height": vp["height"]})

                try:
                    page.goto(dev_server_url, wait_until="networkidle", timeout=PLAYWRIGHT_NAVIGATION_TIMEOUT)
                except TimeoutError:
                    logger.warning("Navigation timeout for viewport %d, skipping", width)
                    continue

                timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
                label = VIEWPORT_LABELS.get(width, f"{width}w")
                filename = screenshot_dir / f"{label}-{timestamp}.png"

                try:
                    page.screenshot(path=str(filename), full_page=True)
                    logger.info("Screenshot saved: %s", filename)
                    results.append((filename, label))
                except Exception:
                    logger.warning("Screenshot failed for viewport %d", width, exc_info=True)

            browser.close()
    except Exception:
        logger.exception("Failed to initialize Playwright")
        sys.exit(1)

    logger.info("Captured %d screenshots", len(results))
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
