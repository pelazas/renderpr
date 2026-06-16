


class TestDetectFrontendChanges:
    def test_tsx_file_detected(self):
        from src.agent.discovery import detect_frontend_changes
        diff = """diff --git a/src/page.tsx b/src/page.tsx
--- a/src/page.tsx
+++ b/src/page.tsx
@@ -1 +1 @@
-old
+new"""
        assert detect_frontend_changes(diff) is True

    def test_jsx_file_detected(self):
        from src.agent.discovery import detect_frontend_changes
        diff = """diff --git a/src/App.jsx b/src/App.jsx
--- a/src/App.jsx
+++ b/src/App.jsx
@@ -1 +1 @@
-old
+new"""
        assert detect_frontend_changes(diff) is True

    def test_vue_file_detected(self):
        from src.agent.discovery import detect_frontend_changes
        diff = """diff --git a/src/App.vue b/src/App.vue
--- a/src/App.vue
+++ b/src/App.vue
@@ -1 +1 @@
-old
+new"""
        assert detect_frontend_changes(diff) is True

    def test_css_file_detected(self):
        from src.agent.discovery import detect_frontend_changes
        diff = """diff --git a/src/styles.css b/src/styles.css
--- a/src/styles.css
+++ b/src/styles.css
@@ -1 +1 @@
-old
+new"""
        assert detect_frontend_changes(diff) is True

    def test_py_file_not_detected(self):
        from src.agent.discovery import detect_frontend_changes
        diff = """diff --git a/src/main.py b/src/main.py
--- a/src/main.py
+++ b/src/main.py
@@ -1 +1 @@
-old
+new"""
        assert detect_frontend_changes(diff) is False

    def test_ts_file_not_detected(self):
        from src.agent.discovery import detect_frontend_changes
        diff = """diff --git a/src/utils.ts b/src/utils.ts
--- a/src/utils.ts
+++ b/src/utils.ts
@@ -1 +1 @@
-old
+new"""
        assert detect_frontend_changes(diff) is False

    def test_empty_diff_returns_false(self):
        from src.agent.discovery import detect_frontend_changes
        assert detect_frontend_changes("") is False

    def test_backend_only_pr_returns_false(self):
        from src.agent.discovery import detect_frontend_changes
        diff = """diff --git a/Dockerfile b/Dockerfile
--- a/Dockerfile
+++ b/Dockerfile
@@ -1 +1 @@
-old
+new
diff --git a/src/main.py b/src/main.py
--- a/src/main.py
+++ b/src/main.py
@@ -1 +1 @@
-old
+new"""
        assert detect_frontend_changes(diff) is False

    def test_mixed_frontend_backend_returns_true(self):
        from src.agent.discovery import detect_frontend_changes
        diff = """diff --git a/src/main.py b/src/main.py
--- a/src/main.py
+++ b/src/main.py
@@ -1 +1 @@
-old
+new
diff --git a/src/App.tsx b/src/App.tsx
--- a/src/App.tsx
+++ b/src/App.tsx
@@ -1 +1 @@
-old
+new"""
        assert detect_frontend_changes(diff) is True


