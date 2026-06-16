"""Remix on-disk preview-servicing writer.

Mirrors :class:`~src.agent.mock_server.NextMockWriter`, but for Remix's flat
resource-route convention: ``app/routes/api.<seg>.<seg>.ts`` where dots map to
URL slashes and a dynamic ``[id]``/``:id`` segment becomes ``$id``. Resource
routes export ``loader``/``action`` (not Next's per-HTTP-method exports), and the
banner host is ``app/root.tsx``. See GitHub issue #47 for the full contract.
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

_ROUTES_DIR = ("app", "routes")
# Sibling-module stem the original handler is moved to when we wrap a changed
# route; the leading underscore keeps the segment non-routable in Remix.
_ORIG_SEGMENT = "_renderpr_orig"
_REMIX_HANDLERS = ("loader", "action")
_ROOT_CANDIDATES = ("app/root.tsx", "app/root.jsx")
_BANNER_REL = "public/__renderpr-unmocked.js"
_BANNER_NAME = _BANNER_REL.split("/")[-1]
_SCRIPT_TAG = '\n        <script src="/__renderpr-unmocked.js"></script>'


def _flat_name_for_api_path(api_path: str) -> str | None:
    """Map ``/api/users/[id]`` -> flat-route stem ``api.users.$id``.

    Drops the leading slash, splits on ``/``, rejects ``.``/``..`` segments,
    requires a leading ``api`` segment, and turns dynamic ``[id]``/``:id``
    segments into ``$id``. Returns the dotted stem (no extension) or ``None``.
    """
    parts = [part for part in api_path.strip("/").split("/") if part]
    if not parts or parts[0] != "api":
        return None
    if any(part in {".", ".."} for part in parts):
        return None
    segments: list[str] = []
    for part in parts:
        if part.startswith("[") and part.endswith("]") and len(part) > 2:
            part = "$" + part[1:-1]
        elif part.startswith(":") and len(part) > 1:
            part = "$" + part[1:]
        segments.append(part)
    return ".".join(segments)


def _api_path_for_flat_name(stem: str) -> str:
    """Reverse of :func:`_flat_name_for_api_path` for a flat-route stem.

    ``api.users.$id`` -> ``/api/users/[id]`` (bracket form so the path matches
    the literal fetch-string keys used as mock keys).
    """
    segments: list[str] = []
    for seg in stem.split("."):
        if seg.startswith("$") and len(seg) > 1:
            seg = "[" + seg[1:] + "]"
        segments.append(seg)
    return "/" + "/".join(segments)


def _discover_api_route_files(repo_path: Path) -> list[Path]:
    """Existing Remix resource routes ``app/routes/api.*.{ts,js}``.

    Excludes our own ``._renderpr_orig`` sibling modules so a re-run doesn't
    treat a previously-moved original as a fresh endpoint.
    """
    routes_dir = repo_path / Path(*_ROUTES_DIR)
    if not routes_dir.is_dir():
        return []
    found: list[Path] = []
    for path in sorted(routes_dir.glob("api.*")):
        if path.suffix not in (".ts", ".js") or not path.is_file():
            continue
        if f".{_ORIG_SEGMENT}" in path.stem:
            continue
        found.append(path)
    return found


def _server_mock_source(body: str, status: int) -> str:
    return (
        "import { json } from '@remix-run/node';\n"
        f"export const loader = () => json({body}, {{ status: {status} }});\n"
    )


def _blind_stub_source(api_path: str) -> str:
    return (
        "import { json } from '@remix-run/node';\n"
        "export const loader = () => json([], { status: 200, headers: { "
        f"'{API_FALLBACK_HEADER}': '{api_path}' }} }});\n"
        "export const action = () => json([], { status: 200, headers: { "
        f"'{API_FALLBACK_HEADER}': '{api_path}' }} }});\n"
    )


def _detect_remix_handlers(source: str) -> list[str]:
    """Which of ``loader``/``action`` a resource route exports, best-effort."""
    found: list[str] = []
    for name in _REMIX_HANDLERS:
        patterns = (
            rf"export\s+const\s+{name}\b",
            rf"export\s+async\s+function\s+{name}\b",
            rf"export\s+function\s+{name}\b",
            rf"export\s*\{{[^}}]*\b{name}\b[^}}]*\}}",
        )
        if any(re.search(p, source) for p in patterns):
            found.append(name)
    return found


def _wrapper_source(api_path: str, orig_stem: str, handlers: list[str]) -> str:
    """Wrapper that runs the original loader/action (moved to a sibling module)
    and falls back to empty data only on a thrown exception or a >=500 response."""
    lines = [
        "// @ts-nocheck",
        "import { json } from '@remix-run/node';",
        f"import * as __orig from './{orig_stem}';",
        f"export * from './{orig_stem}';",
        "const __renderprMsg = (e) =>",
        '  String((e && e.message) || e || "error").replace(/[^\\x20-\\x7E]/g, " ").slice(0, 200);',
        "const __renderprFallback = (msg) =>",
        "  json([], {",
        "    status: 200,",
        "    headers: {",
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
    for name in handlers:
        lines.append(f"export const {name} = __renderprWrap(__orig.{name});")
    return "\n".join(lines) + "\n"


class RemixMockWriter(MockWriter):
    """Remix writer: flat resource-route loaders, loader/action fallbacks, and a
    static banner injected into ``app/root.tsx``."""

    framework = "remix"

    def write_dev_origin_allowlist(self, repo_dir, public_ip):
        return write_vite_allowed_hosts(repo_dir, public_ip)

    def changed_api_paths(self, diff: str | None) -> set[str]:
        """API paths whose Remix resource route the PR added or modified.

        Parsed from the diff's ``+++ b/...`` headers; only
        ``app/routes/api.*.{ts,js}`` files count, ``api.auth.*`` is excluded, and
        the dotted stem is reversed back to ``/api/...`` (bracket form for
        dynamic segments to match literal fetch-string mock keys).
        """
        paths: set[str] = set()
        if not diff:
            return paths
        prefix = Path(*_ROUTES_DIR).as_posix() + "/"
        for line in diff.splitlines():
            if not line.startswith("+++ "):
                continue
            target = line[4:].strip()
            if target.startswith("b/"):
                target = target[2:]
            if target == "/dev/null":
                continue
            route = Path(target)
            if route.suffix not in (".ts", ".js"):
                continue
            posix = route.as_posix()
            if not posix.startswith(prefix):
                continue
            stem = route.stem
            if not stem.startswith("api.") and stem != "api":
                continue
            if f".{_ORIG_SEGMENT}" in stem:
                continue
            if stem == "api.auth" or stem.startswith("api.auth."):
                continue
            paths.add(_api_path_for_flat_name(stem))
        return paths

    def write_server_mocks(
        self, repo_dir, mocks: dict | None, diff: str | None = None
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
                    logger.info(
                        "Skipping server mock for changed route %s; real handler runs",
                        api_path,
                    )
                    continue
                stem = _flat_name_for_api_path(api_path)
                if stem is None:
                    continue

                route_rel = Path(*_ROUTES_DIR) / f"{stem}.ts"
                route_path = repo_path / route_rel
                route_path.parent.mkdir(parents=True, exist_ok=True)
                backup = _backup_file(route_path)
                if backup is not None:
                    generated.append(str(backup.relative_to(repo_path)))

                status = int(mock_data.get("status", 200))
                body = json.dumps(mock_data["body"])
                route_path.write_text(_server_mock_source(body, status))
                generated.append(str(route_rel))
                logger.info("Server mock route written: %s for %s", route_rel, api_path)

        return generated

    def write_unmocked_fallbacks(
        self, repo_dir, mocks: dict | None = None, diff: str | None = None
    ) -> list[str]:
        repo_path = Path(repo_dir)
        route_files = _discover_api_route_files(repo_path)
        if not route_files:
            return []

        explicit = _explicit_mock_paths(mocks)
        changed = self.changed_api_paths(diff)
        generated: list[str] = []

        for route_file in route_files:
            api_path = _api_path_for_flat_name(route_file.stem)
            if (
                api_path.lstrip("/").startswith(_AUTH_API_PREFIX)
                or api_path in explicit
            ):
                continue

            if api_path in changed:
                generated.extend(self._wrap_changed(repo_path, route_file, api_path))
            else:
                generated.extend(self._blind_stub(repo_path, route_file, api_path))
                logger.info("Unmocked API fallback written for %s", api_path)

        return generated

    def _blind_stub(self, repo_path: Path, route_file: Path, api_path: str) -> list[str]:
        generated: list[str] = []
        backup = _backup_file(route_file)
        if backup is not None:
            generated.append(str(backup.relative_to(repo_path)))
        route_file.write_text(_blind_stub_source(api_path))
        generated.append(str(route_file.relative_to(repo_path)))
        return generated

    def _wrap_changed(self, repo_path: Path, route_file: Path, api_path: str) -> list[str]:
        source = route_file.read_text()
        handlers = _detect_remix_handlers(source)
        if not handlers:
            logger.info(
                "Changed route %s has no detectable loader/action; blind-stubbing",
                api_path,
            )
            return self._blind_stub(repo_path, route_file, api_path)

        orig_stem = f"{route_file.stem}.{_ORIG_SEGMENT}"
        orig_module = route_file.with_name(f"{orig_stem}{route_file.suffix}")
        generated: list[str] = []
        backup = _backup_file(route_file)
        if backup is not None:
            generated.append(str(backup.relative_to(repo_path)))
        orig_module.write_text(source)
        generated.append(str(orig_module.relative_to(repo_path)))
        route_file.write_text(_wrapper_source(api_path, orig_stem, handlers))
        generated.append(str(route_file.relative_to(repo_path)))
        logger.info(
            "Changed route %s wrapped (handlers: %s); real handler runs with an error guard",
            api_path,
            ",".join(handlers),
        )
        return generated

    def write_banner(self, repo_dir) -> list[str]:
        repo_path = Path(repo_dir)
        root = next(
            (repo_path / name for name in _ROOT_CANDIDATES if (repo_path / name).exists()),
            None,
        )
        if root is None:
            logger.info("No app/root found; skipping unmocked banner")
            return []

        content = root.read_text()
        if _BANNER_NAME in content:
            return []

        scripts = re.search(r"<Scripts\b", content)
        if scripts:
            insert_at = scripts.start()
        else:
            body = re.search(r"</body>", content)
            if not body:
                logger.info("root <Scripts>/</body> not found; skipping unmocked banner")
                return []
            insert_at = body.start()

        generated: list[str] = []
        banner = repo_path / _BANNER_REL
        banner.parent.mkdir(parents=True, exist_ok=True)
        banner_backup = _backup_file(banner)
        if banner_backup is not None:
            generated.append(str(banner_backup.relative_to(repo_path)))
        banner.write_text(_BANNER_JS)
        generated.append(_BANNER_REL)

        root_backup = _backup_file(root)
        if root_backup is not None:
            generated.append(str(root_backup.relative_to(repo_path)))
        root.write_text(content[:insert_at] + _SCRIPT_TAG + content[insert_at:])
        generated.append(str(root.relative_to(repo_path)))
        logger.info("Unmocked banner injected into %s", root.relative_to(repo_path))
        return generated


register_mock_writer("remix", RemixMockWriter())
