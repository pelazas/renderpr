import base64
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
Given the user request and the full file contents below, output every edit needed to fully satisfy it.

RULES:
- You may ONLY modify CSS classes, HTML structure, or text content.
- Do NOT add or modify JavaScript/TypeScript logic, event handlers, state, imports, or API calls.
- Target Tailwind classes where possible.
- The user describes what they SEE — map their words to the implementation:
  - A colour word covers the whole Tailwind family: "purple" may be `purple`, `violet`, `indigo`, or `fuchsia`; "orange" may be `orange` or `amber`. Match on the visible colour, not on the literal word.
  - Text colour is often produced by a GRADIENT, not a `text-*` class: look for `bg-gradient-to-*` with `from-*` / `via-*` / `to-*` colour stops combined with `bg-clip-text text-transparent`. To recolour such text you MUST change EVERY gradient stop (`from-`, `via-`, AND `to-`), not just one.
- Output ALL edits required. One visual change often needs several class edits — return one entry per class you change.
- Output valid JSON only. No explanation, no markdown.

OUTPUT FORMAT:
{
  "edits": [
    {"file": "relative/path/to/file.tsx", "line": <line number>, "oldString": "<exact existing string to replace>", "newString": "<replacement string>"}
  ],
  "actions": []
}

Each oldString must match the file EXACTLY (including whitespace) and be specific enough to locate — prefer a single className token (e.g. "via-indigo-700") over a long line.

Optionally, if the change is inside hidden UI such as a modal, overlay, dropdown, popover, drawer, accordion, or toggle that requires a click to reveal,
include an "actions" array only when the source contains a real interactive trigger. Use the exact trigger text and include source evidence:
  "actions": [{"type": "click", "selector": "text=Open modal", "sourceText": "Open modal", "reason": "button opens modal"}]
For icon-only triggers with aria-label="Open menu", use selector "[aria-label='Open menu']" and sourceText "Open menu".
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


def _user_content(text: str, images: list[bytes] | None) -> object:
    """Build OpenRouter user-message content. With images, returns the multimodal
    list form (text + inline base64 PNGs) so a vision model can SEE the rendered
    page; without, returns the plain text string.
    """
    if not images:
        return text
    content: list[dict] = [{"type": "text", "text": text}]
    for image in images:
        encoded = base64.b64encode(image).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}})
    return content


def request_edit(
    query: str,
    api_key: str,
    frontend_root: str | None = None,
    images: list[bytes] | None = None,
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

    grounding = (
        "\n\nThe attached screenshot(s) show the CURRENT rendered page. Use them to "
        "identify exactly which element and colour the user means, then locate the "
        "matching classes in the files below."
        if images else ""
    )
    user_message = f"Request: {query}{grounding}\n\nFiles:\n\n" + "\n\n".join(file_contents)

    messages: list[dict] = [
        {"role": "system", "content": CODE_EDIT_PROMPT},
        {"role": "user", "content": _user_content(user_message, images)},
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

    return _normalize_edits(edit)


def _normalize_edits(parsed: object) -> dict:
    """Coerce the LLM response into {"edits": [...], "actions": [...]}.

    Accepts the canonical {"edits": [...], "actions": [...]} object, a bare list
    of edits, or a single legacy {"file", ...} edit dict.
    """
    if isinstance(parsed, list):
        edits, actions = parsed, []
    elif isinstance(parsed, dict) and "edits" in parsed:
        edits = parsed["edits"]
        actions = parsed.get("actions", [])
    elif isinstance(parsed, dict) and "file" in parsed:
        # Legacy single-edit shape; carry any inline actions up to the top level.
        edits = [parsed]
        actions = parsed.get("actions", [])
    else:
        raise EditGenerationError(f"Unexpected edit shape: {str(parsed)[:200]}")

    if not isinstance(edits, list) or not edits:
        raise EditGenerationError(f"No edits in response: {str(parsed)[:200]}")
    if not all(isinstance(e, dict) for e in edits):
        raise EditGenerationError(f"Edit entries must be objects: {str(edits)[:200]}")

    return {"edits": edits, "actions": actions if isinstance(actions, list) else []}
