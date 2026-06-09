from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def mock_playwright(monkeypatch):
    class MockPage:
        def __init__(self):
            self.viewport_size = None
            self.goto_url = None
            self.screenshot_path = None

        def set_viewport_size(self, size):
            self.viewport_size = size

        def goto(self, url, **kw):
            self.goto_url = url

        def screenshot(self, path, **kw):
            self.screenshot_path = path
            Path(path).touch()

    class MockContext:
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
        "playwright.sync_api.sync_playwright",
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
        assert any("Mobile XS" in l for l in labels)
        assert any("Tablet" in l for l in labels)
        assert any("Desktop" in l for l in labels)
        assert any("Desktop XL" in l for l in labels)

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
        assert all(" - /" in l or " - /dashboard" in l for l in labels)

    def test_action_click_included_in_label(self, tmp_path):
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
