"""Inject user-provided env vars/secrets so real-config apps render.

Reads `.env.example` (and/or `.renderpr.yml`'s `env` block) to learn which vars
an app expects, then materialises the user's stored secret values two ways so
every framework sees them:

  * an ephemeral ``.env.local`` written into the frontend dir (build-time
    ``NEXT_PUBLIC_*`` / ``VITE_*`` inlining, and frameworks that only read
    dotenv files), and
  * a process-env dict merged into the dev server's environment.

The ``.env.local`` is tracked as runtime-generated so it is excluded from
``@renderpr apply`` commits and cleaned up afterwards.
"""
import logging
import re
from pathlib import Path

from src.agent.config import ENV_EXAMPLE_NAMES, ENV_LOCAL_FILENAME

logger = logging.getLogger(__name__)

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Per-framework prefix a var must carry to be exposed to client-side code.
# ``None`` means the framework has no client-exposure prefix convention (the
# value reaches the app purely via process env / explicit server access).
FRAMEWORK_PUBLIC_PREFIXES: dict[str, str | None] = {
    "next": "NEXT_PUBLIC_",
    "vite": "VITE_",
    "sveltekit": "PUBLIC_",
    "astro": "PUBLIC_",
    "cra": "REACT_APP_",
    "remix": None,
    "spa": None,
}


def find_env_example(frontend_dir: Path | str, override: str | None = None) -> Path | None:
    directory = Path(frontend_dir)
    names = (override,) if override else ENV_EXAMPLE_NAMES
    for name in names:
        if not name:
            continue
        candidate = directory / name
        if candidate.exists():
            return candidate
    return None


def parse_env_keys(env_example_path: Path | str) -> list[str]:
    """Return the variable names declared in a dotenv-style example file."""
    keys: list[str] = []
    try:
        text = Path(env_example_path).read_text()
    except OSError:
        return keys
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if _KEY_RE.match(key) and key not in keys:
            keys.append(key)
    return keys


def required_env_keys(frontend_dir: Path | str, config_env: dict) -> list[str]:
    """Union of vars declared in the example file and `.renderpr.yml`'s `env.vars`."""
    example = find_env_example(frontend_dir, config_env.get("from"))
    keys = parse_env_keys(example) if example else []
    for var in config_env.get("vars", []) or []:
        if var not in keys:
            keys.append(var)
    return keys


def build_injected_env(
    frontend_dir: Path | str,
    config_env: dict,
    secrets: dict[str, str],
    framework: str | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Return ``(injected_env, missing_required)``.

    Only secrets the app *declares* it needs — via ``.env.example`` and/or
    ``.renderpr.yml``'s ``env.vars`` — are injected into the app/dev-server env.
    Provider/auth secrets that renderpr loads purely for the auth layer (e.g.
    ``SUPABASE_SERVICE_ROLE_KEY``, ``*_JWT_SECRET``, ``CLERK_SECRET_KEY``) are
    intentionally NOT handed to the app process. ``missing_required`` lists
    declared vars that have no provided value (logged as a warning; surfaced for
    the progress comment).
    """
    required = required_env_keys(frontend_dir, config_env)
    injected = {key: secrets[key] for key in required if key in secrets}
    missing = [k for k in required if k not in secrets]
    if missing:
        logger.warning("Declared env vars without a provided value: %s", missing)
    if injected:
        logger.info("Injecting %d env var(s): %s", len(injected), sorted(injected.keys()))
    if framework is not None and framework in FRAMEWORK_PUBLIC_PREFIXES:
        prefix = FRAMEWORK_PUBLIC_PREFIXES[framework]
        if prefix is None:
            if framework == "remix":
                logger.info(
                    "Remix does not auto-load .env.local; injected vars are still "
                    "provided via the dev server's process env.",
                )
        else:
            for key in injected:
                if not key.startswith(prefix):
                    logger.warning(
                        "Injected env var %r lacks the %r prefix, so it won't be "
                        "exposed to the client for framework %r.",
                        key,
                        prefix,
                        framework,
                    )
    return injected, missing


def _dotenv_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def write_env_local(
    frontend_dir: Path | str,
    injected: dict[str, str],
    repo_dir: Path | str,
) -> str | None:
    """Write an ephemeral ``.env.local`` into ``frontend_dir``.

    Returns its path **relative to ``repo_dir``** (for runtime-file tracking), or
    ``None`` when there is nothing to inject. Values are double-quoted and escaped
    so multi-line secrets (e.g. a Firebase service-account JSON) survive.
    """
    if not injected:
        return None
    frontend = Path(frontend_dir)
    target = frontend / ENV_LOCAL_FILENAME
    body = "".join(f"{key}={_dotenv_quote(value)}\n" for key, value in injected.items())
    target.write_text(body)
    rel = str(target.relative_to(repo_dir))
    logger.info("Wrote ephemeral %s with %d var(s)", rel, len(injected))
    return rel
