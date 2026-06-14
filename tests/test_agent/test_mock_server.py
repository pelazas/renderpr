import json

from src.agent.mock_server import (
    changed_api_paths,
    restore_runtime_files,
    write_dev_origin_allowlist,
    write_next_allowed_origin,
    write_server_mocks,
    write_unmocked_api_fallbacks,
    write_unmocked_banner,
    write_vite_allowed_hosts,
)


def _diff_touching(*rel_paths):
    return "".join(
        f"diff --git a/{rel} b/{rel}\n--- a/{rel}\n+++ b/{rel}\n@@ -1 +1 @@\n-old\n+new\n"
        for rel in rel_paths
    )


def _make_api_route(tmp_path, rel, body="export async function GET() { return Response.json([]); }"):
    route = tmp_path / rel
    route.parent.mkdir(parents=True, exist_ok=True)
    route.write_text(body)
    return route


def test_unmocked_fallback_replaces_route_with_empty_and_header(tmp_path):
    route = _make_api_route(tmp_path, "src/app/api/users/route.ts")
    generated = write_unmocked_api_fallbacks(tmp_path, "next")

    content = route.read_text()
    assert "Response.json([]" in content
    assert 'x-renderpr-unmocked": "/api/users"' in content
    assert "src/app/api/users/route.ts" in generated
    assert "src/app/api/users/route.ts.renderpr.bak" in generated


def test_unmocked_fallback_skips_auth_and_explicit_mocks(tmp_path):
    _make_api_route(tmp_path, "src/app/api/auth/[...nextauth]/route.ts", "export const GET = 1;")
    _make_api_route(tmp_path, "src/app/api/posts/route.ts", "export const GET = 2;")
    mocked = _make_api_route(tmp_path, "src/app/api/users/route.ts", "export const GET = 3;")

    write_unmocked_api_fallbacks(tmp_path, "next", mocks={"local": {"/api/users": {"body": []}}})

    assert (tmp_path / "src/app/api/auth/[...nextauth]/route.ts").read_text() == "export const GET = 1;"
    assert mocked.read_text() == "export const GET = 3;"
    assert "x-renderpr-unmocked" in (tmp_path / "src/app/api/posts/route.ts").read_text()


def test_unmocked_fallback_skipped_for_non_next(tmp_path):
    _make_api_route(tmp_path, "src/app/api/users/route.ts")
    assert write_unmocked_api_fallbacks(tmp_path, "vite") == []


def test_unmocked_banner_injects_script_and_writes_file(tmp_path):
    layout = tmp_path / "src/app/layout.tsx"
    layout.parent.mkdir(parents=True)
    layout.write_text('<html><body className="x">{children}</body></html>')

    generated = write_unmocked_banner(tmp_path, "next")

    assert '<script src="/__renderpr-unmocked.js"></script>' in layout.read_text()
    assert (tmp_path / "public/__renderpr-unmocked.js").exists()
    assert "src/app/layout.tsx" in generated
    assert "public/__renderpr-unmocked.js" in generated


def test_unmocked_banner_skips_without_body(tmp_path):
    layout = tmp_path / "src/app/layout.tsx"
    layout.parent.mkdir(parents=True)
    layout.write_text("export default function L() { return null; }")
    assert write_unmocked_banner(tmp_path, "next") == []


def test_write_server_mocks_skipped_for_non_next_framework(tmp_path):
    generated = write_server_mocks(
        tmp_path,
        {"localhost": {"/api/users": {"body": [{"id": 1}], "status": 200}}},
        framework="vite",
    )
    assert generated == []
    assert not (tmp_path / "src/app/api/users/route.ts").exists()


def test_write_server_mocks_writes_for_next(tmp_path):
    generated = write_server_mocks(
        tmp_path,
        {"localhost": {"/api/users": {"body": [{"id": 1}], "status": 200}}},
        framework="next",
    )
    assert "src/app/api/users/route.ts" in generated


