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
from src.agent.routing import (
    EXCLUDED_DIRS,
    SOURCE_EXTENSIONS,
    NextAppRouterStrategy,
    RoutingStrategy,
    get_strategy,
)

logger = logging.getLogger(__name__)

# Default strategy backing the module-level helpers and the zero-arg call sites
# that historically assumed Next.js App Router.
_NEXT_APP = NextAppRouterStrategy()


ROUTE_AUGMENT_PROMPT = """You are a frontend routing analyzer. A PR changed some files and a deterministic analysis already identified these routes as affected:

{deterministic_routes}

Given the git diff and full file contents below, identify any ADDITIONAL routes that are affected but missing from the list above, AND any internal API data-fetches that must be mocked so the affected pages render with data instead of erroring.

Route rules:
- Only return routes that are genuinely affected. False positives are worse than false negatives here — deterministic already covers the obvious cases.
- A route file change (e.g., {route_example_file}) -> its direct route — but these are already in the list.
- A shared component change -> routes that use it — check if the list above already captures them. Only add if missing.
- A global stylesheet or config change -> all routes — already in the list if the deterministic analyzer handled it.
- ALWAYS include ALL routes that have changed route files ({route_file_examples})
- If uncertain about a route, include it anyway (false positive > false negative)
- {routing_description}
- Strip query parameters from routes — just return the path
- Do NOT include API/endpoint routes in the "routes" list — those belong in "mocks".

Interaction (action) rules:
- A route's "actions" drive Playwright to reveal content that is HIDDEN by default, BEFORE screenshotting. The capture takes a baseline screenshot, then runs the actions, then captures a second "after interaction" screenshot — so a modal/dropdown introduced by the PR is actually visible in the review.
- ONLY add a click action when the PR adds or changes content inside a modal, dialog, dropdown, popover, drawer, sheet, accordion, overlay, or toggle that is hidden by default and requires a click to reveal. Look at the source for a useState toggle (e.g. `setIsModalOpen(true)`), a `<dialog>` opened via `showModal()`, or conditional rendering tied to a trigger button. Do NOT guess — if nothing is hidden behind a click, use an empty actions list.
- A click action MUST include ALL of these fields, or it will be discarded:
  - "type": "click"
  - "sourceText": the EXACT, COMPLETE visible text of the trigger element, copied verbatim from the source (e.g. "+ Add user"). Make it specific enough to match ONLY the trigger — do not shorten it, since a substring like "Add user" can also match a heading inside the revealed modal and break the click.
  - "selector": MUST be exactly "text=<sourceText>" using that same text (e.g. "text=+ Add user"). If the trigger has no visible text, use "[aria-label='<sourceText>']" with its aria-label as the sourceText instead.
  - "reason": a short explanation that NAMES what gets revealed using one of these words: modal, dialog, dropdown, popover, drawer, sheet, accordion, overlay, toggle, menu (e.g. "Clicking opens the add-user modal").
- A wait action is {{"type": "wait", "ms": <integer>}}. If no interaction is needed for a route, its "actions" must be an empty list.

Mock rules:
- The pages run in an ephemeral environment with NO database or backend. Any page that fetches data from an internal API route at runtime will error (e.g. "x.map is not a function") unless that endpoint is mocked.
- Scan the file contents for client-side or server-side data fetches to internal API paths — calls like `fetch('/api/users')` or `axios.get('/api/items')`. Only consider paths beginning with `/api/`.
- For every such internal API path used by an affected route, provide a mock with a realistic response `body`. INFER THE SHAPE from how the data is consumed in the component: if the code does `users.map(u => ... u.id ... u.name ... u.role ...)`, the body must be an array of objects containing those exact fields. Provide 2-4 plausible sample items for list endpoints, or a single object for detail endpoints.
- The "body" field is REQUIRED and must be a JSON array or object (the literal data the endpoint returns). The "status" field is optional (defaults to 200).
- Only mock endpoints you can see an actual fetch/axios call for in the provided file contents. Do not invent endpoints.
- If there are no internal API data-fetches, use an empty object: "mocks": {{}}.

Output ONLY valid JSON with this exact schema (actions is usually [] — only fill it for click-to-reveal cases per the Interaction rules):
{{"routes": [{{"path": "/...", "reason": "...", "actions": [{{"type": "click", "sourceText": "+ Add user", "selector": "text=+ Add user", "reason": "Clicking opens the add-user modal"}}]}}], "mocks": {{"local": {{"/api/...": {{"body": [], "status": 200}}}}}}}}

If no additional routes are affected and nothing needs mocking, output: {{"routes": [], "mocks": {{}}}}"""


