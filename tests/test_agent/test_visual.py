import json
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def mock_playwright(monkeypatch):
    class MockPage:
        def __init__(self):
            self.viewport_size = None
            self.goto_url = None
            self.screenshot_path = None
            self.actions_called: list[tuple] = []
            self.event_handlers: dict[str, list] = {}
            self.init_scripts: list[str] = []
            self.evaluate_calls: list[tuple] = []
            self.ready_state: str = "complete"

        def set_viewport_size(self, size):
            self.viewport_size = size

        def goto(self, url, **kw):
            self.goto_url = url

        def screenshot(self, path, **kw):
            self.screenshot_path = path
            Path(path).touch()

        def route(self, pattern, handler):
            pass

        def click(self, selector: str):
            self.actions_called.append(("click", selector))

        def wait_for_timeout(self, ms: int):
            self.actions_called.append(("wait", ms))

        def on(self, event, handler):
            self.event_handlers.setdefault(event, []).append(handler)

        def add_init_script(self, script: str):
            self.init_scripts.append(script)

        def evaluate(self, expression: str):
            self.evaluate_calls.append(expression)
            if "readyState" in expression:
                return self.ready_state

    class MockContext:
        def route(self, pattern, handler):
            pass

        def new_page(self):
            return MockPage()

    class MockBrowser:
        def launch(self):
            return self

        def new_context(self):
            return MockContext()

        def close(self):
            pass

    class MockPlaywright:
        chromium = MockBrowser()

    class MockSyncPlaywright:
        def __enter__(self):
            return MockPlaywright()

        def __exit__(self, *a):
            pass

    monkeypatch.setattr(
        "src.agent.visual.sync_playwright",
        lambda: MockSyncPlaywright(),
    )


class TestCaptureScreenshots:
    def test_returns_screenshots_for_default_route(self, tmp_path):
        from src.agent.visual import capture_screenshots

        result = capture_screenshots(
            "http://localhost:3000",
            screenshot_dir=tmp_path,
        )

        assert len(result) == 4
        for path, label in result:
            assert isinstance(path, Path)
            assert isinstance(label, str)
            assert path.exists()
            assert " - /" in label

    def test_screenshot_directory_created(self, tmp_path):
        from src.agent.visual import capture_screenshots

        screenshot_dir = tmp_path / "screenshots"
        assert not screenshot_dir.exists()

        capture_screenshots(
            "http://localhost:3000",
            screenshot_dir=screenshot_dir,
        )

        assert screenshot_dir.exists()
        assert screenshot_dir.is_dir()

    def test_screenshots_have_viewport_labels(self, tmp_path):
        from src.agent.visual import capture_screenshots

        result = capture_screenshots(
            "http://localhost:3000",
            screenshot_dir=tmp_path,
        )

        labels = [label for _, label in result]
        assert any("Mobile XS" in label_text for label_text in labels)
        assert any("Tablet" in label_text for label_text in labels)
        assert any("Desktop" in label_text for label_text in labels)
        assert any("Desktop XL" in label_text for label_text in labels)

    def test_multiple_routes_captured(self, tmp_path):
        from src.agent.visual import capture_screenshots

        routes = [
            {"path": "/", "actions": [], "reason": "home"},
            {"path": "/dashboard", "actions": [{"type": "wait", "ms": 500}], "reason": "test"},
        ]

        result = capture_screenshots(
            "http://localhost:3000",
            screenshot_dir=tmp_path,
            routes=routes,
        )

        assert len(result) == 8
        labels = [label for _, label in result]
        assert all(" - /" in label_text or " - /dashboard" in label_text for label_text in labels)

    def test_route_label_included(self, tmp_path):
        from src.agent.visual import capture_screenshots

        routes = [
            {"path": "/profile", "actions": [], "reason": "test"},
        ]

        result = capture_screenshots(
            "http://localhost:3000",
            screenshot_dir=tmp_path,
            routes=routes,
        )

        assert len(result) == 4
        assert all(" - /profile" in label for _, label in result)





