import json
import logging

import pytest
from pytest import MonkeyPatch

from src.agent.main import _clone_repo, _fetch_diff, _fetch_secrets, _get_installation_token, _post_comment, _start_dev_server, run


def _mock_all_deps(monkeypatch, caplog=None):
    monkeypatch.setattr("src.agent.main._fetch_secrets", lambda: {"app_id": "1", "private_key": "k", "openrouter_api_key": "o"})
    monkeypatch.setattr("src.agent.main._get_installation_token", lambda *a, **kw: "fake-token")
    monkeypatch.setattr("src.agent.main._clone_repo", lambda *a, **kw: None)
    monkeypatch.setattr("src.agent.main._start_dev_server", lambda: None)
    monkeypatch.setattr("src.agent.main._fetch_diff", lambda *a, **kw: "")
    monkeypatch.setattr("src.agent.main._capture_screenshots", lambda: ([], []))
    monkeypatch.setattr("src.agent.review.run_review", lambda *a, **kw: "## Review\n\nLooks good.")
    monkeypatch.setattr("src.agent.main._post_comment", lambda *a, **kw: None)


def test_run_logs_env_vars(caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    monkeypatch.setenv("INSTALLATION_ID", "999")
    monkeypatch.setenv("REPO_FULL_NAME", "test-owner/test-repo")
    monkeypatch.setenv("PR_NUMBER", "42")
    _mock_all_deps(monkeypatch)

    run()

    assert "Installation ID: 999" in caplog.text
    assert "Repository: test-owner/test-repo" in caplog.text
    assert "PR Number: 42" in caplog.text
    assert "Dev server ready. Proceeding to review..." in caplog.text
    assert "Fetched diff for PR #42" in caplog.text
    assert "Captured 0 screenshots" in caplog.text
    assert "RenderPR agent finished" in caplog.text


def test_run_defaults_when_missing_env(caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    monkeypatch.delenv("INSTALLATION_ID", raising=False)
    monkeypatch.delenv("REPO_FULL_NAME", raising=False)
    monkeypatch.delenv("PR_NUMBER", raising=False)
    _mock_all_deps(monkeypatch)

    run()

    assert "Installation ID: unknown" in caplog.text
    assert "Repository: unknown" in caplog.text
    assert "PR Number: unknown" in caplog.text
    assert "Fetched diff for PR #unknown" in caplog.text
    assert "Captured 0 screenshots" in caplog.text
    assert "RenderPR agent finished" in caplog.text


def _mock_client(response):
    def _respond(*a, **kw):
        return response(*a, **kw) if callable(response) else response
    class MockClient:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
        def post(self, *a, **kw):
            return _respond(*a, **kw)
        def get(self, *a, **kw):
            return _respond(*a, **kw)
    return MockClient()


class TestGetInstallationToken:
    def test_token_success(self, monkeypatch: MonkeyPatch):
        monkeypatch.setattr(
            "src.agent.main.jwt.encode",
            lambda *a, **kw: "fake-jwt",
        )
        import httpx
        mock_resp = httpx.Response(201, json={"token": "ghs_fake-installation-token"})
        monkeypatch.setattr(httpx, "Client", lambda *a, **kw: _mock_client(mock_resp))

        token = _get_installation_token(
            installation_id="999",
            app_id="123456",
            private_key="fake-key",
        )

        assert token == "ghs_fake-installation-token"

    def test_token_4xx_exits(self, monkeypatch: MonkeyPatch):
        monkeypatch.setattr(
            "src.agent.main.jwt.encode",
            lambda *a, **kw: "fake-jwt",
        )
        import httpx
        mock_resp = httpx.Response(401, json={"message": "Bad credentials"})
        monkeypatch.setattr(httpx, "Client", lambda *a, **kw: _mock_client(mock_resp))

        with pytest.raises(SystemExit):
            _get_installation_token(
                installation_id="999",
                app_id="123456",
                private_key="fake-key",
            )

    def test_token_5xx_retry_once(self, monkeypatch: MonkeyPatch):
        monkeypatch.setattr(
            "src.agent.main.jwt.encode",
            lambda *a, **kw: "fake-jwt",
        )
        import httpx
        call_count = [0]
        responses = [
            httpx.Response(500, json={"message": "Server error"}),
            httpx.Response(201, json={"token": "ghs_fake-token"}),
        ]

        def mock_post(self, *a, **kw):
            idx = call_count[0]
            call_count[0] += 1
            return responses[idx]

        monkeypatch.setattr(httpx, "Client", lambda *a, **kw: _mock_client(mock_post))

        token = _get_installation_token(
            installation_id="999",
            app_id="123456",
            private_key="fake-key",
        )

        assert token == "ghs_fake-token"
        assert call_count[0] == 2


class TestCloneRepo:
    def test_clone_success(self, monkeypatch: MonkeyPatch):
        calls = []

        def mock_run(cmd, **kw):
            calls.append(cmd)
            import subprocess
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr("subprocess.run", mock_run)

        _clone_repo(
            repo_full_name="test-owner/test-repo",
            pr_number="42",
            token="ghs_fake-token",
        )

        assert len(calls) == 3
        assert calls[0][:2] == ["git", "clone"]
        assert "test-owner/test-repo.git" in calls[0][2]
        assert "x-access-token:ghs_fake-token" in calls[0][2]
        assert calls[1] == ["git", "-C", "/app/repo", "fetch", "origin", "pull/42/head:review-pr"]
        assert calls[2] == ["git", "-C", "/app/repo", "checkout", "review-pr"]

    def test_clone_failure_retry_then_exit(self, monkeypatch: MonkeyPatch):
        attempt = [0]

        def mock_run(cmd, **kw):
            attempt[0] += 1
            raise Exception("git error")

        monkeypatch.setattr("subprocess.run", mock_run)

        with pytest.raises(SystemExit):
            _clone_repo(
                repo_full_name="test-owner/test-repo",
                pr_number="42",
                token="ghs_fake-token",
            )

        from src.agent.config import RETRY_MAX_ATTEMPTS
        assert attempt[0] == RETRY_MAX_ATTEMPTS


class TestStartDevServer:
    def test_no_package_json(self, monkeypatch: MonkeyPatch):
        monkeypatch.setattr("os.path.exists", lambda p: False)

        with pytest.raises(SystemExit):
            _start_dev_server()

    def test_npm_ci_fails(self, monkeypatch: MonkeyPatch):
        monkeypatch.setattr("os.path.exists", lambda p: True)

        def mock_run(cmd, **kw):
            raise Exception("npm ci error")

        monkeypatch.setattr("subprocess.run", mock_run)

        with pytest.raises(SystemExit):
            _start_dev_server()

    def test_dev_server_ready_on_first_poll(self, monkeypatch: MonkeyPatch):
        monkeypatch.setattr("os.path.exists", lambda p: True)
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: None)

        import subprocess
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: type("Proc", (), {"kill": lambda self: None, "pid": 123})())

        import httpx
        mock_resp = httpx.Response(200)
        monkeypatch.setattr(httpx, "Client", lambda *a, **kw: _mock_client(mock_resp))

        _start_dev_server()

    def test_dev_server_timeout(self, monkeypatch: MonkeyPatch):
        monkeypatch.setattr("src.agent.main.DEV_SERVER_START_TIMEOUT", 1)
        monkeypatch.setattr("src.agent.main.DEV_SERVER_POLL_INTERVAL", 0.1)

        monkeypatch.setattr("os.path.exists", lambda p: True)
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: None)

        import subprocess
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: type("Proc", (), {"kill": lambda self: None, "pid": 123})())

        import httpx

        def fail_get(*a, **kw):
            raise httpx.ConnectError("Connection refused")

        monkeypatch.setattr(httpx, "Client", lambda *a, **kw: _mock_client(fail_get))

        with pytest.raises(SystemExit):
            _start_dev_server()


