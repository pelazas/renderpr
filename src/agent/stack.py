"""Stack detection: figure out a repo's package manager and framework, and
derive the commands needed to install dependencies and boot its dev server.

This decouples RenderPR from its original npm + Next.js + port-3000 assumption
(issue #30). Detection is pure and filesystem-driven so it stays easy to test.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from src.agent.config import (
    DEFAULT_PACKAGE_MANAGER,
    FRAMEWORK_DEFAULT_PORTS,
    LOCKFILES,
)

logger = logging.getLogger(__name__)

# Frameworks whose dev server needs a `--host` CLI flag to bind 0.0.0.0.
_HOST_FLAG_FRAMEWORKS = frozenset({"vite", "astro", "sveltekit", "remix"})


@dataclass
class LaunchProfile:
    package_manager: str
    framework: str
    install_command: list[str]
    dev_command: list[str]
    dev_env: dict[str, str]
    default_port: int


def detect_package_manager(install_dir: Path | str) -> tuple[str, bool]:
    """Return (package_manager, has_lockfile) by inspecting lockfiles in install_dir.

    Precedence follows config.LOCKFILES insertion order; a pnpm/yarn/bun lockfile
    wins over package-lock.json when both are present. Falls back to npm with
    has_lockfile=False when no lockfile exists.
    """
    install_path = Path(install_dir)
    for lockfile, pm in LOCKFILES.items():
        if (install_path / lockfile).exists():
            return pm, True
    return DEFAULT_PACKAGE_MANAGER, False


def _read_dependencies(package_json_path: Path | str) -> dict[str, str]:
    try:
        data = json.loads(Path(package_json_path).read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    deps: dict[str, str] = {}
    deps.update(data.get("dependencies") or {})
    deps.update(data.get("devDependencies") or {})
    return deps


def detect_framework(package_json_path: Path | str) -> str:
    """Detect the framework family from a package.json's dependencies.

    Returns one of: next, remix, astro, sveltekit, cra, vite, or spa (fallback).
    """
    deps = _read_dependencies(package_json_path)
    if "next" in deps:
        return "next"
    if any(d.startswith("@remix-run/") for d in deps) or "@react-router/dev" in deps:
        return "remix"
    if "astro" in deps:
        return "astro"
    if "@sveltejs/kit" in deps:
        return "sveltekit"
    if "react-scripts" in deps:
        return "cra"
    if "vite" in deps:
        return "vite"
    return "spa"


def _parse_package_manager_field(package_json_path: Path | str) -> tuple[str, int] | None:
    """Parse package.json's ``packageManager`` field into (name, major version).

    Corepack convention: ``"yarn@4.5.0"`` → ("yarn", 4), ``"pnpm@9"`` → ("pnpm", 9).
    A trailing ``+sha512...`` integrity hash is ignored. Returns None when the
    field is absent or doesn't carry a parseable ``name@major`` shape.
    """
    try:
        data = json.loads(Path(package_json_path).read_text())
    except (json.JSONDecodeError, OSError):
        return None
    field = data.get("packageManager")
    if not isinstance(field, str) or "@" not in field:
        return None
    name, _, version = field.partition("@")
    version = version.split("+", 1)[0]
    major_str = version.split(".", 1)[0]
    if not name or not major_str.isdigit():
        return None
    return name, int(major_str)


def detect_yarn_major(
    install_dir: Path | str, package_json_path: Path | str
) -> int | None:
    """Best-effort Yarn major version, or None when it can't be determined.

    The ``packageManager`` field is authoritative; failing that, a ``.yarnrc.yml``
    in install_dir signals Yarn Berry (v2+), so we return 2 as a conservative
    lower bound. Returns None when there's no Yarn signal at all.
    """
    parsed = _parse_package_manager_field(package_json_path)
    if parsed is not None and parsed[0] == "yarn":
        return parsed[1]
    if (Path(install_dir) / ".yarnrc.yml").exists():
        return 2
    return None


def build_install_command(
    package_manager: str, has_lockfile: bool, yarn_major: int | None = None
) -> list[str]:
    """The command that installs dependencies reproducibly for this PM.

    Uses the frozen/CI variant when a lockfile exists, falling back to a plain
    install otherwise. Yarn Berry (v2+) replaced ``--frozen-lockfile`` with
    ``--immutable``; classic Yarn and an unknown version keep the legacy flag.
    """
    if package_manager == "npm":
        return ["npm", "ci"] if has_lockfile else ["npm", "install"]
    if not has_lockfile:
        return [package_manager, "install"]
    if package_manager == "yarn" and yarn_major is not None and yarn_major >= 2:
        return [package_manager, "install", "--immutable"]
    return [package_manager, "install", "--frozen-lockfile"]


def build_dev_command(package_manager: str, framework: str) -> list[str]:
    """The command that starts the dev server, with a `--host` flag for the
    frameworks that need one to bind 0.0.0.0. npm requires `--` to forward args
    to the script; pnpm/yarn/bun forward them directly.
    """
    cmd = [package_manager, "run", "dev"]
    if framework in _HOST_FLAG_FRAMEWORKS:
        if package_manager == "npm":
            cmd.append("--")
        cmd.append("--host")
    return cmd


def build_dev_env(framework: str) -> dict[str, str]:
    """Env overrides that make the dev server reachable, by framework. Vite/Astro/
    SvelteKit use the `--host` CLI flag instead, so they need nothing here.
    """
    if framework == "next":
        return {"HOST": "0.0.0.0"}
    if framework == "cra":
        return {"HOST": "0.0.0.0", "DANGEROUSLY_DISABLE_HOST_CHECK": "true"}
    return {}


def build_launch_profile(
    install_dir: Path | str,
    package_json_path: Path | str,
) -> LaunchProfile:
    """Detect the stack and assemble everything needed to install and boot it.

    install_dir is where the lockfile/node_modules live (workspace root); the
    framework is read from the frontend package.json.
    """
    package_manager, has_lockfile = detect_package_manager(install_dir)
    framework = detect_framework(package_json_path)
    yarn_major = (
        detect_yarn_major(install_dir, package_json_path)
        if package_manager == "yarn"
        else None
    )
    profile = LaunchProfile(
        package_manager=package_manager,
        framework=framework,
        install_command=build_install_command(package_manager, has_lockfile, yarn_major),
        dev_command=build_dev_command(package_manager, framework),
        dev_env=build_dev_env(framework),
        default_port=FRAMEWORK_DEFAULT_PORTS.get(framework, FRAMEWORK_DEFAULT_PORTS["spa"]),
    )
    logger.info(
        "Launch profile: pm=%s framework=%s install=%s dev=%s port=%d",
        package_manager,
        framework,
        " ".join(profile.install_command),
        " ".join(profile.dev_command),
        profile.default_port,
    )
    return profile