class RouteInferenceError(Exception):
    pass


_MAX_REVERSE_DEPS: Final[int] = 5
_MAX_BFS_DEPTH: Final[int] = 5


def file_to_route(file_path: str) -> str | None:
    """Map a source file to its Next.js App Router route (the historical default).

    Kept as a module-level helper for callers that predate per-framework
    strategies; other frameworks go through get_strategy(...).file_to_route.
    """
    return _NEXT_APP.file_to_route(file_path)


def _is_layout_file(file_path: str) -> tuple[bool, str]:
    return _NEXT_APP.is_layout_file(file_path)


def _is_global_file(file_path: str) -> bool:
    return _NEXT_APP.is_global_file(file_path)


def _discover_all_routes(repo_path: Path) -> list[str]:
    return _NEXT_APP.discover_all_routes(repo_path)


def _routes_under_prefix(prefix: str, all_routes: list[str]) -> list[str]:
    """Return all routes that live under the given prefix (or are exactly the prefix)."""
    sep = "" if prefix == "/" else "/"
    target = prefix + sep
    return [r for r in all_routes if r == prefix or r.startswith(target)]


def _find_importers(
    stems: list[str],
    exclude_paths: set[str],
    source_extensions: tuple[str, ...] = SOURCE_EXTENSIONS,
) -> list[str]:
    repo_path = Path(REPO_DIR)
    if not repo_path.exists():
        logger.warning("Repo directory %s does not exist for import scanning", REPO_DIR)
        return []

    # Anchor the stem as a path-final component or directory import: it must be
    # preceded by the opening quote or a `/`/`.` and followed by a quote, `/`,
    # or `.`. This keeps `index` from matching `reindex` and lets directory
    # imports like `../components/Modal` resolve.
    patterns = [
        re.compile(rf"""["'](?:[^"']*[./])?{re.escape(s)}["'/.]""")
        for s in stems
    ]

    importers: list[str] = []
    for f in repo_path.rglob("*"):
        if any(part in EXCLUDED_DIRS for part in f.relative_to(repo_path).parts):
            continue
        if f.suffix not in source_extensions:
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


def _bfs_to_pages(
    start_file: str,
    repo_path: Path,
    strategy: RoutingStrategy | None = None,
    max_depth: int = _MAX_BFS_DEPTH,
) -> set[str]:
    """Walk the import graph from start_file until we hit the framework's page files.

    Returns the set of page file paths (relative) that import the changed file
    (transitively, up to max_depth hops).
    """
    strategy = strategy or _NEXT_APP
    found_pages: set[str] = set()
    visited: set[str] = set()
    current_layer: list[str] = [start_file]

    for _ in range(max_depth + 1):
        next_layer: list[str] = []
        for src in current_layer:
            if src in visited:
                continue
            visited.add(src)

            if strategy.is_page_file(src):
                found_pages.add(src)
                continue

            stem = Path(src).stem
            importers = _find_importers([stem], visited, strategy.source_extensions)
            for imp in importers:
                if imp not in visited:
                    next_layer.append(imp)
        if not next_layer:
            break
        current_layer = next_layer

    return found_pages


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


