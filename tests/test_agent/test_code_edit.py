import pytest

from src.agent.code_edit import (
    EditGenerationError,
    _build_directory_tree,
    _normalize_edits,
    validate_edit,
)


class TestNormalizeEdits:
    def test_canonical_edits_object(self):
        parsed = {"edits": [{"file": "a.tsx", "line": 1, "oldString": "x", "newString": "y"}], "actions": []}
        out = _normalize_edits(parsed)
        assert out["edits"] == parsed["edits"]
        assert out["actions"] == []

    def test_bare_list_of_edits(self):
        parsed = [{"file": "a.tsx", "line": 1, "oldString": "x", "newString": "y"}]
        out = _normalize_edits(parsed)
        assert out["edits"] == parsed
        assert out["actions"] == []

    def test_legacy_single_edit_with_actions(self):
        parsed = {"file": "a.tsx", "line": 1, "oldString": "x", "newString": "y", "actions": [{"type": "click"}]}
        out = _normalize_edits(parsed)
        assert out["edits"] == [parsed]
        assert out["actions"] == [{"type": "click"}]

    def test_empty_edits_raises(self):
        with pytest.raises(EditGenerationError):
            _normalize_edits({"edits": []})

    def test_unexpected_shape_raises(self):
        with pytest.raises(EditGenerationError):
            _normalize_edits({"nonsense": True})


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



