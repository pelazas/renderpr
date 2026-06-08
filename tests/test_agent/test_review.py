
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


@ pytest.fixture(autouse=True)
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


class TestBuildContent:
    def test_contains_diff_text(self, tmp_path):
        from src.agent.review import _build_content

        result = _build_content("my diff content", [])
        assert any("my diff content" in p["text"] for p in result if p["type"] == "text")

    def test_includes_screenshots_when_provided(self, tmp_path):
        from src.agent.review import _build_content

        png = tmp_path / "test.png"
        png.write_bytes(b"fake-png-data")

        result = _build_content("diff", [png])
        texts = [p for p in result if p["type"] == "text"]
        images = [p for p in result if p["type"] == "image_url"]
        assert any("## Screenshots" in p["text"] for p in texts)
        assert len(images) == 1

    def test_skip_screenshots_when_empty(self, tmp_path):
        from src.agent.review import _build_content

        result = _build_content("diff", [])
        images = [p for p in result if p["type"] == "image_url"]
        texts_joined = " ".join(p.get("text", "") for p in result if p["type"] == "text")
        assert len(images) == 0
        assert "## Screenshots" not in texts_joined

    def test_skips_unreadable_file(self, tmp_path):
        from src.agent.review import _build_content

        missing = tmp_path / "missing.png"
        result = _build_content("diff", [missing])
        images = [p for p in result if p["type"] == "image_url"]
        assert len(images) == 0


class TestGuessViewportLabel:
    def test_desktop_xl_matched_before_desktop(self):
        from src.agent.review import _guess_viewport_label
        path = type("Path", (), {"stem": "Desktop XL-20250101T120000"})()
        assert _guess_viewport_label(path) == "Viewport: Desktop XL"

    def test_regular_desktop_still_matches(self):
        from src.agent.review import _guess_viewport_label
        path = type("Path", (), {"stem": "Desktop-20250101T120000"})()
        assert _guess_viewport_label(path) == "Viewport: Desktop"

    def test_tablet_label(self):
        from src.agent.review import _guess_viewport_label
        path = type("Path", (), {"stem": "Tablet-20250101T120000"})()
        assert _guess_viewport_label(path) == "Viewport: Tablet"

    def test_mobile_xs_label(self):
        from src.agent.review import _guess_viewport_label
        path = type("Path", (), {"stem": "Mobile XS-20250101T120000"})()
        assert _guess_viewport_label(path) == "Viewport: Mobile XS"

    def test_no_match_returns_fallback(self):
        from src.agent.review import _guess_viewport_label
        path = type("Path", (), {"stem": "unknown-viewport"})()
        assert "unknown-viewport" in _guess_viewport_label(path)


SAMPLE_DIFF = "diff --git a/src/page.tsx b/src/page.tsx\n+ new code\n- old code"


def test_run_review_success(tmp_path, mock_httpx_client):
    screenshot = tmp_path / "screenshot.png"
    screenshot.write_text("fake-png-content")

    mock_httpx_client([
        httpx.Response(200, json={
            "choices": [{"message": {"content": "## Review\n\nLooks good."}}],
        }),
    ])

    from src.agent.review import run_review

    result = run_review(
        diff=SAMPLE_DIFF,
        screenshot_paths=[screenshot],
        openrouter_api_key="sk-or-fake",
    )

    assert "## Review" in result
    assert "Looks good." in result


def test_run_review_retry_on_429(tmp_path, mock_httpx_client):
    screenshot = tmp_path / "screenshot.png"
    screenshot.write_text("fake-png-content")

    client = mock_httpx_client([
        httpx.Response(429, json={"error": "Rate limited"}),
        httpx.Response(200, json={
            "choices": [{"message": {"content": "## Review\n\nAll good."}}],
        }),
    ])

    from src.agent.review import run_review

    result = run_review(
        diff=SAMPLE_DIFF,
        screenshot_paths=[screenshot],
        openrouter_api_key="sk-or-fake",
    )

    assert "All good." in result
    assert client.call_count == 2


def test_run_review_4xx_exits(tmp_path, mock_httpx_client):
    screenshot = tmp_path / "screenshot.png"
    screenshot.write_text("fake-png-content")

    mock_httpx_client([
        httpx.Response(401, json={"error": "Unauthorized"}),
    ])

    from src.agent.review import ReviewError, run_review

    with pytest.raises(ReviewError):
        run_review(
            diff=SAMPLE_DIFF,
            screenshot_paths=[screenshot],
            openrouter_api_key="sk-or-fake",
        )


def test_run_review_malformed_response(tmp_path, mock_httpx_client):
    mock_httpx_client([
        httpx.Response(200, json={"unexpected": "structure"}),
    ])

    from src.agent.review import ReviewError, run_review

    with pytest.raises(ReviewError):
        run_review(
            diff=SAMPLE_DIFF,
            screenshot_paths=[],
            openrouter_api_key="sk-or-fake",
        )


def test_run_review_screenshot_read_error(tmp_path, mock_httpx_client):
    missing = tmp_path / "does-not-exist.png"

    mock_httpx_client([
        httpx.Response(200, json={
            "choices": [{"message": {"content": "## Review\n\nNo images sent."}}],
        }),
    ])

    from src.agent.review import run_review

    result = run_review(
        diff=SAMPLE_DIFF,
        screenshot_paths=[missing],
        openrouter_api_key="sk-or-fake",
    )

    assert "No images sent." in result


def test_run_review_empty_screenshots(tmp_path, mock_httpx_client):
    mock_httpx_client([
        httpx.Response(200, json={
            "choices": [{"message": {"content": "## Review\n\nNo screenshots."}}],
        }),
    ])

    from src.agent.review import run_review

    result = run_review(
        diff=SAMPLE_DIFF,
        screenshot_paths=[],
        openrouter_api_key="sk-or-fake",
    )

    assert "No screenshots." in result
