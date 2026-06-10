import json
import logging
import re
import time
from pathlib import Path
from typing import Final

import httpx

from src.agent.config import (
    FRONTEND_EXTENSIONS,
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

ROUTE_INFERENCE_PROMPT = """You are a frontend routing analyzer. Given a git diff, full file contents of changed files, their reverse dependencies (files that import the changed files), and the project file tree,
identify which routes are affected by this change and what interactions are needed to surface the changes visually.

You MUST also identify API calls in the changed files and generate mock JSON response data for them.

Rules:
- A route file change (e.g., app/dashboard/page.tsx) -> direct route (/dashboard)
- A shared component change (e.g., components/Button.tsx) -> routes that use it
- ALWAYS include ALL routes that have changed route files (page.tsx, layout.tsx, etc.)
- ONLY include actions if the change is inside a modal, dropdown, overlay, or toggle that is hidden by default and requires a click to reveal. Do NOT guess or assume — look at the source code for useState toggles or conditional rendering tied to a button.
- If you include an action, derive the selector from the exact button text in the source code. For a button with text "Open", use "text=Open".
- If uncertain about a route, include it anyway (false positive > false negative)
- The project uses file-system based routing (Next.js App Router style)
- Strip query parameters from routes — just return the path
- Do NOT include routes that are API routes (route.ts, api/)

For mock data:
- Scan the changed files for fetch(), axios, useQuery(), or other API call patterns
- For each unique API endpoint found, generate realistic mock JSON response data
- Use the full file contents to infer the shape of the data (field names, types, nesting)
- Include the mock under the "mocks" key, keyed by domain and path
- If no API calls are found, omit the "mocks" key entirely

Output ONLY valid JSON with this exact schema:
{"routes": [{"path": "/...", "reason": "...", "actions": []}], "mocks": {"api.example.com": {"/api/path": {"body": {...}, "status": 200}}}}

The "status" field in mocks is optional (defaults to 200). The "body" field is required.
Each action object: {"type": "click" | "wait", "selector"?: "css-selector", "ms"?: number}
If no interaction needed, actions should be an empty list."""


class RouteInferenceError(Exception):
    pass


EXCLUDED_DIRS: Final[set[str]] = {".git", "node_modules", ".next", "__pycache__", ".venv", "dist", "build", ".cache"}
SOURCE_EXTENSIONS: Final[tuple[str, ...]] = (".ts", ".tsx", ".js", ".jsx")

_MAX_REVERSE_DEPS: Final[int] = 5


def _find_importers(stems: list[str], exclude_paths: set[str]) -> list[str]:
    repo_path = Path(REPO_DIR)
    if not repo_path.exists():
        logger.warning("Repo directory %s does not exist for import scanning", REPO_DIR)
        return []

    patterns = [re.compile(rf"""["'][^"']*{re.escape(s)}["']""") for s in stems]

    importers: list[str] = []
    for f in repo_path.rglob("*"):
        if any(part in EXCLUDED_DIRS for part in f.relative_to(repo_path).parts):
            continue
        if f.suffix not in SOURCE_EXTENSIONS:
            continue
        rel = str(f.relative_to(repo_path))
        if rel in exclude_paths:
            continue
        try:
            content = f.read_text(errors="replace")
            for p in patterns:
                if p.search(content):
                    importers.append(rel)
                    break
        except OSError:
            continue

    return importers[:_MAX_REVERSE_DEPS]


def _get_changed_files(diff: str) -> list[str]:
    files: list[str] = []
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            files.append(line[6:])
    return files


def _read_full_files(file_paths: list[str]) -> dict[str, str]:
    repo_path = Path(REPO_DIR)
    contents: dict[str, str] = {}
    for fp in file_paths:
        full_path = repo_path / fp
        if full_path.exists() and full_path.is_file():
            try:
                contents[fp] = full_path.read_text()
            except OSError:
                logger.warning("Could not read %s", fp)
    return contents


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


def _validate_routes(routes: list[dict]) -> list[dict]:
    valid: list[dict] = []
    for r in routes:
        if not isinstance(r.get("path"), str) or not r["path"].startswith("/"):
            continue
        actions = r.get("actions", [])
        if not isinstance(actions, list):
            actions = []
        validated_actions: list[dict] = []
        for a in actions:
            if a.get("type") not in ("click", "wait"):
                continue
            if a["type"] == "click" and not isinstance(a.get("selector"), str):
                continue
            validated_actions.append(a)
        valid.append({"path": r["path"], "reason": r.get("reason", ""), "actions": validated_actions})
    return valid


def _validate_mocks(mocks: dict | None) -> dict:
    if not isinstance(mocks, dict):
        return {}

    validated: dict = {}
    for domain, endpoints in mocks.items():
        if not isinstance(domain, str) or not isinstance(endpoints, dict):
            continue
        validated_endpoints: dict = {}
        for path, mock_data in endpoints.items():
            if not isinstance(path, str) or not path.startswith("/"):
                continue
            if not isinstance(mock_data, dict):
                continue
            body = mock_data.get("body")
            if not isinstance(body, (dict, list)):
                continue
            validated_endpoints[path] = {
                "body": body,
                "status": mock_data.get("status", 200),
            }
        if validated_endpoints:
            validated[domain] = validated_endpoints
    return validated


def _extract_json(text: str) -> dict | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
        return None


def infer_routes(
    diff: str,
    repo_tree: str,
    openrouter_api_key: str,
) -> tuple[list[dict], dict]:
    url = f"{OPENROUTER_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {openrouter_api_key}",
        "Content-Type": "application/json",
    }

    changed_files = _get_changed_files(diff)
    file_contents = _read_full_files(changed_files)
    file_contents = {fp: c for fp, c in file_contents.items() if Path(fp).suffix in FRONTEND_EXTENSIONS}
    if file_contents:
        logger.info("Sending full contents for %d frontend file(s): %s", len(file_contents), list(file_contents.keys()))

    stems = [Path(fp).stem for fp in changed_files]
    exclude_paths = set(changed_files)
    reverse_dep_paths = _find_importers(stems, exclude_paths)
    reverse_contents = _read_full_files(reverse_dep_paths)
    reverse_contents = {fp: c for fp, c in reverse_contents.items() if Path(fp).suffix in FRONTEND_EXTENSIONS}

    changed_section = "\n".join(
        f"### {fp}\n\n```tsx\n{content}\n```" for fp, content in file_contents.items()
    ) if file_contents else "(none)"

    reverse_section = "\n".join(
        f"### {fp}\n\n```tsx\n{content}\n```" for fp, content in reverse_contents.items()
    ) if reverse_contents else "(none detected)"

    frontend_diff_lines: list[str] = []
    in_frontend_block = False
    for line in diff.splitlines():
        if line.startswith("diff --git a/") and any(line[13:].endswith(ext) for ext in FRONTEND_EXTENSIONS):
            in_frontend_block = True
        elif line.startswith("diff --git "):
            in_frontend_block = False
        if in_frontend_block:
            frontend_diff_lines.append(line)
    filtered_diff = "\n".join(frontend_diff_lines)

    user_content = f"## Git Diff\n\n```diff\n{filtered_diff or '(no frontend file changes in diff)'}\n```\n\n## Project File Tree\n\n```\n{repo_tree}\n```\n\n## Full File Contents (changed files)\n\n{changed_section}\n\n## Reverse Dependencies (files that import changed files)\n\n{reverse_section}"

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
                logger.info("LLM response length: %d chars, has 'mocks': %s", len(raw), "'mocks' in raw")
                logger.info("LLM response tail: ...%s", raw[-800:] if len(raw) > 800 else raw)
                parsed = _extract_json(raw)
                if parsed is None:
                    logger.warning("Could not extract JSON from route inference response, falling back to homepage")
                    return _fallback_routes(), {}
                raw_routes = parsed.get("routes", [])
                raw_mocks = parsed.get("mocks")
                routes = _validate_routes(raw_routes)
                mocks = _validate_mocks(raw_mocks)
                if routes:
                    logger.info("Inferred %d route(s): %s", len(routes), [r["path"] for r in routes])
                    if mocks:
                        mock_paths = sum(len(eps) for eps in mocks.values())
                        logger.info("Generated mocks for %d domain(s), %d path(s): %s", len(mocks), mock_paths, {d: list(eps.keys()) for d, eps in mocks.items()})
                    return routes, mocks
                logger.warning("LLM returned empty or invalid routes, falling back to homepage")
                return _fallback_routes(), {}
            except (KeyError, IndexError, TypeError):
                logger.warning("Unexpected OpenRouter response shape, falling back to homepage")
                return _fallback_routes(), {}

        logger.error(
            "OpenRouter API error (attempt %d/%d): %d %s",
            attempt + 1, RETRY_MAX_ATTEMPTS, resp.status_code, resp.text,
        )

        if 400 <= resp.status_code < 500 and resp.status_code != 429:
            return _fallback_routes(), {}

        if attempt < RETRY_MAX_ATTEMPTS - 1:
            delay = min(LLM_RETRY_BASE_DELAY * (2 ** attempt), LLM_RETRY_MAX_DELAY)
            jitter = delay * LLM_RETRY_JITTER
            time.sleep(delay + jitter)

    logger.warning("Route inference failed after %d attempts, falling back to homepage", RETRY_MAX_ATTEMPTS)
    return _fallback_routes(), {}


def _fallback_routes() -> list[dict]:
    return [{"path": "/", "reason": "fallback", "actions": []}]