def test_write_vite_allowed_hosts_patches_define_config(tmp_path):
    cfg = tmp_path / "vite.config.ts"
    cfg.write_text("import { defineConfig } from 'vite'\nexport default defineConfig({\n  plugins: [],\n})\n")
    generated = write_vite_allowed_hosts(tmp_path, "54.1.2.3")
    content = cfg.read_text()
    assert "allowedHosts: ['54.1.2.3']" in content
    assert "plugins: []" in content
    assert "vite.config.ts" in generated


def test_write_vite_allowed_hosts_skips_when_no_config(tmp_path):
    assert write_vite_allowed_hosts(tmp_path, "54.1.2.3") == []


def test_write_dev_origin_allowlist_dispatches_by_framework(tmp_path):
    # Non-next/vite frameworks degrade to a no-op.
    assert write_dev_origin_allowlist(tmp_path, "54.1.2.3", "sveltekit") == []
    # Next creates a config.
    generated = write_dev_origin_allowlist(tmp_path, "54.1.2.3", "next")
    assert any("next.config" in g for g in generated)


def test_write_server_mocks_creates_next_api_route(tmp_path):
    generated = write_server_mocks(
        tmp_path,
        {"localhost": {"/api/users": {"body": [{"id": 1, "name": "Alice"}], "status": 200}}},
    )

    route = tmp_path / "src/app/api/users/route.ts"
    assert route.exists()
    assert route.read_text() == (
        "export async function GET() {\n"
        "  return Response.json([{\"id\": 1, \"name\": \"Alice\"}], { status: 200 });\n"
        "}\n"
    )
    assert "src/app/api/users/route.ts" in generated


def test_write_server_mocks_backs_up_existing_route(tmp_path):
    route = tmp_path / "src/app/api/users/route.ts"
    route.parent.mkdir(parents=True)
    route.write_text("export async function GET() { return Response.json([]); }\n")

    generated = write_server_mocks(
        tmp_path,
        {"localhost": {"/api/users": {"body": {"ok": True}, "status": 201}}},
    )

    backup = tmp_path / "src/app/api/users/route.ts.renderpr.bak"
    assert backup.read_text() == "export async function GET() { return Response.json([]); }\n"
    assert "src/app/api/users/route.ts" in generated
    assert "src/app/api/users/route.ts.renderpr.bak" in generated
    assert "{\"ok\": true}" in route.read_text()
    assert "status: 201" in route.read_text()


def test_restore_runtime_files_restores_backup(tmp_path):
    route = tmp_path / "src/app/api/users/route.ts"
    backup = tmp_path / "src/app/api/users/route.ts.renderpr.bak"
    route.parent.mkdir(parents=True)
    route.write_text("mock\n")
    backup.write_text("original\n")

    restore_runtime_files(tmp_path, ["src/app/api/users/route.ts", "src/app/api/users/route.ts.renderpr.bak"])

    assert route.read_text() == "original\n"
    assert not backup.exists()


def test_restore_runtime_files_deletes_created_route(tmp_path):
    route = tmp_path / "src/app/api/users/route.ts"
    route.parent.mkdir(parents=True)
    route.write_text("mock\n")

    restore_runtime_files(tmp_path, ["src/app/api/users/route.ts"])

    assert not route.exists()


def test_write_next_allowed_origin_patches_existing_config(tmp_path):
    config = tmp_path / "next.config.ts"
    config.write_text("const nextConfig = {};\n\nexport default nextConfig;\n")

    generated = write_next_allowed_origin(tmp_path, "54.1.2.3")

    assert "next.config.ts" in generated
    assert "next.config.ts.renderpr.bak" in generated
    assert "nextConfig.allowedDevOrigins = ['54.1.2.3'];" in config.read_text()


def test_write_next_allowed_origin_creates_config_when_missing(tmp_path):
    generated = write_next_allowed_origin(tmp_path, "54.1.2.3")

    config = tmp_path / "next.config.mjs"
    assert config.exists()
    assert "allowedDevOrigins: ['54.1.2.3']" in config.read_text()
    assert "next.config.mjs" in generated