def _classify_file(
    file_path: str,
    repo_path: Path,
    all_routes: list[str],
    strategy: RoutingStrategy | None = None,
) -> list[str]:
    """Deterministically compute which routes are affected by a change to this file.

    Order of classification:
    1. a route/page file -> its own route
    2. a global stylesheet/config -> all routes
    3. a layout -> all routes under its directory
    4. anything else (shared component, util, etc.) -> BFS import graph to find pages
    """
    strategy = strategy or _NEXT_APP
    route = strategy.file_to_route(file_path)
    if route is not None:
        return [route]

    if strategy.is_global_file(file_path):
        return list(all_routes) if all_routes else ["/"]

    is_layout, prefix = strategy.is_layout_file(file_path)
    if is_layout:
        scoped = _routes_under_prefix(prefix, all_routes)
        return scoped if scoped else [prefix]

    page_files = _bfs_to_pages(file_path, repo_path, strategy)
    routes: list[str] = []
    for page in page_files:
        r = strategy.file_to_route(page)
        if r and r not in routes:
            routes.append(r)
    return routes


def _deterministic_routes(changed_files: list[str], strategy: RoutingStrategy | None = None) -> list[str]:
    """Compute the deterministic set of routes affected by the given changed files."""
    strategy = strategy or _NEXT_APP
    repo_path = Path(REPO_DIR)
    all_routes = strategy.discover_all_routes(repo_path)
    routes: list[str] = []
    for f in changed_files:
        for r in _classify_file(f, repo_path, all_routes, strategy):
            if r not in routes:
                routes.append(r)
    return routes


def _is_valid_click_action(action: dict, file_contents: dict[str, str] | None) -> bool:
    selector = action.get("selector")
    source_text = action.get("sourceText")
    reason = action.get("reason")
    if not all(isinstance(value, str) and value.strip() for value in (selector, source_text, reason)):
        return False
    if selector not in {f"text={source_text}", f"[aria-label='{source_text}']", f'[aria-label="{source_text}"]'}:
        return False
    if not _has_reveal_reason(reason):
        return False
    if not file_contents:
        return False
    return any(_has_interactive_source_text(content, source_text) for content in file_contents.values())


def _has_reveal_reason(reason: str) -> bool:
    return bool(re.search(
        r"\b(modal|dialog|dropdown|popover|drawer|sheet|accordion|overlay|toggle|hidden|reveal|open|menu)\b",
        reason,
        re.IGNORECASE,
    ))


_INTERACTIVE_PATTERNS = [
    re.compile(r"<button[^>]*>[^<]*"),
    re.compile(r"<a[^>]*href=[^>]*>"),
    re.compile(r"onClick="),
    re.compile(r"onPress="),
    re.compile(r"DropdownMenuTrigger"),
    re.compile(r"DialogTrigger"),
    re.compile(r"PopoverTrigger"),
    re.compile(r"SheetTrigger"),
    re.compile(r"TooltipTrigger"),
    re.compile(r"role=[\"']?button[\"']?"),
    re.compile(r"aria-haspopup=[\"']?true"),
    re.compile(r"<summary[^>]*>"),
    re.compile(r"<details[^>]*>"),
    re.compile(r"<label[^>]*>"),
    re.compile(r"type=[\"']?(submit|button|reset)[\"']?"),
    re.compile(r"tabIndex=[\"']?0"),
    re.compile(r"aria-label=[\"'][^\"']*"),
]


def _has_interactive_source_text(content: str, source_text: str) -> bool:
    text = re.escape(source_text)
    for pattern in _INTERACTIVE_PATTERNS:
        if re.search(rf"{pattern.pattern}\s*[^<]*{text}", content, re.IGNORECASE):
            return True
    return False


def _validate_routes(routes: list[dict], file_contents: dict[str, str] | None = None) -> list[dict]:
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
            if a["type"] == "click":
                if not _is_valid_click_action(a, file_contents):
                    continue
            if a["type"] == "wait" and not isinstance(a.get("ms"), int):
                continue
            validated_actions.append(a)
        valid.append({"path": r["path"], "reason": r.get("reason", ""), "actions": validated_actions})
    return valid