class TestFetchSecrets:
    def test_fetch_secrets_success(self, monkeypatch: MonkeyPatch):
        monkeypatch.setenv("GITHUB_PARAM_NAME", "/renderpr/github-app")
        monkeypatch.setenv("OPENROUTER_PARAM_NAME", "/renderpr/openrouter")

        def mock_get_parameter(self, **kw):
            name = kw["Name"]
            if name == "/renderpr/github-app":
                return {
                    "Parameter": {
                        "Value": json.dumps({
                            "app_id": "123456",
                            "private_key": "-----BEGIN RSA PRIVATE KEY-----\nFAKE\n-----END RSA PRIVATE KEY-----",
                        })
                    }
                }
            if name == "/renderpr/openrouter":
                return {
                    "Parameter": {
                        "Value": "sk-or-fake",
                    }
                }
            raise ValueError(f"Unexpected param: {name}")

        import boto3
        monkeypatch.setattr(boto3, "client", lambda *a, **kw: type("SSMClient", (), {"get_parameter": mock_get_parameter})())

        result = _fetch_secrets()

        assert result["app_id"] == "123456"
        assert "FAKE" in result["private_key"]
        assert result["openrouter_api_key"] == "sk-or-fake"

    def test_fetch_secrets_missing_github_env(self, monkeypatch: MonkeyPatch):
        monkeypatch.delenv("GITHUB_PARAM_NAME", raising=False)
        monkeypatch.setenv("OPENROUTER_PARAM_NAME", "/renderpr/openrouter")

        with pytest.raises(SystemExit):
            _fetch_secrets()

    def test_fetch_secrets_ssm_error(self, monkeypatch: MonkeyPatch):
        monkeypatch.setenv("GITHUB_PARAM_NAME", "/renderpr/github-app")
        monkeypatch.setenv("OPENROUTER_PARAM_NAME", "/renderpr/openrouter")

        import boto3
        def mock_get_parameter(self, **kw):
            raise Exception("SSM access denied")

        monkeypatch.setattr(boto3, "client", lambda *a, **kw: type("SSMClient", (), {"get_parameter": mock_get_parameter})())

        with pytest.raises(SystemExit):
            _fetch_secrets()


