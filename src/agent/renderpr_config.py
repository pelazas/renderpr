"""Loader for the optional repo-level `.renderpr.yml` config file.

The file is a layered override: anything it sets wins over auto-detection, and
anything it omits keeps the zero-config behaviour. It is the delivery vehicle for
env-var declarations and the auth method used by auth-gated apps.

Shape (all keys optional):

    env:
      from: .env.example          # which example file to read required vars from
      vars: [NEXT_PUBLIC_API_URL]  # explicit var names (instead of/atop `from`)
    auth:
      type: nextauth               # one of config.AUTH_TYPES
      user: { email: ..., name: ..., role: ... }
      # provider/strategy-specific extras pass through untouched, e.g.
      # version: v5 | cookieName: ... | storage: cookie | baseUrl: ...
"""
import logging
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from src.agent.config import AUTH_TYPES, RENDERPR_CONFIG_NAMES

logger = logging.getLogger(__name__)


class ConfigError(Exception):
    """Raised when a `.renderpr.yml` exists but is invalid.

    The message is surfaced to the user through the PR progress comment, so it is
    written to be human-readable.
    """


# Returned when no config file is present: zero-config defaults.
_EMPTY: dict[str, Any] = {"env": {}, "auth": None}


def find_config_file(repo_dir: Path | str) -> Path | None:
    repo = Path(repo_dir)
    for name in RENDERPR_CONFIG_NAMES:
        candidate = repo / name
        if candidate.exists():
            return candidate
    return None


def load_config(repo_dir: Path | str) -> dict:
    """Load + validate `.renderpr.yml`, merged over defaults.

    Always returns a dict with keys ``env`` (dict) and ``auth`` (dict | None).
    No file -> defaults. Present but malformed -> :class:`ConfigError`.
    """
    path = find_config_file(repo_dir)
    if path is None:
        return dict(_EMPTY)

    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except (yaml.YAMLError, OSError) as exc:
        raise ConfigError(f"could not read {path.name}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"{path.name}: top-level content must be a mapping (key: value pairs)")

    config = _validate(raw, path.name)
    logger.info("Loaded %s (env vars: %s, auth: %s)", path.name,
                _env_var_count(config["env"]), (config["auth"] or {}).get("type"))
    return config


def _validate(raw: dict, name: str) -> dict:
    env = raw.get("env") or {}
    if not isinstance(env, dict):
        raise ConfigError(f"{name}: 'env' must be a mapping")
    if "from" in env and not isinstance(env["from"], str):
        raise ConfigError(f"{name}: 'env.from' must be a string (a dotenv filename)")
    if "vars" in env:
        if not (isinstance(env["vars"], list) and all(isinstance(v, str) for v in env["vars"])):
            raise ConfigError(f"{name}: 'env.vars' must be a list of variable names")

    auth = raw.get("auth")
    if auth is not None:
        if not isinstance(auth, dict):
            raise ConfigError(f"{name}: 'auth' must be a mapping")
        atype = auth.get("type")
        if atype not in AUTH_TYPES:
            raise ConfigError(
                f"{name}: 'auth.type' is {atype!r}; must be one of {', '.join(AUTH_TYPES)}"
            )
        user = auth.get("user")
        if user is not None and not isinstance(user, dict):
            raise ConfigError(f"{name}: 'auth.user' must be a mapping")

    return {"env": env, "auth": auth}


def _env_var_count(env: dict) -> int:
    return len(env.get("vars", []) or [])
