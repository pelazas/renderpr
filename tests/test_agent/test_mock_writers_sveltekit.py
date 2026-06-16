import json

from src.agent.mock_server import (
    get_mock_writer,
    restore_runtime_files,
)
from src.agent.mock_writers.sveltekit import SvelteKitMockWriter

WRITER = SvelteKitMockWriter()


def _diff_touching(*rel_paths):
    return "".join(
        f"diff --git a/{rel} b/{rel}\n--- a/{rel}\n+++ b/{rel}\n@@ -1 +1 @@\n-old\n+new\n"
        for rel in rel_paths
    )


def _make_server_file(tmp_path, rel, body="export const GET = () => new Response('[]');"):
    route = tmp_path / rel
    route.parent.mkdir(parents=True, exist_ok=True)
    route.write_text(body)
    return route


# --- registry smoke ---------------------------------------------------------


def test_registered_writer_is_sveltekit():
    assert get_mock_writer("sveltekit").framework == "sveltekit"


# --- changed_api_paths ------------------------------------------------------


def test_changed_api_paths_maps_server_files():
    diff = _diff_touching(
        "src/routes/api/users/+server.ts",
        "src/routes/api/posts/[id]/+server.js",
    )
    assert WRITER.changed_api_paths(diff) == {"/api/users", "/api/posts/[id]"}


def test_changed_api_paths_excludes_auth():
    diff = _diff_touching("src/routes/api/auth/login/+server.ts")
    assert WRITER.changed_api_paths(diff) == set()


def test_changed_api_paths_ignores_non_api_and_non_server_files():
    diff = _diff_touching(
        "src/routes/about/+page.svelte",
        "src/routes/api/users/+page.server.ts",
        "src/lib/util.ts",
    )
    assert WRITER.changed_api_paths(diff) == set()


def test_changed_api_paths_empty_diff():
    assert WRITER.changed_api_paths(None) == set()
    assert WRITER.changed_api_paths("") == set()


# --- write_server_mocks -----------------------------------------------------


def test_write_server_mocks_path_and_content(tmp_path):
    generated = WRITER.write_server_mocks(
        tmp_path,
        {"localhost": {"/api/users": {"body": [{"id": 1}], "status": 201}}},
    )
    route = tmp_path / "src/routes/api/users/+server.ts"
    assert "src/routes/api/users/+server.ts" in generated
    content = route.read_text()
    assert "import { json } from '@sveltejs/kit';" in content
    assert "export const GET = () => json([{\"id\": 1}], { status: 201 });" in content


def test_write_server_mocks_default_status_200(tmp_path):
    WRITER.write_server_mocks(tmp_path, {"l": {"/api/x": {"body": {"a": 1}}}})
    content = (tmp_path / "src/routes/api/x/+server.ts").read_text()
    assert "{ status: 200 }" in content


def test_write_server_mocks_skips_changed_routes(tmp_path):
    diff = _diff_touching("src/routes/api/users/+server.ts")
    generated = WRITER.write_server_mocks(
        tmp_path,
        {"l": {"/api/users": {"body": []}}},
        diff=diff,
    )
    assert generated == []
    assert not (tmp_path / "src/routes/api/users/+server.ts").exists()


def test_write_server_mocks_backs_up_existing(tmp_path):
    existing = _make_server_file(
        tmp_path, "src/routes/api/users/+server.ts", "original handler"
    )
    generated = WRITER.write_server_mocks(
        tmp_path, {"l": {"/api/users": {"body": []}}}
    )
    backup = tmp_path / "src/routes/api/users/+server.ts.renderpr.bak"
    assert backup.exists()
    assert backup.read_text() == "original handler"
    assert "src/routes/api/users/+server.ts.renderpr.bak" in generated
    assert "json([]" in existing.read_text()


def test_write_server_mocks_no_mocks(tmp_path):
    assert WRITER.write_server_mocks(tmp_path, None) == []
    assert WRITER.write_server_mocks(tmp_path, {}) == []


def test_write_server_mocks_rejects_bad_paths(tmp_path):
    generated = WRITER.write_server_mocks(
        tmp_path,
        {"l": {"/api/../etc": {"body": []}, "/notapi/x": {"body": []}}},
    )
    assert generated == []