def test_write_server_mocks_ignores_invalid_paths(tmp_path):
    generated = write_server_mocks(
        tmp_path,
        {"localhost": {"http://bad": {"body": {"ok": True}}, "/api/users": {"status": 200}}},
    )

    assert generated == []
    assert not (tmp_path / "src/app/api/users/route.ts").exists()


def test_generated_route_body_is_valid_json(tmp_path):
    write_server_mocks(
        tmp_path,
        {"localhost": {"/api/users": {"body": {"ok": True}, "status": 200}}},
    )

    route = tmp_path / "src/app/api/users/route.ts"
    body = route.read_text().split("Response.json(", 1)[1].split(", { status", 1)[0]
    assert json.loads(body) == {"ok": True}


def test_changed_api_paths_parses_only_route_files():
    diff = _diff_touching(
        "src/app/api/users/route.ts",
        "src/app/api/users/[id]/route.ts",
        "src/app/api/posts/helper.ts",   # not a route file
        "src/app/page.tsx",              # not under api
        "src/app/api/auth/[...nextauth]/route.ts",  # auth excluded
    )
    assert changed_api_paths(diff) == {"/api/users", "/api/users/[id]"}


def test_changed_api_paths_empty_for_no_diff():
    assert changed_api_paths(None) == set()
    assert changed_api_paths("") == set()


def test_changed_route_is_wrapped_not_blind_stubbed(tmp_path):
    route = _make_api_route(
        tmp_path,
        "src/app/api/users/route.ts",
        "export async function GET() { return Response.json([{ id: 1 }]); }",
    )
    diff = _diff_touching("src/app/api/users/route.ts")

    generated = write_unmocked_api_fallbacks(tmp_path, "next", diff=diff)

    wrapper = route.read_text()
    # The wrapper runs the real handler instead of returning a hardcoded [].
    assert "_renderpr_orig_route" in wrapper
    assert "export const GET = __renderprWrap" in wrapper
    assert "x-renderpr-route-error" in wrapper
    assert "const res = await fn" in wrapper
    # A 5xx response (e.g. a DB-backed route that try/catches into a 500 in the
    # database-less preview) degrades like a throw, so the page doesn't crash.
    assert "res.status >= 500" in wrapper
    # The error message goes into an HTTP header, so non-printable/non-ASCII
    # chars (e.g. newlines in a stack-y message) must be stripped or building
    # the fallback response would itself throw.
    assert r"replace(/[^\x20-\x7E]/g, " in wrapper

    orig = tmp_path / "src/app/api/users/_renderpr_orig_route.ts"
    assert "return Response.json([{ id: 1 }])" in orig.read_text()
    # Both the moved-aside module and the backup are tracked for cleanup.
    assert "src/app/api/users/_renderpr_orig_route.ts" in generated
    assert "src/app/api/users/route.ts.renderpr.bak" in generated


def test_unchanged_route_still_blind_stubbed_when_diff_present(tmp_path):
    route = _make_api_route(tmp_path, "src/app/api/posts/route.ts", "export async function GET() {}")
    diff = _diff_touching("src/app/api/users/route.ts")  # touches a different route

    write_unmocked_api_fallbacks(tmp_path, "next", diff=diff)

    assert "Response.json([]" in route.read_text()
    assert "x-renderpr-unmocked" in route.read_text()
    assert "_renderpr_orig_route" not in route.read_text()


def test_changed_route_with_no_detectable_handler_falls_back_to_stub(tmp_path):
    route = _make_api_route(tmp_path, "src/app/api/users/route.ts", "// nothing exported here")
    diff = _diff_touching("src/app/api/users/route.ts")

    write_unmocked_api_fallbacks(tmp_path, "next", diff=diff)

    assert "Response.json([]" in route.read_text()
    assert not (tmp_path / "src/app/api/users/_renderpr_orig_route.ts").exists()


def test_write_server_mocks_skips_changed_route(tmp_path):
    diff = _diff_touching("src/app/api/users/route.ts")
    generated = write_server_mocks(
        tmp_path,
        {"localhost": {"/api/users": {"body": [{"id": 1}], "status": 200}}},
        diff=diff,
    )

    assert generated == []
    assert not (tmp_path / "src/app/api/users/route.ts").exists()