class TestFindNearestPackageJson:
    def test_finds_package_json_in_same_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.discovery.REPO_DIR", str(tmp_path))
        pkg = tmp_path / "package.json"
        pkg.write_text('{"name": "test"}')

        from src.agent.discovery import find_nearest_package_json
        result = find_nearest_package_json(["src/page.tsx"])
        assert result == str(pkg)

    def test_walks_up_from_deeply_nested_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.discovery.REPO_DIR", str(tmp_path))
        changed = tmp_path / "packages" / "web" / "src" / "page.tsx"
        changed.parent.mkdir(parents=True)
        pkg = tmp_path / "packages" / "web" / "package.json"
        pkg.write_text('{"name": "web"}')

        from src.agent.discovery import find_nearest_package_json
        result = find_nearest_package_json(["packages/web/src/page.tsx"])
        assert result == str(pkg)

    def test_returns_root_package_json_if_none_in_subdir(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.discovery.REPO_DIR", str(tmp_path))
        pkg = tmp_path / "package.json"
        pkg.write_text('{"name": "root"}')

        from src.agent.discovery import find_nearest_package_json
        result = find_nearest_package_json(["src/page.tsx"])
        assert result == str(pkg)

    def test_returns_none_if_no_package_json_at_all(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.discovery.REPO_DIR", str(tmp_path))

        from src.agent.discovery import find_nearest_package_json
        result = find_nearest_package_json(["src/page.tsx"])
        assert result is None

    def test_respects_depth_limit(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.discovery.REPO_DIR", str(tmp_path))
        monkeypatch.setattr("src.agent.discovery.MAX_PACKAGE_SCAN_DEPTH", 2)

        deep_dir = tmp_path / "a" / "b" / "c" / "d"
        deep_dir.mkdir(parents=True)
        (tmp_path / "a" / "b" / "c" / "package.json").write_text('{"name": "deep"}')

        from src.agent.discovery import find_nearest_package_json
        result = find_nearest_package_json(["a/b/c/d/file.tsx"])
        assert result is None

    def test_multiple_changed_files_same_package(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.discovery.REPO_DIR", str(tmp_path))
        pkg = tmp_path / "packages" / "web" / "package.json"
        pkg.parent.mkdir(parents=True)
        pkg.write_text('{"name": "web"}')

        from src.agent.discovery import find_nearest_package_json
        result = find_nearest_package_json([
            "packages/web/src/page.tsx",
            "packages/web/src/components/Header.tsx",
        ])
        assert result == str(pkg)


class TestFindWorkspaceRoot:
    def test_returns_parent_with_workspaces(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.discovery.REPO_DIR", str(tmp_path))
        root_pkg = tmp_path / "package.json"
        root_pkg.write_text('{"workspaces": ["packages/*"]}')
        web_pkg = tmp_path / "packages" / "web" / "package.json"
        web_pkg.parent.mkdir(parents=True)
        web_pkg.write_text('{"name": "web"}')

        from src.agent.discovery import find_workspace_root
        result = find_workspace_root(str(web_pkg))
        assert result == str(root_pkg)

    def test_returns_none_if_no_workspaces(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.discovery.REPO_DIR", str(tmp_path))
        pkg = tmp_path / "package.json"
        pkg.write_text('{"name": "root"}')

        from src.agent.discovery import find_workspace_root
        result = find_workspace_root(str(pkg))
        assert result is None

    def test_returns_none_if_package_json_not_found(self):
        from src.agent.discovery import find_workspace_root
        result = find_workspace_root("/nonexistent/path/package.json")
        assert result is None

    def test_root_package_itself_has_workspaces(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.discovery.REPO_DIR", str(tmp_path))
        pkg = tmp_path / "package.json"
        pkg.write_text('{"workspaces": ["packages/*"]}')

        from src.agent.discovery import find_workspace_root
        result = find_workspace_root(str(pkg))
        assert result is None

    def test_pnpm_workspace_yaml_is_a_root(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.discovery.REPO_DIR", str(tmp_path))
        root_pkg = tmp_path / "package.json"
        root_pkg.write_text('{"name": "monorepo"}')  # no workspaces key
        (tmp_path / "pnpm-workspace.yaml").write_text("packages:\n  - 'packages/*'\n")
        web_pkg = tmp_path / "packages" / "web" / "package.json"
        web_pkg.parent.mkdir(parents=True)
        web_pkg.write_text('{"name": "web"}')

        from src.agent.discovery import find_workspace_root
        result = find_workspace_root(str(web_pkg))
        assert result == str(root_pkg)

    def test_pnpm_workspace_yaml_without_root_package_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.discovery.REPO_DIR", str(tmp_path))
        (tmp_path / "pnpm-workspace.yaml").write_text("packages:\n  - 'packages/*'\n")
        web_pkg = tmp_path / "packages" / "web" / "package.json"
        web_pkg.parent.mkdir(parents=True)
        web_pkg.write_text('{"name": "web"}')

        from src.agent.discovery import find_workspace_root
        result = find_workspace_root(str(web_pkg))
        # Workspace root recognized even with no root package.json; the install
        # dir is still resolvable via the returned path's parent.
        assert result == str(tmp_path / "package.json")

    def test_pnpm_workspace_yaml_at_pkg_dir_not_self_referential(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.discovery.REPO_DIR", str(tmp_path))
        # A single-package repo whose own dir has pnpm-workspace.yaml shouldn't
        # report itself as a distinct workspace root.
        pkg = tmp_path / "package.json"
        pkg.write_text('{"name": "solo"}')
        (tmp_path / "pnpm-workspace.yaml").write_text("packages:\n  - '.'\n")

        from src.agent.discovery import find_workspace_root
        result = find_workspace_root(str(pkg))
        assert result is None


class TestVerifyDevScript:
    def test_dev_script_exists(self, tmp_path):
        pkg = tmp_path / "package.json"
        pkg.write_text('{"scripts": {"dev": "next dev"}}')

        from src.agent.discovery import verify_dev_script
        assert verify_dev_script(str(pkg)) is True

    def test_no_dev_script(self, tmp_path):
        pkg = tmp_path / "package.json"
        pkg.write_text('{"scripts": {"build": "next build"}}')

        from src.agent.discovery import verify_dev_script
        assert verify_dev_script(str(pkg)) is False

    def test_no_scripts_at_all(self, tmp_path):
        pkg = tmp_path / "package.json"
        pkg.write_text('{"name": "test"}')

        from src.agent.discovery import verify_dev_script
        assert verify_dev_script(str(pkg)) is False

    def test_file_not_found(self):
        from src.agent.discovery import verify_dev_script
        assert verify_dev_script("/nonexistent/package.json") is False


class TestGetChangedFiles:
    def test_extracts_from_diff(self):
        from src.agent.routes import _get_changed_files

        diff = """diff --git a/src/page.tsx b/src/page.tsx
--- a/src/page.tsx
+++ b/src/page.tsx
diff --git a/src/Header.tsx b/src/Header.tsx
--- a/src/Header.tsx
+++ b/src/Header.tsx"""
        result = _get_changed_files(diff)
        assert "src/page.tsx" in result
        assert "src/Header.tsx" in result

    def test_empty_diff_returns_empty(self):
        from src.agent.routes import _get_changed_files
        assert _get_changed_files("") == []


class TestDiscoverFrontend:
    def test_full_discovery_returns_package_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.discovery.REPO_DIR", str(tmp_path))
        pkg = tmp_path / "package.json"
        pkg.write_text('{"scripts": {"dev": "next dev"}}')

        diff = """diff --git a/src/page.tsx b/src/page.tsx
--- a/src/page.tsx
+++ b/src/page.tsx
@@ -1 +1 @@
-old
+new"""

        from src.agent.discovery import discover_frontend
        result = discover_frontend(diff)
        assert result["has_frontend"] is True
        assert result["package_json_path"] == str(pkg)
        assert result["dev_command"] == "npm run dev"

    def test_no_frontend_files_returns_early(self):
        from src.agent.discovery import discover_frontend
        diff = """diff --git a/src/main.py b/src/main.py
--- a/src/main.py
+++ b/src/main.py
@@ -1 +1 @@
-old
+new"""
        result = discover_frontend(diff)
        assert result["has_frontend"] is False
        assert result["reason"] is not None

    def test_no_package_json_returns_no_package(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.discovery.REPO_DIR", str(tmp_path))
        diff = """diff --git a/src/page.tsx b/src/page.tsx
--- a/src/page.tsx
+++ b/src/page.tsx
@@ -1 +1 @@
-old
+new"""

        from src.agent.discovery import discover_frontend
        result = discover_frontend(diff)
        assert result["has_frontend"] is True
        assert result["package_json_path"] is None

    def test_no_dev_script_returns_no_command(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.discovery.REPO_DIR", str(tmp_path))
        pkg = tmp_path / "package.json"
        pkg.write_text('{"name": "test"}')

        diff = """diff --git a/src/page.tsx b/src/page.tsx
--- a/src/page.tsx
+++ b/src/page.tsx
@@ -1 +1 @@
-old
+new"""

        from src.agent.discovery import discover_frontend
        result = discover_frontend(diff)
        assert result["has_frontend"] is True
        assert result["package_json_path"] == str(pkg)
        assert result["dev_command"] is None

    def test_monorepo_detects_workspace_root(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.discovery.REPO_DIR", str(tmp_path))
        root_pkg = tmp_path / "package.json"
        root_pkg.write_text('{"workspaces": ["packages/*"]}')
        web_pkg = tmp_path / "packages" / "web" / "package.json"
        web_pkg.parent.mkdir(parents=True)
        web_pkg.write_text('{"scripts": {"dev": "next dev"}}')

        diff = """diff --git a/packages/web/src/page.tsx b/packages/web/src/page.tsx
--- a/packages/web/src/page.tsx
+++ b/packages/web/src/page.tsx
@@ -1 +1 @@
-old
+new"""

        from src.agent.discovery import discover_frontend
        result = discover_frontend(diff)
        assert result["has_frontend"] is True
        assert result["package_json_path"] == str(web_pkg)
        assert result["workspace_root"] == str(root_pkg)
        assert result["dev_command"] == "npm run dev"

    def test_empty_diff_returns_no_frontend(self):
        from src.agent.discovery import discover_frontend
        result = discover_frontend("")
        assert result["has_frontend"] is False

    def test_launch_profile_reflects_detected_stack(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.discovery.REPO_DIR", str(tmp_path))
        (tmp_path / "pnpm-lock.yaml").write_text("")
        pkg = tmp_path / "package.json"
        pkg.write_text('{"dependencies": {"vite": "5.0.0"}, "scripts": {"dev": "vite"}}')

        diff = """diff --git a/src/App.tsx b/src/App.tsx
--- a/src/App.tsx
+++ b/src/App.tsx
@@ -1 +1 @@
-old
+new"""

        from src.agent.discovery import discover_frontend
        result = discover_frontend(diff)
        profile = result["launch_profile"]
        assert profile.package_manager == "pnpm"
        assert profile.framework == "vite"
        assert result["dev_command"] == "pnpm run dev --host"
        assert profile.default_port == 5173
