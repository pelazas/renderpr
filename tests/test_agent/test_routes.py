import json

import httpx
import pytest

from src.agent.config import REPO_DIR


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
    def test_returns_parsed_routes(self, mock_httpx_client):
        mock_httpx_client([
            httpx.Response(200, json={
                "choices": [{"message": {"content": json.dumps({
                    "routes": [
                        {"path": "/dashboard", "reason": "file changed", "actions": []},
                        {"path": "/profile", "reason": "component used", "actions": [
                            {"type": "click", "selector": "#menu"},
                            {"type": "wait", "ms": 500},
                        ]},
                    ],
                })}}],
            }),
        ])

        from src.agent.routes import infer_routes

        result = infer_routes("diff content", "file tree", "sk-or-fake")

        assert len(result) == 2
        assert result[0]["path"] == "/dashboard"
        assert result[1]["path"] == "/profile"
        assert len(result[1]["actions"]) == 2

    def test_empty_routes_falls_back(self, mock_httpx_client):
        mock_httpx_client([
            httpx.Response(200, json={
                "choices": [{"message": {"content": json.dumps({"routes": []})}}],
            }),
        ])

        from src.agent.routes import infer_routes

        result = infer_routes("diff", "tree", "sk-or-fake")
        assert result == [{"path": "/", "reason": "fallback", "actions": []}]

    def test_malformed_json_falls_back(self, mock_httpx_client):
        mock_httpx_client([
            httpx.Response(200, json={
                "choices": [{"message": {"content": "not json at all"}}],
            }),
        ])

        from src.agent.routes import infer_routes

        result = infer_routes("diff", "tree", "sk-or-fake")
        assert result == [{"path": "/", "reason": "fallback", "actions": []}]

    def test_api_4xx_falls_back(self, mock_httpx_client):
        mock_httpx_client([
            httpx.Response(401, json={"error": "Unauthorized"}),
        ])

        from src.agent.routes import infer_routes

        result = infer_routes("diff", "tree", "sk-or-fake")
        assert result == [{"path": "/", "reason": "fallback", "actions": []}]

    def test_api_5xx_retry_then_fallback(self, mock_httpx_client):
        mock_httpx_client([
            httpx.Response(500, json={"error": "Server error"}),
            httpx.Response(500, json={"error": "Server error"}),
            httpx.Response(500, json={"error": "Server error"}),
        ])

        from src.agent.routes import infer_routes

        result = infer_routes("diff", "tree", "sk-or-fake")
        assert result == [{"path": "/", "reason": "fallback", "actions": []}]


class TestValidateRoutes:
    def test_valid_routes_pass_through(self):
        from src.agent.routes import _validate_routes

        routes = [
            {"path": "/a", "reason": "r1", "actions": []},
            {"path": "/b", "reason": "r2", "actions": [{"type": "click", "selector": "#x"}, {"type": "wait", "ms": 500}]},
        ]

        result = _validate_routes(routes)
        assert len(result) == 2

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
            (tmp_path / f"importer{i}.tsx").write_text(f"import x from './target';")

        from src.agent.routes import _find_importers

        result = _find_importers(["target"], set())
        assert len(result) <= 2

    def test_missing_repo_returns_empty(self, monkeypatch):
        monkeypatch.setattr("src.agent.routes.REPO_DIR", "/nonexistent")

        from src.agent.routes import _find_importers

        result = _find_importers(["Modal"], set())
        assert result == []
