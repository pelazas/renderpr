"""Astro on-disk preview-servicing writer.

Mirrors :class:`NextMockWriter` for Astro projects: server mocks as
``src/pages/api/<path>.ts`` endpoints, unmocked-API fallbacks (blind-stub or
wrap a PR-changed handler), and an in-app banner injected via Astro middleware.
Reuses the shared helpers/constants from :mod:`src.agent.mock_server`.
"""

import json
import logging
import re
from pathlib import Path

from src.agent.mock_server import (
    API_ERROR_HEADER,
    API_FALLBACK_HEADER,
    _AUTH_API_PREFIX,
    _BANNER_JS,
    _backup_file,
    _explicit_mock_paths,
    MockWriter,
    register_mock_writer,
    write_astro_allowed_hosts,
)

logger = logging.getLogger(__name__)

# Static banner script + the Astro middleware that injects it into HTML.
_BANNER_REL = "public/__renderpr-unmocked.js"
_MIDDLEWARE_REL = "src/middleware.ts"
# Sibling module the original handler is moved to when we wrap a changed route.
_ORIG_MODULE_SUFFIX = "._renderpr_orig"
_HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
_API_FILE_SUFFIXES = (".ts", ".js")

_MIDDLEWARE_SOURCE = (
    "import type { MiddlewareHandler } from 'astro';\n"
    "export const onRequest: MiddlewareHandler = async (context, next) => {\n"
    "  const res = await next();\n"
    "  const ct = res.headers.get('content-type') || '';\n"
    "  if (!ct.includes('text/html')) return res;\n"
    "  let html = await res.text();\n"
    "  if (!html.includes('/__renderpr-unmocked.js')) {\n"
    "    html = html.replace('</body>', "
    "'<script src=\"/__renderpr-unmocked.js\"></script></body>');\n"
    "  }\n"
    "  return new Response(html, { status: res.status, headers: res.headers });\n"
    "};\n"
)


def _mock_endpoint_path(api_path: str) -> Path | None:
    """Map a ``/api/...`` path to ``src/pages/api/<path>.ts``.

    All but the last segment become directories; the last becomes ``<seg>.ts``.
    Requires a leading ``api/`` segment and rejects ``.``/``..`` segments.
    """
    parts = [part for part in api_path.strip("/").split("/") if part]
    if not parts or parts[0] != "api" or any(part in {".", ".."} for part in parts):
        return None
    return Path("src/pages") / Path(*parts[:-1]) / f"{parts[-1]}.ts"


def _api_file_to_api_path(rel: Path) -> str | None:
    """Map an existing ``src/pages/api/**/*.{ts,js}`` file to ``/api/...``.

    Strips the ``src/pages`` prefix and the file extension; a trailing
    ``index`` segment collapses to its parent directory.
    """
    parts = rel.parts
    if len(parts) < 3 or parts[0] != "src" or parts[1] != "pages" or parts[2] != "api":
        return None
    if rel.suffix not in _API_FILE_SUFFIXES:
        return None
    middle = list(parts[2:])  # 'api', ..., '<file>'
    middle[-1] = rel.stem  # drop extension on the last segment
    if middle[-1] == "index":
        middle = middle[:-1]
    return "/" + "/".join(middle)


def _changed_api_paths(diff: str | None) -> set[str]:
    """API paths whose Astro endpoint the PR added or modified.

    Parsed from the diff's ``+++ b/...`` headers; only files matching
    ``src/pages/api/**/*.{ts,js}`` count, and auth routes are excluded.
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
        api_path = _api_file_to_api_path(Path(target))
        if api_path is None or api_path.lstrip("/").startswith(_AUTH_API_PREFIX):
            continue
        paths.add(api_path)
    return paths


def _server_mock_source(body: str, status: int) -> str:
    return (
        "export const GET = () =>\n"
        f"  new Response(JSON.stringify({body}), {{ status: {status}, "
        "headers: { 'content-type': 'application/json' } });\n"
    )


def _write_server_mocks(repo_dir: Path | str, mocks: dict | None, diff: str | None = None) -> list[str]:
    if not mocks:
        return []

    repo_path = Path(repo_dir)
    changed = _changed_api_paths(diff)
    generated: list[str] = []

    for endpoints in mocks.values():
        if not isinstance(endpoints, dict):
            continue
        for api_path, mock_data in endpoints.items():
            if not isinstance(api_path, str) or not isinstance(mock_data, dict) or "body" not in mock_data:
                continue
            if api_path in changed:
                # The PR changed this endpoint; let its real (wrapped) handler
                # run rather than masking it with a synthetic mock body.
                logger.info("Skipping server mock for changed route %s; real handler runs", api_path)
                continue
            route_rel = _mock_endpoint_path(api_path)
            if route_rel is None:
                continue

            route_path = repo_path / route_rel
            route_path.parent.mkdir(parents=True, exist_ok=True)
            backup = _backup_file(route_path)
            if backup is not None:
                generated.append(str(backup.relative_to(repo_path)))

            status = int(mock_data.get("status", 200))
            body = json.dumps(mock_data["body"])
            route_path.write_text(_server_mock_source(body, status))
            generated.append(str(route_rel))
            logger.info("Server mock endpoint written: %s for %s", route_rel, api_path)

    return generated


class AstroMockWriter(MockWriter):
    """Astro writer carrying the on-disk preview-servicing logic."""

    framework = "astro"

    def write_server_mocks(
        self, repo_dir: Path | str, mocks: dict | None, diff: str | None = None
    ) -> list[str]:
        return _write_server_mocks(repo_dir, mocks, diff=diff)

    def write_dev_origin_allowlist(self, repo_dir, public_ip):
        return write_astro_allowed_hosts(repo_dir, public_ip)

    def changed_api_paths(self, diff: str | None) -> set[str]:
        return _changed_api_paths(diff)


register_mock_writer("astro", AstroMockWriter())
