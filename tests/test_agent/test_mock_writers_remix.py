"""Tests for the Remix on-disk preview-servicing writer (issue #47, lane 3)."""

import json


from src.agent.mock_server import (
    API_ERROR_HEADER,
    API_FALLBACK_HEADER,
    get_mock_writer,
    restore_runtime_files,
)
from src.agent.mock_writers.remix import RemixMockWriter

WRITER = RemixMockWriter()


def _routes(tmp_path):
    d = tmp_path / "app" / "routes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _diff_touching(path):
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1 +1 @@\n"
        "+x\n"
    )


# --- registry --------------------------------------------------------------


def test_registered_writer_is_remix():
    writer = get_mock_writer("remix")
    assert writer.framework == "remix"
    assert isinstance(writer, RemixMockWriter)


# --- write_server_mocks ----------------------------------------------------


def test_server_mock_writes_loader_resource_route(tmp_path):
    mocks = {"page": {"/api/users": {"body": [{"id": 1}], "status": 200}}}
    generated = WRITER.write_server_mocks(tmp_path, mocks)

    route = tmp_path / "app/routes/api.users.ts"
    assert route.exists()
    assert "app/routes/api.users.ts" in generated
    content = route.read_text()
    assert "import { json } from '@remix-run/node';" in content
    assert "export const loader = () => json([{\"id\": 1}], { status: 200 });" in content


def test_server_mock_dynamic_segment_maps_to_dollar(tmp_path):
    mocks = {"page": {"/api/users/[id]": {"body": {"id": 1}, "status": 200}}}
    WRITER.write_server_mocks(tmp_path, mocks)
    assert (tmp_path / "app/routes/api.users.$id.ts").exists()


def test_server_mock_colon_segment_maps_to_dollar(tmp_path):
    mocks = {"page": {"/api/users/:id": {"body": 1}}}
    WRITER.write_server_mocks(tmp_path, mocks)
    assert (tmp_path / "app/routes/api.users.$id.ts").exists()


def test_server_mock_status_default_200(tmp_path):
    mocks = {"page": {"/api/x": {"body": []}}}
    WRITER.write_server_mocks(tmp_path, mocks)
    assert "status: 200" in (tmp_path / "app/routes/api.x.ts").read_text()


def test_server_mock_skipped_for_changed_route(tmp_path):
    mocks = {"page": {"/api/users": {"body": [1]}}}
    diff = _diff_touching("app/routes/api.users.ts")
    generated = WRITER.write_server_mocks(tmp_path, mocks, diff=diff)
    assert generated == []
    assert not (tmp_path / "app/routes/api.users.ts").exists()


def test_server_mock_backs_up_existing(tmp_path):
    routes = _routes(tmp_path)
    (routes / "api.users.ts").write_text("export const loader = () => 'real';")
    mocks = {"page": {"/api/users": {"body": []}}}
    generated = WRITER.write_server_mocks(tmp_path, mocks)
    assert "app/routes/api.users.ts.renderpr.bak" in generated
    assert (routes / "api.users.ts.renderpr.bak").read_text() == "export const loader = () => 'real';"


def test_server_mock_rejects_non_api_and_dotdot(tmp_path):
    mocks = {"page": {"/foo/bar": {"body": []}, "/api/../x": {"body": []}}}
    assert WRITER.write_server_mocks(tmp_path, mocks) == []


def test_server_mock_empty_mocks(tmp_path):
    assert WRITER.write_server_mocks(tmp_path, None) == []
    assert WRITER.write_server_mocks(tmp_path, {}) == []


def test_server_mock_body_is_valid_json(tmp_path):
    body = [{"id": 1, "name": "a"}, {"id": 2}]
    WRITER.write_server_mocks(tmp_path, {"p": {"/api/items": {"body": body}}})
    content = (tmp_path / "app/routes/api.items.ts").read_text()
    payload = content.split("json(", 1)[1].rsplit(", { status", 1)[0]
    assert json.loads(payload) == body


# --- write_unmocked_fallbacks: blind stub ----------------------------------


def test_blind_stub_loader_and_action_with_header(tmp_path):
    routes = _routes(tmp_path)
    (routes / "api.posts.ts").write_text("export const loader = () => realData();")
    generated = WRITER.write_unmocked_fallbacks(tmp_path)

    content = (routes / "api.posts.ts").read_text()
    assert f"'{API_FALLBACK_HEADER}': '/api/posts'" in content
    assert "export const loader = () => json([], { status: 200" in content
    assert "export const action = () => json([], { status: 200" in content
    assert "app/routes/api.posts.ts" in generated
    assert "app/routes/api.posts.ts.renderpr.bak" in generated


