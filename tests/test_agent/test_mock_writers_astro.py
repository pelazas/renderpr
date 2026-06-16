import json

import pytest

from src.agent.mock_server import get_mock_writer, restore_runtime_files

WRITER = get_mock_writer("astro")


def _diff_touching(*rel_paths):
    return "".join(
        f"diff --git a/{rel} b/{rel}\n--- a/{rel}\n+++ b/{rel}\n@@ -1 +1 @@\n-old\n+new\n"
        for rel in rel_paths
    )


def _make_endpoint(tmp_path, rel, body="export const GET = () => new Response('[]');"):
    f = tmp_path / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body)
    return f


# --- writer wiring ----------------------------------------------------------


def test_writer_registered_with_framework_name():
    assert WRITER.framework == "astro"


# --- write_server_mocks -----------------------------------------------------


def test_server_mock_path_and_content(tmp_path):
    mocks = {"local": {"/api/users": {"body": [{"id": 1}], "status": 201}}}
    generated = WRITER.write_server_mocks(tmp_path, mocks)

    endpoint = tmp_path / "src/pages/api/users.ts"
    content = endpoint.read_text()
    assert endpoint.exists()
    assert "export const GET" in content
    assert json.dumps([{"id": 1}]) in content
    assert "status: 201" in content
    assert "'content-type': 'application/json'" in content
    assert "src/pages/api/users.ts" in generated


def test_server_mock_nested_dynamic_segment(tmp_path):
    mocks = {"local": {"/api/users/[id]": {"body": {"id": 5}}}}
    WRITER.write_server_mocks(tmp_path, mocks)

    endpoint = tmp_path / "src/pages/api/users/[id].ts"
    assert endpoint.exists()
    assert "status: 200" in endpoint.read_text()


def test_server_mock_skipped_for_changed(tmp_path):
    mocks = {"local": {"/api/users": {"body": []}}}
    diff = _diff_touching("src/pages/api/users.ts")
    generated = WRITER.write_server_mocks(tmp_path, mocks, diff=diff)

    assert generated == []
    assert not (tmp_path / "src/pages/api/users.ts").exists()


def test_server_mock_backs_up_existing(tmp_path):
    existing = _make_endpoint(tmp_path, "src/pages/api/users.ts", "ORIGINAL")
    generated = WRITER.write_server_mocks(tmp_path, {"l": {"/api/users": {"body": []}}})

    assert "src/pages/api/users.ts.renderpr.bak" in generated
    assert (tmp_path / "src/pages/api/users.ts.renderpr.bak").read_text() == "ORIGINAL"
    assert existing.read_text() != "ORIGINAL"


def test_server_mock_empty_and_invalid(tmp_path):
    assert WRITER.write_server_mocks(tmp_path, None) == []
    assert WRITER.write_server_mocks(tmp_path, {"l": {"/notapi": {"body": []}}}) == []
    assert WRITER.write_server_mocks(tmp_path, {"l": {"/api/..": {"body": []}}}) == []


# --- write_unmocked_fallbacks -----------------------------------------------


def test_blind_stub_content_and_header(tmp_path):
    _make_endpoint(tmp_path, "src/pages/api/posts.ts")
    generated = WRITER.write_unmocked_fallbacks(tmp_path)

    content = (tmp_path / "src/pages/api/posts.ts").read_text()
    assert "JSON.stringify([])" in content
    assert "'x-renderpr-unmocked': '/api/posts'" in content
    assert "export const GET = __renderprUnmocked;" in content
    assert "export const DELETE = __renderprUnmocked;" in content
    assert "src/pages/api/posts.ts" in generated
    assert "src/pages/api/posts.ts.renderpr.bak" in generated


def test_fallback_skips_auth_and_explicit(tmp_path):
    _make_endpoint(tmp_path, "src/pages/api/auth/login.ts", "AUTH")
    mocked = _make_endpoint(tmp_path, "src/pages/api/users.ts", "MOCKED")
    _make_endpoint(tmp_path, "src/pages/api/posts.ts", "export const GET = 1;")

    WRITER.write_unmocked_fallbacks(tmp_path, mocks={"l": {"/api/users": {"body": []}}})

    assert (tmp_path / "src/pages/api/auth/login.ts").read_text() == "AUTH"
    assert mocked.read_text() == "MOCKED"
    assert "x-renderpr-unmocked" in (tmp_path / "src/pages/api/posts.ts").read_text()


def test_fallback_no_api_dir(tmp_path):
    assert WRITER.write_unmocked_fallbacks(tmp_path) == []


def test_fallback_index_collapses(tmp_path):
    _make_endpoint(tmp_path, "src/pages/api/index.ts")
    WRITER.write_unmocked_fallbacks(tmp_path)
    assert "'x-renderpr-unmocked': '/api'" in (tmp_path / "src/pages/api/index.ts").read_text()


