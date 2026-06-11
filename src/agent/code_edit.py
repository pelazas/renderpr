import json
import logging
import time
from pathlib import Path

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

FILE_SELECTOR_PROMPT = """You are helping a code editing assistant find the right files to edit.
Given a user request and a list of files in the project, return which files need to be edited.

Respond with a JSON array of file paths, like:
["src/app/page.tsx", "src/components/Modal.tsx"]

Return ONLY the JSON array, no explanation. Include 1-3 files at most."""

CODE_EDIT_PROMPT = """You generate code edits for a frontend Pull Request.
Given the user request and the full file contents below, output a JSON edit.

RULES:
- You may ONLY modify CSS classes, HTML structure, or text content.
- Do NOT add or modify JavaScript/TypeScript logic, event handlers, state, imports, or API calls.
- Target Tailwind classes where possible.
- Output valid JSON only. No explanation, no markdown.

OUTPUT FORMAT:
{
  "file": "relative/path/to/file.tsx",
  "line": <line number of the change>,
  "oldString": "<exact existing string to replace>",
  "newString": "<replacement string>"
}

Optionally, if the change is inside hidden UI such as a modal, overlay, dropdown, popover, drawer, accordion, or toggle that requires a click to reveal,
include an "actions" array only when the source contains a real interactive trigger. Use the exact trigger text and include source evidence:
  "actions": [{"type": "click", "selector": "text=Open modal", "sourceText": "Open modal", "reason": "button opens modal"}]
Never create actions for rendered data, table cells, badges, names, roles, statuses, headings, labels, or arbitrary visible text.

The oldString must match the file exactly. Be precise with whitespace."""


class EditGenerationError(Exception):
    pass


def _build_directory_tree(frontend_root: str) -> str:
    root = Path(frontend_root) if Path(frontend_root).is_absolute() else Path(REPO_DIR) / frontend_root
    lines: list[str] = []
    _walk_tree(root, root, lines)
    return "\n".join(lines) if lines else "(no source files found)"


def _walk_tree(base: Path, current: Path, lines: list[str], prefix: str = "") -> None:
    entries = sorted(current.iterdir(), key=lambda e: (e.is_file(), e.name))
    for i, entry in enumerate(entries):
        is_last = i == len(entries) - 1
        connector = "└── " if is_last else "├── "
        if entry.is_dir():
            if entry.name.startswith(".") or entry.name == "node_modules":
                continue
            lines.append(f"{prefix}{connector}{entry.name}/")
            extension = "    " if is_last else "│   "
            _walk_tree(base, entry, lines, prefix + extension)
        elif entry.suffix in FRONTEND_EXTENSIONS:
            rel = entry.relative_to(base)
            lines.append(f"{prefix}{connector}{rel}")


def _call_llm(messages: list[dict], api_key: str) -> str:
    import httpx

    url = f"{OPENROUTER_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": LLM_MODEL,
        "messages": messages,
    }

    for attempt in range(RETRY_MAX_ATTEMPTS):
        with httpx.Client(timeout=LLM_CLIENT_TIMEOUT) as client:
            resp = client.post(url, headers=headers, json=body)

        if resp.status_code == 200:
            data = resp.json()
            try:
                return data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                raise EditGenerationError(f"Unexpected OpenRouter response: {str(data)[:200]}")

        if 400 <= resp.status_code < 500 and resp.status_code != 429:
            raise EditGenerationError(f"Non-retryable OpenRouter error: {resp.status_code} {resp.text[:200]}")

        if attempt < RETRY_MAX_ATTEMPTS - 1:
            delay = min(LLM_RETRY_BASE_DELAY * (2 ** attempt), LLM_RETRY_MAX_DELAY)
            jitter = delay * LLM_RETRY_JITTER
            time.sleep(delay + jitter)

    raise EditGenerationError(f"OpenRouter API failed after {RETRY_MAX_ATTEMPTS} attempts")


def _select_files(query: str, directory_tree: str, api_key: str) -> list[str]:
    messages = [
        {"role": "system", "content": FILE_SELECTOR_PROMPT},
        {"role": "user", "content": f"Request: {query}\n\nProject files:\n{directory_tree}"},
    ]
    response = _call_llm(messages, api_key)
    response = response.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    try:
        files = json.loads(response)
        if not isinstance(files, list):
            raise EditGenerationError(f"File selector did not return a list: {response[:200]}")
        return [f for f in files if isinstance(f, str)]
    except json.JSONDecodeError:
        raise EditGenerationError(f"File selector returned invalid JSON: {response[:200]}")


def validate_edit(edit: dict) -> bool:
    required_keys = {"file", "line", "oldString", "newString"}
    if not required_keys.issubset(edit.keys()):
        return False
    filepath = Path(REPO_DIR) / edit["file"]
    if not filepath.exists():
        return False
    content = filepath.read_text()
    return edit["oldString"] in content


def find_occurrence_in_file(file_path: str, old_string: str, line_hint: int | None) -> int | None:
    """Return the byte offset of old_string in the file, preferring the occurrence
    closest to the given line hint. Returns None if not found.
    """
    from src.agent.editor import _find_occurrence
    full_path = Path(REPO_DIR) / file_path
    if not full_path.exists():
        return None
    content = full_path.read_text()
    return _find_occurrence(content, old_string, line_hint)


def request_edit(
    query: str,
    api_key: str,
    frontend_root: str | None = None,
) -> dict:
    root = frontend_root or ""
    tree = _build_directory_tree(root)

    selected = _select_files(query, tree, api_key)
    if not selected:
        raise EditGenerationError("No files selected for edit")

    logger.info("Selected files for edit: %s", selected)

    file_contents: list[str] = []
    for fp in selected:
        full_path = Path(REPO_DIR) / fp
        if full_path.exists() and full_path.is_file():
            content = full_path.read_text()
            file_contents.append(f"--- {fp} ---\n{content}")

    user_message = f"Request: {query}\n\nFiles:\n\n" + "\n\n".join(file_contents)

    messages = [
        {"role": "system", "content": CODE_EDIT_PROMPT},
        {"role": "user", "content": user_message},
    ]

    response = _call_llm(messages, api_key)
    response = response.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    try:
        edit = json.loads(response)
    except json.JSONDecodeError:
        logger.warning("LLM returned malformed JSON on first attempt, retrying...")
        messages.append({"role": "assistant", "content": response})
        messages.append({"role": "user", "content": "Your last response was not valid JSON. Return ONLY valid JSON with no extra text."})
        response = _call_llm(messages, api_key)
        response = response.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            edit = json.loads(response)
        except json.JSONDecodeError:
            raise EditGenerationError(f"LLM returned invalid JSON after retry: {response[:200]}")

    if not isinstance(edit, dict):
        raise EditGenerationError(f"Edit is not a dict: {edit}")

    return edit