def test_fallback_skips_auth_and_explicit(tmp_path):
    routes = _routes(tmp_path)
    (routes / "api.auth.session.ts").write_text("export const loader = () => x();")
    (routes / "api.explicit.ts").write_text("export const loader = () => x();")
    (routes / "api.open.ts").write_text("export const loader = () => x();")
    mocks = {"page": {"/api/explicit": {"body": []}}}
    WRITER.write_unmocked_fallbacks(tmp_path, mocks=mocks)

    assert "realData" not in (routes / "api.open.ts").read_text()  # stubbed
    assert (routes / "api.auth.session.ts").read_text() == "export const loader = () => x();"
    assert (routes / "api.explicit.ts").read_text() == "export const loader = () => x();"


def test_fallback_ignores_orig_sibling(tmp_path):
    routes = _routes(tmp_path)
    (routes / "api.x._renderpr_orig.ts").write_text("orig")
    assert WRITER.write_unmocked_fallbacks(tmp_path) == []


def test_fallback_no_routes_dir(tmp_path):
    assert WRITER.write_unmocked_fallbacks(tmp_path) == []


# --- write_unmocked_fallbacks: wrap ----------------------------------------


def test_changed_route_is_wrapped(tmp_path):
    routes = _routes(tmp_path)
    src = "export const loader = () => db();\nexport const action = () => save();\n"
    (routes / "api.posts.ts").write_text(src)
    diff = _diff_touching("app/routes/api.posts.ts")
    generated = WRITER.write_unmocked_fallbacks(tmp_path, diff=diff)

    orig = routes / "api.posts._renderpr_orig.ts"
    assert orig.exists() and orig.read_text() == src
    assert "app/routes/api.posts._renderpr_orig.ts" in generated

    wrapper = (routes / "api.posts.ts").read_text()
    assert wrapper.startswith("// @ts-nocheck")
    assert "import * as __orig from './api.posts._renderpr_orig';" in wrapper
    assert "export * from './api.posts._renderpr_orig';" in wrapper
    assert "__renderprWrap" in wrapper
    assert f"'{API_ERROR_HEADER}': msg" in wrapper
    assert "res.status >= 500" in wrapper
    assert "replace(/[^\\x20-\\x7E]/g" in wrapper
    assert ".slice(0, 200)" in wrapper
    assert "export const loader = __renderprWrap(__orig.loader);" in wrapper
    assert "export const action = __renderprWrap(__orig.action);" in wrapper


def test_changed_route_emits_only_detected_handlers(tmp_path):
    routes = _routes(tmp_path)
    (routes / "api.posts.ts").write_text("export const loader = () => db();\n")
    diff = _diff_touching("app/routes/api.posts.ts")
    WRITER.write_unmocked_fallbacks(tmp_path, diff=diff)
    wrapper = (routes / "api.posts.ts").read_text()
    assert "__renderprWrap(__orig.loader)" in wrapper
    assert "action = __renderprWrap" not in wrapper


def test_changed_route_no_handler_falls_back_to_blind_stub(tmp_path):
    routes = _routes(tmp_path)
    (routes / "api.posts.ts").write_text("const helper = 1;\n")
    diff = _diff_touching("app/routes/api.posts.ts")
    WRITER.write_unmocked_fallbacks(tmp_path, diff=diff)
    assert not (routes / "api.posts._renderpr_orig.ts").exists()
    content = (routes / "api.posts.ts").read_text()
    assert f"'{API_FALLBACK_HEADER}': '/api/posts'" in content
    assert "export const action" in content


def test_unchanged_route_blind_stubbed_when_diff_present(tmp_path):
    routes = _routes(tmp_path)
    (routes / "api.other.ts").write_text("export const loader = () => db();\n")
    diff = _diff_touching("app/routes/api.posts.ts")
    WRITER.write_unmocked_fallbacks(tmp_path, diff=diff)
    assert not (routes / "api.other._renderpr_orig.ts").exists()
    assert "__renderprWrap" not in (routes / "api.other.ts").read_text()


# --- write_banner ----------------------------------------------------------


def test_banner_injects_before_scripts(tmp_path):
    root = tmp_path / "app" / "root.tsx"
    root.parent.mkdir(parents=True)
    root.write_text("<body>\n        <Scripts />\n      </body>")
    generated = WRITER.write_banner(tmp_path)

    content = root.read_text()
    assert content.index("__renderpr-unmocked.js") < content.index("<Scripts")
    assert (tmp_path / "public/__renderpr-unmocked.js").exists()
    assert "public/__renderpr-unmocked.js" in generated
    assert "app/root.tsx" in generated


def test_banner_injects_before_body_when_no_scripts(tmp_path):
    root = tmp_path / "app" / "root.tsx"
    root.parent.mkdir(parents=True)
    root.write_text("<body>\n  hello\n</body>")
    WRITER.write_banner(tmp_path)
    content = root.read_text()
    assert "__renderpr-unmocked.js" in content
    assert content.index("__renderpr-unmocked.js") < content.index("</body>")


