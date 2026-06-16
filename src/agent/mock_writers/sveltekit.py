"""SvelteKit on-disk preview-servicing writer.

Mirrors :class:`~src.agent.mock_server.NextMockWriter`, but for SvelteKit's
file-based server routes. Endpoints live at ``src/routes/api/<path>/+server.ts``
exporting HTTP-method handlers (``GET``/``POST``/...) that return ``json(...)``
from ``@sveltejs/kit``; SvelteKit's internal ``event.fetch`` resolves
same-origin ``/api/...`` requests to these on-disk handlers.
"""

import logging
from pathlib import Path

from src.agent.mock_server import (
    MockWriter,
    _AUTH_API_PREFIX,
    register_mock_writer,
    write_vite_allowed_hosts,
)

logger = logging.getLogger(__name__)

_SERVER_FILE_NAMES = ("+server.ts", "+server.js")


def _server_file_to_api_path(route_rel: Path) -> str | None:
    """``src/routes/api/users/+server.ts`` -> ``/api/users`` (None if not an api endpoint)."""
    parts = route_rel.parts
    if (
        len(parts) < 4
        or parts[0] != "src"
        or parts[1] != "routes"
        or parts[2] != "api"
    ):
        return None
    middle = parts[3:-1]  # segments between 'api' and the +server.* filename
    return "/api/" + "/".join(middle) if middle else "/api"


def _mock_server_path(api_path: str) -> Path | None:
    """``/api/users`` -> ``src/routes/api/users/+server.ts`` (None if invalid)."""
    parts = [part for part in api_path.strip("/").split("/") if part]
    if not parts or parts[0] != "api" or any(part in {".", ".."} for part in parts):
        return None
    return Path("src/routes") / Path(*parts) / "+server.ts"


class SvelteKitMockWriter(MockWriter):
    """SvelteKit writer carrying the on-disk preview-servicing logic."""

    framework = "sveltekit"

    def changed_api_paths(self, diff: str | None) -> set[str]:
        """API paths whose SvelteKit ``+server`` endpoint the PR added/modified.

        Parsed from the diff's ``+++ b/...`` headers; only files matching
        ``src/routes/api/**/+server.{ts,js}`` count, and auth routes are excluded.
        """
        paths: set[str] = set()
        if not diff:
            return paths
        for line in diff.splitlines():
            if not line.startswith("+++ "):
                continue
            target = line[4:].strip()
            if target.startswith("b/"):
                target = target[2:]
            if target == "/dev/null":
                continue
            route_rel = Path(target)
            if route_rel.name not in _SERVER_FILE_NAMES:
                continue
            api_path = _server_file_to_api_path(route_rel)
            if api_path is None or api_path.lstrip("/").startswith(_AUTH_API_PREFIX):
                continue
            paths.add(api_path)
        return paths

    def write_dev_origin_allowlist(
        self, repo_dir: Path | str, public_ip: str
    ) -> list[str]:
        return write_vite_allowed_hosts(repo_dir, public_ip)


register_mock_writer("sveltekit", SvelteKitMockWriter())
