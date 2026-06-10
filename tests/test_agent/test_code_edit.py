import httpx
import pytest

from src.agent.code_edit import (
    EditGenerationError,
    _build_directory_tree,
    validate_edit,
)


class TestBuildDirectoryTree:
    def test_returns_tree(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "page.tsx").write_text("")
        (tmp_path / "src" / "style.css").write_text("")
        tree = _build_directory_tree(str(tmp_path))
        assert "page.tsx" in tree
        assert "style.css" in tree

    def test_skips_node_modules(self, tmp_path):
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "big.tsx").write_text("")
        tree = _build_directory_tree(str(tmp_path))
        assert "big.tsx" not in tree

    def test_skips_hidden_dirs(self, tmp_path):
        (tmp_path / ".next").mkdir()
        (tmp_path / ".next" / "build.tsx").write_text("")
        tree = _build_directory_tree(str(tmp_path))
        assert "build.tsx" not in tree

    def test_empty_dir(self, tmp_path):
        tree = _build_directory_tree(str(tmp_path))
        assert "(no source files found)" in tree


class TestValidateEdit:
    def test_valid_edit(self, tmp_path):
        file = tmp_path / "test.tsx"
        file.write_text("hello world")
        edit = {"file": str(file), "line": 1, "oldString": "hello", "newString": "hi"}
        assert validate_edit(edit)

    def test_missing_key(self):
        assert not validate_edit({"file": "x.tsx", "line": 1})

    def test_file_not_found(self):
        edit = {"file": "/nonexistent/file.tsx", "line": 1, "oldString": "x", "newString": "y"}
        assert not validate_edit(edit)

    def test_old_string_not_in_file(self, tmp_path):
        file = tmp_path / "test.tsx"
        file.write_text("something else")
        edit = {"file": str(file), "line": 1, "oldString": "not found", "newString": "y"}
        assert not validate_edit(edit)



