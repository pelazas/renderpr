"""SvelteKit on-disk preview-servicing writer.

Mirrors :class:`~src.agent.mock_server.NextMockWriter`, but for SvelteKit's
file-based server routes. Endpoints live at ``src/routes/api/<path>/+server.ts``
exporting HTTP-method handlers (``GET``/``POST``/...) that return ``json(...)``
from ``@sveltejs/kit``; SvelteKit's internal ``event.fetch`` resolves
same-origin ``/api/...`` requests to these on-disk handlers.
"""

import json
import logging
import re
from pathlib import Path

from src.agent.mock_server import (
    API_ERROR_HEADER,
    API_FALLBACK_HEADER,
    MockWriter,
    _AUTH_API_PREFIX,
    _BANNER_JS,
    _backup_file,
    _explicit_mock_paths,
    register_mock_writer,
    write_vite_allowed_hosts,
)

logger = logging.getLogger(__name__)

_SERVER_FILE_NAMES = ("+server.ts", "+server.js")
_HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
# Sibling module the original handler is moved to when we wrap a changed route.
# SvelteKit only treats ``+server.*`` files as routes, so a plain
# ``_renderpr_orig_server.ts`` sibling is import-only and non-routable.
_ORIG_MODULE_STEM = "_renderpr_orig_server"
_APP_HTML_REL = "src/app.html"
_BANNER_STATIC_REL = "static/__renderpr-unmocked.js"


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


def _detect_handler_methods(source: str) -> list[str]:
    """HTTP method handlers exported by a SvelteKit ``+server`` module, best-effort."""
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