def _validate_mocks(mocks: dict | None, file_contents: dict[str, str] | None = None) -> dict:
    if not isinstance(mocks, dict):
        return {}

    combined_code = " ".join(file_contents.values()) if file_contents else ""

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
            # Skip mocks unless there's an actual fetch/axios call to this path
            if file_contents and not _has_fetch_call(combined_code, path):
                logger.info("Discarding mock for %s: no fetch call found in source code", path)
                continue
            validated_endpoints[path] = {
                "body": body,
                "status": mock_data.get("status", 200),
            }
        if validated_endpoints:
            validated[domain] = validated_endpoints
    return validated


def _has_fetch_call(code: str, api_path: str) -> bool:
    """Check if code contains a fetch/axios call to the given API path."""
    patterns = [
        rf"""fetch\s*\(\s*['\"]{re.escape(api_path)}['\"]""",
        rf"""axios\.\w+\s*\(\s*['\"]{re.escape(api_path)}['\"]""",
        rf"""axios\s*\(\s*['\"]{re.escape(api_path)}['\"]""",
    ]
    return any(re.search(p, code) for p in patterns)


def _strip_code_fence(text: str) -> str:
    text = re.sub(r"^```\w*\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _find_balanced_json_objects(text: str) -> list[str]:
    """Return all balanced top-level JSON objects in text, longest first."""
    candidates: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == "{":
            depth = 0
            start = i
            in_string = False
            escape = False
            for j in range(i, len(text)):
                c = text[j]
                if escape:
                    escape = False
                    continue
                if c == "\\" and in_string:
                    escape = True
                    continue
                if c == '"':
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        candidates.append(text[start : j + 1])
                        i = j
                        break
        i += 1
    candidates.sort(key=len, reverse=True)
    return candidates


def _extract_balanced_json(text: str) -> dict | None:
    """Try to extract a JSON object from text, handling fences, preamble, and nested objects."""
    cleaned = _strip_code_fence(text)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    for candidate in _find_balanced_json_objects(cleaned):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    return None


def _validate_and_repair_json(text: str) -> str:
    """Best-effort fix for common JSON issues from LLMs (trailing commas, single quotes)."""
    text = re.sub(r",\s*([}\]])", r"\1", text)
    return text


def _llm_augment_routes(
    deterministic_routes: list[str],
    diff: str,
    file_contents: dict[str, str],
    api_key: str,
    strategy: RoutingStrategy | None = None,
) -> tuple[list[dict], dict]:
    """Ask the LLM for additional routes the deterministic pass may have missed.

    Returns (validated_routes, validated_mocks). Returns ([], {}) on any failure
    — the deterministic set is still correct on its own.
    """
    strategy = strategy or _NEXT_APP
    url = f"{OPENROUTER_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    det_str = ", ".join(deterministic_routes) if deterministic_routes else "(none)"
    user_content = (
        f"## Already-Detected Routes\n\n{det_str}\n\n"
        f"## Git Diff\n\n```diff\n{diff[:8000]}\n```\n\n"
        f"## Project File Tree\n\n```\n{build_repo_tree()[:4000]}\n```\n\n"
    )
    if file_contents:
        snippets = []
        for fp, c in list(file_contents.items())[:5]:
            snippets.append(f"### {fp}\n\n```tsx\n{c[:2000]}\n```")
        user_content += "\n\n## Changed File Contents\n\n" + "\n\n".join(snippets)

    body = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": ROUTE_AUGMENT_PROMPT.format(deterministic_routes=det_str, **strategy.prompt_context())},
            {"role": "user", "content": user_content},
        ],
    }

    for attempt in range(RETRY_MAX_ATTEMPTS):
        try:
            with httpx.Client(timeout=LLM_CLIENT_TIMEOUT) as client:
                resp = client.post(url, headers=headers, json=body)
        except Exception:
            logger.warning("LLM augment request failed (attempt %d)", attempt + 1, exc_info=True)
            if attempt < RETRY_MAX_ATTEMPTS - 1:
                _sleep_backoff(attempt)
            continue

        if resp.status_code == 200:
            data = resp.json()
            try:
                raw = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                logger.warning("LLM augment: unexpected response shape")
                return [], {}

            parsed = _extract_balanced_json(raw) or _extract_balanced_json(_validate_and_repair_json(raw))
            if parsed is None:
                logger.warning("LLM augment: could not extract JSON from response")
                if attempt < RETRY_MAX_ATTEMPTS - 1:
                    _sleep_backoff(attempt)
                    body["messages"].append({"role": "assistant", "content": raw})
                    body["messages"].append({"role": "user", "content": "Return ONLY valid JSON. No prose, no markdown fences."})
                continue

            raw_routes = parsed.get("routes", [])
            raw_mocks = parsed.get("mocks")
            routes = _validate_routes(raw_routes, file_contents)
            mocks = _validate_mocks(raw_mocks, file_contents)
            return routes, mocks

        if 400 <= resp.status_code < 500 and resp.status_code != 429:
            logger.warning("LLM augment: non-retryable %d %s", resp.status_code, resp.text[:200])
            return [], {}

        logger.warning("LLM augment: HTTP %d (attempt %d)", resp.status_code, attempt + 1)
        if attempt < RETRY_MAX_ATTEMPTS - 1:
            _sleep_backoff(attempt)

    logger.warning("LLM augment: exhausted retries, returning empty")
    return [], {}