class TestCaptureScreenshotsWithMocks:
    def test_mocks_do_not_crash_capture(self, tmp_path):
        from src.agent.visual import capture_screenshots

        mocks = {
            "api.example.com": {
                "/api/users": {"body": {"users": []}, "status": 200},
            }
        }

        result = capture_screenshots(
            "http://localhost:3000",
            screenshot_dir=tmp_path,
            routes=[{"path": "/", "actions": [], "reason": "home"}],
            mocks=mocks,
        )

        assert len(result) == 4

    def test_no_mocks_when_not_provided(self, tmp_path):
        from src.agent.visual import capture_screenshots

        result = capture_screenshots(
            "http://localhost:3000",
            screenshot_dir=tmp_path,
            routes=[{"path": "/", "actions": [], "reason": "home"}],
        )

        assert len(result) == 4

    def test_empty_mocks(self, tmp_path):
        from src.agent.visual import capture_screenshots

        result = capture_screenshots(
            "http://localhost:3000",
            screenshot_dir=tmp_path,
            routes=[{"path": "/", "actions": [], "reason": "home"}],
            mocks={},
        )

        assert len(result) == 4

    def test_mocks_register_context_route_without_init_script(self, tmp_path, monkeypatch):
        from src.agent import visual as visual_mod

        seen = {"routes": [], "init_scripts": []}

        class MockPage:
            def set_viewport_size(self, size): pass
            def goto(self, url, **kw): pass
            def wait_for_timeout(self, ms): pass
            def evaluate(self, expr): return "complete"
            def screenshot(self, path, **kw): Path(path).touch()
            def click(self, selector, **kw): pass
            def on(self, event, handler): pass
            def add_init_script(self, script):
                seen["init_scripts"].append(script)

        class MockContext:
            def route(self, pattern, handler):
                seen["routes"].append((pattern, handler))

            def new_page(self):
                return MockPage()

        class MockBrowser:
            def new_context(self): return MockContext()
            def close(self): pass

        class MockPlaywright:
            class chromium:
                @staticmethod
                def launch(): return MockBrowser()

        class MockSyncPlaywright:
            def __enter__(self): return MockPlaywright()
            def __exit__(self, *a): pass

        monkeypatch.setattr("src.agent.visual.sync_playwright", lambda: MockSyncPlaywright())

        visual_mod.capture_screenshots(
            "http://localhost:3000",
            screenshot_dir=tmp_path,
            routes=[{"path": "/users", "actions": [], "reason": "test"}],
            mocks={
                "api.example.com": {
                    "/api/users": {"body": [{"id": 1, "name": "Alice"}], "status": 200}
                }
            },
        )

        assert seen["routes"]
        assert seen["routes"][0][0] == "**/*"
        assert seen["init_scripts"] == []

    def test_context_mock_handler_fulfills_matching_path(self, tmp_path, monkeypatch):
        from src.agent import visual as visual_mod

        captured = {"handler": None, "fulfilled": None, "continued": False}

        class MockRequest:
            url = "http://127.0.0.1:3000/api/users"
            method = "GET"

        class MockRoute:
            request = MockRequest()

            def fulfill(self, **kw):
                captured["fulfilled"] = kw

            def continue_(self):
                captured["continued"] = True

        class MockPage:
            def set_viewport_size(self, size): pass
            def goto(self, url, **kw): pass
            def wait_for_timeout(self, ms): pass
            def evaluate(self, expr): return "complete"
            def screenshot(self, path, **kw): Path(path).touch()
            def click(self, selector, **kw): pass
            def on(self, event, handler): pass
            def add_init_script(self, script): pass

        class MockContext:
            def route(self, pattern, handler):
                captured["handler"] = handler

            def new_page(self):
                return MockPage()

        class MockBrowser:
            def new_context(self): return MockContext()
            def close(self): pass

        class MockPlaywright:
            class chromium:
                @staticmethod
                def launch(): return MockBrowser()

        class MockSyncPlaywright:
            def __enter__(self): return MockPlaywright()
            def __exit__(self, *a): pass

        monkeypatch.setattr("src.agent.visual.sync_playwright", lambda: MockSyncPlaywright())

        visual_mod.capture_screenshots(
            "http://localhost:3000",
            screenshot_dir=tmp_path,
            routes=[{"path": "/users", "actions": [], "reason": "test"}],
            mocks={
                "api.example.com": {
                    "/api/users": {"body": [{"id": 1, "name": "Alice"}], "status": 200}
                }
            },
        )

        assert captured["handler"] is not None
        captured["handler"](MockRoute())

        assert captured["fulfilled"]["status"] == 200
        assert captured["fulfilled"]["content_type"] == "application/json"
        assert json.loads(captured["fulfilled"]["body"]) == [{"id": 1, "name": "Alice"}]
        assert captured["continued"] is False

    def test_context_mock_handler_continues_unmatched_path(self, tmp_path, monkeypatch):
        from src.agent import visual as visual_mod

        captured = {"handler": None, "fulfilled": None, "continued": False}

        class MockRequest:
            url = "http://127.0.0.1:3000/_next/static/chunk.js"
            method = "GET"

        class MockRoute:
            request = MockRequest()

            def fulfill(self, **kw):
                captured["fulfilled"] = kw

            def continue_(self):
                captured["continued"] = True

        class MockPage:
            def set_viewport_size(self, size): pass
            def goto(self, url, **kw): pass
            def wait_for_timeout(self, ms): pass
            def evaluate(self, expr): return "complete"
            def screenshot(self, path, **kw): Path(path).touch()
            def click(self, selector, **kw): pass
            def on(self, event, handler): pass
            def add_init_script(self, script): pass

        class MockContext:
            def route(self, pattern, handler):
                captured["handler"] = handler

            def new_page(self):
                return MockPage()

        class MockBrowser:
            def new_context(self): return MockContext()
            def close(self): pass

        class MockPlaywright:
            class chromium:
                @staticmethod
                def launch(): return MockBrowser()

        class MockSyncPlaywright:
            def __enter__(self): return MockPlaywright()
            def __exit__(self, *a): pass

        monkeypatch.setattr("src.agent.visual.sync_playwright", lambda: MockSyncPlaywright())

        visual_mod.capture_screenshots(
            "http://localhost:3000",
            screenshot_dir=tmp_path,
            routes=[{"path": "/users", "actions": [], "reason": "test"}],
            mocks={
                "api.example.com": {
                    "/api/users": {"body": [{"id": 1, "name": "Alice"}], "status": 200}
                }
            },
        )

        assert captured["handler"] is not None
        captured["handler"](MockRoute())

        assert captured["fulfilled"] is None
        assert captured["continued"] is True

    def test_hydration_diagnostics_registered(self, tmp_path):
        """capture_screenshots must register a pageerror handler and
        call page.evaluate to inspect document.readyState after navigation,
        so we can diagnose silent React hydration failures."""
        import unittest.mock as um
        from src.agent import visual as visual_mod

        seen = {"on_calls": [], "evaluate_calls": []}

        class MockPg:
            def set_viewport_size(self, size): pass
            def goto(self, url, **kw): pass
            def screenshot(self, path, **kw):
                Path(path).touch()
            def route(self, p, h): pass
            def click(self, s): pass
            def wait_for_timeout(self, ms): pass
            def on(self, event, handler):
                seen["on_calls"].append(event)
            def add_init_script(self, script): pass
            def evaluate(self, expr):
                seen["evaluate_calls"].append(expr)
                return "complete"

        class P:
            class chromium:
                @staticmethod
                def launch():
                    class B:
                        @staticmethod
                        def new_context():
                            class Ctx:
                                @staticmethod
                                def new_page():
                                    return MockPg()
                            return Ctx()
                        def close(self): pass
                    return B()

            def __enter__(self): return self
            def __exit__(self, *a): pass

        with um.patch("src.agent.visual.sync_playwright", lambda: P()):
            visual_mod.capture_screenshots(
                "http://localhost:3000",
                screenshot_dir=tmp_path,
                routes=[{"path": "/", "actions": [], "reason": "home"}],
            )

        assert "pageerror" in seen["on_calls"], (
            f"Expected page.on('pageerror', ...) to be registered for hydration "
            f"diagnostics, but only registered: {seen['on_calls']}"
        )
        assert any("readyState" in e for e in seen["evaluate_calls"]), (
            f"Expected page.evaluate(...) to check document.readyState for "
            f"hydration diagnostics, but evaluate calls were: {seen['evaluate_calls']}"
        )


