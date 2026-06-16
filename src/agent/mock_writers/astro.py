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


def _blind_stub_source(api_path: str) -> str:
    return (
        "const __renderprUnmocked = () =>\n"
        "  new Response(JSON.stringify([]), { status: 200, headers: { "
        "'content-type': 'application/json', "
        f"'{API_FALLBACK_HEADER}': '{api_path}' }} }});\n"
        "export const GET = __renderprUnmocked;\n"
        "export const POST = __renderprUnmocked;\n"
        "export const PUT = __renderprUnmocked;\n"
        "export const PATCH = __renderprUnmocked;\n"
        "export const DELETE = __renderprUnmocked;\n"
    )


def _detect_handler_methods(source: str) -> list[str]:
    """HTTP method handlers exported by an Astro endpoint module, best-effort."""
    found: list[str] = []
    for method in _HTTP_METHODS:
        patterns = (
            rf"export\s+async\s+function\s+{method}\b",
            rf"export\s+function\s+{method}\b",
            rf"export\s+const\s+{method}\b",
            rf"export\s*\{{[^}}]*\b{method}\b[^}}]*\}}",
        )
        if any(re.search(p, source) for p in patterns):
            found.append(method)
    return found


def _wrapper_source(api_path: str, methods: list[str], orig_import: str) -> str:
    lines = [
        "// @ts-nocheck",
        f'import * as __orig from "./{orig_import}";',
        f'export * from "./{orig_import}";',
        "const __renderprMsg = (e) =>",
        '  String((e && e.message) || e || "error").replace(/[^\\x20-\\x7E]/g, " ").slice(0, 200);',
        "const __renderprFallback = (msg) =>",
        "  new Response(JSON.stringify([]), {",
        "    status: 200,",
        "    headers: {",
        "      'content-type': 'application/json',",
        f"      '{API_FALLBACK_HEADER}': '{api_path}',",
        f"      '{API_ERROR_HEADER}': msg,",
        "    },",
        "  });",
        "const __renderprWrap = (fn) =>",
        "  async (...args) => {",
        "    try {",
        "      const res = await fn(...args);",
        '      if (res && typeof res.status === "number" && res.status >= 500) {',
        f'        console.error("[renderpr] handler for {api_path} returned status " + res.status);',
        '        return __renderprFallback("HTTP " + res.status);',
        "      }",
        "      return res;",
        "    } catch (err) {",
        f'      console.error("[renderpr] handler for {api_path} threw:", err);',
        "      return __renderprFallback(__renderprMsg(err));",
        "    }",
        "  };",
    ]
    for method in methods:
        lines.append(f"export const {method} = __renderprWrap((__orig as any).{method});")
    return "\n".join(lines) + "\n"


def _blind_stub_endpoint(repo_path: Path, route_file: Path, route_rel: Path, api_path: str) -> list[str]:
    generated: list[str] = []
    backup = _backup_file(route_file)
    if backup is not None:
        generated.append(str(backup.relative_to(repo_path)))
    route_file.write_text(_blind_stub_source(api_path))
    generated.append(str(route_rel))
    return generated


def _wrap_changed_endpoint(repo_path: Path, route_file: Path, route_rel: Path, api_path: str) -> list[str]:
    """Guard a PR-changed endpoint: move the real handler to a sibling module and
    replace the endpoint with a wrapper that runs it, falling back to empty data
    only on a thrown exception or a 5xx response (logged + error header)."""
    source = route_file.read_text()
    methods = _detect_handler_methods(source)
    if not methods:
        logger.info("Changed endpoint %s has no detectable handlers; blind-stubbing", api_path)
        return _blind_stub_endpoint(repo_path, route_file, route_rel, api_path)

    orig_stem = f"{route_file.stem}{_ORIG_MODULE_SUFFIX}"
    orig_module = route_file.with_name(f"{orig_stem}{route_file.suffix}")
    generated: list[str] = []
    backup = _backup_file(route_file)
    if backup is not None:
        generated.append(str(backup.relative_to(repo_path)))
    orig_module.write_text(source)
    generated.append(str(orig_module.relative_to(repo_path)))
    route_file.write_text(_wrapper_source(api_path, methods, orig_stem))
    generated.append(str(route_rel))
    logger.info("Changed endpoint %s wrapped (methods: %s)", api_path, ",".join(methods))
    return generated


