import json
import logging
from pathlib import Path

from src.agent.config import FRONTEND_EXTENSIONS, MAX_PACKAGE_SCAN_DEPTH, REPO_DIR
from src.agent.routes import _get_changed_files

logger = logging.getLogger(__name__)


def detect_frontend_changes(diff: str) -> bool:
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
            ext = Path(path).suffix
            if ext in FRONTEND_EXTENSIONS:
                return True
    return False


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
    while True:
        candidate = current / "package.json"
        if candidate.exists():
            try:
                data = json.loads(candidate.read_text())
                if "workspaces" in data and candidate != pkg_path:
                    return str(candidate)
            except (json.JSONDecodeError, OSError):
                pass
        if current == repo_path or current.parent == current:
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
