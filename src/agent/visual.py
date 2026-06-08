import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

from src.agent.config import PLAYWRIGHT_NAVIGATION_TIMEOUT, REPO_DIR, VIEWPORTS, VIEWPORT_LABELS

logger = logging.getLogger(__name__)


def capture_screenshots(
    dev_server_url: str,
    screenshot_dir: Path | None = None,
) -> list[Path]:
    if screenshot_dir is None:
        screenshot_dir = Path(REPO_DIR) / ".renderpr" / "screenshots"

    screenshot_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []

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
                    paths.append(filename)
                except Exception:
                    logger.warning("Screenshot failed for viewport %d", width, exc_info=True)

            browser.close()
    except Exception:
        logger.exception("Failed to initialize Playwright")
        sys.exit(1)

    logger.info("Captured %d screenshots", len(paths))
    return paths
