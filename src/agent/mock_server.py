import json
import logging
import re
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

BACKUP_SUFFIX = ".renderpr.bak"
NEXT_CONFIG_NAMES = (
    "next.config.ts",
    "next.config.mjs",
    "next.config.js",
    "next.config.cjs",
)
VITE_CONFIG_NAMES = (
    "vite.config.ts",
    "vite.config.mts",
    "vite.config.js",
    "vite.config.mjs",
)
# Frameworks whose server process (RSC/SSR) fetches data outside the browser,
# so a browser-level page.route() mock can't intercept it — these need on-disk
# mock route handlers. SPA frameworks fetch client-side and are fully covered by
# the Playwright interception in visual.py.
_SERVER_MOCK_FRAMEWORKS = frozenset({"next"})

# Header stamped on every catch-all fallback response so the in-app banner can
# tell the user which endpoint wasn't mocked.
API_FALLBACK_HEADER = "x-renderpr-unmocked"
_AUTH_API_PREFIX = "api/auth"
_ROUTE_FILE_NAMES = ("route.ts", "route.js", "route.tsx", "route.jsx")
_BANNER_REL = "public/__renderpr-unmocked.js"
_LAYOUT_CANDIDATES = ("src/app/layout.tsx", "src/app/layout.jsx", "src/app/layout.js")

# Vanilla client script: wraps fetch, and when a response carries the fallback
# header, shows a dismissible "this route isn't mocked" notice in the middle of
# the page. Served statically from /public so there's nothing to escape inline.
_BANNER_JS = """(function () {
  if (window.__renderprUnmockedInit) return;
  window.__renderprUnmockedInit = true;
  var seen = {};
  var orig = window.fetch;
  window.fetch = function () {
    return orig.apply(this, arguments).then(function (res) {
      try {
        var p = res.headers.get("x-renderpr-unmocked");
        if (p && !seen[p]) { seen[p] = true; render(); }
      } catch (e) {}
      return res;
    });
  };
  function render() {
    var paths = Object.keys(seen);
    var id = "__renderpr-unmocked-banner";
    var box = document.getElementById(id);
    if (!box) {
      box = document.createElement("div");
      box.id = id;
      box.style.cssText = "position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:2147483647;max-width:420px;width:calc(100vw - 32px);background:#1e1b4b;color:#e0e7ff;font:14px/1.5 system-ui,-apple-system,sans-serif;padding:20px 22px;border-radius:14px;box-shadow:0 12px 40px rgba(0,0,0,.4);border:1px solid #4338ca";
      document.body.appendChild(box);
    }
    var list = paths.map(function (p) {
      return "<code style='background:#312e81;padding:1px 6px;border-radius:5px;color:#c7d2fe'>" + p + "</code>";
    }).join(" ");
    box.innerHTML =
      "<div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:8px'>" +
        "<strong style='color:#fff'>renderpr preview</strong>" +
        "<button onclick=\\"document.getElementById('" + id + "').remove()\\" style='background:none;border:0;color:#a5b4fc;font-size:18px;cursor:pointer;line-height:1'>&times;</button>" +
      "</div>" +
      "<div>The route " + list + " " + (paths.length > 1 ? "aren't" : "isn't") +
      " mocked because your PR didn't change this area. Showing empty data \\u2014 this isn't a bug in your code.</div>";
  }
})();
"""


def _route_file_to_api_path(route_rel: Path) -> str | None:
    parts = route_rel.parts
    if len(parts) < 4 or parts[0] != "src" or parts[1] != "app" or parts[2] != "api":
        return None
    middle = parts[3:-1]  # segments between 'api' and the route.* filename
    return "/api/" + "/".join(middle) if middle else "/api"


def _explicit_mock_paths(mocks: dict | None) -> set[str]:
    paths: set[str] = set()
    if not mocks:
        return paths
    for endpoints in mocks.values():
        if not isinstance(endpoints, dict):
            continue
        for api_path in endpoints:
            if isinstance(api_path, str) and api_path.startswith("/"):
                paths.add(api_path)
    return paths


def _fallback_route_source(api_path: str) -> str:
    return (
        "const __renderprUnmocked = () =>\n"
        f'  Response.json([], {{ status: 200, headers: {{ "{API_FALLBACK_HEADER}": "{api_path}" }} }});\n'
        "export const GET = __renderprUnmocked;\n"
        "export const POST = __renderprUnmocked;\n"
        "export const PUT = __renderprUnmocked;\n"
        "export const PATCH = __renderprUnmocked;\n"
        "export const DELETE = __renderprUnmocked;\n"
    )


