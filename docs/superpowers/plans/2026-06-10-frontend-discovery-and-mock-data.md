# Frontend Discovery & AI-Generated Mock Data Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Two independent features — (1) gracefully skip PRs without frontend changes and find the right frontend package in monorepos; (2) replace empty states / loading spinners with realistic mock API data in screenshots.

**Architecture:**
- **PR 1 (Frontend Discovery):** Pure file-extension heuristics on the git diff. No LLM calls. Scan for `.tsx`/`.jsx`/`.vue`/`.svelte`/`.astro`/`.css`/`.html` etc. If none found, post "no frontend changes" comment and exit before `npm ci`. If found, walk UP from changed files to locate the right `package.json`, respecting workspace boundaries.
- **PR 2 (AI Mock Data):** Extend the existing route inference LLM call (`routes.py`) to also output mock API response data. No new LLM call. Register Playwright route handlers in `visual.py` to intercept network requests and return mock JSON before taking screenshots.

**Tech Stack:** Python 3.12, Playwright, OpenRouter LLM (reused call)

---

## File Map

### PR 1: Frontend Discovery

| File | Action | Responsibility |
|------|--------|---------------|
| `src/agent/discovery.py` | **Create** | Frontend detection (diff parsing, package.json walking, workspace detection) |
| `src/agent/config.py` | **Modify** | Add `FRONTEND_EXTENSIONS`, `MAX_PACKAGE_SCAN_DEPTH` |
| `src/agent/main.py` | **Modify** | Swap fetch_diff before start_dev_server; call discovery; post skip comment; pass package_dir to dev server |
| `tests/test_agent/test_discovery.py` | **Create** | Tests for all discovery functions |
| `tests/test_agent/test_main.py` | **Modify** | Update _start_dev_server tests for new signature; add discovery integration tests |

### PR 2: AI-Generated Mock Data

| File | Action | Responsibility |
|------|--------|---------------|
| `src/agent/routes.py` | **Modify** | Extend LLM prompt and output schema to include `mocks`; add `_validate_mocks()` |
| `src/agent/visual.py` | **Modify** | `capture_screenshots()` accepts `mocks` param; register `page.route()` after browser launch |
| `src/agent/main.py` | **Modify** | Pass mocks from route inference output through to `capture_screenshots()` |
| `tests/test_agent/test_routes.py` | **Modify** | Tests for mock output and validation |
| `tests/test_agent/test_visual.py` | **Modify** | Tests for mock registration in page |
| `tests/test_agent/test_main.py` | **Modify** | Tests for mock wiring through pipeline |

---

# PR 1: Frontend Discovery

## Task 1.1: Add configuration constants

**Files:**
- Modify: `src/agent/config.py`

- [ ] **Add new constants to `src/agent/config.py`**

Insert after line 16 (`DEV_SERVER_POLL_INTERVAL: int = 2`):

```python
# Diff-based frontend detection
FRONTEND_EXTENSIONS: Final[tuple[str, ...]] = (
    ".tsx", ".jsx", ".vue", ".svelte", ".astro",
    ".css", ".scss", ".less", ".html",
)

# Package.json discovery
MAX_PACKAGE_SCAN_DEPTH: Final[int] = 5
```

- [ ] **Run existing tests to confirm nothing broke**

Run: `pytest tests/test_agent/test_config.py -x -q`
Expected: PASS

- [ ] **Commit**

```bash
git add src/agent/config.py
git commit -m "feat(config): add frontend discovery constants"
```

## Task 1.2: Write failing tests for discovery module

**Files:**
- Create: `tests/test_agent/test_discovery.py`

- [ ] **Write `tests/test_agent/test_discovery.py`**

