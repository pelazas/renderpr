"""Remix on-disk preview-servicing writer.

Mirrors :class:`~src.agent.mock_server.NextMockWriter`, but for Remix's flat
resource-route convention: ``app/routes/api.<seg>.<seg>.ts`` where dots map to
URL slashes and a dynamic ``[id]``/``:id`` segment becomes ``$id``. Resource
routes export ``loader``/``action`` (not Next's per-HTTP-method exports), and the
banner host is ``app/root.tsx``. See GitHub issue #47 for the full contract.
"""

import logging
from pathlib import Path

from src.agent.mock_server import (
    MockWriter,
    register_mock_writer,
    write_vite_allowed_hosts,
)

logger = logging.getLogger(__name__)

_ROUTES_DIR = ("app", "routes")
# Sibling-module stem the original handler is moved to when we wrap a changed
# route; the leading underscore keeps the segment non-routable in Remix.
_ORIG_SEGMENT = "_renderpr_orig"


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


register_mock_writer("remix", RemixMockWriter())
