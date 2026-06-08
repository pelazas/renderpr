import base64
import logging
import sys
import time
from pathlib import Path

import httpx

from src.agent.config import (
    LLM_MODEL,
    LLM_RETRY_BASE_DELAY,
    LLM_RETRY_MAX_DELAY,
    LLM_RETRY_JITTER,
    OPENROUTER_BASE_URL,
    RETRY_MAX_ATTEMPTS,
)

logger = logging.getLogger(__name__)

REVIEW_PROMPT = """You are a frontend review bot for a Pull Request.
Review the code diff and screenshots below.

Analyze:
1. **Layout & Responsiveness** — Are there visual regressions at any viewport width?
2. **Code Quality** — Any issues in the diff?
3. **Accessibility** — WCAG violations visible in screenshots?
4. **Usability** — UX concerns?

Format your response as structured markdown with clear sections.
Be concise but specific. Reference line numbers from the diff where relevant.
If everything looks good, say so — don't fabricate issues."""


def _build_content(diff: str, screenshot_paths: list[Path]) -> list[dict]:
    content: list[dict] = [
        {"type": "text", "text": f"## Code Diff\n\n```diff\n{diff}\n```"},
    ]

    if screenshot_paths:
        content.append({"type": "text", "text": "## Screenshots\n"})
        for path in screenshot_paths:
            label = _guess_viewport_label(path)
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")
            content.append({
                "type": "text",
                "text": f"### {label}\n",
            })
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            })

    return content


def _guess_viewport_label(path: Path) -> str:
    name = path.stem
    for label in ["Mobile XS", "Tablet", "Desktop", "Desktop XL"]:
        if label in name:
            return f"Viewport: {label}"
    return f"Screenshot: {name}"


def run_review(
    diff: str,
    screenshot_paths: list[Path],
    openrouter_api_key: str,
) -> str:
    url = f"{OPENROUTER_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {openrouter_api_key}",
        "Content-Type": "application/json",
    }

    body = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": REVIEW_PROMPT},
            {"role": "user", "content": _build_content(diff, screenshot_paths)},
        ],
    }

    for attempt in range(RETRY_MAX_ATTEMPTS):
        with httpx.Client(timeout=120) as client:
            resp = client.post(url, headers=headers, json=body)

        if resp.status_code == 200:
            data = resp.json()
            return data["choices"][0]["message"]["content"]

        logger.error(
            "OpenRouter API error (attempt %d/%d): %d %s",
            attempt + 1, RETRY_MAX_ATTEMPTS, resp.status_code, resp.text,
        )

        if 400 <= resp.status_code < 500 and resp.status_code != 429:
            logger.error("Non-retryable error from OpenRouter, exiting")
            sys.exit(1)

        if attempt < RETRY_MAX_ATTEMPTS - 1:
            delay = min(
                LLM_RETRY_BASE_DELAY * (2 ** attempt),
                LLM_RETRY_MAX_DELAY,
            )
            jitter = delay * LLM_RETRY_JITTER
            time.sleep(delay + jitter)

    logger.error("OpenRouter API failed after %d attempts", RETRY_MAX_ATTEMPTS)
    sys.exit(1)