def _sleep_backoff(attempt: int) -> None:
    delay = min(LLM_RETRY_BASE_DELAY * (2 ** attempt), LLM_RETRY_MAX_DELAY)
    jitter = delay * LLM_RETRY_JITTER
    time.sleep(delay + jitter)


def _merge_routes(deterministic: list[str], llm_routes: list[dict]) -> list[dict]:
    """Merge deterministic route list with LLM-augmented routes.

    Deterministic routes come first, in order. For routes that appear in both
    lists, the deterministic entry is kept but its actions are replaced with
    the LLM's (since the LLM can add click/wait actions the deterministic
    analysis can't infer). LLM-only routes are appended in the order the LLM
    returned them.
    """
    by_path: dict[str, dict] = {}
    llm_by_path: dict[str, dict] = {r["path"]: r for r in llm_routes}

    for r in deterministic:
        llm_match = llm_by_path.get(r)
        if llm_match and llm_match.get("actions"):
            by_path[r] = {
                "path": r,
                "reason": llm_match.get("reason", "deterministic"),
                "actions": llm_match["actions"],
            }
        else:
            by_path[r] = {"path": r, "reason": "deterministic", "actions": []}

    for r in llm_routes:
        if r["path"] not in by_path:
            by_path[r["path"]] = r

    return [by_path[r] for r in deterministic] + [v for k, v in by_path.items() if k not in set(deterministic)]


def infer_routes(
    diff: str,
    repo_tree: str,
    openrouter_api_key: str,
    framework: str = "next",
) -> tuple[list[dict], dict]:
    strategy = get_strategy(framework, Path(REPO_DIR))
    logger.info("Route inference using %s strategy (framework=%s)", strategy.name, framework)

    changed_files = _get_changed_files(diff)
    if not changed_files:
        logger.warning("No changed files in diff, falling back to homepage")
        return _fallback_routes(), {}

    logger.info("Deterministic route inference for %d file(s): %s", len(changed_files), changed_files)
    deterministic = _deterministic_routes(changed_files, strategy)
    logger.info("Deterministic routes: %s", deterministic)

    file_contents = _read_full_files(changed_files)
    file_contents = {fp: c for fp, c in file_contents.items() if Path(fp).suffix in FRONTEND_EXTENSIONS}
    if file_contents:
        logger.info("Sending full contents for %d frontend file(s): %s", len(file_contents), list(file_contents.keys()))

    llm_routes, llm_mocks = _llm_augment_routes(deterministic, diff, file_contents, openrouter_api_key, strategy)
    if llm_routes:
        logger.info("LLM augment added %d additional route(s): %s", len(llm_routes), [r["path"] for r in llm_routes])
    if llm_mocks:
        logger.info("LLM augment provided %d mock domain(s)", len(llm_mocks))

    merged = _merge_routes(deterministic, llm_routes)
    if not merged:
        logger.warning("No routes from any source, falling back to homepage")
        return _fallback_routes(), llm_mocks

    return merged, llm_mocks