# --- write_unmocked_fallbacks (blind-stub) ----------------------------------


def test_unmocked_blind_stub_content_and_header(tmp_path):
    route = _make_server_file(tmp_path, "src/routes/api/users/+server.ts")
    generated = WRITER.write_unmocked_fallbacks(tmp_path)
    content = route.read_text()
    assert "import { json } from '@sveltejs/kit';" in content
    assert 'json([], { status: 200, headers: { "x-renderpr-unmocked": "/api/users" } })' in content
    assert "export const GET = __renderprUnmocked;" in content
    assert "export const DELETE = __renderprUnmocked;" in content
    assert "src/routes/api/users/+server.ts" in generated
    assert "src/routes/api/users/+server.ts.renderpr.bak" in generated


def test_unmocked_skips_auth_and_explicit(tmp_path):
    _make_server_file(
        tmp_path, "src/routes/api/auth/login/+server.ts", "export const GET = 1;"
    )
    mocked = _make_server_file(
        tmp_path, "src/routes/api/users/+server.ts", "export const GET = 2;"
    )
    posts = _make_server_file(
        tmp_path, "src/routes/api/posts/+server.ts", "export const GET = 3;"
    )
    WRITER.write_unmocked_fallbacks(
        tmp_path, mocks={"l": {"/api/users": {"body": []}}}
    )
    assert (
        tmp_path / "src/routes/api/auth/login/+server.ts"
    ).read_text() == "export const GET = 1;"
    assert mocked.read_text() == "export const GET = 2;"
    assert "x-renderpr-unmocked" in posts.read_text()


def test_unmocked_no_api_dir(tmp_path):
    assert WRITER.write_unmocked_fallbacks(tmp_path) == []


# --- write_unmocked_fallbacks (wrap changed) --------------------------------


def test_unmocked_wraps_changed_route(tmp_path):
    orig_src = (
        "import { json } from '@sveltejs/kit';\n"
        "export async function GET() { return json([1]); }\n"
        "export const POST = () => json([]);\n"
    )
    route = _make_server_file(tmp_path, "src/routes/api/users/+server.ts", orig_src)
    diff = _diff_touching("src/routes/api/users/+server.ts")
    generated = WRITER.write_unmocked_fallbacks(tmp_path, diff=diff)

    sibling = tmp_path / "src/routes/api/users/_renderpr_orig_server.ts"
    assert sibling.exists()
    assert sibling.read_text() == orig_src

    wrapper = route.read_text()
    assert wrapper.startswith("// @ts-nocheck")
    assert "import * as __orig from './_renderpr_orig_server';" in wrapper
    assert "export * from './_renderpr_orig_server';" in wrapper
    assert "__renderprWrap" in wrapper
    assert '"x-renderpr-route-error": msg' in wrapper
    assert "res.status >= 500" in wrapper
    assert "replace(/[^\\x20-\\x7E]/g" in wrapper
    assert "export const GET = __renderprWrap(__orig.GET);" in wrapper
    assert "export const POST = __renderprWrap(__orig.POST);" in wrapper

    assert "src/routes/api/users/_renderpr_orig_server.ts" in generated
    assert "src/routes/api/users/+server.ts.renderpr.bak" in generated
    assert "src/routes/api/users/+server.ts" in generated


def test_unmocked_changed_no_handlers_blind_stubs(tmp_path):
    route = _make_server_file(
        tmp_path, "src/routes/api/users/+server.ts", "const x = 1; // no exports\n"
    )
    diff = _diff_touching("src/routes/api/users/+server.ts")
    generated = WRITER.write_unmocked_fallbacks(tmp_path, diff=diff)
    content = route.read_text()
    assert "__renderprUnmocked" in content
    assert not (tmp_path / "src/routes/api/users/_renderpr_orig_server.ts").exists()
    assert "src/routes/api/users/+server.ts" in generated


# --- write_banner -----------------------------------------------------------