def _write_unmocked_fallbacks(
    repo_dir: Path | str, mocks: dict | None = None, diff: str | None = None
) -> list[str]:
    """Make every unmocked ``src/pages/api/**/*.{ts,js}`` endpoint degrade
    gracefully. Auth + explicitly-mocked endpoints are left untouched; PR-changed
    ones are wrapped (real handler runs with an error guard), the rest blind-stubbed."""
    repo_path = Path(repo_dir)
    api_dir = repo_path / "src" / "pages" / "api"
    if not api_dir.is_dir():
        return []

    explicit = _explicit_mock_paths(mocks)
    changed = _changed_api_paths(diff)
    generated: list[str] = []

    for route_file in sorted(api_dir.rglob("*")):
        if not route_file.is_file() or route_file.suffix not in _API_FILE_SUFFIXES:
            continue
        if route_file.stem.endswith(_ORIG_MODULE_SUFFIX):
            # Sibling holding a wrapped endpoint's original handler; not routable.
            continue
        route_rel = route_file.relative_to(repo_path)
        api_path = _api_file_to_api_path(route_rel)
        if api_path is None or api_path.lstrip("/").startswith(_AUTH_API_PREFIX) or api_path in explicit:
            continue

        if api_path in changed:
            generated.extend(_wrap_changed_endpoint(repo_path, route_file, route_rel, api_path))
        else:
            generated.extend(_blind_stub_endpoint(repo_path, route_file, route_rel, api_path))
            logger.info("Unmocked API fallback written for %s", api_path)

    return generated


def _write_banner(repo_dir: Path | str) -> list[str]:
    """Write the static banner script + an Astro middleware that injects it into
    HTML responses. The middleware is **create-when-absent only**: if
    ``src/middleware.ts`` already exists we never merge (corruption risk) — we log
    and skip, still returning the banner JS path."""
    repo_path = Path(repo_dir)
    generated: list[str] = []

    banner = repo_path / _BANNER_REL
    middleware = repo_path / _MIDDLEWARE_REL
    # Idempotent: a previous run already wrote the banner (and either created or
    # deliberately skipped the middleware), so there's nothing left to do.
    if banner.exists() and banner.read_text() == _BANNER_JS:
        return []

    banner.parent.mkdir(parents=True, exist_ok=True)
    banner_backup = _backup_file(banner)
    if banner_backup is not None:
        generated.append(str(banner_backup.relative_to(repo_path)))
    banner.write_text(_BANNER_JS)
    generated.append(_BANNER_REL)

    if middleware.exists():
        logger.info(
            "%s already exists; skipping middleware injection to avoid corruption", _MIDDLEWARE_REL
        )
        return generated

    middleware.parent.mkdir(parents=True, exist_ok=True)
    middleware.write_text(_MIDDLEWARE_SOURCE)
    generated.append(_MIDDLEWARE_REL)
    logger.info("Unmocked banner middleware created at %s", _MIDDLEWARE_REL)
    return generated


class AstroMockWriter(MockWriter):
    """Astro writer carrying the on-disk preview-servicing logic."""

    framework = "astro"

    def write_server_mocks(
        self, repo_dir: Path | str, mocks: dict | None, diff: str | None = None
    ) -> list[str]:
        return _write_server_mocks(repo_dir, mocks, diff=diff)

    def write_unmocked_fallbacks(
        self, repo_dir: Path | str, mocks: dict | None = None, diff: str | None = None
    ) -> list[str]:
        return _write_unmocked_fallbacks(repo_dir, mocks=mocks, diff=diff)

    def write_banner(self, repo_dir: Path | str) -> list[str]:
        return _write_banner(repo_dir)

    def write_dev_origin_allowlist(self, repo_dir, public_ip):
        return write_astro_allowed_hosts(repo_dir, public_ip)

    def changed_api_paths(self, diff: str | None) -> set[str]:
        return _changed_api_paths(diff)


register_mock_writer("astro", AstroMockWriter())