class TestUploadScreenshots:
    def test_uploads_all_pairs(self, tmp_path, monkeypatch):
        put_calls = []

        class MockS3Client:
            def put_object(self, **kw):
                put_calls.append(kw)

        monkeypatch.setattr("boto3.client", lambda *a, **kw: MockS3Client())

        from src.agent.visual import upload_screenshots

        png1 = tmp_path / "a.png"
        png1.write_bytes(b"png1-data")
        png2 = tmp_path / "b.png"
        png2.write_bytes(b"png2-data")

        pairs = upload_screenshots("my-bucket", "42", [(png1, "Mobile XS"), (png2, "Desktop")])

        assert len(pairs) == 2
        assert len(put_calls) == 2
        for (url, label), call in zip(pairs, put_calls):
            assert call["Bucket"] == "my-bucket"
            assert call["ContentType"] == "image/png"
            assert call["Key"].startswith("screenshots/42/")
            assert call["Key"].endswith(".png")
            assert label in ("Mobile XS", "Desktop")
            assert "my-bucket.s3.amazonaws.com" in url

    def test_upload_failure_skips(self, tmp_path, monkeypatch):
        class FailingS3Client:
            def put_object(self, **kw):
                raise Exception("S3 error")

        monkeypatch.setattr("boto3.client", lambda *a, **kw: FailingS3Client())

        from src.agent.visual import upload_screenshots

        png = tmp_path / "a.png"
        png.write_bytes(b"data")

        pairs = upload_screenshots("my-bucket", "42", [(png, "Mobile XS")])
        assert pairs == []