class _MockClient:
    def __init__(self, get_func):
        self._get = get_func
    def __enter__(self):
        return self
    def __exit__(self, *a):
        pass
    def get(self, *a, **kw):
        return self._get(*a, **kw)


class TestFetchDiff:
    def test_fetch_diff_success(self, monkeypatch: MonkeyPatch):
        import httpx
        mock_resp = httpx.Response(200, text="diff --git a/file.tsx b/file.tsx")
        monkeypatch.setattr(httpx, "Client", lambda *a, **kw: _mock_client(mock_resp))

        result = _fetch_diff(token="t", repo_full_name="o/r", pr_number="1")
        assert "diff --git" in result

    def test_fetch_diff_4xx_exits(self, monkeypatch: MonkeyPatch):
        import httpx
        mock_resp = httpx.Response(404, text="Not found")
        monkeypatch.setattr(httpx, "Client", lambda *a, **kw: _mock_client(mock_resp))

        with pytest.raises(SystemExit):
            _fetch_diff(token="t", repo_full_name="o/r", pr_number="1")

    def test_fetch_diff_5xx_retry_then_exit(self, monkeypatch: MonkeyPatch):
        import httpx
        call_count = [0]
        responses = [
            httpx.Response(500, text="Server error"),
            httpx.Response(500, text="Server error"),
            httpx.Response(500, text="Server error"),
        ]

        def mock_get(self, *a, **kw):
            idx = call_count[0]
            call_count[0] += 1
            return responses[idx]

        mock_client = _MockClient(mock_get)
        monkeypatch.setattr(httpx, "Client", lambda *a, **kw: mock_client)

        from src.agent.config import RETRY_MAX_ATTEMPTS
        with pytest.raises(SystemExit):
            _fetch_diff(token="t", repo_full_name="o/r", pr_number="1")
        assert call_count[0] == RETRY_MAX_ATTEMPTS


SAMPLE_DIFF = """diff --git a/src/page.tsx b/src/page.tsx
index abc..def 100644
--- a/src/page.tsx
+++ b/src/page.tsx
@@ -1,5 +1,7 @@
 function Page() {
-  return <div>old</div>;
+  return <div>new</div>;
+  return <div>new2</div>;
 }
diff --git a/src/Header.tsx b/src/Header.tsx
index 123..456 100644
--- a/src/Header.tsx
+++ b/src/Header.tsx
@@ -1,3 +1,5 @@
 function Header() {
+  return <header>new</header>;
+  return <header>new2</header>;
 }
"""

SAMPLE_DIFF_SINGLE_FILE = """diff --git a/src/page.tsx b/src/page.tsx
index abc..def 100644
--- a/src/page.tsx
+++ b/src/page.tsx
@@ -1,5 +1,3 @@
 function Page() {
-  return <div>old</div>;
-  return <div>old2</div>;
 }
"""


class TestParseDiffSummary:
    def test_parses_multiple_files(self):
        from src.agent.main import _parse_diff_summary

        result = _parse_diff_summary(SAMPLE_DIFF)
        assert "src/page.tsx (+2/-1)" in result
        assert "src/Header.tsx (+2/-0)" in result

    def test_parses_single_file(self):
        from src.agent.main import _parse_diff_summary

        result = _parse_diff_summary(SAMPLE_DIFF_SINGLE_FILE)
        assert "src/page.tsx (+0/-2)" in result

    def test_empty_diff_returns_fallback(self):
        from src.agent.main import _parse_diff_summary

        result = _parse_diff_summary("")
        assert result == "(no file changes detected)"

    def test_no_file_changes_returns_fallback(self):
        from src.agent.main import _parse_diff_summary

        result = _parse_diff_summary("no diff content here")
        assert result == "(no file changes detected)"

class TestPostComment:
    def test_post_comment_success(self, monkeypatch):
        import httpx
        mock_resp = httpx.Response(201, json={"id": 1})
        monkeypatch.setattr(httpx, "Client", lambda *a, **kw: _mock_client(mock_resp))

        _post_comment(
            token="fake-token",
            repo_full_name="owner/repo",
            pr_number="42",
            body="## Review\n\nLooks good.",
        )

    def test_post_comment_failure_exits(self, monkeypatch):
        import httpx
        mock_resp = httpx.Response(400, json={"message": "Error"})
        monkeypatch.setattr(httpx, "Client", lambda *a, **kw: _mock_client(mock_resp))

        with pytest.raises(SystemExit):
            _post_comment(
                token="fake-token",
                repo_full_name="owner/repo",
                pr_number="42",
                body="## Review",
            )