def test_changed_endpoint_wrapped(tmp_path):
    src = (
        "export async function GET() { return new Response('[]'); }\n"
        "export const POST = () => new Response('ok');\n"
    )
    _make_endpoint(tmp_path, "src/pages/api/users.ts", src)
    diff = _diff_touching("src/pages/api/users.ts")
    generated = WRITER.write_unmocked_fallbacks(tmp_path, diff=diff)

    orig = tmp_path / "src/pages/api/users._renderpr_orig.ts"
    assert orig.exists()
    assert orig.read_text() == src
    wrapper = (tmp_path / "src/pages/api/users.ts").read_text()
    assert wrapper.startswith("// @ts-nocheck")
    assert 'import * as __orig from "./users._renderpr_orig";' in wrapper
    assert "__renderprWrap" in wrapper
    assert "x-renderpr-route-error" in wrapper
    assert "res.status >= 500" in wrapper
    assert "\\x20-\\x7E" in wrapper  # ASCII strip guard
    assert "export const GET = __renderprWrap" in wrapper
    assert "export const POST = __renderprWrap" in wrapper
    assert "src/pages/api/users._renderpr_orig.ts" in generated


def test_changed_endpoint_no_handler_blind_stubs(tmp_path):
    _make_endpoint(tmp_path, "src/pages/api/users.ts", "const x = 1; // no exports")
    diff = _diff_touching("src/pages/api/users.ts")
    WRITER.write_unmocked_fallbacks(tmp_path, diff=diff)

    content = (tmp_path / "src/pages/api/users.ts").read_text()
    assert "__renderprUnmocked" in content
    assert not (tmp_path / "src/pages/api/users._renderpr_orig.ts").exists()


def test_orig_sibling_not_rediscovered(tmp_path):
    # An existing _renderpr_orig sibling must not be treated as its own endpoint.
    _make_endpoint(tmp_path, "src/pages/api/users._renderpr_orig.ts", "ORIG")
    WRITER.write_unmocked_fallbacks(tmp_path)
    assert (tmp_path / "src/pages/api/users._renderpr_orig.ts").read_text() == "ORIG"


def test_astro_page_files_ignored(tmp_path):
    _make_endpoint(tmp_path, "src/pages/api/data.astro", "<html></html>")
    assert WRITER.write_unmocked_fallbacks(tmp_path) == []
    assert (tmp_path / "src/pages/api/data.astro").read_text() == "<html></html>"


# --- write_banner -----------------------------------------------------------


def test_banner_creates_static_js_and_middleware(tmp_path):
    generated = WRITER.write_banner(tmp_path)

    banner = tmp_path / "public/__renderpr-unmocked.js"
    middleware = tmp_path / "src/middleware.ts"
    assert banner.exists()
    assert "__renderprUnmockedInit" in banner.read_text()
    assert middleware.exists()
    mw = middleware.read_text()
    assert "export const onRequest" in mw
    assert "/__renderpr-unmocked.js" in mw
    assert "public/__renderpr-unmocked.js" in generated
    assert "src/middleware.ts" in generated


def test_banner_skips_existing_middleware(tmp_path):
    existing = tmp_path / "src/middleware.ts"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text("export const onRequest = () => {};")
    generated = WRITER.write_banner(tmp_path)

    assert existing.read_text() == "export const onRequest = () => {};"
    assert "src/middleware.ts" not in generated
    assert "public/__renderpr-unmocked.js" in generated


def test_banner_idempotent(tmp_path):
    first = WRITER.write_banner(tmp_path)
    assert first
    second = WRITER.write_banner(tmp_path)
    assert second == []


# --- changed_api_paths ------------------------------------------------------


def test_changed_api_paths_mapping_and_auth_exclusion():
    diff = _diff_touching(
        "src/pages/api/users.ts",
        "src/pages/api/users/[id].ts",
        "src/pages/api/index.ts",
        "src/pages/api/auth/login.ts",
        "src/pages/api/data.astro",
        "README.md",
    )
    assert WRITER.changed_api_paths(diff) == {"/api/users", "/api/users/[id]", "/api"}


def test_changed_api_paths_empty():
    assert WRITER.changed_api_paths(None) == set()
    assert WRITER.changed_api_paths("") == set()


# --- restore_runtime_files round-trip ---------------------------------------


def test_restore_round_trip(tmp_path):
    existing = _make_endpoint(tmp_path, "src/pages/api/users.ts", "ORIGINAL")
    generated = WRITER.write_server_mocks(tmp_path, {"l": {"/api/users": {"body": []}}})
    generated += WRITER.write_unmocked_fallbacks(tmp_path)
    generated += WRITER.write_banner(tmp_path)

    assert existing.read_text() != "ORIGINAL"
    assert (tmp_path / "public/__renderpr-unmocked.js").exists()

    restore_runtime_files(tmp_path, generated)

    assert existing.read_text() == "ORIGINAL"
    assert not (tmp_path / "src/pages/api/users.ts.renderpr.bak").exists()
    assert not (tmp_path / "public/__renderpr-unmocked.js").exists()
    assert not (tmp_path / "src/middleware.ts").exists()
