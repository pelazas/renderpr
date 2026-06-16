"""Per-framework routing strategies (issue #30, Phase 2).

Each strategy knows how its framework maps source files to URL routes, so route
inference works beyond Next.js App Router. Frameworks without file-system
routing (Vite/CRA SPAs) use the base strategy, which yields no deterministic
routes — inference then degrades to the homepage plus LLM-found routes.

This module is a lower layer: it imports nothing from routes.py, so routes.py
can delegate to it without a circular import.
"""

import logging
import re
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

EXCLUDED_DIRS: Final[set[str]] = {
    ".git", "node_modules", ".next", "__pycache__", ".venv",
    "dist", "build", ".cache", ".svelte-kit", ".astro",
}
SOURCE_EXTENSIONS: Final[tuple[str, ...]] = (".ts", ".tsx", ".js", ".jsx")

_JS = r"(?:tsx|jsx|ts|js)"


def _is_dynamic(route: str) -> bool:
    """Routes with params can't be screenshot without a concrete value."""
    return any(token in route for token in ("[", "]", "$", ":"))


def _fs_route_from_inner(inner: str) -> str:
    """Map a routing-dir-relative path (no extension) to a URL, index -> parent."""
    if inner in ("", "index"):
        return "/"
    if inner.endswith("/index"):
        inner = inner[: -len("/index")]
    return "/" + inner


class RoutingStrategy:
    """Base strategy: a single-page app with no file-system routing."""

    name = "spa"

    def file_to_route(self, file_path: str) -> str | None:
        return None

    def is_page_file(self, file_path: str) -> bool:
        return self.file_to_route(file_path) is not None

    def is_layout_file(self, file_path: str) -> tuple[bool, str]:
        return False, ""

    def is_global_file(self, file_path: str) -> bool:
        return False

    def discover_all_routes(self, repo_path: Path) -> list[str]:
        return []

    def prompt_context(self) -> dict:
        return {
            "routing_description": (
                "This is a single-page app (Vite/CRA) with no file-system routing. "
                "Routes are declared in client-side router config (e.g. react-router "
                '`<Route path="/x">`). Identify route paths from the router setup and '
                "the changed components."
            ),
            "route_example_file": "the client-side router config",
            "route_file_examples": "client-side router routes",
        }

    def _walk(self, repo_path: Path, suffixes: tuple[str, ...]):
        for f in repo_path.rglob("*"):
            if any(part in EXCLUDED_DIRS for part in f.relative_to(repo_path).parts):
                continue
            if f.is_file() and f.name.endswith(suffixes):
                yield str(f.relative_to(repo_path))


_NEXT_INTERCEPT_RE = re.compile(r"^\(\.+\).+|^\(\.{3}\).+")
_NEXT_GROUP_RE = re.compile(r"^\(.+\)$")


def _normalize_next_segments(inner: str) -> str | None:
    """Map App Router segments between `app/` and `page`/`layout` to a URL path.

    Drops route-group `(group)` and `@slot` parallel-route segments (they don't
    appear in the URL). Returns None for intercepting routes `(.)`, `(..)`,
    `(...)` (not screenshottable). `inner` is the path between `app` and the
    final file, e.g. "/(marketing)/about" -> "/about".
    """
    raw_segments = [s for s in inner.split("/") if s]
    out: list[str] = []
    for seg in raw_segments:
        if _NEXT_INTERCEPT_RE.match(seg):
            return None
        if _NEXT_GROUP_RE.match(seg):
            continue
        if seg.startswith("@"):
            continue
        out.append(seg)
    return "/" + "/".join(out) if out else "/"