def write_unmocked_api_fallbacks(repo_dir: Path | str, framework: str = "next", mocks: dict | None = None) -> list[str]:
    """Replace every unmocked `src/app/api/**/route.*` handler with a benign
    fallback that returns `[]` plus the fallback header, so navigating to a route
    whose data wasn't mocked renders empty instead of crashing. Auth routes and
    explicitly-mocked endpoints are left untouched.
    """
    if framework not in _SERVER_MOCK_FRAMEWORKS:
        return []

    repo_path = Path(repo_dir)
    api_dir = repo_path / "src" / "app" / "api"
    if not api_dir.is_dir():
        return []

    explicit = _explicit_mock_paths(mocks)
    generated: list[str] = []

    for route_file in sorted(api_dir.rglob("route.*")):
        if route_file.name not in _ROUTE_FILE_NAMES:
            continue
        route_rel = route_file.relative_to(repo_path)
        api_path = _route_file_to_api_path(route_rel)
        if api_path is None or api_path.lstrip("/").startswith(_AUTH_API_PREFIX) or api_path in explicit:
            continue

        backup = _backup_file(route_file)
        if backup is not None:
            generated.append(str(backup.relative_to(repo_path)))
        route_file.write_text(_fallback_route_source(api_path))
        generated.append(str(route_rel))
        logger.info("Unmocked API fallback written for %s", api_path)

    return generated


def write_unmocked_banner(repo_dir: Path | str, framework: str = "next") -> list[str]:
    """Inject a small client script that shows an in-page notice when a route's
    data came from an unmocked-fallback response. Best-effort: skips cleanly if
    there's no recognizable root layout `<body>` to attach it to."""
    if framework not in _SERVER_MOCK_FRAMEWORKS:
        return []

    repo_path = Path(repo_dir)
    layout = next((repo_path / name for name in _LAYOUT_CANDIDATES if (repo_path / name).exists()), None)
    if layout is None:
        logger.info("No root layout found; skipping unmocked banner")
        return []

    content = layout.read_text()
    if _BANNER_REL.split("/")[-1] in content:
        return []
    match = re.search(r"<body[^>]*>", content)
    if not match:
        logger.info("Layout <body> not found; skipping unmocked banner")
        return []

    generated: list[str] = []
    banner = repo_path / _BANNER_REL
    banner.parent.mkdir(parents=True, exist_ok=True)
    banner_backup = _backup_file(banner)
    if banner_backup is not None:
        generated.append(str(banner_backup.relative_to(repo_path)))
    banner.write_text(_BANNER_JS)
    generated.append(_BANNER_REL)

    layout_backup = _backup_file(layout)
    if layout_backup is not None:
        generated.append(str(layout_backup.relative_to(repo_path)))
    insert_at = match.end()
    tag = '\n        <script src="/__renderpr-unmocked.js"></script>'
    layout.write_text(content[:insert_at] + tag + content[insert_at:])
    generated.append(str(layout.relative_to(repo_path)))
    logger.info("Unmocked banner injected into %s", layout.relative_to(repo_path))
    return generated


def _backup_file(path: Path) -> Path | None:
    if not path.exists():
        return None
    backup = path.with_name(f"{path.name}{BACKUP_SUFFIX}")
    if not backup.exists():
        shutil.copy2(path, backup)
    return backup


def _mock_route_path(api_path: str) -> Path | None:
    parts = [part for part in api_path.strip("/").split("/") if part]
    if not parts or parts[0] != "api" or any(part in {".", ".."} for part in parts):
        return None
    return Path("src/app") / Path(*parts) / "route.ts"


def write_server_mocks(repo_dir: Path | str, mocks: dict | None, framework: str = "next") -> list[str]:
    if not mocks:
        return []
    if framework not in _SERVER_MOCK_FRAMEWORKS:
        # SPA/other frameworks fetch client-side; visual.py's page.route()
        # interception already covers them, so don't write Next route handlers.
        logger.info("Skipping server mock files for framework %s (browser-layer mocks cover it)", framework)
        return []

    repo_path = Path(repo_dir)
    generated: list[str] = []

    for endpoints in mocks.values():
        if not isinstance(endpoints, dict):
            continue
        for api_path, mock_data in endpoints.items():
            if not isinstance(api_path, str) or not isinstance(mock_data, dict) or "body" not in mock_data:
                continue
            route_rel = _mock_route_path(api_path)
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
                "export async function GET() {\n"
                f"  return Response.json({body}, {{ status: {status} }});\n"
                "}\n"
            )
            generated.append(str(route_rel))
            logger.info("Server mock route written: %s for %s", route_rel, api_path)

    return generated


def _find_config(repo_path: Path, names: tuple[str, ...]) -> Path | None:
    for name in names:
        path = repo_path / name
        if path.exists():
            return path
    return None