def _server_handler_wrapper_source(api_path: str, methods: list[str]) -> str:
    """Source that runs the original handlers (moved to a sibling module) and falls
    back to empty data only when they error — a thrown exception or a 5xx response
    (e.g. a DB-backed route that try/catches into a 500 because the preview has no
    database). A working/changed handler still renders its real output."""
    lines: list[str] = [
        "// @ts-nocheck",
        "import { json } from '@sveltejs/kit';",
        f"import * as __orig from './{_ORIG_MODULE_STEM}';",
        f"export * from './{_ORIG_MODULE_STEM}';",
        "const __renderprMsg = (e) =>",
        '  String((e && e.message) || e || "error").replace(/[^\\x20-\\x7E]/g, " ").slice(0, 200);',
        "const __renderprFallback = (msg) =>",
        "  json([], {",
        "    status: 200,",
        "    headers: {",
        f'      "{API_FALLBACK_HEADER}": "{api_path}",',
        f'      "{API_ERROR_HEADER}": msg,',
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
        lines.append(f"export const {method} = __renderprWrap(__orig.{method});")
    return "\n".join(lines) + "\n"


def _fallback_server_source(api_path: str) -> str:
    return (
        "import { json } from '@sveltejs/kit';\n"
        "const __renderprUnmocked = () =>\n"
        f'  json([], {{ status: 200, headers: {{ "{API_FALLBACK_HEADER}": "{api_path}" }} }});\n'
        "export const GET = __renderprUnmocked;\n"
        "export const POST = __renderprUnmocked;\n"
        "export const PUT = __renderprUnmocked;\n"
        "export const PATCH = __renderprUnmocked;\n"
        "export const DELETE = __renderprUnmocked;\n"
    )


def _blind_stub_route(
    repo_path: Path, route_file: Path, route_rel: Path, api_path: str
) -> list[str]:
    generated: list[str] = []
    backup = _backup_file(route_file)
    if backup is not None:
        generated.append(str(backup.relative_to(repo_path)))
    route_file.write_text(_fallback_server_source(api_path))
    generated.append(str(route_rel))
    return generated


def _wrap_changed_route(
    repo_path: Path, route_file: Path, route_rel: Path, api_path: str
) -> list[str]:
    """Guard a PR-changed endpoint instead of stubbing it: move the real handlers
    to a sibling module and replace the endpoint with a wrapper that runs them,
    falling back to empty data only on a thrown exception or a 5xx response."""
    source = route_file.read_text()
    methods = _detect_handler_methods(source)
    if not methods:
        logger.info(
            "Changed route %s has no detectable handlers; blind-stubbing", api_path
        )
        return _blind_stub_route(repo_path, route_file, route_rel, api_path)

    orig_module = route_file.with_name(f"{_ORIG_MODULE_STEM}{route_file.suffix}")
    generated: list[str] = []
    backup = _backup_file(route_file)
    if backup is not None:
        generated.append(str(backup.relative_to(repo_path)))
    orig_module.write_text(source)
    generated.append(str(orig_module.relative_to(repo_path)))
    route_file.write_text(_server_handler_wrapper_source(api_path, methods))
    generated.append(str(route_rel))
    logger.info(
        "Changed route %s wrapped (methods: %s); real handler runs with an error guard",
        api_path,
        ",".join(methods),
    )
    return generated


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

    def write_server_mocks(
        self, repo_dir: Path | str, mocks: dict | None, diff: str | None = None
    ) -> list[str]:
        if not mocks:
            return []

        repo_path = Path(repo_dir)
        changed = self.changed_api_paths(diff)
        generated: list[str] = []

        for endpoints in mocks.values():
            if not isinstance(endpoints, dict):
                continue
            for api_path, mock_data in endpoints.items():
                if (
                    not isinstance(api_path, str)
                    or not isinstance(mock_data, dict)
                    or "body" not in mock_data
                ):
                    continue
                if api_path in changed:
                    # The PR changed this route; let its real (wrapped) handler run
                    # rather than masking it with a synthetic mock body.
                    logger.info(
                        "Skipping server mock for changed route %s; real handler runs",
                        api_path,
                    )
                    continue
                route_rel = _mock_server_path(api_path)
                if route_rel is None:
                    continue

                route_path = repo_path / route_rel
                route_path.parent.mkdir(parents=True, exist_ok=True)
                backup = _backup_file(route_path)
                if backup is not None:
                    generated.append(str(backup.relative_to(repo_path)))

                status = int(mock_data.get("status", 200))
                body = json.dumps(mock_data["body"])
                route_path.write_text(
                    "import { json } from '@sveltejs/kit';\n"
                    f"export const GET = () => json({body}, {{ status: {status} }});\n"
                )
                generated.append(str(route_rel))
                logger.info(
                    "Server mock route written: %s for %s", route_rel, api_path
                )

        return generated

    def write_unmocked_fallbacks(
        self, repo_dir: Path | str, mocks: dict | None = None, diff: str | None = None
    ) -> list[str]:
        """Make every unmocked ``src/routes/api/**/+server.*`` endpoint degrade
        gracefully. Auth routes and explicitly-mocked endpoints are left untouched.
        PR-changed endpoints are wrapped (real handler runs); the rest are stubbed."""
        repo_path = Path(repo_dir)
        api_dir = repo_path / "src" / "routes" / "api"
        if not api_dir.is_dir():
            return []

        explicit = _explicit_mock_paths(mocks)
        changed = self.changed_api_paths(diff)
        generated: list[str] = []

        candidates = sorted(api_dir.rglob("+server.ts")) + sorted(
            api_dir.rglob("+server.js")
        )
        for route_file in sorted(candidates):
            if route_file.name not in _SERVER_FILE_NAMES:
                continue
            route_rel = route_file.relative_to(repo_path)
            api_path = _server_file_to_api_path(route_rel)
            if (
                api_path is None
                or api_path.lstrip("/").startswith(_AUTH_API_PREFIX)
                or api_path in explicit
            ):
                continue

            if api_path in changed:
                generated.extend(
                    _wrap_changed_route(repo_path, route_file, route_rel, api_path)
                )
            else:
                generated.extend(
                    _blind_stub_route(repo_path, route_file, route_rel, api_path)
                )
                logger.info("Unmocked API fallback written for %s", api_path)

        return generated

    def write_banner(self, repo_dir: Path | str) -> list[str]:
        """Inject the in-app banner script. Host is ``src/app.html``; the script is
        served statically from ``static/__renderpr-unmocked.js``. Best-effort: skips
        if there's no ``app.html`` or no ``</body>`` to attach to."""
        repo_path = Path(repo_dir)
        app_html = repo_path / _APP_HTML_REL
        if not app_html.exists():
            logger.info("No src/app.html found; skipping unmocked banner")
            return []

        content = app_html.read_text()
        if _BANNER_STATIC_REL.split("/")[-1] in content:
            return []
        idx = content.find("</body>")
        if idx == -1:
            logger.info("app.html </body> not found; skipping unmocked banner")
            return []

        generated: list[str] = []
        banner = repo_path / _BANNER_STATIC_REL
        banner.parent.mkdir(parents=True, exist_ok=True)
        banner_backup = _backup_file(banner)
        if banner_backup is not None:
            generated.append(str(banner_backup.relative_to(repo_path)))
        banner.write_text(_BANNER_JS)
        generated.append(_BANNER_STATIC_REL)

        html_backup = _backup_file(app_html)
        if html_backup is not None:
            generated.append(str(html_backup.relative_to(repo_path)))
        tag = '\n    <script src="/__renderpr-unmocked.js"></script>'
        app_html.write_text(content[:idx] + tag + content[idx:])
        generated.append(str(app_html.relative_to(repo_path)))
        logger.info("Unmocked banner injected into %s", _APP_HTML_REL)
        return generated

    def write_dev_origin_allowlist(
        self, repo_dir: Path | str, public_ip: str
    ) -> list[str]:
        return write_vite_allowed_hosts(repo_dir, public_ip)


register_mock_writer("sveltekit", SvelteKitMockWriter())