class NextAppRouterStrategy(RoutingStrategy):
    name = "next-app"

    _GLOBAL_FILES = frozenset({
        "src/app/globals.css", "app/globals.css",
        "next.config.js", "next.config.mjs", "next.config.ts",
        "src/middleware.ts", "middleware.ts",
    })

    def file_to_route(self, file_path: str) -> str | None:
        normalized = file_path.replace("\\", "/")
        # route.{ts,tsx,js,jsx} files are API handlers, not screenshottable pages.
        if re.search(rf"(?:^|/)app(?:/[^/]+)*/route\.{_JS}$", normalized):
            return None
        match = re.search(rf"(?:^|/)app((?:/[^/]+)*)/page\.{_JS}$", normalized)
        if match:
            return _normalize_next_segments(match.group(1))
        if re.search(rf"(?:^|/)app/page\.{_JS}$", normalized):
            return "/"
        return None

    def is_page_file(self, file_path: str) -> bool:
        return self.file_to_route(file_path) is not None

    def is_layout_file(self, file_path: str) -> tuple[bool, str]:
        normalized = file_path.replace("\\", "/")
        match = re.search(rf"(?:^|/)app((?:/[^/]+)*)/layout\.{_JS}$", normalized)
        if match:
            normalized_route = _normalize_next_segments(match.group(1))
            if normalized_route is None:
                return False, ""
            return True, normalized_route
        if re.search(rf"(?:^|/)app/layout\.{_JS}$", normalized):
            return True, "/"
        return False, ""

    def is_global_file(self, file_path: str) -> bool:
        return file_path in self._GLOBAL_FILES

    def discover_all_routes(self, repo_path: Path) -> list[str]:
        if not (repo_path / "app").exists() and not (repo_path / "src" / "app").exists():
            return []
        routes: set[str] = set()
        for rel in self._walk(repo_path, (".tsx", ".jsx", ".ts", ".js")):
            if not re.search(rf"(?:^|/)page\.{_JS}$", rel):
                continue
            route = self.file_to_route(rel)
            if route:
                routes.add(route)
        return sorted(routes)

    def prompt_context(self) -> dict:
        return {
            "routing_description": "The project uses file-system based routing (Next.js App Router style).",
            "route_example_file": "app/dashboard/page.tsx",
            "route_file_examples": "page.tsx, layout.tsx",
        }


class NextPagesRouterStrategy(RoutingStrategy):
    name = "next-pages"

    _PAGE_RE = re.compile(rf"(?:^|/)(?:src/)?pages/(.+)\.{_JS}$")

    def file_to_route(self, file_path: str) -> str | None:
        match = self._PAGE_RE.search(file_path.replace("\\", "/"))
        if not match:
            return None
        inner = match.group(1)
        if inner.startswith("api/") or Path(inner).name in ("_app", "_document", "_error"):
            return None
        route = _fs_route_from_inner(inner)
        return None if _is_dynamic(route) else route

    def is_global_file(self, file_path: str) -> bool:
        normalized = file_path.replace("\\", "/")
        if re.search(rf"(?:^|/)(?:src/)?pages/(?:_app|_document)\.{_JS}$", normalized):
            return True
        return normalized.endswith(("next.config.js", "next.config.mjs", "next.config.ts", "globals.css"))

    def discover_all_routes(self, repo_path: Path) -> list[str]:
        routes: set[str] = set()
        for rel in self._walk(repo_path, SOURCE_EXTENSIONS):
            route = self.file_to_route(rel)
            if route:
                routes.add(route)
        return sorted(routes)

    def prompt_context(self) -> dict:
        return {
            "routing_description": "The project uses file-system based routing (Next.js Pages Router): pages/about.tsx -> /about, pages/index.tsx -> /.",
            "route_example_file": "pages/dashboard.tsx",
            "route_file_examples": "files under pages/",
        }


class AstroStrategy(RoutingStrategy):
    name = "astro"

    _PAGE_RE = re.compile(r"(?:^|/)(?:src/)?pages/(.+)\.(?:astro|md|mdx)$")

    def file_to_route(self, file_path: str) -> str | None:
        match = self._PAGE_RE.search(file_path.replace("\\", "/"))
        if not match:
            return None
        route = _fs_route_from_inner(match.group(1))
        return None if _is_dynamic(route) else route

    def is_global_file(self, file_path: str) -> bool:
        return file_path.replace("\\", "/").endswith(("astro.config.mjs", "astro.config.ts", "astro.config.js"))

    def discover_all_routes(self, repo_path: Path) -> list[str]:
        routes: set[str] = set()
        for rel in self._walk(repo_path, (".astro", ".md", ".mdx")):
            route = self.file_to_route(rel)
            if route:
                routes.add(route)
        return sorted(routes)

    def prompt_context(self) -> dict:
        return {
            "routing_description": "The project uses Astro file-system routing under src/pages (.astro/.md/.mdx): src/pages/about.astro -> /about.",
            "route_example_file": "src/pages/about.astro",
            "route_file_examples": "files under src/pages/",
        }


