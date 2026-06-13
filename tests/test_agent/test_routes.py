import json

import httpx
import pytest



class _MockClient:
    def __init__(self, responses):
        self.responses = responses
        self.call_count = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def post(self, *a, **kw):
        resp = self.responses[self.call_count]
        self.call_count += 1
        return resp


@pytest.fixture(autouse=True)
def mock_httpx_client(monkeypatch):
    client_ref = {"instance": None}

    def make_client(*a, **kw):
        return client_ref["instance"]

    def helper(responses=None):
        if responses is not None:
            client_ref["instance"] = _MockClient(responses)
        return client_ref["instance"]

    monkeypatch.setattr(httpx, "Client", make_client)
    return helper


class TestInferRoutes:
    def test_page_file_yields_its_route_without_llm(self, mock_httpx_client, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.routes.REPO_DIR", str(tmp_path))
        (tmp_path / "app" / "users" / "page.tsx").parent.mkdir(parents=True)
        (tmp_path / "app" / "users" / "page.tsx").write_text("export default function Page() { return null; }")
        mock_httpx_client([])

        from src.agent.routes import infer_routes

        diff = "+++ b/app/users/page.tsx"
        routes, mocks = infer_routes(diff, "tree", "sk-or-fake")
        assert len(routes) == 1
        assert routes[0]["path"] == "/users"
        assert mocks == {}

    def test_deterministic_routes_come_first(self, mock_httpx_client, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.routes.REPO_DIR", str(tmp_path))
        (tmp_path / "app" / "profile" / "page.tsx").parent.mkdir(parents=True)
        (tmp_path / "app" / "profile" / "page.tsx").write_text(
            "export default function Page() { return <button>Open menu</button>; }"
        )
        (tmp_path / "app" / "dashboard" / "page.tsx").parent.mkdir(parents=True)
        (tmp_path / "app" / "dashboard" / "page.tsx").write_text("export default function Page() { return null; }")
        mock_httpx_client([
            httpx.Response(200, json={
                "choices": [{"message": {"content": json.dumps({
                    "routes": [
                        {"path": "/dashboard", "reason": "file changed", "actions": []},
                    ],
                })}}],
            }),
        ])

        from src.agent.routes import infer_routes

        diff = "+++ b/app/profile/page.tsx"
        routes, mocks = infer_routes(diff, "file tree", "sk-or-fake")

        assert len(routes) == 2
        assert routes[0]["path"] == "/profile"
        assert routes[1]["path"] == "/dashboard"

    def test_llm_actions_are_preserved_on_deterministic_route(self, mock_httpx_client, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.routes.REPO_DIR", str(tmp_path))
        (tmp_path / "app" / "profile" / "page.tsx").parent.mkdir(parents=True)
        (tmp_path / "app" / "profile" / "page.tsx").write_text(
            "export default function Page() { return <button>Open menu</button>; }"
        )
        mock_httpx_client([
            httpx.Response(200, json={
                "choices": [{"message": {"content": json.dumps({
                    "routes": [
                        {"path": "/profile", "reason": "click reveals menu", "actions": [
                            {
                                "type": "click",
                                "selector": "text=Open menu",
                                "sourceText": "Open menu",
                                "reason": "button opens dropdown",
                            },
                            {"type": "wait", "ms": 500},
                        ]},
                    ],
                })}}],
            }),
        ])

        from src.agent.routes import infer_routes

        diff = "+++ b/app/profile/page.tsx"
        routes, mocks = infer_routes(diff, "file tree", "sk-or-fake")
        assert len(routes) == 1
        assert routes[0]["path"] == "/profile"
        assert len(routes[0]["actions"]) == 2
        assert mocks == {}

    def test_empty_diff_falls_back(self, mock_httpx_client, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.routes.REPO_DIR", str(tmp_path))
        mock_httpx_client([])

        from src.agent.routes import infer_routes

        routes, mocks = infer_routes("no diff here", "tree", "sk-or-fake")
        assert routes == [{"path": "/", "reason": "fallback", "actions": []}]
        assert mocks == {}

    def test_llm_failure_keeps_deterministic_routes(self, mock_httpx_client, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.routes.REPO_DIR", str(tmp_path))
        (tmp_path / "app" / "users" / "page.tsx").parent.mkdir(parents=True)
        (tmp_path / "app" / "users" / "page.tsx").write_text("export default function Page() { return null; }")
        mock_httpx_client([
            httpx.Response(200, json={
                "choices": [{"message": {"content": "not json at all"}}],
            }),
        ])

        from src.agent.routes import infer_routes

        diff = "+++ b/app/users/page.tsx"
        routes, mocks = infer_routes(diff, "tree", "sk-or-fake")
        assert len(routes) == 1
        assert routes[0]["path"] == "/users"
        assert mocks == {}

    def test_llm_5xx_retry_then_deterministic_survives(self, mock_httpx_client, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.routes.REPO_DIR", str(tmp_path))
        (tmp_path / "app" / "users" / "page.tsx").parent.mkdir(parents=True)
        (tmp_path / "app" / "users" / "page.tsx").write_text("export default function Page() { return null; }")
        mock_httpx_client([
            httpx.Response(500, json={"error": "Server error"}),
            httpx.Response(500, json={"error": "Server error"}),
            httpx.Response(500, json={"error": "Server error"}),
        ])

        from src.agent.routes import infer_routes

        diff = "+++ b/app/users/page.tsx"
        routes, mocks = infer_routes(diff, "tree", "sk-or-fake")
        assert len(routes) == 1
        assert routes[0]["path"] == "/users"
        assert mocks == {}


class TestValidateRoutes:
    def test_valid_routes_pass_through(self):
        from src.agent.routes import _validate_routes

        routes = [
            {"path": "/a", "reason": "r1", "actions": []},
            {"path": "/b", "reason": "r2", "actions": [
                {
                    "type": "click",
                    "selector": "text=Open filters",
                    "sourceText": "Open filters",
                    "reason": "button opens filter dropdown",
                },
                {"type": "wait", "ms": 500},
            ]},
        ]

        file_contents = {
            "app/users/page.tsx": """
                export default function UsersPage() {
                  return <button onClick={() => setOpen(true)}>Open filters</button>;
                }
            """,
        }

        result = _validate_routes(routes, file_contents)
        assert len(result) == 2
        assert len(result[1]["actions"]) == 2

    def test_invalid_paths_are_filtered(self):
        from src.agent.routes import _validate_routes

        routes = [
            {"path": "", "reason": "empty"},
            {"path": "not-a-path", "reason": "no-slash"},
            {"path": 123, "reason": "not-string"},
        ]

        result = _validate_routes(routes)
        assert result == []

    def test_invalid_actions_are_filtered(self):
        from src.agent.routes import _validate_routes

        routes = [
            {"path": "/valid", "actions": [
                {"type": "hover", "selector": "#x"},
                {"type": "click"},
                {"type": "wait"},
            ]},
        ]

        result = _validate_routes(routes)
        assert len(result) == 1
        assert result[0]["actions"] == []

    def test_accepts_click_when_source_text_is_interactive_trigger(self):
        from src.agent.routes import _validate_routes

        routes = [{"path": "/settings", "actions": [{
            "type": "click",
            "selector": "text=Open settings",
            "sourceText": "Open settings",
            "reason": "button opens settings modal",
        }]}]
        file_contents = {
            "app/settings/page.tsx": """
                export default function SettingsPage() {
                  return (
                    <Dialog>
                      <DialogTrigger asChild>
                        <button>Open settings</button>
                      </DialogTrigger>
                      <DialogContent>Changed modal content</DialogContent>
                    </Dialog>
                  );
                }
            """,
        }

        result = _validate_routes(routes, file_contents)

        assert result[0]["actions"] == routes[0]["actions"]

    def test_rejects_click_when_source_text_is_plain_rendered_data(self):
        from src.agent.routes import _validate_routes

        routes = [{"path": "/users", "actions": [{
            "type": "click",
            "selector": "text=Admin",
            "sourceText": "Admin",
            "reason": "users/page.tsx changed",
        }]}]
        file_contents = {
            "app/users/page.tsx": """
                export default function UsersPage() {
                  return <td>Admin</td>;
                }
            """,
        }

        result = _validate_routes(routes, file_contents)

        assert result[0]["actions"] == []

    def test_rejects_click_without_source_evidence(self):
        from src.agent.routes import _validate_routes

        routes = [{"path": "/users", "actions": [{
            "type": "click",
            "selector": "text=Active",
            "sourceText": "Active",
            "reason": "users/page.tsx changed",
        }]}]
        file_contents = {
            "app/users/page.tsx": """
                export default function UsersPage() {
                  return <button>Open filters</button>;
                }
            """,
        }

        result = _validate_routes(routes, file_contents)

        assert result[0]["actions"] == []

    def test_accepts_dropdown_trigger_component_action(self):
        from src.agent.routes import _validate_routes

        routes = [{"path": "/users", "actions": [{
            "type": "click",
            "selector": "text=Filters",
            "sourceText": "Filters",
            "reason": "DropdownMenuTrigger opens hidden filters dropdown",
        }]}]
        file_contents = {
            "app/users/page.tsx": """
                export default function UsersPage() {
                  return (
                    <DropdownMenu>
                      <DropdownMenuTrigger>Filters</DropdownMenuTrigger>
                      <DropdownMenuContent>Status controls changed here</DropdownMenuContent>
                    </DropdownMenu>
                  );
                }
            """,
        }

        result = _validate_routes(routes, file_contents)

        assert result[0]["actions"] == routes[0]["actions"]

    def test_accepts_aria_label_button_trigger_action(self):
        from src.agent.routes import _validate_routes

        routes = [{"path": "/users", "actions": [{
            "type": "click",
            "selector": "[aria-label='Open menu']",
            "sourceText": "Open menu",
            "reason": "button opens menu dropdown",
        }]}]
        file_contents = {
            "app/users/page.tsx": """
                export default function UsersPage() {
                  return <button aria-label="Open menu"><MenuIcon /></button>;
                }
            """,
        }

        result = _validate_routes(routes, file_contents)

        assert result[0]["actions"] == routes[0]["actions"]


class TestBuildRepoTree:
    def test_excludes_common_dirs(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.routes.REPO_DIR", str(tmp_path))

        (tmp_path / "app" / "page.tsx").parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "app" / "page.tsx").write_text("")
        (tmp_path / "node_modules" / "pkg" / "index.js").parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "node_modules" / "pkg" / "index.js").write_text("")
        (tmp_path / ".next" / "build" / "output.js").parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / ".next" / "build" / "output.js").write_text("")

        from src.agent.routes import build_repo_tree

        tree = build_repo_tree()
        lines = tree.splitlines()
        assert "app/page.tsx" in lines
        assert "node_modules/pkg/index.js" not in lines
        assert ".next/build/output.js" not in lines

    def test_missing_dir_returns_empty(self, monkeypatch):
        monkeypatch.setattr("src.agent.routes.REPO_DIR", "/nonexistent/path")

        from src.agent.routes import build_repo_tree

        assert build_repo_tree() == ""


class TestGetChangedFiles:
    def test_extracts_files_from_diff(self):
        from src.agent.routes import _get_changed_files

        diff = """diff --git a/src/app/page.tsx b/src/app/page.tsx
--- a/src/app/page.tsx
+++ b/src/app/page.tsx
diff --git a/src/app/users/page.tsx b/src/app/users/page.tsx
--- a/src/app/users/page.tsx
+++ b/src/app/users/page.tsx"""
        result = _get_changed_files(diff)
        assert "src/app/page.tsx" in result
        assert "src/app/users/page.tsx" in result

    def test_empty_diff_returns_empty(self):
        from src.agent.routes import _get_changed_files
        assert _get_changed_files("") == []


class TestFindImporters:
    def test_finds_files_that_import_the_stem(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.routes.REPO_DIR", str(tmp_path))

        (tmp_path / "components" / "Modal.tsx").parent.mkdir(parents=True)
        (tmp_path / "components" / "Modal.tsx").write_text("export const Modal = () => <div />;")
        (tmp_path / "app" / "page.tsx").parent.mkdir(parents=True)
        (tmp_path / "app" / "page.tsx").write_text("import { Modal } from '../components/Modal';\n// page content")
        (tmp_path / "app" / "other.tsx").write_text("no import here")

        from src.agent.routes import _find_importers

        result = _find_importers(["Modal"], set())
        assert "app/page.tsx" in result
        assert "app/other.tsx" not in result

    def test_excludes_self(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.routes.REPO_DIR", str(tmp_path))

        (tmp_path / "Modal.tsx").write_text("export {}\n")
        from src.agent.routes import _find_importers

        result = _find_importers(["Modal"], {"Modal.tsx"})
        assert "Modal.tsx" not in result

    def test_multiple_stems(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.routes.REPO_DIR", str(tmp_path))

        (tmp_path / "a.tsx").write_text("import x from './targetA';")
        (tmp_path / "b.tsx").write_text("import y from './targetB';")
        (tmp_path / "c.tsx").write_text("import z from './other';")

        from src.agent.routes import _find_importers

        result = _find_importers(["targetA", "targetB"], set())
        assert "a.tsx" in result
        assert "b.tsx" in result
        assert "c.tsx" not in result

    def test_respects_max_limit(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.routes.REPO_DIR", str(tmp_path))
        monkeypatch.setattr("src.agent.routes._MAX_REVERSE_DEPS", 2)

        for i in range(5):
            (tmp_path / f"importer{i}.tsx").write_text("import x from './target';")

        from src.agent.routes import _find_importers

        result = _find_importers(["target"], set())
        assert len(result) <= 2

    def test_missing_repo_returns_empty(self, monkeypatch):
        monkeypatch.setattr("src.agent.routes.REPO_DIR", "/nonexistent")

        from src.agent.routes import _find_importers

        result = _find_importers(["Modal"], set())
        assert result == []


class TestExtractJson:
    def test_plain_json(self):
        from src.agent.routes import _extract_balanced_json
        assert _extract_balanced_json('{"routes": []}') == {"routes": []}

    def test_extracts_from_markdown_fence(self):
        from src.agent.routes import _extract_balanced_json
        result = _extract_balanced_json("```json\n{\"routes\": []}\n```")
        assert result == {"routes": []}

    def test_extracts_from_surrounding_text(self):
        from src.agent.routes import _extract_balanced_json
        result = _extract_balanced_json("Here is the result: {\"routes\": [{\"path\": \"/\"}]}.")
        assert result == {"routes": [{"path": "/"}]}

    def test_returns_none_for_invalid(self):
        from src.agent.routes import _extract_balanced_json
        assert _extract_balanced_json("not json at all") is None

    def test_picks_largest_balanced_object_when_multiple(self):
        from src.agent.routes import _extract_balanced_json
        text = 'noise {"small": true} more noise {"routes": [{"path": "/x"}]}'
        result = _extract_balanced_json(text)
        assert result == {"routes": [{"path": "/x"}]}

    def test_handles_nested_objects(self):
        from src.agent.routes import _extract_balanced_json
        text = '{"routes": [{"path": "/a", "actions": [{"type": "click", "selector": "text=Go"}]}]}'
        result = _extract_balanced_json(text)
        assert result == {"routes": [{"path": "/a", "actions": [{"type": "click", "selector": "text=Go"}]}]}


class TestValidateMocks:
    def test_valid_mocks_pass_through(self):
        from src.agent.routes import _validate_mocks
        mocks = {
            "api.example.com": {
                "/api/users": {"body": {"users": [{"id": 1}]}},
                "/api/posts": {"body": {"posts": []}, "status": 200},
            }
        }
        result = _validate_mocks(mocks)
        assert result["api.example.com"]["/api/users"]["body"] == {"users": [{"id": 1}]}
        assert result["api.example.com"]["/api/users"]["status"] == 200
        assert result["api.example.com"]["/api/posts"]["body"] == {"posts": []}

    def test_invalid_domain_skipped(self):
        from src.agent.routes import _validate_mocks
        mocks = {
            "api.example.com": {"/api/users": {"body": {"ok": True}}},
            123: {"/api/bad": {"body": {}}},
        }
        result = _validate_mocks(mocks)
        assert "api.example.com" in result
        assert 123 not in result

    def test_invalid_path_skipped(self):
        from src.agent.routes import _validate_mocks
        mocks = {
            "api.example.com": {
                "/api/users": {"body": {"ok": True}},
                123: {"body": {}},
            }
        }
        result = _validate_mocks(mocks)
        assert "/api/users" in result["api.example.com"]
        assert 123 not in result["api.example.com"]

    def test_missing_body_skipped(self):
        from src.agent.routes import _validate_mocks
        mocks = {
            "api.example.com": {
                "/api/valid": {"body": {"ok": True}},
                "/api/invalid": {"status": 200},
            }
        }
        result = _validate_mocks(mocks)
        assert "/api/valid" in result["api.example.com"]
        assert "/api/invalid" not in result["api.example.com"]

    def test_body_not_dict_or_list_skipped(self):
        from src.agent.routes import _validate_mocks
        mocks = {
            "api.example.com": {
                "/api/dict": {"body": {"ok": True}},
                "/api/array": {"body": [{"id": 1}]},
                "/api/invalid": {"body": "string instead of dict or list"},
            }
        }
        result = _validate_mocks(mocks)
        assert "/api/dict" in result["api.example.com"]
        assert "/api/array" in result["api.example.com"]
        assert "/api/invalid" not in result["api.example.com"]

    def test_non_dict_mocks_value(self):
        from src.agent.routes import _validate_mocks
        mocks = {"api.example.com": "not a dict"}
        result = _validate_mocks(mocks)
        assert result == {}

    def test_empty_mocks(self):
        from src.agent.routes import _validate_mocks
        result = _validate_mocks({})
        assert result == {}

    def test_status_defaults_to_200(self):
        from src.agent.routes import _validate_mocks
        mocks = {
            "api.example.com": {
                "/api/users": {"body": {"ok": True}},
            }
        }
        result = _validate_mocks(mocks)
        assert result["api.example.com"]["/api/users"]["status"] == 200

    def test_none_mocks(self):
        from src.agent.routes import _validate_mocks
        result = _validate_mocks(None)
        assert result == {}


class TestDeterministicClassification:
    def test_page_file_routes_to_itself(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.routes.REPO_DIR", str(tmp_path))
        (tmp_path / "app" / "users" / "page.tsx").parent.mkdir(parents=True)
        (tmp_path / "app" / "users" / "page.tsx").write_text("")

        from src.agent.routes import _deterministic_routes

        assert _deterministic_routes(["app/users/page.tsx"]) == ["/users"]

    def test_globals_css_routes_to_every_route(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.routes.REPO_DIR", str(tmp_path))
        for route_path in ["", "/users", "/dashboard"]:
            d = tmp_path / "app" / route_path.strip("/") if route_path else tmp_path / "app"
            (d / "page.tsx").parent.mkdir(parents=True, exist_ok=True)
            (d / "page.tsx").write_text("")
        (tmp_path / "app" / "globals.css").write_text("")

        from src.agent.routes import _deterministic_routes

        result = _deterministic_routes(["app/globals.css"])
        assert "/" in result
        assert "/users" in result
        assert "/dashboard" in result

    def test_layout_file_routes_to_its_segment(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.routes.REPO_DIR", str(tmp_path))
        (tmp_path / "app" / "dashboard" / "layout.tsx").parent.mkdir(parents=True)
        (tmp_path / "app" / "dashboard" / "layout.tsx").write_text("")
        (tmp_path / "app" / "dashboard" / "page.tsx").write_text("")

        from src.agent.routes import _deterministic_routes

        result = _deterministic_routes(["app/dashboard/layout.tsx"])
        assert result == ["/dashboard"]

    def test_layout_at_root_includes_all_routes(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.routes.REPO_DIR", str(tmp_path))
        (tmp_path / "app" / "layout.tsx").parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "app" / "layout.tsx").write_text("")
        (tmp_path / "app" / "page.tsx").write_text("")
        (tmp_path / "app" / "users" / "page.tsx").parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "app" / "users" / "page.tsx").write_text("")

        from src.agent.routes import _deterministic_routes

        result = _deterministic_routes(["app/layout.tsx"])
        assert "/" in result
        assert "/users" in result

    def test_shared_component_traces_to_consuming_pages(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.routes.REPO_DIR", str(tmp_path))
        (tmp_path / "components" / "Button.tsx").parent.mkdir(parents=True)
        (tmp_path / "components" / "Button.tsx").write_text("export const Button = () => null;")
        (tmp_path / "app" / "users" / "page.tsx").parent.mkdir(parents=True)
        (tmp_path / "app" / "users" / "page.tsx").write_text(
            "import { Button } from '../../components/Button';\nexport default () => <Button />;"
        )
        (tmp_path / "app" / "dashboard" / "page.tsx").parent.mkdir(parents=True)
        (tmp_path / "app" / "dashboard" / "page.tsx").write_text(
            "import { Button } from '../../components/Button';\nexport default () => <Button />;"
        )

        from src.agent.routes import _deterministic_routes

        result = _deterministic_routes(["components/Button.tsx"])
        assert set(result) == {"/users", "/dashboard"}

    def test_shared_component_with_no_pages_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.routes.REPO_DIR", str(tmp_path))
        (tmp_path / "lib" / "utils.ts").parent.mkdir(parents=True)
        (tmp_path / "lib" / "utils.ts").write_text("export const noop = () => null;")

        from src.agent.routes import _deterministic_routes

        assert _deterministic_routes(["lib/utils.ts"]) == []

    def test_deduplicates_routes_across_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.routes.REPO_DIR", str(tmp_path))
        (tmp_path / "app" / "users" / "page.tsx").parent.mkdir(parents=True)
        (tmp_path / "app" / "users" / "page.tsx").write_text("")
        (tmp_path / "components" / "Button.tsx").parent.mkdir(parents=True)
        (tmp_path / "components" / "Button.tsx").write_text("export const Button = () => null;")
        (tmp_path / "app" / "users" / "components-in-users.tsx").parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "app" / "users" / "components-in-users.tsx").write_text(
            "import { Button } from '../../components/Button'; export default () => <Button />;"
        )

        from src.agent.routes import _deterministic_routes

        result = _deterministic_routes(["app/users/page.tsx", "components/Button.tsx"])
        assert result == ["/users"]


class TestMockOutput:
    def test_llm_augment_can_add_mocks(self, mock_httpx_client, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.routes.REPO_DIR", str(tmp_path))
        (tmp_path / "app" / "users" / "page.tsx").parent.mkdir(parents=True)
        (tmp_path / "app" / "users" / "page.tsx").write_text(
            "export default () => { fetch('/api/users').then(r => r.json()); return null; };"
        )
        mock_httpx_client([
            httpx.Response(200, json={
                "choices": [{"message": {"content": json.dumps({
                    "routes": [],
                    "mocks": {
                        "api.example.com": {
                            "/api/users": {"body": {"users": [{"id": 1, "name": "Alice"}]}},
                        },
                    },
                })}}],
            }),
        ])

        from src.agent.routes import infer_routes

        diff = "+++ b/app/users/page.tsx"
        routes, mocks = infer_routes(diff, "tree", "sk-or-fake")
        assert len(routes) == 1
        assert routes[0]["path"] == "/users"
        assert "/api/users" in mocks.get("api.example.com", {})

    def test_llm_failure_does_not_block_deterministic_routes(self, mock_httpx_client, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.routes.REPO_DIR", str(tmp_path))
        (tmp_path / "app" / "users" / "page.tsx").parent.mkdir(parents=True)
        (tmp_path / "app" / "users" / "page.tsx").write_text("export default () => null;")
        mock_httpx_client([
            httpx.Response(200, json={
                "choices": [{"message": {"content": json.dumps({
                    "routes": [{"path": "/users", "actions": []}],
                })}}],
            }),
        ])

        from src.agent.routes import infer_routes

        diff = "+++ b/app/users/page.tsx"
        routes, mocks = infer_routes(diff, "tree", "sk-or-fake")
        assert len(routes) == 1
        assert routes[0]["path"] == "/users"
        assert mocks == {}