```python
from pathlib import Path

import pytest


class TestDetectFrontendChanges:
    def test_tsx_file_detected(self):
        from src.agent.discovery import detect_frontend_changes
        diff = """diff --git a/src/page.tsx b/src/page.tsx
--- a/src/page.tsx
+++ b/src/page.tsx
@@ -1 +1 @@
-old
+new"""
        assert detect_frontend_changes(diff) is True

    def test_jsx_file_detected(self):
        from src.agent.discovery import detect_frontend_changes
        diff = """diff --git a/src/App.jsx b/src/App.jsx
--- a/src/App.jsx
+++ b/src/App.jsx
@@ -1 +1 @@
-old
+new"""
        assert detect_frontend_changes(diff) is True

    def test_vue_file_detected(self):
        from src.agent.discovery import detect_frontend_changes
        diff = """diff --git a/src/App.vue b/src/App.vue
--- a/src/App.vue
+++ b/src/App.vue
@@ -1 +1 @@
-old
+new"""
        assert detect_frontend_changes(diff) is True

    def test_css_file_detected(self):
        from src.agent.discovery import detect_frontend_changes
        diff = """diff --git a/src/styles.css b/src/styles.css
--- a/src/styles.css
+++ b/src/styles.css
@@ -1 +1 @@
-old
+new"""
        assert detect_frontend_changes(diff) is True

    def test_py_file_not_detected(self):
        from src.agent.discovery import detect_frontend_changes
        diff = """diff --git a/src/main.py b/src/main.py
--- a/src/main.py
+++ b/src/main.py
@@ -1 +1 @@
-old
+new"""
        assert detect_frontend_changes(diff) is False

    def test_ts_file_not_detected(self):
        from src.agent.discovery import detect_frontend_changes
        diff = """diff --git a/src/utils.ts b/src/utils.ts
--- a/src/utils.ts
+++ b/src/utils.ts
@@ -1 +1 @@
-old
+new"""
        assert detect_frontend_changes(diff) is False

    def test_empty_diff_returns_false(self):
        from src.agent.discovery import detect_frontend_changes
        assert detect_frontend_changes("") is False

    def test_backend_only_pr_returns_false(self):
        from src.agent.discovery import detect_frontend_changes
        diff = """diff --git a/Dockerfile b/Dockerfile
--- a/Dockerfile
+++ b/Dockerfile
@@ -1 +1 @@
-old
+new
diff --git a/src/main.py b/src/main.py
--- a/src/main.py
+++ b/src/main.py
@@ -1 +1 @@
-old
+new"""
        assert detect_frontend_changes(diff) is False

    def test_mixed_frontend_backend_returns_true(self):
        from src.agent.discovery import detect_frontend_changes
        diff = """diff --git a/src/main.py b/src/main.py
--- a/src/main.py
+++ b/src/main.py
@@ -1 +1 @@
-old
+new
diff --git a/src/App.tsx b/src/App.tsx
--- a/src/App.tsx
+++ b/src/App.tsx
@@ -1 +1 @@
-old
+new"""
        assert detect_frontend_changes(diff) is True


class TestFindNearestPackageJson:
    def test_finds_package_json_in_same_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.discovery.REPO_DIR", str(tmp_path))
        pkg = tmp_path / "package.json"
        pkg.write_text('{"name": "test"}')

        from src.agent.discovery import find_nearest_package_json
        result = find_nearest_package_json(["src/page.tsx"])
        assert result == str(pkg)

    def test_walks_up_from_deeply_nested_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.discovery.REPO_DIR", str(tmp_path))
        changed = tmp_path / "packages" / "web" / "src" / "page.tsx"
        changed.parent.mkdir(parents=True)
        pkg = tmp_path / "packages" / "web" / "package.json"
        pkg.write_text('{"name": "web"}')

        from src.agent.discovery import find_nearest_package_json
        result = find_nearest_package_json(["packages/web/src/page.tsx"])
        assert result == str(pkg)

    def test_returns_root_package_json_if_none_in_subdir(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.discovery.REPO_DIR", str(tmp_path))
        pkg = tmp_path / "package.json"
        pkg.write_text('{"name": "root"}')

        from src.agent.discovery import find_nearest_package_json
        result = find_nearest_package_json(["src/page.tsx"])
        assert result == str(pkg)

    def test_returns_none_if_no_package_json_at_all(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.discovery.REPO_DIR", str(tmp_path))

        from src.agent.discovery import find_nearest_package_json
        result = find_nearest_package_json(["src/page.tsx"])
        assert result is None

    def test_respects_depth_limit(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.discovery.REPO_DIR", str(tmp_path))
        monkeypatch.setattr("src.agent.discovery.MAX_PACKAGE_SCAN_DEPTH", 2)

        deep_dir = tmp_path / "a" / "b" / "c" / "d"
        deep_dir.mkdir(parents=True)
        (tmp_path / "a" / "b" / "c" / "package.json").write_text('{"name": "deep"}')

        from src.agent.discovery import find_nearest_package_json
        result = find_nearest_package_json(["a/b/c/d/file.tsx"])
        assert result is None

    def test_multiple_changed_files_same_package(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.discovery.REPO_DIR", str(tmp_path))
        pkg = tmp_path / "packages" / "web" / "package.json"
        pkg.parent.mkdir(parents=True)
        pkg.write_text('{"name": "web"}')

        from src.agent.discovery import find_nearest_package_json
        result = find_nearest_package_json([
            "packages/web/src/page.tsx",
            "packages/web/src/components/Header.tsx",
        ])
        assert result == str(pkg)


class TestFindWorkspaceRoot:
    def test_returns_parent_with_workspaces(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.discovery.REPO_DIR", str(tmp_path))
        root_pkg = tmp_path / "package.json"
        root_pkg.write_text('{"workspaces": ["packages/*"]}')
        web_pkg = tmp_path / "packages" / "web" / "package.json"
        web_pkg.parent.mkdir(parents=True)
        web_pkg.write_text('{"name": "web"}')

        from src.agent.discovery import find_workspace_root
        result = find_workspace_root(str(web_pkg))
        assert result == str(root_pkg)

    def test_returns_none_if_no_workspaces(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.discovery.REPO_DIR", str(tmp_path))
        pkg = tmp_path / "package.json"
        pkg.write_text('{"name": "root"}')

        from src.agent.discovery import find_workspace_root
        result = find_workspace_root(str(pkg))
        assert result is None

    def test_returns_none_if_package_json_not_found(self):
        from src.agent.discovery import find_workspace_root
        result = find_workspace_root("/nonexistent/path/package.json")
        assert result is None

    def test_root_package_itself_has_workspaces(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.discovery.REPO_DIR", str(tmp_path))
        pkg = tmp_path / "package.json"
        pkg.write_text('{"workspaces": ["packages/*"]}')

        from src.agent.discovery import find_workspace_root
        result = find_workspace_root(str(pkg))
        assert result is None


class TestVerifyDevScript:
    def test_dev_script_exists(self, tmp_path):
        pkg = tmp_path / "package.json"
        pkg.write_text('{"scripts": {"dev": "next dev"}}')

        from src.agent.discovery import verify_dev_script
        assert verify_dev_script(str(pkg)) is True

    def test_no_dev_script(self, tmp_path):
        pkg = tmp_path / "package.json"
        pkg.write_text('{"scripts": {"build": "next build"}}')

        from src.agent.discovery import verify_dev_script
        assert verify_dev_script(str(pkg)) is False

    def test_no_scripts_at_all(self, tmp_path):
        pkg = tmp_path / "package.json"
        pkg.write_text('{"name": "test"}')

        from src.agent.discovery import verify_dev_script
        assert verify_dev_script(str(pkg)) is False

    def test_file_not_found(self):
        from src.agent.discovery import verify_dev_script
        assert verify_dev_script("/nonexistent/package.json") is False


class TestGetChangedFiles:
    def test_extracts_from_diff(self):
        from src.agent.discovery import _get_changed_files

        diff = """diff --git a/src/page.tsx b/src/page.tsx
--- a/src/page.tsx
+++ b/src/page.tsx
diff --git a/src/Header.tsx b/src/Header.tsx
--- a/src/Header.tsx
+++ b/src/Header.tsx"""
        result = _get_changed_files(diff)
        assert "src/page.tsx" in result
        assert "src/Header.tsx" in result

    def test_empty_diff_returns_empty(self):
        from src.agent.discovery import _get_changed_files
        assert _get_changed_files("") == []


class TestDiscoverFrontend:
    def test_full_discovery_returns_package_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.discovery.REPO_DIR", str(tmp_path))
        pkg = tmp_path / "package.json"
        pkg.write_text('{"scripts": {"dev": "next dev"}}')

        diff = """diff --git a/src/page.tsx b/src/page.tsx
--- a/src/page.tsx
+++ b/src/page.tsx
@@ -1 +1 @@
-old
+new"""

        from src.agent.discovery import discover_frontend
        result = discover_frontend(diff)
        assert result["has_frontend"] is True
        assert result["package_json_path"] == str(pkg)
        assert result["dev_command"] == "npm run dev"

    def test_no_frontend_files_returns_early(self):
        from src.agent.discovery import discover_frontend
        diff = """diff --git a/src/main.py b/src/main.py
--- a/src/main.py
+++ b/src/main.py
@@ -1 +1 @@
-old
+new"""
        result = discover_frontend(diff)
        assert result["has_frontend"] is False
        assert result["reason"] is not None

    def test_no_package_json_returns_no_package(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.discovery.REPO_DIR", str(tmp_path))
        diff = """diff --git a/src/page.tsx b/src/page.tsx
--- a/src/page.tsx
+++ b/src/page.tsx
@@ -1 +1 @@
-old
+new"""

        from src.agent.discovery import discover_frontend
        result = discover_frontend(diff)
        assert result["has_frontend"] is True
        assert result["package_json_path"] is None

    def test_no_dev_script_returns_no_command(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.discovery.REPO_DIR", str(tmp_path))
        pkg = tmp_path / "package.json"
        pkg.write_text('{"name": "test"}')

        diff = """diff --git a/src/page.tsx b/src/page.tsx
--- a/src/page.tsx
+++ b/src/page.tsx
@@ -1 +1 @@
-old
+new"""

        from src.agent.discovery import discover_frontend
        result = discover_frontend(diff)
        assert result["has_frontend"] is True
        assert result["package_json_path"] == str(pkg)
        assert result["dev_command"] is None

    def test_monorepo_detects_workspace_root(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.discovery.REPO_DIR", str(tmp_path))
        root_pkg = tmp_path / "package.json"
        root_pkg.write_text('{"workspaces": ["packages/*"]}')
        web_pkg = tmp_path / "packages" / "web" / "package.json"
        web_pkg.parent.mkdir(parents=True)
        web_pkg.write_text('{"scripts": {"dev": "next dev"}}')

        diff = """diff --git a/packages/web/src/page.tsx b/packages/web/src/page.tsx
--- a/packages/web/src/page.tsx
+++ b/packages/web/src/page.tsx
@@ -1 +1 @@
-old
+new"""

        from src.agent.discovery import discover_frontend
        result = discover_frontend(diff)
        assert result["has_frontend"] is True
        assert result["package_json_path"] == str(web_pkg)
        assert result["workspace_root"] == str(root_pkg)
        assert result["dev_command"] == "npm run dev"

    def test_empty_diff_returns_no_frontend(self):
        from src.agent.discovery import discover_frontend
        result = discover_frontend("")
        assert result["has_frontend"] is False
```

