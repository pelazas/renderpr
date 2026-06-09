import json
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
    REPO_DIR,
    RETRY_MAX_ATTEMPTS,
)

logger = logging.getLogger(__name__)

ROUTE_INFERENCE_PROMPT = """You are a frontend routing analyzer. Given a git diff and the project file tree,
identify which routes are affected by this change and what interactions are needed to surface the changes visually.

Rules:
- A route file change (e.g., app/dashboard/page.tsx) -> direct route (/dashboard)
- A shared component change (e.g., components/Button.tsx) -> routes that use it
- If interactions are needed (click to open modal/dropdown), specify the CSS selector
- If uncertain about a route, include it anyway (false positive > false negative)
- The project uses file-system based routing (Next.js App Router style)
- Strip query parameters from routes — just return the path
- Do NOT include routes that are API routes (route.ts, api/)

Output ONLY valid JSON with this exact schema:
{"routes": [{"path": "/...", "reason": "...", "actions": []}]}

Each action object: {"type": "click" | "wait", "selector"?: "css-selector", "ms"?: number}
If no interaction needed, actions should be an empty list."""


class RouteInferenceError(Exception):
    pass


def build_repo_tree() -> str:
    repo_path = Path(REPO_DIR)
    if not repo_path.exists():
        logger.warning("Repo directory %s does not exist", REPO_DIR)
        return ""

    excludes = {".git", "node_modules", ".next", "__pycache__", ".venv", "dist", "build", ".cache"}

    paths: list[str] = []
    for f in repo_path.rglob("*"):
        if any(part in excludes for part in f.relative_to(repo_path).parts):
            continue
        if f.is_file():
            rel = str(f.relative_to(repo_path))
            paths.append(rel)

    paths.sort()
    return "\n".join(paths)


def infer_routes(
    diff: str,
    repo_tree: str,
    openrouter_api_key: str,
) -> list[dict]:
    url = f"{OPENROUTER_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {openrouter_api_key}",
        "Content-Type": "application/json",
    }

    user_content = f"## Git Diff\n\n```diff\n{diff}\n```\n\n## Project File Tree\n\n```\n{repo_tree}\n```"

    body = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": ROUTE_INFERENCE_PROMPT},
            {"role": "user", "content": user_content},
        ],
    }

    for attempt in range(RETRY_MAX_ATTEMPTS):
        with httpx.Client(timeout=LLM_CLIENT_TIMEOUT) as client:
            resp = client.post(url, headers=headers, json=body)

        if resp.status_code == 200:
            data = resp.json()
            try:
                raw = data["choices"][0]["message"]["content"]
                parsed = json.loads(raw)
                routes = parsed.get("routes", [])
                if routes:
                    logger.info("Inferred %d route(s): %s", len(routes), [r["path"] for r in routes])
                    return routes
                logger.warning("LLM returned empty routes list, falling back to homepage")
                return _fallback_routes()
            except (KeyError, IndexError, json.JSONDecodeError, TypeError):
                logger.warning("Failed to parse route inference response, falling back to homepage")
                return _fallback_routes()

        logger.error(
            "OpenRouter API error (attempt %d/%d): %d %s",
            attempt + 1, RETRY_MAX_ATTEMPTS, resp.status_code, resp.text,
        )

        if 400 <= resp.status_code < 500 and resp.status_code != 429:
            return _fallback_routes()

        if attempt < RETRY_MAX_ATTEMPTS - 1:
            delay = min(LLM_RETRY_BASE_DELAY * (2 ** attempt), LLM_RETRY_MAX_DELAY)
            jitter = delay * LLM_RETRY_JITTER
            time.sleep(delay + jitter)

    logger.warning("Route inference failed after %d attempts, falling back to homepage", RETRY_MAX_ATTEMPTS)
    return _fallback_routes()


def _fallback_routes() -> list[dict]:
    return [{"path": "/", "reason": "fallback", "actions": []}]
