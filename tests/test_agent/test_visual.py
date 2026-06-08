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
    def test_returns_list_of_paths(self, tmp_path):
        from src.agent.visual import capture_screenshots

        result = capture_screenshots(
            "http://localhost:3000",
            screenshot_dir=tmp_path,
        )

        assert len(result) == 4
        assert all(isinstance(p, Path) for p in result)
        assert all(p.exists() for p in result)

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

    def test_screenshots_have_viewport_labels_in_filename(self, tmp_path):
        from src.agent.visual import capture_screenshots

        result = capture_screenshots(
            "http://localhost:3000",
            screenshot_dir=tmp_path,
        )

        filenames = [p.name for p in result]
        assert any("Mobile XS" in n for n in filenames)
        assert any("Tablet" in n for n in filenames)
        assert any("Desktop" in n for n in filenames)
        assert any("Desktop XL" in n for n in filenames)
