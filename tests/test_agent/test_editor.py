
from src.agent.editor import apply_edit, wait_for_dev_server, revert_edit


class TestApplyEdit:
    def test_successful_replacement(self, tmp_path):
        file = tmp_path / "test.tsx"
        file.write_text('className="bg-blue-500"')
        edit = {"file": str(file), "line": 1, "oldString": "bg-blue-500", "newString": "bg-orange-500"}
        assert apply_edit(edit)
        assert file.read_text() == 'className="bg-orange-500"'

    def test_file_not_found(self):
        edit = {"file": "/nonexistent/file.tsx", "line": 1, "oldString": "x", "newString": "y"}
        assert not apply_edit(edit)

    def test_old_string_not_found(self, tmp_path):
        file = tmp_path / "test.tsx"
        file.write_text("something else")
        edit = {"file": str(file), "line": 1, "oldString": "nonexistent", "newString": "whatever"}
        assert not apply_edit(edit)

    def test_disambiguates_by_line(self, tmp_path):
        file = tmp_path / "test.tsx"
        file.write_text("a\na\na\n")
        edit = {"file": str(file), "line": 3, "oldString": "a", "newString": "b"}
        assert apply_edit(edit)
        assert file.read_text() == "a\na\nb\n"

    def test_disambiguates_to_nearest_line(self, tmp_path):
        file = tmp_path / "test.tsx"
        file.write_text("a\na\na\n")
        edit = {"file": str(file), "line": 2, "oldString": "a", "newString": "b"}
        assert apply_edit(edit)
        assert file.read_text() == "a\nb\na\n"


class TestWaitForDevServer:
    def test_returns_true_on_200(self, monkeypatch):
        class MockResponse:
            status_code = 200
            text = "<html>ok</html>"

        def mock_get(*a, **kw):
            return MockResponse()

        monkeypatch.setattr("httpx.get", mock_get)
        assert wait_for_dev_server("http://localhost:3000")

    def test_returns_false_on_timeout(self, monkeypatch):
        def mock_get(*a, **kw):
            raise ConnectionError()

        monkeypatch.setattr("httpx.get", mock_get)
        assert not wait_for_dev_server("http://localhost:3000", timeout=0.5, interval=0.1)

    def test_returns_false_on_error_overlay(self, monkeypatch):
        responses = iter([
            type("R", (), {"status_code": 200, "text": "<html>nextjs__container_errors</html>"})(),
            type("R", (), {"status_code": 200, "text": "<html>nextjs__container_errors</html>"})(),
            type("R", (), {"status_code": 200, "text": "<html>nextjs__container_errors</html>"})(),
        ])

        def mock_get(*a, **kw):
            return next(responses)

        monkeypatch.setattr("httpx.get", mock_get)
        assert not wait_for_dev_server("http://localhost:3000", timeout=0.3, interval=0.05)


class TestRevertEdit:
    def test_calls_git_checkout(self, monkeypatch):
        calls = []

        def mock_run(*a, **kw):
            calls.append(a[0])
            return type("R", (), {"returncode": 0})()

        monkeypatch.setattr("subprocess.run", mock_run)
        revert_edit({"file": "src/page.tsx"})
        assert ["git", "checkout", "src/page.tsx"] in calls
