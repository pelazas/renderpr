import base64
import logging
import time
from pathlib import Path

import httpx

from src.agent.config import (
    LLM_CLIENT_TIMEOUT,
    LLM_MODEL,
    LLM_RETRY_BASE_DELAY,
    LLM_RETRY_MAX_DELAY,
    LLM_RETRY_JITTER,
    OPENROUTER_BASE_URL,
    RETRY_MAX_ATTEMPTS,
)

logger = logging.getLogger(__name__)


class ReviewError(Exception):
    pass

REVIEW_PROMPT = """You are a frontend review bot for a Pull Request.
Review the code diff and screenshots below.

Analyze:
1. **Layout & Responsiveness** — Are there visual regressions at any viewport width?
2. **Code Quality** — Any issues in the diff?
3. **Accessibility** — WCAG violations visible in screenshots?
4. **Usability** — UX concerns?

Available screenshots are listed with identifiers like `[viewport - /route]`.
When discussing a visual issue, reference the relevant screenshot by placing its identifier inline, e.g.:
"I noticed the button is misaligned on mobile [Mobile XS - /dashboard]"

Only reference screenshots that support your analysis. Don't list every screenshot.
If everything looks good, say so — don't fabricate issues.

Format your response as structured markdown with clear sections.
Be concise but specific. Reference line numbers from the diff where relevant."""


def _build_content(
    diff: str,
    screenshot_paths: list[Path],
) -> list[dict]:
    content: list[dict] = [
        {"type": "text", "text": f"## Code Diff\n\n```diff\n{diff}\n```"},
    ]

    if screenshot_paths:
        label_list = ", ".join(_guess_viewport_label(p) for p in screenshot_paths)
        content.append({"type": "text", "text": f"## Screenshots\n\nAvailable: {label_list}\n"})
        for path in screenshot_paths:
            label = _guess_viewport_label(path)
            try:
                with open(path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("utf-8")
            except OSError:
                logger.warning("Cannot read screenshot %s, skipping", path)
                continue
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
    for label in ["Desktop XL", "Desktop", "Tablet", "Mobile XS"]:
        if label in name:
            return f"Viewport: {label}"
    return f"Screenshot: {name}"


def _inline_references(text: str, url_pairs: list[tuple[str, str]]) -> str:
    import re

    ref_to_url: dict[str, str] = {}
    for url, label in url_pairs:
        ref_to_url[f"[{label}]"] = url

    pattern = r"\[[^\]]+\]"

    def replace_ref(match: re.Match) -> str:
        ref = match.group(0)
        url = ref_to_url.get(ref)
        if url:
            return f'<img width="400" src="{url}" alt="{ref.strip("[]")}">'
        return ref

    return re.sub(pattern, replace_ref, text)


def run_review(
    diff: str,
    screenshot_paths: list[Path],
    openrouter_api_key: str,
    screenshot_urls: list[tuple[str, str]] | None = None,
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
        with httpx.Client(timeout=LLM_CLIENT_TIMEOUT) as client:
            resp = client.post(url, headers=headers, json=body)

        if resp.status_code == 200:
            data = resp.json()
            try:
                text = data["choices"][0]["message"]["content"]
                if screenshot_urls:
                    text = _inline_references(text, screenshot_urls)
                return text
            except (KeyError, IndexError, TypeError):
                raise ReviewError(f"Unexpected OpenRouter response shape: {str(data)[:200]}")

        logger.error(
            "OpenRouter API error (attempt %d/%d): %d %s",
            attempt + 1, RETRY_MAX_ATTEMPTS, resp.status_code, resp.text,
        )

        if 400 <= resp.status_code < 500 and resp.status_code != 429:
            raise ReviewError(f"Non-retryable OpenRouter error: {resp.status_code} {resp.text[:200]}")

        if attempt < RETRY_MAX_ATTEMPTS - 1:
            delay = min(
                LLM_RETRY_BASE_DELAY * (2 ** attempt),
                LLM_RETRY_MAX_DELAY,
            )
            jitter = delay * LLM_RETRY_JITTER
            time.sleep(delay + jitter)

    raise ReviewError(f"OpenRouter API failed after {RETRY_MAX_ATTEMPTS} attempts")
