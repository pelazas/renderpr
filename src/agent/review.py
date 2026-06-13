import base64
import logging
import re
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

Write your review using this exact structure:

# RenderPR Overview

---

## RenderPR Summary

Write 3-4 bullet points summarizing the overall changes. Do NOT organize by route/file — this should be a high-level summary of what the PR does as a whole.

## UI Changes

For each distinct change in the PR, create a subsection with a `### <change name>` heading. For each change:

1. Write 1-2 sentences describing the change. If the change appears on a specific route, mention it.
2. Reference the Desktop screenshot inline: [Desktop - /route]
3. End the section with a new line containing: [All views: /route]

Always include [All views: /route] at the end of every change section so readers can access other viewports without extra inline images. If a change applies to multiple routes, list them separately.

Rules for screenshot references:
- Always show the Desktop viewport as the main image. Do NOT include multiple inline images per change.
- Only reference Mobile XS, Tablet, or Desktop XL inline if you have a specific, important reason (e.g., "the modal overflows the viewport on Mobile XS"). Always explain why you're showing a non-desktop viewport.
- Use [Desktop - /route] format for the main image.
- Some routes also have an "after interaction" screenshot (e.g. "Desktop - /route after interaction") captured after clicking a trigger to reveal hidden content such as an opened modal or dropdown. When a change is about content that is revealed by interaction, use the Desktop variant — [Desktop - /route after interaction] — as that section's main image, and [All views: /route after interaction] at the end. Always prefer the Desktop variant; only fall back to another viewport's "after interaction" image if no Desktop one is available.

Additional rules:
- If there are any major security vulnerabilities, point them out in their own subsection.
- If everything looks good, just say so — don't fabricate issues.
- Format as plain markdown. Do NOT wrap your response in a code block or fence.
- Be concise but specific. Reference line numbers from the diff where relevant."""


def _build_content(
    diff: str,
    screenshot_paths: list[Path],
    screenshot_labels: list[str] | None = None,
) -> list[dict]:
    content: list[dict] = [
        {"type": "text", "text": f"## Code Diff\n\n```diff\n{diff}\n```"},
    ]

    if screenshot_paths:
        if screenshot_labels:
            label_list = ", ".join(screenshot_labels)
        else:
            label_list = ", ".join(f"[{_guess_viewport_label(p)}]" for p in screenshot_paths)
        content.append({"type": "text", "text": f"## Screenshots\n\nAvailable identifiers: {label_list}\n"})
        for i, path in enumerate(screenshot_paths):
            identifier = screenshot_labels[i] if screenshot_labels else _guess_viewport_label(path)
            try:
                with open(path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("utf-8")
            except OSError:
                logger.warning("Cannot read screenshot %s, skipping", path)
                continue
            content.append({
                "type": "text",
                "text": f"### {identifier}\n",
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


def _strip_code_fence(text: str) -> str:
    text = re.sub(r"^```\w*", "", text)
    text = re.sub(r"```$", "", text)
    return text.strip()


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
            return f'\n\n<img width="400" src="{url}" alt="{ref.strip("[]")}">\n\n'
        return ref

    return re.sub(pattern, replace_ref, text)


def _inline_all_views(text: str, screenshot_urls: list[tuple[str, str]]) -> str:
    route_views: dict[str, list[tuple[str, str]]] = {}
    for url, label in screenshot_urls:
        parts = label.split(" - ", 1)
        if len(parts) == 2:
            viewport, route = parts
            route_views.setdefault(route, []).append((viewport, url))

    pattern = r"\[All views:\s*(/[^\]]*)\]"

    def replace_all_views(match: re.Match) -> str:
        route = match.group(1).strip()
        views = route_views.get(route, [])
        non_desktop = [(v, u) for v, u in views if v != "Desktop"]
        if not non_desktop:
            return ""
        return " · ".join(f"[View on {v}]({u})" for v, u in sorted(non_desktop))

    return re.sub(pattern, replace_all_views, text)


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

    screenshot_labels = [label for _, label in screenshot_urls] if screenshot_urls else None

    body = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": REVIEW_PROMPT},
            {"role": "user", "content": _build_content(diff, screenshot_paths, screenshot_labels)},
        ],
    }

    for attempt in range(RETRY_MAX_ATTEMPTS):
        with httpx.Client(timeout=LLM_CLIENT_TIMEOUT) as client:
            resp = client.post(url, headers=headers, json=body)

        if resp.status_code == 200:
            data = resp.json()
            try:
                text = data["choices"][0]["message"]["content"]
                text = _strip_code_fence(text)
                if screenshot_urls:
                    text = _inline_references(text, screenshot_urls)
                    text = _inline_all_views(text, screenshot_urls)
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