def test_banner_jsx_fallback_host(tmp_path):
    root = tmp_path / "app" / "root.jsx"
    root.parent.mkdir(parents=True)
    root.write_text("<body><Scripts /></body>")
    generated = WRITER.write_banner(tmp_path)
    assert "app/root.jsx" in generated


def test_banner_skips_when_no_root(tmp_path):
    assert WRITER.write_banner(tmp_path) == []
    assert not (tmp_path / "public/__renderpr-unmocked.js").exists()


def test_banner_skips_when_no_anchor(tmp_path):
    root = tmp_path / "app" / "root.tsx"
    root.parent.mkdir(parents=True)
    root.write_text("export default function App() { return null; }")
    assert WRITER.write_banner(tmp_path) == []
    assert not (tmp_path / "public/__renderpr-unmocked.js").exists()
    assert "__renderpr-unmocked.js" not in root.read_text()


def test_banner_idempotent_returns_empty_on_second_run(tmp_path):
    root = tmp_path / "app" / "root.tsx"
    root.parent.mkdir(parents=True)
    root.write_text("<body><Scripts /></body>")
    WRITER.write_banner(tmp_path)
    assert WRITER.write_banner(tmp_path) == []


# --- changed_api_paths -----------------------------------------------------


def test_changed_api_paths_mapping():
    diff = (
        _diff_touching("app/routes/api.users.ts")
        + _diff_touching("app/routes/api.users.$id.ts")
        + _diff_touching("app/routes/api.auth.session.ts")
        + _diff_touching("app/components/Nav.tsx")
    )
    assert WRITER.changed_api_paths(diff) == {"/api/users", "/api/users/[id]"}


def test_changed_api_paths_excludes_orig_sibling():
    diff = _diff_touching("app/routes/api.users._renderpr_orig.ts")
    assert WRITER.changed_api_paths(diff) == set()


def test_changed_api_paths_empty():
    assert WRITER.changed_api_paths(None) == set()
    assert WRITER.changed_api_paths("") == set()


# --- restore round-trip + idempotency --------------------------------------


def test_restore_runtime_files_round_trip(tmp_path):
    routes = _routes(tmp_path)
    (routes / "api.users.ts").write_text("REAL")
    mocks = {"page": {"/api/users": {"body": [1]}}}
    generated = WRITER.write_server_mocks(tmp_path, mocks)
    assert (routes / "api.users.ts").read_text() != "REAL"

    restore_runtime_files(tmp_path, generated)
    assert (routes / "api.users.ts").read_text() == "REAL"
    assert not (routes / "api.users.ts.renderpr.bak").exists()


def test_restore_deletes_created_route(tmp_path):
    mocks = {"page": {"/api/new": {"body": []}}}
    generated = WRITER.write_server_mocks(tmp_path, mocks)
    assert (tmp_path / "app/routes/api.new.ts").exists()
    restore_runtime_files(tmp_path, generated)
    assert not (tmp_path / "app/routes/api.new.ts").exists()


def test_wrap_restore_round_trip(tmp_path):
    routes = _routes(tmp_path)
    src = "export const loader = () => db();\n"
    (routes / "api.posts.ts").write_text(src)
    diff = _diff_touching("app/routes/api.posts.ts")
    generated = WRITER.write_unmocked_fallbacks(tmp_path, diff=diff)
    restore_runtime_files(tmp_path, generated)
    assert (routes / "api.posts.ts").read_text() == src
    assert not (routes / "api.posts._renderpr_orig.ts").exists()


def test_fallback_idempotent_skips_orig_on_second_run(tmp_path):
    routes = _routes(tmp_path)
    (routes / "api.posts.ts").write_text("export const loader = () => db();\n")
    diff = _diff_touching("app/routes/api.posts.ts")
    WRITER.write_unmocked_fallbacks(tmp_path, diff=diff)
    first_wrapper = (routes / "api.posts.ts").read_text()
    # second run: the already-wrapped route carries our sentinel and is skipped,
    # so the wrapper is stable and the saved original is not clobbered.
    second = WRITER.write_unmocked_fallbacks(tmp_path, diff=diff)
    assert second == []
    assert (routes / "api.posts.ts").read_text() == first_wrapper
    assert (routes / "api.posts._renderpr_orig.ts").read_text() == "export const loader = () => db();\n"


def test_blind_stub_idempotent_skips_on_second_run(tmp_path):
    routes = _routes(tmp_path)
    (routes / "api.posts.ts").write_text("export const loader = () => db();\n")
    WRITER.write_unmocked_fallbacks(tmp_path)
    first = (routes / "api.posts.ts").read_text()
    assert WRITER.write_unmocked_fallbacks(tmp_path) == []
    assert (routes / "api.posts.ts").read_text() == first