- [ ] **Run tests to confirm they fail**

Run: `pytest tests/test_agent/test_discovery.py -x -v`
Expected: ImportError (module doesn't exist yet)

- [ ] **Commit**

```bash
git add tests/test_agent/test_discovery.py
git commit -m "test(discovery): add failing tests for frontend detection"
```

## Task 1.3: Implement discovery module

**Files:**
- Create: `src/agent/discovery.py`

- [ ] **Write `src/agent/discovery.py`**

```python
import json
import logging
from pathlib import Path

from src.agent.config import FRONTEND_EXTENSIONS, MAX_PACKAGE_SCAN_DEPTH, REPO_DIR

logger = logging.getLogger(__name__)


def detect_frontend_changes(diff: str) -> bool:
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
            ext = Path(path).suffix
            if ext in FRONTEND_EXTENSIONS:
                return True
    return False


def _get_changed_files(diff: str) -> list[str]:
    files: list[str] = []
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            files.append(line[6:])
    return files


def find_nearest_package_json(changed_files: list[str]) -> str | None:
    repo_path = Path(REPO_DIR)

    if not repo_path.exists():
        return None

    root_package = repo_path / "package.json"
    root_found = root_package.exists()

    candidates: set[Path] = set()
    for file_path in changed_files:
        parts = Path(file_path).parts
        for depth in range(len(parts), -1, -1):
            candidate_dir = repo_path.joinpath(*parts[:depth])
            if candidate_dir == repo_path:
                break
            candidate = candidate_dir / "package.json"
            if candidate.exists():
                candidates.add(candidate_dir)
                break

    if candidates:
        sorted_candidates = sorted(candidates, key=lambda p: len(p.parts), reverse=True)
        deepest = sorted_candidates[0]
        relative_depth = len(deepest.relative_to(repo_path).parts)
        if relative_depth <= MAX_PACKAGE_SCAN_DEPTH:
            return str(deepest / "package.json")

    return str(root_package) if root_found else None


def find_workspace_root(package_json_path: str | None) -> str | None:
    if not package_json_path:
        return None

    pkg_path = Path(package_json_path)
    repo_path = Path(REPO_DIR)

    current = pkg_path.parent
    while current != repo_path.parent:
        candidate = current / "package.json"
        if candidate.exists():
            try:
                data = json.loads(candidate.read_text())
                if "workspaces" in data and candidate != pkg_path:
                    return str(candidate)
            except (json.JSONDecodeError, OSError):
                pass
        if current == repo_path:
            break
        current = current.parent

    return None


def verify_dev_script(package_json_path: str | None) -> bool:
    if not package_json_path:
        return False
    try:
        data = json.loads(Path(package_json_path).read_text())
        scripts = data.get("scripts", {})
        return "dev" in scripts
    except (json.JSONDecodeError, OSError):
        return False


def discover_frontend(diff: str) -> dict:
    if not detect_frontend_changes(diff):
        return {
            "has_frontend": False,
            "package_json_path": None,
            "workspace_root": None,
            "dev_command": None,
            "reason": "No frontend files changed in this PR.",
        }

    changed_files = _get_changed_files(diff)
    package_json_path = find_nearest_package_json(changed_files)

    if not package_json_path:
        return {
            "has_frontend": True,
            "package_json_path": None,
            "workspace_root": None,
            "dev_command": None,
            "reason": "Frontend files detected but no Node.js project found (no package.json).",
        }

    if not verify_dev_script(package_json_path):
        return {
            "has_frontend": True,
            "package_json_path": package_json_path,
            "workspace_root": None,
            "dev_command": None,
            "reason": f"Found frontend project at {package_json_path} but no 'dev' script defined.",
        }

    workspace_root = find_workspace_root(package_json_path)

    return {
        "has_frontend": True,
        "package_json_path": package_json_path,
        "workspace_root": workspace_root,
        "dev_command": "npm run dev",
        "reason": None,
    }
```

- [ ] **Run tests to confirm they pass**

Run: `pytest tests/test_agent/test_discovery.py -x -v`
Expected: all tests pass

- [ ] **Commit**

```bash
git add src/agent/discovery.py
git commit -m "feat(discovery): implement frontend detection and package.json discovery"
```

## Task 1.4: Integrate discovery into main.py

**Files:**
- Modify: `src/agent/main.py`
- Modify: `tests/test_agent/test_main.py`

- [ ] **Modify `src/agent/main.py` — add import**

Add this import at line 14 (after `from src.agent.config import ...`):

```python
from src.agent.discovery import discover_frontend
```

- [ ] **Modify `src/agent/main.py` — change `_start_dev_server` to accept package/install dir**

Replace the existing `_start_dev_server()` function (from line 85 to the end of function definition) with one that accepts optional package/install dirs:

```python
def _start_dev_server(
    package_dir: str | None = None,
    install_dir: str | None = None,
) -> None:
    global _dev_server_proc, _dev_server_url

    dev_cwd = Path(package_dir).parent if package_dir else Path(REPO_DIR)
    install_cwd = Path(install_dir).parent if install_dir else dev_cwd

    pkg_json = os.path.join(dev_cwd, "package.json")
    if not os.path.exists(pkg_json):
        logger.error("No package.json found at %s", pkg_json)
        sys.exit(1)

    try:
        subprocess.run(
            ["npm", "ci"],
            cwd=str(install_cwd),
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )
    except Exception:
        logger.exception("npm ci failed")
        sys.exit(1)

    _dev_server_proc = subprocess.Popen(["npm", "run", "dev"], cwd=str(dev_cwd))

    _dev_server_url = f"http://{DEV_SERVER_HOST}:{DEV_SERVER_PORT}/"
    url = _dev_server_url
    deadline = time.time() + DEV_SERVER_START_TIMEOUT
    while time.time() < deadline:
        try:
            with httpx.Client(timeout=30) as client:
                resp = client.get(url)
            logger.info("Dev server ready (status %d)", resp.status_code)
            return
        except httpx.HTTPError:
            time.sleep(DEV_SERVER_POLL_INTERVAL)

    logger.error("Dev server did not start within %ds", DEV_SERVER_START_TIMEOUT)
    if _dev_server_proc:
        _dev_server_proc.kill()
    sys.exit(1)
```

- [ ] **Modify `src/agent/main.py` — reorder `run()` to fetch diff earlier and call discovery**

Replace the current `run()` function body (lines 282-348) with:

```python
def run() -> None:
    logging.basicConfig(level=logging.INFO)

    installation_id = os.environ.get("INSTALLATION_ID", "unknown")
    repo_full_name = os.environ.get("REPO_FULL_NAME", "unknown")
    pr_number = os.environ.get("PR_NUMBER", "unknown")

    logger.info("RenderPR agent started")
    logger.info("Installation ID: %s", installation_id)
    logger.info("Repository: %s", repo_full_name)
    logger.info("PR Number: %s", pr_number)

    secrets = _fetch_secrets()
    token = _get_installation_token(
        installation_id=installation_id,
        app_id=secrets["app_id"],
        private_key=secrets["private_key"],
    )
    _clone_repo(
        repo_full_name=repo_full_name,
        pr_number=pr_number,
        token=token,
    )

    diff = _fetch_diff(
        token=token,
        repo_full_name=repo_full_name,
        pr_number=pr_number,
    )
    logger.info("Fetched diff for PR #%s (%d bytes)", pr_number, len(diff))
    logger.info("Changes: %s", _parse_diff_summary(diff))

    discovery = discover_frontend(diff)
    if not discovery["has_frontend"]:
        _post_comment(
            token=token,
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            body=f"## RenderPR\n\n{discovery['reason']}\n\nSkipping review.",
        )
        logger.info("No frontend changes detected. Exiting gracefully.")
        return

    if discovery["package_json_path"] is None or discovery["dev_command"] is None:
        _post_comment(
            token=token,
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            body=f"## RenderPR\n\n{discovery['reason']}\n\nSkipping review.",
        )
        logger.info("Cannot start dev server. Exiting gracefully.")
        return

    _start_dev_server(
        package_dir=discovery["package_json_path"],
        install_dir=discovery.get("workspace_root"),
    )

    logger.info("Dev server ready. Proceeding to review...")

    screenshot_paths, screenshot_urls = _capture_screenshots(diff, secrets)
    logger.info(
        "Captured %d screenshots: %s",
        len(screenshot_paths),
        ", ".join(p.name for p in screenshot_paths),
    )

    from src.agent.review import ReviewError, run_review

    try:
        review_body = run_review(
            diff=diff,
            screenshot_paths=screenshot_paths,
            openrouter_api_key=secrets["openrouter_api_key"],
            screenshot_urls=screenshot_urls,
        )
    except ReviewError:
        logger.exception("Review failed")
        sys.exit(1)

    _post_comment(
        token=token,
        repo_full_name=repo_full_name,
        pr_number=pr_number,
        body=review_body,
    )

    logger.info("RenderPR agent finished")
```

- [ ] **Update `tests/test_agent/test_main.py` — update `_start_dev_server` tests for new signature**

The existing `_start_dev_server` tests mock internal functions (`os.path.exists`, `subprocess.run`, etc.) and don't test the default behavior change. The tests for `test_no_package_json`, `test_npm_ci_fails`, `test_dev_server_ready_on_first_poll`, and `test_dev_server_timeout` all test `_start_dev_server()` with no arguments. Since `package_dir` and `install_dir` are optional (default `None`), they should still work. **No test changes needed for existing tests.**

Add a new test for the explicit package dir parameter:

```python
class TestStartDevServer:
    # ... existing tests ...

    def test_start_dev_server_with_package_dir(self, monkeypatch):
        monkeypatch.setattr("os.path.exists", lambda p: True)
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: None)

        import subprocess
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: type("Proc", (), {"kill": lambda self: None, "pid": 123})())

        import httpx
        mock_resp = httpx.Response(200)
        monkeypatch.setattr(httpx, "Client", lambda *a, **kw: _mock_client(mock_resp))

        _start_dev_server(
            package_dir="/app/repo/packages/web/package.json",
            install_dir="/app/repo/package.json",
        )
```

- [ ] **Add a new test class for discovery integration in `tests/test_agent/test_main.py`**

Add at the end of the file:

```python
class TestDiscoveryIntegration:
    def test_no_frontend_skips_and_posts_comment(self, monkeypatch):
        monkeypatch.setattr("src.agent.main._fetch_secrets", lambda: {"app_id": "1", "private_key": "k", "openrouter_api_key": "o"})
        monkeypatch.setattr("src.agent.main._get_installation_token", lambda *a, **kw: "fake-token")
        monkeypatch.setattr("src.agent.main._clone_repo", lambda *a, **kw: None)
        monkeypatch.setattr("src.agent.main._fetch_diff", lambda *a, **kw: "diff --git a/main.py b/main.py\n--- a/main.py\n+++ b/main.py\n@@ -1 +1 @@\n-old\n+new")

        posted = []
        monkeypatch.setattr("src.agent.main._post_comment", lambda *a, body, **kw: posted.append(body))
        monkeypatch.setattr("src.agent.main._start_dev_server", lambda *a, **kw: None)
        monkeypatch.setattr("src.agent.main._capture_screenshots", lambda *a, **kw: ([], []))
        monkeypatch.setattr("src.agent.review.run_review", lambda *a, **kw: "## Review")

        run()

        assert len(posted) == 1
        assert "no frontend" in posted[0].lower()

    def test_frontend_without_package_skips_and_posts(self, monkeypatch, tmp_path):
        monkeypatch.setattr("src.agent.main._fetch_secrets", lambda: {"app_id": "1", "private_key": "k", "openrouter_api_key": "o"})
        monkeypatch.setattr("src.agent.main._get_installation_token", lambda *a, **kw: "fake-token")
        monkeypatch.setattr("src.agent.main._clone_repo", lambda *a, **kw: None)
        monkeypatch.setattr("src.agent.discovery.REPO_DIR", str(tmp_path))
        monkeypatch.setattr("src.agent.main._fetch_diff", lambda *a, **kw: "diff --git a/page.tsx b/page.tsx\n--- a/page.tsx\n+++ b/page.tsx\n@@ -1 +1 @@\n-old\n+new")

        posted = []
        monkeypatch.setattr("src.agent.main._post_comment", lambda *a, body, **kw: posted.append(body))
        monkeypatch.setattr("src.agent.main._start_dev_server", lambda *a, **kw: None)
        monkeypatch.setattr("src.agent.main._capture_screenshots", lambda *a, **kw: ([], []))
        monkeypatch.setattr("src.agent.review.run_review", lambda *a, **kw: "## Review")

        run()

        assert len(posted) == 1
        assert "no package.json" in posted[0].lower()

    def test_frontend_with_package_proceeds_normally(self, monkeypatch, tmp_path):
        monkeypatch.setattr("src.agent.main._fetch_secrets", lambda: {"app_id": "1", "private_key": "k", "openrouter_api_key": "o"})
        monkeypatch.setattr("src.agent.main._get_installation_token", lambda *a, **kw: "fake-token")
        monkeypatch.setattr("src.agent.main._clone_repo", lambda *a, **kw: None)

        pkg = tmp_path / "package.json"
        pkg.write_text('{"scripts": {"dev": "next dev"}}')
        monkeypatch.setattr("src.agent.discovery.REPO_DIR", str(tmp_path))
        monkeypatch.setattr("src.agent.main._fetch_diff", lambda *a, **kw: "diff --git a/page.tsx b/page.tsx\n--- a/page.tsx\n+++ b/page.tsx\n@@ -1 +1 @@\n-old\n+new")

        started_with = {}
        def track_start(package_dir=None, install_dir=None, **kw):
            started_with["package_dir"] = package_dir
            started_with["install_dir"] = install_dir
        monkeypatch.setattr("src.agent.main._start_dev_server", track_start)

        monkeypatch.setattr("src.agent.main._capture_screenshots", lambda *a, **kw: ([], []))
        monkeypatch.setattr("src.agent.review.run_review", lambda *a, **kw: "## Review")
        posted = []
        monkeypatch.setattr("src.agent.main._post_comment", lambda *a, body, **kw: posted.append(body))

        run()

        assert len(posted) == 1
        assert "## Review" in posted[0]
```

- [ ] **Run all tests to confirm they pass**

Run: `pytest tests/test_agent/test_main.py tests/test_agent/test_discovery.py -x -v`
Expected: all tests pass

- [ ] **Commit**

```bash
git add src/agent/main.py tests/test_agent/test_main.py
git commit -m "feat(main): integrate frontend discovery and move diff fetch before dev server"
```

- [ ] **Run full test suite to verify nothing regressed**

Run: `pytest tests/ -x -q`
Expected: all tests pass (0 errors, 0 failures)

---

# PR 2: AI-Generated Mock Data

## Task 2.1: Write failing tests for mock data in routes

**Files:**
- Modify: `tests/test_agent/test_routes.py`

- [ ] **Add mock-related tests to `tests/test_agent/test_routes.py`**

Add after the `TestExtractJson` class:

```python
class TestValidateMocks:
    def test_valid_mocks_pass_through(self):
        from src.agent.routes import _validate_mocks
        mocks = {
            "api.example.com": {
                "/api/users": {"body": {"users": [{"id": 1}]}},
                "/api/posts": {"body": {"posts": []}, "status": 200},
            }
        }
        result = _validate_mocks(mocks)
        assert result == mocks

    def test_invalid_domain_skipped(self):
        from src.agent.routes import _validate_mocks
        mocks = {
            "api.example.com": {"/api/users": {"body": {"ok": True}}},
            123: {"/api/bad": {"body": {}}},
        }
        result = _validate_mocks(mocks)
        assert "api.example.com" in result
        assert 123 not in result

    def test_invalid_path_skipped(self):
        from src.agent.routes import _validate_mocks
        mocks = {
            "api.example.com": {
                "/api/users": {"body": {"ok": True}},
                123: {"body": {}},
            }
        }
        result = _validate_mocks(mocks)
        assert "/api/users" in result["api.example.com"]
        assert 123 not in result["api.example.com"]

    def test_missing_body_skipped(self):
        from src.agent.routes import _validate_mocks
        mocks = {
            "api.example.com": {
                "/api/valid": {"body": {"ok": True}},
                "/api/invalid": {"status": 200},
            }
        }
        result = _validate_mocks(mocks)
        assert "/api/valid" in result["api.example.com"]
        assert "/api/invalid" not in result["api.example.com"]

    def test_body_not_dict_skipped(self):
        from src.agent.routes import _validate_mocks
        mocks = {
            "api.example.com": {
                "/api/valid": {"body": {"ok": True}},
                "/api/invalid": {"body": "string instead of dict"},
            }
        }
        result = _validate_mocks(mocks)
        assert "/api/valid" in result["api.example.com"]
        assert "/api/invalid" not in result["api.example.com"]

    def test_non_dict_mocks_value(self):
        from src.agent.routes import _validate_mocks
        mocks = {"api.example.com": "not a dict"}
        result = _validate_mocks(mocks)
        assert result == {}

    def test_empty_mocks(self):
        from src.agent.routes import _validate_mocks
        result = _validate_mocks({})
        assert result == {}

    def test_status_defaults_to_200(self):
        from src.agent.routes import _validate_mocks
        mocks = {
            "api.example.com": {
                "/api/users": {"body": {"ok": True}},
            }
        }
        result = _validate_mocks(mocks)
        assert result["api.example.com"]["/api/users"]["status"] == 200

    def test_none_mocks(self):
        from src.agent.routes import _validate_mocks
        result = _validate_mocks(None)
        assert result == {}


class TestMockOutput:
    def test_route_inference_includes_mocks(self, mock_httpx_client):
        mock_httpx_client([
            httpx.Response(200, json={
                "choices": [{"message": {"content": json.dumps({
                    "routes": [{"path": "/", "reason": "test", "actions": []}],
                    "mocks": {
                        "api.example.com": {
                            "/api/users": {
                                "body": {"users": [{"id": 1, "name": "Alice"}]},
                            },
                        },
                    },
                })}}],
            }),
        ])

        from src.agent.routes import infer_routes

        routes, mocks = infer_routes("diff", "tree", "sk-or-fake")
        assert len(routes) == 1
        assert "/api/users" in mocks.get("api.example.com", {})

    def test_no_mocks_in_response(self, mock_httpx_client):
        mock_httpx_client([
            httpx.Response(200, json={
                "choices": [{"message": {"content": json.dumps({
                    "routes": [{"path": "/", "reason": "test", "actions": []}],
                })}}],
            }),
        ])

        from src.agent.routes import infer_routes

        routes, mocks = infer_routes("diff", "tree", "sk-or-fake")
        assert len(routes) == 1
        assert mocks == {}

    def test_empty_routes_with_mocks_still_proceeds(self, mock_httpx_client):
        mock_httpx_client([
            httpx.Response(200, json={
                "choices": [{"message": {"content": json.dumps({
                    "routes": [{"path": "/", "reason": "fallback", "actions": []}],
                    "mocks": {"api.example.com": {"/api/ping": {"body": {"ok": True}}}},
                })}}],
            }),
        ])

        from src.agent.routes import infer_routes

        routes, mocks = infer_routes("diff", "tree", "sk-or-fake")
        assert len(routes) == 1
        assert "api.example.com" in mocks
```

- [ ] **Run new tests to confirm they fail**

Run: `pytest tests/test_agent/test_routes.py::TestValidateMocks tests/test_agent/test_routes.py::TestMockOutput -x -v`
Expected: Failures (import errors, function signature mismatches)

- [ ] **Commit**

```bash
git add tests/test_agent/test_routes.py
git commit -m "test(routes): add failing tests for mock data output and validation"
```

## Task 2.2: Extend routes.py with mock data

**Files:**
- Modify: `src/agent/routes.py`

- [ ] **Extend the `ROUTE_INFERENCE_PROMPT` to include mock data instructions**

Replace the existing prompt with one that also asks for mocks:

```python
ROUTE_INFERENCE_PROMPT = """You are a frontend routing analyzer. Given a git diff, full file contents of changed files, their reverse dependencies (files that import the changed files), and the project file tree,
identify which routes are affected by this change and what interactions are needed to surface the changes visually.

You MUST also identify API calls in the changed files and generate mock JSON response data for them.

Rules:
- A route file change (e.g., app/dashboard/page.tsx) -> direct route (/dashboard)
- A shared component change (e.g., components/Button.tsx) -> routes that use it
- ALWAYS include ALL routes that have changed route files (page.tsx, layout.tsx, etc.)
- If interactions are needed (click to open modal/dropdown), use the file contents and reverse dependencies to trace the full chain: trigger button -> state -> rendered component
- Reverse dependencies are files that import the changed files — use them to find buttons, state hooks, and event handlers
- For clicking buttons by their visible text, use Playwright's `:has-text()` pseudo-selector (e.g., `"button:has-text('Open Modal')"`)
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
```

- [ ] **Add `_validate_mocks` function to `routes.py`**

Add after the `_validate_routes` function:

```python
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
            if not isinstance(body, dict):
                continue
            validated_endpoints[path] = {
                "body": body,
                "status": mock_data.get("status", 200),
            }
        if validated_endpoints:
            validated[domain] = validated_endpoints
    return validated
```

- [ ] **Modify `infer_routes` to return routes AND mocks**

Change the function signature and return type. The function currently returns `list[dict]`. Change it to return `tuple[list[dict], dict]`.

At the end of `infer_routes`, change the return statements:

Before (line ~208-216):
```python
                raw_routes = parsed.get("routes", [])
                routes = _validate_routes(raw_routes)
                if routes:
                    logger.info("Inferred %d route(s): %s", len(routes), [r["path"] for r in routes])
                    return routes
                logger.warning("LLM returned empty or invalid routes, falling back to homepage")
                return _fallback_routes()
```

After:
```python
                raw_routes = parsed.get("routes", [])
                raw_mocks = parsed.get("mocks")
                routes = _validate_routes(raw_routes)
                mocks = _validate_mocks(raw_mocks)
                if routes:
                    logger.info("Inferred %d route(s): %s", len(routes), [r["path"] for r in routes])
                    if mocks:
                        logger.info("Generated mocks for %d domain(s)", len(mocks))
                    return routes, mocks
                logger.warning("LLM returned empty or invalid routes, falling back to homepage")
                return _fallback_routes(), {}
```

And change the fallback return at the very end.
Change `return _fallback_routes()` to `return _fallback_routes(), {}` (4 occurrences).
Change `return [{"path": "/", "reason": "fallback", "actions": []}]` to just the routes part — the function itself returns a tuple now.

- [ ] **Update `_fallback_routes` signature** — not needed, keep as-is.

- [ ] **Update callers of `infer_routes`** — only `main.py` calls this. We'll handle that in Task 2.4.

- [ ] **Run tests to confirm they pass**

Run: `pytest tests/test_agent/test_routes.py::TestValidateMocks tests/test_agent/test_routes.py::TestMockOutput tests/test_agent/test_routes.py::TestValidateRoutes tests/test_agent/test_routes.py::TestInferRoutes -x -v`
Expected: all pass (existing tests might fail on function signature change — see below for TestInferRoutes fixes)

- [ ] **Fix existing TestInferRoutes tests for new return type**

Since `infer_routes` now returns `(routes, mocks)`, update existing tests that call it:

```python
# Old:
result = infer_routes(...)
assert len(result) == 2

# New:
routes, mocks = infer_routes(...)
assert len(routes) == 2
assert isinstance(mocks, dict)
```

Apply this change throughout `TestInferRoutes` and all its test methods.

- [ ] **Commit**

```bash
git add src/agent/routes.py tests/test_agent/test_routes.py
git commit -m "feat(routes): extend route inference to generate mock data"
```

## Task 2.3: Write failing tests for mock data in visual.py

**Files:**
- Modify: `tests/test_agent/test_visual.py`

- [ ] **Add mock-related tests to `tests/test_agent/test_visual.py`**

At the end of the file, before any fixture definitions, add:

```python
class TestCaptureScreenshotsWithMocks:
    def test_mocks_are_registered_on_page(self, tmp_path, monkeypatch):
        registered_routes = []

        class MockPageWithRoute:
            def __init__(self):
                self.viewport_size = None
                self.goto_url = None
                self.screenshot_path = None

            def set_viewport_size(self, size):
                self.viewport_size = size

            def goto(self, url, **kw):
                self.goto_url = url

            def screenshot(self, path, **kw):
                self.screenshot_path = path
                Path(path).touch()

            def route(self, pattern, handler):
                registered_routes.append((pattern, handler))

        class MockContextWithRoute:
            def new_page(self):
                return MockPageWithRoute()

        class MockBrowserWithRoute:
            def launch(self):
                return self
            def new_context(self):
                return MockContextWithRoute()
            def close(self):
                pass

        class MockPlaywrightWithRoute:
            chromium = MockBrowserWithRoute()

        class MockSyncPlaywrightWithRoute:
            def __enter__(self):
                return MockPlaywrightWithRoute()
            def __exit__(self, *a):
                pass

        monkeypatch.setattr(
            "playwright.sync_api.sync_playwright",
            lambda: MockSyncPlaywrightWithRoute(),
        )

        from src.agent.visual import capture_screenshots

        mocks = {
            "api.example.com": {
                "/api/users": {"body": {"users": []}, "status": 200},
                "/api/posts": {"body": {"posts": []}},
            }
        }

        capture_screenshots(
            "http://localhost:3000",
            screenshot_dir=tmp_path,
            routes=[{"path": "/", "actions": [], "reason": "home"}],
            mocks=mocks,
        )

        assert len(registered_routes) == 2
        patterns = [p for p, _ in registered_routes]
        assert any("api.example.com" in p for p in patterns)
        assert any("/api/users" in p or "/api/posts" in p for p in patterns)

    def test_no_mocks_when_not_provided(self, tmp_path, monkeypatch):
        registered_routes = []

        class MockPageWithRoute:
            def __init__(self):
                self.viewport_size = None
                self.goto_url = None
                self.screenshot_path = None

            def set_viewport_size(self, size):
                self.viewport_size = size

            def goto(self, url, **kw):
                self.goto_url = url

            def screenshot(self, path, **kw):
                self.screenshot_path = path
                Path(path).touch()

            def route(self, pattern, handler):
                registered_routes.append((pattern, handler))

        # Reuse same mocks as above
        monkeypatch.setattr(
            "playwright.sync_api.sync_playwright",
            lambda: type("MS", (), {
                "__enter__": lambda self: type("MP", (), {"chromium": type("MB", (), {
                    "launch": lambda self: type("MBR", (), {
                        "new_context": lambda self: type("MCR", (), {"new_page": lambda self: MockPageWithRoute()}),
                        "close": lambda self: None,
                    })(),
                })()})(),
                "__exit__": lambda self, *a: None,
            })(),
        )

        from src.agent.visual import capture_screenshots

        capture_screenshots(
            "http://localhost:3000",
            screenshot_dir=tmp_path,
            routes=[{"path": "/", "actions": [], "reason": "home"}],
        )

        assert len(registered_routes) == 0

    def test_empty_mocks_dict_registers_nothing(self, tmp_path, monkeypatch):
        registered_routes = []

        class MockPageWithRoute:
            def route(self, pattern, handler):
                registered_routes.append(pattern)

        monkeypatch.setattr(
            "playwright.sync_api.sync_playwright",
            lambda: type("MS", (), {
                "__enter__": lambda self: type("MP", (), {"chromium": type("MB", (), {
                    "launch": lambda self: type("MBR", (), {
                        "new_context": lambda self: type("MCR", (), {"new_page": lambda self: MockPageWithRoute()}),
                        "close": lambda self: None,
                    })(),
                })()})(),
                "__exit__": lambda self, *a: None,
            })(),
        )

        from src.agent.visual import capture_screenshots

        capture_screenshots(
            "http://localhost:3000",
            screenshot_dir=tmp_path,
            routes=[{"path": "/", "actions": [], "reason": "home"}],
            mocks={},
        )

        assert len(registered_routes) == 0
```

Wait, the mock classes are getting complex. Let me simplify using the existing `mock_playwright` fixture approach. Actually, the problem is I can't easily extend the existing fixture since it returns a `MockPage` without a `route` method. Let me add `route` to the existing `MockPage` class in the fixture, so all existing tests still work.

Actually, the simplest approach: add a `route` method to `MockPage` that does nothing. Then add new tests that inspect whether `route` was called.

Better approach: add a `route` method to `MockPage` that records calls in a module-level list, and inspect that in new tests. But that would require shared state across tests.

Simplest clean approach: modify the existing `MockPage` to have a no-op `route` method, and then in the new tests, use a subclass that records calls. Let me revise:

- [ ] **Simplify — just add `route` as no-op to `MockPage` in the fixture**

Modify the existing `mock_playwright` fixture in `tests/test_agent/test_visual.py`:

Add to `MockPage`:
```python
def route(self, pattern, handler):
    pass
```

Then the new tests:

```python
class TestCaptureScreenshotsWithMocks:
    def test_mocks_are_registered_on_page(self, tmp_path):
        from src.agent.visual import capture_screenshots

        mocks = {
            "api.example.com": {
                "/api/users": {"body": {"users": []}, "status": 200},
                "/api/posts": {"body": {"posts": []}},
            }
        }

        result = capture_screenshots(
            "http://localhost:3000",
            screenshot_dir=tmp_path,
            routes=[{"path": "/", "actions": [], "reason": "home"}],
            mocks=mocks,
        )

        assert len(result) == 4

    def test_no_mocks_when_not_provided(self, tmp_path):
        from src.agent.visual import capture_screenshots

        result = capture_screenshots(
            "http://localhost:3000",
            screenshot_dir=tmp_path,
            routes=[{"path": "/", "actions": [], "reason": "home"}],
        )

        assert len(result) == 4

    def test_empty_mocks_dict_captures_normally(self, tmp_path):
        from src.agent.visual import capture_screenshots

        result = capture_screenshots(
            "http://localhost:3000",
            screenshot_dir=tmp_path,
            routes=[{"path": "/", "actions": [], "reason": "home"}],
            mocks={},
        )

        assert len(result) == 4
```

Actually, these tests validate that the function accepts `mocks` without crashing, but they don't verify that routes are actually registered. To verify that, I need to track whether `route()` was called on the page. Let me use a simpler spy approach.

Actually, the simplest approach that works with the existing fixture structure: use monkeypatch to spy on the route method. But the fixture creates Page instances internally.

Let me think about this differently. The mock page in the fixture doesn't have a `route` method, so if `capture_screenshots` tries to call `page.route()`, it'll crash with `AttributeError`. That's actually what we want for the failing tests — they should fail because `route()` doesn't exist on MockPage.

So:
1. Don't add `route()` to MockPage yet.
2. Write tests that pass `mocks` and expect them to work → they fail because `route()` doesn't exist on MockPage.
3. Add `route()` to MockPage as a no-op (along with the implementation in visual.py).
4. Tests pass.

That's the TDD flow. Let me write it that way.

So the tests are simple — they'll fail because MockPage doesn't have `route()`:

```python
class TestCaptureScreenshotsWithMocks:
    def test_mocks_do_not_crash_capture(self, tmp_path):
        from src.agent.visual import capture_screenshots

        mocks = {
            "api.example.com": {
                "/api/users": {"body": {"users": []}, "status": 200},
            }
        }

        result = capture_screenshots(
            "http://localhost:3000",
            screenshot_dir=tmp_path,
            routes=[{"path": "/", "actions": [], "reason": "home"}],
            mocks=mocks,
        )

        assert len(result) == 4

    def test_no_mocks_when_not_provided(self, tmp_path):
        from src.agent.visual import capture_screenshots

        result = capture_screenshots(
            "http://localhost:3000",
            screenshot_dir=tmp_path,
            routes=[{"path": "/", "actions": [], "reason": "home"}],
        )

        assert len(result) == 4

    def test_empty_mocks(self, tmp_path):
        from src.agent.visual import capture_screenshots

        result = capture_screenshots(
            "http://localhost:3000",
            screenshot_dir=tmp_path,
            routes=[{"path": "/", "actions": [], "reason": "home"}],
            mocks={},
        )

        assert len(result) == 4
```

These will fail with AttributeError because MockPage doesn't have `route()`. 

- [ ] **Run tests to confirm they fail**

Run: `pytest tests/test_agent/test_visual.py::TestCaptureScreenshotsWithMocks -x -v`
Expected: AttributeError (MockPage has no attribute 'route')

- [ ] **Commit**

```bash
git add tests/test_agent/test_visual.py
git commit -m "test(visual): add failing tests for mock data in screenshots"
```

## Task 2.4: Implement mock data in visual.py

**Files:**
- Modify: `src/agent/visual.py`

- [ ] **Add `page.route()` to the existing `MockPage` in test fixture**

Add to `MockPage` in `mock_playwright` fixture (`tests/test_agent/test_visual.py`):

```python
def route(self, pattern, handler):
    pass
```

- [ ] **Modify `capture_screenshots` to accept and register mocks**

In `src/agent/visual.py`, add `mocks` parameter:

```python
def capture_screenshots(
    dev_server_url: str,
    screenshot_dir: Path | None = None,
    routes: list[dict] | None = None,
    mocks: dict | None = None,
) -> list[tuple[Path, str]]:
```

After `page = context.new_page()` and before the route loop, add:

```python
            if mocks:
                for domain, endpoints in mocks.items():
                    for path, mock_data in endpoints.items():
                        pattern = f"**{path}"
                        body = json.dumps(mock_data["body"])
                        status = mock_data.get("status", 200)
                        page.route(
                            pattern,
                            lambda route, b=body, s=status: route.fulfill(
                                status=s,
                                content_type="application/json",
                                body=b,
                            ),
                        )
                logger.info("Registered %d mock endpoint(s)", sum(len(e) for e in mocks.values()))
```

Make sure to import `json` at the top of the file if not already imported.

- [ ] **Run tests to confirm they pass**

Run: `pytest tests/test_agent/test_visual.py -x -v`
Expected: all tests pass

- [ ] **Commit**

```bash
git add src/agent/visual.py tests/test_agent/test_visual.py
git commit -m "feat(visual): register mock API handlers via page.route()"
```

## Task 2.5: Wire mocks through main.py

**Files:**
- Modify: `src/agent/main.py`
- Modify: `tests/test_agent/test_main.py`

- [ ] **Modify `_capture_screenshots` in `main.py` to pass mocks through**

Current function:

```python
def _capture_screenshots(
    diff: str,
    secrets: dict,
) -> tuple[list[Path], list[tuple[str, str]]]:
    from src.agent.routes import build_repo_tree, infer_routes
    from src.agent.visual import capture_screenshots, upload_screenshots

    repo_tree = build_repo_tree()
    routes = infer_routes(diff, repo_tree, secrets["openrouter_api_key"])
    logger.info("Routes to screenshot: %s", [r["path"] for r in routes])

    screenshot_dir = Path(REPO_DIR) / ".renderpr" / "screenshots"
    results = capture_screenshots(_dev_server_url, screenshot_dir=screenshot_dir, routes=routes)
    ...
```

Update to unpack the new tuple return:

```python
def _capture_screenshots(
    diff: str,
    secrets: dict,
) -> tuple[list[Path], list[tuple[str, str]]]:
    from src.agent.routes import build_repo_tree, infer_routes
    from src.agent.visual import capture_screenshots, upload_screenshots

    repo_tree = build_repo_tree()
    routes, mocks = infer_routes(diff, repo_tree, secrets["openrouter_api_key"])
    logger.info("Routes to screenshot: %s", [r["path"] for r in routes])
    if mocks:
        logger.info("Mocks configured for %d domain(s): %s", len(mocks), list(mocks.keys()))

    screenshot_dir = Path(REPO_DIR) / ".renderpr" / "screenshots"
    results = capture_screenshots(
        _dev_server_url,
        screenshot_dir=screenshot_dir,
        routes=routes,
        mocks=mocks,
    )
    ...
```

- [ ] **Update `test_main.py` mock for `infer_routes`**

The `_mock_all_deps` function mocks `_capture_screenshots` entirely, so the inner call to `infer_routes` doesn't apply there.

But the `test_run_posts_review` test uses `_mock_all_deps` which mocks the whole `_capture_screenshots` — no change needed.

Add a new test that verifies mocks pass through:

```python
class TestCaptureScreenshotsWithMocks:
    def test_mocks_passed_to_capture_screenshots(self, monkeypatch):
        from src.agent.visual import capture_screenshots, upload_screenshots

        captured_kwargs = {}
        def fake_infer_routes(diff, tree, key):
            return (
                [{"path": "/", "reason": "test", "actions": []}],
                {"api.example.com": {"/api/users": {"body": {"ok": True}}}},
            )

        def fake_capture(url, screenshot_dir=None, routes=None, mocks=None):
            captured_kwargs["mocks"] = mocks
            return [(Path("/tmp/test.png"), "Desktop - /")]

        monkeypatch.setattr("src.agent.routes.infer_routes", fake_infer_routes)
        monkeypatch.setattr("src.agent.visual.capture_screenshots", fake_capture)
        monkeypatch.setattr("src.agent.visual.upload_screenshots", lambda *a, **kw: [])
        monkeypatch.setattr("src.agent.main._dev_server_url", "http://localhost:3000")
        monkeypatch.setattr("src.agent.main.REPO_DIR", "/tmp")

        from src.agent.main import _capture_screenshots

        _capture_screenshots("diff content", {"openrouter_api_key": "sk-or-fake"})

        assert captured_kwargs.get("mocks") == {
            "api.example.com": {"/api/users": {"body": {"ok": True}}},
        }
```

Wait, `_capture_screenshots` does from src.agent.routes import infer_routes inside the function body. The monkeypatch on `src.agent.routes.infer_routes` should work since it patches at the module level, not import time.

Actually, looking at how monkeypatch works: `monkeypatch.setattr("src.agent.routes.infer_routes", fake_infer_routes)` patches the attribute on the module, so any code that accesses `src.agent.routes.infer_routes` will get the patched version, regardless of when it was imported. This should work.

- [ ] **Run all tests to confirm they pass**

Run: `pytest tests/test_agent/test_visual.py tests/test_agent/test_routes.py tests/test_agent/test_main.py -x -v`
Expected: all tests pass

- [ ] **Run full test suite**

Run: `pytest tests/ -x -q`
Expected: all tests pass (0 errors, 0 failures)

- [ ] **Commit**

```bash
git add src/agent/main.py tests/test_agent/test_main.py
git commit -m "feat(main): wire mock data from route inference through to screenshots"
```

---

## Verification

After both PRs are complete, run:

```bash
# Python
pytest tests/ --cov=src --cov-fail-under=80

# Lint
ruff check src/ tests/
mypy src/
```

Expected: 0 errors, 0 lint warnings, all tests passing, coverage >= 80%.