def test_banner_injects_into_app_html_and_writes_static(tmp_path):
    app = tmp_path / "src/app.html"
    app.parent.mkdir(parents=True)
    app.write_text("<html><head></head><body>%sveltekit.body%</body></html>")
    generated = WRITER.write_banner(tmp_path)
    content = app.read_text()
    assert '<script src="/__renderpr-unmocked.js"></script>' in content
    assert content.index("script src") < content.index("</body>")
    assert (tmp_path / "static/__renderpr-unmocked.js").exists()
    assert "src/app.html" in generated
    assert "static/__renderpr-unmocked.js" in generated


def test_banner_skips_when_no_app_html(tmp_path):
    assert WRITER.write_banner(tmp_path) == []


def test_banner_skips_when_no_body(tmp_path):
    app = tmp_path / "src/app.html"
    app.parent.mkdir(parents=True)
    app.write_text("<html><head></head></html>")
    assert WRITER.write_banner(tmp_path) == []
    assert not (tmp_path / "static/__renderpr-unmocked.js").exists()


def test_banner_backs_up_existing_static(tmp_path):
    app = tmp_path / "src/app.html"
    app.parent.mkdir(parents=True)
    app.write_text("<body></body>")
    static = tmp_path / "static/__renderpr-unmocked.js"
    static.parent.mkdir(parents=True)
    static.write_text("old script")
    generated = WRITER.write_banner(tmp_path)
    assert (tmp_path / "static/__renderpr-unmocked.js.renderpr.bak").read_text() == "old script"
    assert "static/__renderpr-unmocked.js.renderpr.bak" in generated


# --- restore round-trip + idempotency ---------------------------------------


def test_restore_runtime_files_round_trip(tmp_path):
    # existing endpoint that gets stubbed, plus a fresh server mock, plus banner
    existing = _make_server_file(
        tmp_path, "src/routes/api/posts/+server.ts", "ORIGINAL POSTS"
    )
    app = tmp_path / "src/app.html"
    app.parent.mkdir(parents=True, exist_ok=True)
    original_html = "<html><body>%sveltekit.body%</body></html>"
    app.write_text(original_html)

    mocks = {"l": {"/api/users": {"body": [1]}}}
    generated: list[str] = []
    generated += WRITER.write_server_mocks(tmp_path, mocks)
    generated += WRITER.write_unmocked_fallbacks(tmp_path, mocks=mocks)
    generated += WRITER.write_banner(tmp_path)

    # mutations applied
    assert (tmp_path / "src/routes/api/users/+server.ts").exists()
    assert existing.read_text() != "ORIGINAL POSTS"
    assert "script src" in app.read_text()

    restore_runtime_files(tmp_path, generated)

    assert not (tmp_path / "src/routes/api/users/+server.ts").exists()
    assert existing.read_text() == "ORIGINAL POSTS"
    assert app.read_text() == original_html
    assert not (tmp_path / "static/__renderpr-unmocked.js").exists()


def test_idempotency_second_run_stable(tmp_path):
    _make_server_file(tmp_path, "src/routes/api/posts/+server.ts", "ORIGINAL")
    app = tmp_path / "src/app.html"
    app.parent.mkdir(parents=True, exist_ok=True)
    app.write_text("<html><body></body></html>")

    WRITER.write_server_mocks(tmp_path, {"l": {"/api/users": {"body": [1]}}})
    WRITER.write_unmocked_fallbacks(tmp_path)
    first_banner = WRITER.write_banner(tmp_path)
    assert first_banner  # first run does work

    users_after_first = (tmp_path / "src/routes/api/users/+server.ts").read_text()
    posts_after_first = (tmp_path / "src/routes/api/posts/+server.ts").read_text()
    html_after_first = app.read_text()

    WRITER.write_server_mocks(tmp_path, {"l": {"/api/users": {"body": [1]}}})
    WRITER.write_unmocked_fallbacks(tmp_path)
    second_banner = WRITER.write_banner(tmp_path)

    assert second_banner == []
    assert (tmp_path / "src/routes/api/users/+server.ts").read_text() == users_after_first
    assert (tmp_path / "src/routes/api/posts/+server.ts").read_text() == posts_after_first
    assert app.read_text() == html_after_first
    # banner not duplicated
    assert html_after_first.count("__renderpr-unmocked.js") == 1
