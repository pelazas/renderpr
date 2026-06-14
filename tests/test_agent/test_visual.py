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
            self.url: str = ""

        def set_viewport_size(self, size):
            self.viewport_size = size

        def goto(self, url, **kw):
            self.goto_url = url
            self.url = url

        def screenshot(self, path, **kw):
            self.screenshot_path = path
            Path(path).touch()

        def route(self, pattern, handler):
            pass

        def click(self, selector: str, **kw):
            self.actions_called.append(("click", selector))

        def wait_for_selector(self, selector: str, **kw):
            self.actions_called.append(("wait_for_selector", selector, kw))

        def wait_for_function(self, expression: str, **kw):
            self.actions_called.append(("wait_for_function", expression, kw))

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

        def new_context(self, **kwargs):
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

    def test_browser_crash_relaunches_and_retries(self, tmp_path, monkeypatch):
        """A renderer crash mid-capture must relaunch the browser and retry the
        route, returning the recovered screenshots instead of aborting the run."""
        import src.agent.visual as visual_mod

        calls = {"n": 0}

        def flaky_route(page, base_url, route, screenshot_dir, login_signals=None, changed_selector=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("Target crashed")
            return [(screenshot_dir / "recovered.png", f"{route['path']} - recovered")]

        monkeypatch.setattr(visual_mod, "_screenshot_route", flaky_route)

        result = visual_mod.capture_screenshots(
            "http://localhost:3000",
            screenshot_dir=tmp_path,
            routes=[{"path": "/", "actions": []}],
        )

        assert calls["n"] == 2  # crashed once, retried after relaunch
        assert len(result) == 1

    def test_total_failure_exits_nonzero(self, tmp_path, monkeypatch):
        """If every route crashes through all retries, capture exits non-zero so
        the review is reported as failed rather than silently empty."""
        import src.agent.visual as visual_mod

        def always_crash(page, base_url, route, screenshot_dir, login_signals=None, changed_selector=None):
            raise RuntimeError("Target crashed")

        monkeypatch.setattr(visual_mod, "_screenshot_route", always_crash)

        with pytest.raises(SystemExit):
            visual_mod.capture_screenshots(
                "http://localhost:3000",
                screenshot_dir=tmp_path,
                routes=[{"path": "/", "actions": []}],
            )

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

    def test_wait_only_actions_run_before_baseline_screenshot(self, tmp_path, monkeypatch):
        from src.agent import visual as visual_mod

        events = []

        class WaitPage:
            def set_viewport_size(self, size): pass
            def goto(self, url, **kw): pass
            def wait_for_timeout(self, ms): events.append(("wait", ms))
            def evaluate(self, expr): return "complete"
            def screenshot(self, path, **kw):
                events.append(("screenshot", Path(path).name))
                Path(path).touch()
            def click(self, selector, **kw): pass
            def wait_for_selector(self, selector, **kw): pass
            def wait_for_function(self, expression, **kw): pass
            def on(self, event, handler): pass

        class Context:
            def route(self, pattern, handler): pass
            def new_page(self): return WaitPage()

        class Browser:
            def new_context(self): return Context()
            def close(self): pass

        class Playwright:
            class chromium:
                @staticmethod
                def launch(): return Browser()

        class SyncPlaywright:
            def __enter__(self): return Playwright()
            def __exit__(self, *a): pass

        monkeypatch.setattr("src.agent.visual.sync_playwright", lambda: SyncPlaywright())

        visual_mod.capture_screenshots(
            "http://localhost:3000",
            screenshot_dir=tmp_path,
            routes=[{
                "path": "/dashboard",
                "actions": [{"type": "wait", "ms": 500}],
                "reason": "test",
            }],
        )

        first_custom_wait = events.index(("wait", 500))
        first_screenshot = next(i for i, event in enumerate(events) if event[0] == "screenshot")
        assert first_custom_wait < first_screenshot

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

    def test_click_actions_add_interacted_screenshots_after_baseline(self, tmp_path):
        from src.agent.visual import capture_screenshots

        routes = [{"path": "/users", "actions": [{"type": "click", "selector": "text=Open filters"}], "reason": "test"}]

        result = capture_screenshots(
            "http://localhost:3000",
            screenshot_dir=tmp_path,
            routes=routes,
        )

        labels = [label for _, label in result]
        assert len(result) == 8
        assert labels.count("Mobile XS - /users") == 1
        assert labels.count("Mobile XS - /users after interaction") == 1

    def test_failed_click_action_still_keeps_baseline_screenshots(self, tmp_path, monkeypatch):
        from src.agent import visual as visual_mod

        class FailingPage:
            def set_viewport_size(self, size): pass
            def goto(self, url, **kw): pass
            def wait_for_timeout(self, ms): pass
            def wait_for_function(self, expression, **kw): pass
            def wait_for_selector(self, selector, **kw):
                raise RuntimeError("missing selector")
            def evaluate(self, expr): return "complete"
            def screenshot(self, path, **kw): Path(path).touch()
            def click(self, selector, **kw): pass
            def on(self, event, handler): pass

        class Context:
            def route(self, pattern, handler): pass
            def new_page(self): return FailingPage()

        class Browser:
            def new_context(self): return Context()
            def close(self): pass

        class Playwright:
            class chromium:
                @staticmethod
                def launch(): return Browser()

        class SyncPlaywright:
            def __enter__(self): return Playwright()
            def __exit__(self, *a): pass

        monkeypatch.setattr("src.agent.visual.sync_playwright", lambda: SyncPlaywright())

        result = visual_mod.capture_screenshots(
            "http://localhost:3000",
            screenshot_dir=tmp_path,
            routes=[{"path": "/users", "actions": [{"type": "click", "selector": "text=Missing"}], "reason": "test"}],
        )

        labels = [label for _, label in result]
        assert len(result) == 4
        assert all("after interaction" not in label for label in labels)





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

    def test_unmocked_api_path_gets_empty_fallback(self, tmp_path, monkeypatch):
        from src.agent import visual as visual_mod

        captured = {"handler": None, "fulfilled": None, "continued": False}

        class MockRequest:
            url = "http://127.0.0.1:3000/api/orders"
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

        # No mocks at all — the catch-all must still register and stub /api.
        visual_mod.capture_screenshots(
            "http://localhost:3000",
            screenshot_dir=tmp_path,
            routes=[{"path": "/", "actions": [], "reason": "test"}],
        )

        assert captured["handler"] is not None
        captured["handler"](MockRoute())

        assert captured["continued"] is False
        assert captured["fulfilled"]["status"] == 200
        assert captured["fulfilled"]["body"] == "[]"
        assert captured["fulfilled"]["headers"]["x-renderpr-unmocked"] == "/api/orders"

    def test_changed_api_path_bypasses_catch_all(self, tmp_path, monkeypatch):
        from src.agent import visual as visual_mod

        captured = {"handler": None, "fulfilled": None, "continued": False}

        class MockRequest:
            url = "http://127.0.0.1:3000/api/orders"
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

        # /api/orders is a PR-changed route, so the catch-all must let it reach
        # the real handler instead of stubbing it with [].
        visual_mod.capture_screenshots(
            "http://localhost:3000",
            screenshot_dir=tmp_path,
            routes=[{"path": "/", "actions": [], "reason": "test"}],
            changed_paths={"/api/orders"},
        )

        assert captured["handler"] is not None
        captured["handler"](MockRoute())

        assert captured["continued"] is True
        assert captured["fulfilled"] is None

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
                                def route(pattern, handler): pass
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


class TestLoginWallDetection:
    def test_url_is_login_wall(self):
        from src.agent.visual import _url_is_login_wall

        assert _url_is_login_wall("http://localhost:3000/login?callbackUrl=/x")
        assert _url_is_login_wall("http://localhost:3000/sign-in")
        assert not _url_is_login_wall("http://localhost:3000/")
        assert not _url_is_login_wall("http://localhost:3000/dashboard")

    def test_login_wall_recorded_in_signals(self, tmp_path):
        from src.agent.visual import capture_screenshots

        signals: list[dict] = []
        capture_screenshots(
            "http://localhost:3000",
            screenshot_dir=tmp_path,
            routes=[{"path": "/login", "actions": [], "reason": "x"}],
            login_signals=signals,
        )
        assert signals and signals[0]["path"] == "/login"

    def test_normal_route_not_flagged(self, tmp_path):
        from src.agent.visual import capture_screenshots

        signals: list[dict] = []
        capture_screenshots(
            "http://localhost:3000",
            screenshot_dir=tmp_path,
            routes=[{"path": "/", "actions": [], "reason": "home"}],
            login_signals=signals,
        )
        assert signals == []


class TestChangedRegionClip:
    def _page(self, count, box, page_height=2000):
        class Loc:
            def count(self): return count
            def bounding_box(self): return box

        class Pg:
            def locator(self, sel): return Loc()
            def evaluate(self, expr): return page_height

        return Pg()

    def test_clip_for_small_element(self):
        from src.agent.visual import _changed_region_clip
        box = {"x": 0, "y": 0, "width": 1280, "height": 60}  # navbar
        clip = _changed_region_clip(self._page(1, box), "nav", 1280, 800)
        assert clip == {"x": 0, "y": 0, "width": 1280, "height": 108}  # +24 top clamped, +bottom pad

    def test_clip_pads_and_clamps_inner_element(self):
        from src.agent.visual import _changed_region_clip
        box = {"x": 100, "y": 300, "width": 200, "height": 50}
        clip = _changed_region_clip(self._page(1, box), ".card", 1280, 800)
        assert clip == {"x": 76, "y": 276, "width": 248, "height": 98}

    def test_none_when_multiple_matches(self):
        from src.agent.visual import _changed_region_clip
        box = {"x": 0, "y": 0, "width": 100, "height": 50}
        assert _changed_region_clip(self._page(2, box), "div", 1280, 800) is None

    def test_none_when_element_covers_most_of_viewport(self):
        from src.agent.visual import _changed_region_clip
        box = {"x": 0, "y": 0, "width": 1280, "height": 700}  # ~0.87 of viewport
        assert _changed_region_clip(self._page(1, box), "main", 1280, 800) is None

    def test_none_when_no_box(self):
        from src.agent.visual import _changed_region_clip
        assert _changed_region_clip(self._page(1, None), "nav", 1280, 800) is None


class TestSaveScreenshotClip:
    def _page(self, captured):
        class Pg:
            def screenshot(self, path, **kw):
                captured.update(kw)
                Path(path).touch()
        return Pg()

    def test_uses_clip_with_clean_label(self, tmp_path):
        from src.agent.visual import _save_screenshot
        captured = {}
        res = _save_screenshot(self._page(captured), tmp_path, "/", "home", 1280, "", clip={"x": 0, "y": 0, "width": 100, "height": 50})
        assert "clip" in captured and "full_page" not in captured
        # Label must stay clean "Viewport - Route" so review reference-matching works.
        assert res is not None and res[1] == "Desktop - /"

    def test_full_page_without_clip(self, tmp_path):
        from src.agent.visual import _save_screenshot
        captured = {}
        res = _save_screenshot(self._page(captured), tmp_path, "/", "home", 1280, "")
        assert captured.get("full_page") is True
        assert res is not None and "(changed region)" not in res[1]