class SvelteKitStrategy(RoutingStrategy):
    name = "sveltekit"

    _PAGE_RE = re.compile(r"(?:^|/)src/routes/(.*)\+page\.svelte$")
    _LAYOUT_RE = re.compile(r"(?:^|/)src/routes/(.*)\+layout\.svelte$")

    def _route_from_match(self, inner: str) -> str:
        segments = [s for s in inner.split("/") if s and not (s.startswith("(") and s.endswith(")"))]
        return "/" + "/".join(segments) if segments else "/"

    def file_to_route(self, file_path: str) -> str | None:
        match = self._PAGE_RE.search(file_path.replace("\\", "/"))
        if not match:
            return None
        route = self._route_from_match(match.group(1))
        return None if _is_dynamic(route) else route

    def is_layout_file(self, file_path: str) -> tuple[bool, str]:
        match = self._LAYOUT_RE.search(file_path.replace("\\", "/"))
        if not match:
            return False, ""
        return True, self._route_from_match(match.group(1))

    def is_global_file(self, file_path: str) -> bool:
        return file_path.replace("\\", "/").endswith(("svelte.config.js", "vite.config.ts", "vite.config.js", "src/app.css"))

    def discover_all_routes(self, repo_path: Path) -> list[str]:
        routes: set[str] = set()
        for rel in self._walk(repo_path, ("+page.svelte",)):
            route = self.file_to_route(rel)
            if route:
                routes.add(route)
        return sorted(routes)

    def prompt_context(self) -> dict:
        return {
            "routing_description": "The project uses SvelteKit file-system routing: src/routes/about/+page.svelte -> /about; route groups like (app) are not part of the URL.",
            "route_example_file": "src/routes/about/+page.svelte",
            "route_file_examples": "+page.svelte, +layout.svelte",
        }


class RemixStrategy(RoutingStrategy):
    name = "remix"

    _ROUTE_RE = re.compile(rf"(?:^|/)app/routes/(.+?)(?:/route)?\.{_JS}$")

    def file_to_route(self, file_path: str) -> str | None:
        match = self._ROUTE_RE.search(file_path.replace("\\", "/"))
        if not match:
            return None
        token = match.group(1)
        if token in ("_index", "_index/index"):
            return "/"
        segments = [s for s in token.split(".") if not s.startswith("_")]
        if any(_is_dynamic(s) for s in segments):
            return None
        return "/" + "/".join(segments) if segments else "/"

    def is_global_file(self, file_path: str) -> bool:
        normalized = file_path.replace("\\", "/")
        return bool(re.search(rf"(?:^|/)app/root\.{_JS}$", normalized)) or normalized.endswith(("vite.config.ts", "vite.config.js"))

    def discover_all_routes(self, repo_path: Path) -> list[str]:
        routes: set[str] = set()
        for rel in self._walk(repo_path, SOURCE_EXTENSIONS):
            if "/app/routes/" not in f"/{rel}" and not rel.startswith("app/routes/"):
                continue
            route = self.file_to_route(rel)
            if route:
                routes.add(route)
        return sorted(routes)

    def prompt_context(self) -> dict:
        return {
            "routing_description": "The project uses Remix routing under app/routes: app/routes/about.tsx -> /about, dotted names like blog.post.tsx -> /blog/post, _index -> /.",
            "route_example_file": "app/routes/about.tsx",
            "route_file_examples": "files under app/routes/",
        }


_STRATEGIES: dict[str, type[RoutingStrategy]] = {
    "next-app": NextAppRouterStrategy,
    "next-pages": NextPagesRouterStrategy,
    "astro": AstroStrategy,
    "sveltekit": SvelteKitStrategy,
    "remix": RemixStrategy,
}


def _resolve_next_variant(repo_path: Path) -> str:
    if (repo_path / "app").exists() or (repo_path / "src" / "app").exists():
        return "next-app"
    if (repo_path / "pages").exists() or (repo_path / "src" / "pages").exists():
        return "next-pages"
    return "next-app"


def get_strategy(framework: str, repo_path: Path | None = None) -> RoutingStrategy:
    """Pick the routing strategy for a detected framework. Next.js is split into
    app- vs pages-router by inspecting the repo; everything without file-system
    routing falls back to the SPA base strategy.
    """
    if framework == "next":
        framework = _resolve_next_variant(repo_path or Path("."))
    strategy_cls = _STRATEGIES.get(framework, RoutingStrategy)
    return strategy_cls()
