
import httpx
import pytest


@pytest.fixture(autouse=True)
def mock_httpx_client(monkeypatch):
    class _MockResponse:
        def __init__(self, status_code, json_data):
            self.status_code = status_code
            self._json = json_data

        def json(self):
            return self._json

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError("error", request=None, response=self)

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

    current = [None]

    def make_client(*a, **kw):
        return current[0]

    def set_responses(responses):
        current[0] = _MockClient(responses)

    monkeypatch.setattr(httpx, "Client", make_client)
    return set_responses


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

    mock_httpx_client([
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


def test_run_review_4xx_exits(tmp_path, mock_httpx_client):
    screenshot = tmp_path / "screenshot.png"
    screenshot.write_text("fake-png-content")

    mock_httpx_client([
        httpx.Response(401, json={"error": "Unauthorized"}),
    ])

    from src.agent.review import run_review

    with pytest.raises(SystemExit):
        run_review(
            diff=SAMPLE_DIFF,
            screenshot_paths=[screenshot],
            openrouter_api_key="sk-or-fake",
        )


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