def _fallback_routes() -> list[dict]:
    return [{"path": "/", "reason": "fallback", "actions": []}]


CHANGED_REGION_PROMPT = """You decide whether a PR's visual change is confined to ONE specific element on the page.

Given the git diff, respond with a CSS selector for the changed element ONLY when the change is localized to a single, identifiable element that occupies a MINORITY of the page (e.g. a navbar, header, footer, sidebar, a specific card, a button). Otherwise respond with null.

Decision heuristic — judge by WHERE it renders, not which file changed:
- Peripheral chrome (navbar, header, footer, sidebar) or one small, self-contained component => return its selector.
- The primary content area, the hero, the page's main column, a layout/spacing change, or several separate areas => return null.

Return null (do NOT guess a selector) when:
- the change affects the whole page, the overall layout, or several separate areas,
- it's a global stylesheet / theme / config change,
- you are not confident which single element it maps to.

Selector rules:
- It must resolve to exactly ONE element on the rendered page.
- NEVER return a page-level container that wraps the main content: not `main`, `body`, `#root`, `#__next`, `html`, or a top-level layout wrapper. If the change lives inside the primary content area, return null instead.
- Prefer stable, semantic selectors: a tag like nav/header/footer/aside, an id (#id), a data attribute ([data-x]), or [aria-label="..."]. Avoid brittle Tailwind / utility class chains.
- A wrong selector is worse than null.

Output ONLY JSON, nothing else:
{"selector": "nav"}   or   {"selector": null}"""


def infer_changed_region(diff: str, api_key: str) -> str | None:
    """Return a CSS selector for the single element a localized PR change maps to,
    or None when the change is page-wide / ambiguous. Best-effort: any failure
    returns None so the caller falls back to full-page screenshots.
    """
    if not diff.strip():
        return None

    url = f"{OPENROUTER_BASE_URL}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": CHANGED_REGION_PROMPT},
            {"role": "user", "content": f"## Git Diff\n\n```diff\n{diff[:8000]}\n```"},
        ],
    }

    for attempt in range(RETRY_MAX_ATTEMPTS):
        try:
            with httpx.Client(timeout=LLM_CLIENT_TIMEOUT) as client:
                resp = client.post(url, headers=headers, json=body)
        except Exception:
            logger.warning("Changed-region request failed (attempt %d)", attempt + 1, exc_info=True)
            if attempt < RETRY_MAX_ATTEMPTS - 1:
                _sleep_backoff(attempt)
            continue

        if resp.status_code == 200:
            try:
                raw = resp.json()["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                return None
            parsed = _extract_balanced_json(raw) or _extract_balanced_json(_validate_and_repair_json(raw))
            if not isinstance(parsed, dict):
                return None
            selector = parsed.get("selector")
            if isinstance(selector, str) and selector.strip():
                logger.info("Changed-region selector inferred: %s", selector.strip())
                return selector.strip()
            return None

        if 400 <= resp.status_code < 500 and resp.status_code != 429:
            logger.warning("Changed-region: non-retryable %d", resp.status_code)
            return None
        if attempt < RETRY_MAX_ATTEMPTS - 1:
            _sleep_backoff(attempt)

    return None