def _find_next_config(repo_path: Path) -> Path | None:
    return _find_config(repo_path, NEXT_CONFIG_NAMES)


def write_next_allowed_origin(repo_dir: Path | str, public_ip: str) -> list[str]:
    if not public_ip or public_ip == "localhost":
        return []

    repo_path = Path(repo_dir)
    generated: list[str] = []
    existing = _find_next_config(repo_path)

    if existing is None:
        config = repo_path / "next.config.mjs"
        config.write_text(
            "const nextConfig = {\n"
            f"  allowedDevOrigins: ['{public_ip}'],\n"
            "};\n\n"
            "export default nextConfig;\n"
        )
        generated.append(str(config.relative_to(repo_path)))
        logger.info("Temporary Next config written with allowedDevOrigins for %s", public_ip)
        return generated

    content = existing.read_text()
    if f"'{public_ip}'" in content or f'"{public_ip}"' in content:
        return []

    backup = _backup_file(existing)
    if backup is not None:
        generated.append(str(backup.relative_to(repo_path)))

    assignment = f"nextConfig.allowedDevOrigins = ['{public_ip}'];\n"
    if "export default nextConfig" in content:
        content = content.replace("export default nextConfig", f"{assignment}\nexport default nextConfig", 1)
    else:
        content = f"{content.rstrip()}\n\n{assignment}"

    existing.write_text(content)
    generated.append(str(existing.relative_to(repo_path)))
    logger.info("Temporary Next config patched with allowedDevOrigins for %s", public_ip)
    return generated


def write_vite_allowed_hosts(repo_dir: Path | str, public_ip: str) -> list[str]:
    """Best-effort: let Vite serve the public IP by injecting server.allowedHosts.

    Only patches an existing `defineConfig({ ... })` config; if the shape isn't
    recognized we skip rather than risk corrupting the file. Screenshots use
    localhost regardless, so a miss only affects the external preview link.
    """
    if not public_ip or public_ip == "localhost":
        return []

    repo_path = Path(repo_dir)
    existing = _find_config(repo_path, VITE_CONFIG_NAMES)
    if existing is None:
        logger.info("No vite config found; skipping allowedHosts (external preview may be blocked)")
        return []

    content = existing.read_text()
    if "allowedHosts" in content:
        return []

    match = re.search(r"defineConfig\s*\(\s*\{", content)
    if not match:
        logger.info("Vite config shape not recognized; skipping allowedHosts")
        return []

    backup = _backup_file(existing)
    generated: list[str] = []
    if backup is not None:
        generated.append(str(backup.relative_to(repo_path)))

    insert_at = match.end()
    injection = f"\n  server: {{ allowedHosts: ['{public_ip}'] }},"
    content = content[:insert_at] + injection + content[insert_at:]
    existing.write_text(content)
    generated.append(str(existing.relative_to(repo_path)))
    logger.info("Vite config patched with server.allowedHosts for %s", public_ip)
    return generated


def write_dev_origin_allowlist(repo_dir: Path | str, public_ip: str, framework: str) -> list[str]:
    """Allow the Fargate task's public IP as a dev-server origin, per framework.

    Next uses allowedDevOrigins; Vite uses server.allowedHosts (best-effort); CRA
    already handles it via DANGEROUSLY_DISABLE_HOST_CHECK; other frameworks need
    nothing or aren't supported, so they degrade to a no-op.
    """
    if framework == "next":
        return write_next_allowed_origin(repo_dir, public_ip)
    if framework == "vite":
        return write_vite_allowed_hosts(repo_dir, public_ip)
    logger.info("No dev-origin allowlist needed for framework %s", framework)
    return []


def restore_runtime_files(repo_dir: Path | str, generated_files: list[str]) -> None:
    repo_path = Path(repo_dir)
    generated = list(dict.fromkeys(generated_files))
    restored: set[str] = set()

    for rel in generated:
        if not rel.endswith(BACKUP_SUFFIX):
            continue
        backup = repo_path / rel
        original_rel = rel[: -len(BACKUP_SUFFIX)]
        original = repo_path / original_rel
        if not backup.exists():
            continue
        try:
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, original)
            backup.unlink()
            restored.add(original_rel)
            logger.info("Restored runtime file from backup: %s", original_rel)
        except OSError:
            logger.warning("Failed to restore runtime backup %s", rel, exc_info=True)

    for rel in generated:
        if rel.endswith(BACKUP_SUFFIX) or rel in restored:
            continue
        path = repo_path / rel
        if not path.exists():
            continue
        try:
            path.unlink()
            logger.info("Deleted runtime-generated file: %s", rel)
        except OSError:
            logger.warning("Failed to delete runtime file %s", rel, exc_info=True)
