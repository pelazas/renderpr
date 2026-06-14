import hashlib
import json
import logging
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from src.agent.main import _clone_repo, _fetch_diff, _fetch_secrets, _get_installation_token, _post_comment, _render_progress, _start_dev_server, _update_comment, run
from src.agent.stack import LaunchProfile


_FRONTEND_DIFF = "diff --git a/src/page.tsx b/src/page.tsx\n--- a/src/page.tsx\n+++ b/src/page.tsx\n@@ -1 +1 @@\n-old\n+new"


def _default_profile(package_manager="npm", framework="next", install_command=None):
    return LaunchProfile(
        package_manager=package_manager,
        framework=framework,
        install_command=install_command or ["npm", "ci"],
        dev_command=["npm", "run", "dev"],
        dev_env={"HOST": "0.0.0.0"} if framework == "next" else {},
        default_port=3000,
    )


def _successful_discovery(*a, **kw):
    return {
        "has_frontend": True,
        "package_json_path": "/app/repo/package.json",
        "workspace_root": None,
        "dev_command": "npm run dev",
        "launch_profile": _default_profile(),
        "reason": None,
    }

class _MockCommandServer:
    def __init__(self, **kw):
        pass
    def start(self):
        return
    def wait_for_command(self):
        return {"action": "shutdown"}


def _mock_all_deps(monkeypatch, posted_body=None):
    monkeypatch.setattr("src.agent.main._fetch_secrets", lambda: {"app_id": "1", "private_key": "k", "openrouter_api_key": "o"})
    monkeypatch.setattr("src.agent.main._get_installation_token", lambda *a, **kw: "fake-token")
    monkeypatch.setattr("src.agent.main._clone_repo", lambda *a, **kw: None)
    monkeypatch.setattr("src.agent.main._start_dev_server", lambda *a, **kw: None)
    monkeypatch.setattr("src.agent.main._fetch_diff", lambda *a, **kw: _FRONTEND_DIFF)
    monkeypatch.setattr("src.agent.main._fetch_pr_meta", lambda *a, **kw: {"head_ref": "review-pr", "head_sha": "abc123", "is_fork": False, "base": {"repo": {"full_name": "test-owner/test-repo"}}})
    monkeypatch.setattr("src.agent.main.discover_frontend", _successful_discovery)
    monkeypatch.setattr("src.agent.main.load_repo_secrets", lambda *a, **kw: {})
    monkeypatch.setattr("src.agent.main._capture_screenshots", lambda *a, **kw: ([], [], []))
    monkeypatch.setattr("src.agent.network.get_public_ip", lambda: "54.1.2.3")
    monkeypatch.setattr("src.agent.main.write_dev_origin_allowlist", lambda *a, **kw: [])
    monkeypatch.setattr("src.agent.review.run_review", lambda *a, **kw: "## Review\n\nLooks good.")
    monkeypatch.setattr("src.agent.command_server.CommandServer", _MockCommandServer)
    # The review flow posts a placeholder comment (returns an id), then edits it via
    # _update_comment for each progress stage and the final review. posted_body collects
    # every body delivered to the PR, whether by a fresh post or an in-place edit.
    if posted_body is not None:
        monkeypatch.setattr("src.agent.main._post_comment", lambda *a, body, **kw: (posted_body.append(body), 999)[1])
        monkeypatch.setattr("src.agent.main._update_comment", lambda *a, body, **kw: (posted_body.append(body), True)[1])
    else:
        monkeypatch.setattr("src.agent.main._post_comment", lambda *a, **kw: 999)
        monkeypatch.setattr("src.agent.main._update_comment", lambda *a, **kw: True)
    monkeypatch.setenv("RENDERPR_PUBLIC_IP", "127.0.0.1")


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
    assert "RenderPR agent entering idle loop" in caplog.text


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
    assert "RenderPR agent entering idle loop" in caplog.text


def _mock_process(**attrs):
    proc = type("MockProc", (), {
        "stdout": type("MockStream", (), {"readline": lambda self: ""})(),
        "wait": lambda self, timeout=None: 0,
        "poll": lambda self: None,
        "returncode": 0,
        "kill": lambda self: None,
        "pid": 123,
    })
    for k, v in attrs.items():
        setattr(proc, k, v)
    return proc()


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
        def patch(self, *a, **kw):
            return _respond(*a, **kw)
    return MockClient()


class TestUntrustedSubprocessEnv:
    def test_strips_credentials_and_secret_refs(self, monkeypatch: MonkeyPatch):
        from src.agent.main import _untrusted_subprocess_env

        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("HOME", "/root")
        monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "/v2/creds/abc")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "shhh")
        monkeypatch.setenv("RENDERPR_COMMAND_TOKEN", "tok")
        monkeypatch.setenv("GITHUB_PARAM_NAME", "/renderpr/github-app")

        env = _untrusted_subprocess_env()

        # Benign vars survive so the dev server still boots.
        assert env["PATH"] == "/usr/bin"
        assert env["HOME"] == "/root"
        # Credential pointers and secret references are gone.
        assert "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI" not in env
        assert "AWS_ACCESS_KEY_ID" not in env
        assert "AWS_SECRET_ACCESS_KEY" not in env
        assert "RENDERPR_COMMAND_TOKEN" not in env
        assert "GITHUB_PARAM_NAME" not in env

    def test_overlays_apply_in_order(self, monkeypatch: MonkeyPatch):
        from src.agent.main import _untrusted_subprocess_env

        monkeypatch.setenv("HOST", "127.0.0.1")
        env = _untrusted_subprocess_env({"FOO": "1", "HOST": "x"}, {"HOST": "0.0.0.0"})

        assert env["FOO"] == "1"
        assert env["HOST"] == "0.0.0.0"  # later overlay wins over earlier and ambient


class TestRunnerPrivilegeDrop:
    def _wire(self, monkeypatch, can_drop):
        import subprocess
        import httpx
        from src.agent import main as m

        monkeypatch.setattr(m, "_can_drop_privileges", lambda: can_drop)
        monkeypatch.setattr(m, "NPM_CACHE_ENABLED", False)  # force the install path
        monkeypatch.setattr("os.path.exists", lambda p: True)

        run_calls: list[list] = []
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **kw: (run_calls.append(list(a[0])), type("R", (), {"returncode": 0})())[1],
        )
        popen_calls: list[dict] = []

        def mock_popen(*a, **kw):
            popen_calls.append(kw)
            return _mock_process()

        monkeypatch.setattr(subprocess, "Popen", mock_popen)
        monkeypatch.setattr(httpx, "Client", lambda *a, **kw: _mock_client(httpx.Response(200)))
        return popen_calls, run_calls

    def test_drops_to_runner_when_root(self, monkeypatch):
        from src.agent import main as m

        popen_calls, run_calls = self._wire(monkeypatch, can_drop=True)
        m._start_dev_server(_default_profile())

        install_kw, dev_kw = popen_calls[0], popen_calls[1]
        assert install_kw["user"] == m.RUNNER_UID and install_kw["group"] == m.RUNNER_GID
        assert dev_kw["user"] == m.RUNNER_UID and dev_kw["group"] == m.RUNNER_GID
        # Untrusted children get a writable HOME and never the credential pointers.
        assert dev_kw["env"]["HOME"] == m.RUNNER_HOME
        assert "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI" not in dev_kw["env"]
        # The repo tree is handed to the runner before any untrusted process runs.
        assert any(c[:2] == ["chown", "-R"] for c in run_calls)

    def test_no_drop_when_not_root(self, monkeypatch):
        from src.agent import main as m

        popen_calls, run_calls = self._wire(monkeypatch, can_drop=False)
        m._start_dev_server(_default_profile())

        install_kw, dev_kw = popen_calls[0], popen_calls[1]
        assert install_kw.get("user") is None and install_kw.get("group") is None
        assert dev_kw.get("user") is None and dev_kw.get("group") is None
        assert dev_kw["env"].get("HOME") != m.RUNNER_HOME
        assert not any(c[:2] == ["chown", "-R"] for c in run_calls)


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

    def test_token_request_is_scoped_to_repo_and_min_perms(self, monkeypatch: MonkeyPatch):
        monkeypatch.setattr("src.agent.main.jwt.encode", lambda *a, **kw: "fake-jwt")
        import httpx

        captured: dict = {}

        class CapturingClient:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def post(self, url, headers=None, json=None):
                captured["json"] = json
                return httpx.Response(201, json={"token": "ghs_scoped"})

        monkeypatch.setattr(httpx, "Client", lambda *a, **kw: CapturingClient())

        token = _get_installation_token(
            installation_id="999",
            app_id="123456",
            private_key="fake-key",
            repo_full_name="acme/web",
        )

        assert token == "ghs_scoped"
        assert captured["json"]["repositories"] == ["web"]
        perms = captured["json"]["permissions"]
        assert perms["contents"] == "write"
        assert perms["pull_requests"] == "write"
        assert perms["issues"] == "write"
        assert set(perms) == {"contents", "pull_requests", "issues", "metadata"}

    def test_token_request_omits_repositories_when_unknown(self, monkeypatch: MonkeyPatch):
        monkeypatch.setattr("src.agent.main.jwt.encode", lambda *a, **kw: "fake-jwt")
        import httpx

        captured: dict = {}

        class CapturingClient:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def post(self, url, headers=None, json=None):
                captured["json"] = json
                return httpx.Response(201, json={"token": "ghs_unscoped"})

        monkeypatch.setattr(httpx, "Client", lambda *a, **kw: CapturingClient())

        _get_installation_token(installation_id="999", app_id="1", private_key="k")

        assert "repositories" not in captured["json"]
        assert "permissions" in captured["json"]

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
            _start_dev_server(_default_profile())

    def test_npm_ci_fails(self, monkeypatch: MonkeyPatch):
        monkeypatch.setattr("os.path.exists", lambda p: True)

        def mock_popen(cmd, **kw):
            raise Exception("npm ci error")

        import subprocess
        monkeypatch.setattr(subprocess, "Popen", mock_popen)

        with pytest.raises(SystemExit):
            _start_dev_server(_default_profile())

    def test_dev_server_ready_on_first_poll(self, monkeypatch: MonkeyPatch):
        monkeypatch.setattr("os.path.exists", lambda p: True)

        import subprocess
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: _mock_process())

        import httpx
        mock_resp = httpx.Response(200)
        monkeypatch.setattr(httpx, "Client", lambda *a, **kw: _mock_client(mock_resp))

        _start_dev_server(_default_profile())

    def test_dev_server_binds_all_interfaces_but_polls_localhost(self, monkeypatch: MonkeyPatch):
        monkeypatch.setattr("os.path.exists", lambda p: True)

        popen_calls = []

        def mock_popen(*a, **kw):
            popen_calls.append((a, kw))
            return _mock_process()

        import subprocess
        monkeypatch.setattr(subprocess, "Popen", mock_popen)

        requested_urls = []

        import httpx

        class MockClient:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def get(self, url):
                requested_urls.append(url)
                return httpx.Response(200)

        monkeypatch.setattr(httpx, "Client", MockClient)

        _start_dev_server(_default_profile())

        dev_server_call = popen_calls[1][1]
        assert dev_server_call["env"]["HOST"] == "0.0.0.0"
        assert requested_urls == ["http://localhost:3000/"]

    def test_dev_server_timeout(self, monkeypatch: MonkeyPatch):
        monkeypatch.setattr("src.agent.main.DEV_SERVER_START_TIMEOUT", 1)
        monkeypatch.setattr("src.agent.main.DEV_SERVER_POLL_INTERVAL", 0.1)

        monkeypatch.setattr("os.path.exists", lambda p: True)
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: None)

        import subprocess
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: _mock_process())

        import httpx

        def fail_get(*a, **kw):
            raise httpx.ConnectError("Connection refused")

        monkeypatch.setattr(httpx, "Client", lambda *a, **kw: _mock_client(fail_get))

        with pytest.raises(SystemExit):
            _start_dev_server(_default_profile())

    def test_start_dev_server_with_package_dir(self, monkeypatch):
        monkeypatch.setattr("os.path.exists", lambda p: True)

        import subprocess
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: _mock_process())

        import httpx
        mock_resp = httpx.Response(200)
        monkeypatch.setattr(httpx, "Client", lambda *a, **kw: _mock_client(mock_resp))

        _start_dev_server(
            _default_profile(),
            package_dir="/app/repo/packages/web/package.json",
            install_dir="/app/repo/package.json",
        )


class _PopenSpy:
    def __init__(self, **attrs):
        self.calls: list[dict] = []
        self.returncode = 0
        self.args: list = []
        self.stdout = type("MockStream", (), {"readline": lambda self: ""})()

    def __call__(self, cmd, **kw):
        self.calls.append({"cmd": cmd, "kw": kw})
        self.args = cmd
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return None

    def communicate(self, input=None, timeout=None):
        return ("", "")

    def kill(self):
        pass

    @property
    def pid(self):
        return 123


def _set_up_dev_server_test(monkeypatch, tmp_path, package_json_exists=True, lockfile_exists=True):
    pkg = tmp_path / "package.json"
    if package_json_exists:
        pkg.write_text('{"scripts": {"dev": "next dev"}}')
    if lockfile_exists:
        lock = tmp_path / "package-lock.json"
        lock.write_text('{"lockfileVersion": 3}')

    monkeypatch.setattr("os.path.exists", lambda p: True)
    monkeypatch.setenv("SCREENSHOT_BUCKET", "test-bucket")
    monkeypatch.setattr("src.agent.main._dev_server_url", "http://localhost:3000")
    monkeypatch.setattr("src.agent.main.REPO_DIR", str(tmp_path))
    return pkg


def _make_npm_ci_tarball(tmp_path, dst_path):
    import tarfile
    src = tmp_path / "node_modules_src"
    src.mkdir(exist_ok=True)
    (src / "dummy.txt").write_text("ok")
    with tarfile.open(str(dst_path), "w:gz") as tar:
        tar.add(str(src), arcname="node_modules")


class _MockS3Miss:
    def __init__(self):
        self.upload_called = False

    def head_object(self, Bucket=None, Key=None):
        from botocore.exceptions import ClientError
        # Real S3 surfaces a missing key on head_object as code "404".
        raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "head_object")

    def download_file(self, bucket, key, path):
        pass

    def upload_file(self, *a, **kw):
        self.upload_called = True

    def delete_object(self, Bucket=None, Key=None):
        pass


class _MockS3GenericError:
    def __init__(self):
        self.upload_called = False

    def head_object(self, Bucket=None, Key=None):
        from botocore.exceptions import ClientError
        raise ClientError({"Error": {"Code": "InternalError", "Message": "Oops"}}, "head_object")

    def download_file(self, bucket, key, path):
        pass

    def upload_file(self, *a, **kw):
        self.upload_called = True

    def delete_object(self, Bucket=None, Key=None):
        pass


def _setup_health_check(monkeypatch):
    import httpx
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: _mock_client(httpx.Response(200)))


class TestNpmCache:
    def test_cache_hit_skips_npm_ci(self, monkeypatch, tmp_path):
        _set_up_dev_server_test(monkeypatch, tmp_path)
        monkeypatch.setattr("src.agent.main.NPM_CACHE_ENABLED", True)
        monkeypatch.setattr("src.agent.main._npm_cache_key", lambda *a, **kw: "npm-fakehash")

        # Create a real, valid tarball that the mock will "download"
        real_tarball = tmp_path / "real_tarball.tar.gz"
        _make_npm_ci_tarball(tmp_path, real_tarball)

        class MockS3:
            def __init__(self):
                self.head_called = False
                self.downloaded = False
                self.upload_called = False
            def head_object(self, Bucket=None, Key=None):
                self.head_called = True
                return {"ResponseMetadata": {"HTTPStatusCode": 200}}
            def download_file(self, bucket, key, path):
                import shutil
                shutil.copy(str(real_tarball), str(path))
                self.downloaded = True
            def upload_file(self, *a, **kw):
                self.upload_called = True
            def delete_object(self, Bucket=None, Key=None):
                pass

        mock_s3 = MockS3()
        monkeypatch.setattr("boto3.client", lambda *a, **kw: mock_s3)

        # Mock Popen: only intercept npm commands; let tar run for real
        import subprocess as sp_module
        original_popen = sp_module.Popen
        npm_calls: list[dict] = []

        class PopenPassthrough:
            def __init__(self, cmd, **kw):
                if isinstance(cmd, list) and len(cmd) > 0 and cmd[0] == "npm":
                    npm_calls.append({"cmd": cmd, "kw": kw})
                    self.args = cmd
                    self.stdout = type("MockStream", (), {"readline": lambda self: ""})()
                    self.returncode = 0
                    self._mocked = True
                else:
                    self._real = original_popen(cmd, **kw)
                    self._mocked = False
            def __enter__(self):
                if not self._mocked:
                    return self._real.__enter__()
                return self
            def __exit__(self, *a):
                if not self._mocked:
                    return self._real.__exit__(*a)
                return False
            def wait(self, timeout=None):
                if not self._mocked:
                    return self._real.wait(timeout=timeout)
                return 0
            def poll(self):
                if not self._mocked:
                    return self._real.poll()
                return None
            def communicate(self, input=None, timeout=None):
                if not self._mocked:
                    return self._real.communicate(input, timeout=timeout)
                return ("", "")
            def kill(self):
                if not self._mocked:
                    return self._real.kill()
            @property
            def pid(self):
                if not self._mocked:
                    return self._real.pid
                return 123

        monkeypatch.setattr(sp_module, "Popen", PopenPassthrough)
        _setup_health_check(monkeypatch)

        _start_dev_server(_default_profile())

        npm_ci_calls = [c for c in npm_calls if c["cmd"][:2] == ["npm", "ci"]]
        assert len(npm_ci_calls) == 0, f"npm ci should be skipped, got {npm_ci_calls}"
        assert mock_s3.head_called
        assert mock_s3.downloaded

    def test_cache_miss_runs_npm_ci(self, monkeypatch, tmp_path):
        _set_up_dev_server_test(monkeypatch, tmp_path)
        monkeypatch.setattr("src.agent.main.NPM_CACHE_ENABLED", True)
        monkeypatch.setattr("src.agent.main._npm_cache_key", lambda *a, **kw: "npm-fakehash")
        monkeypatch.setattr("boto3.client", lambda *a, **kw: _MockS3Miss())

        spy = _PopenSpy()
        import subprocess
        monkeypatch.setattr(subprocess, "Popen", spy)
        _setup_health_check(monkeypatch)

        _start_dev_server(_default_profile())

        npm_ci_calls = [c for c in spy.calls if c["cmd"][:2] == ["npm", "ci"]]
        assert len(npm_ci_calls) == 1

    def test_cache_generic_error_falls_back_to_npm_ci(self, monkeypatch, tmp_path):
        _set_up_dev_server_test(monkeypatch, tmp_path)
        monkeypatch.setattr("src.agent.main.NPM_CACHE_ENABLED", True)
        monkeypatch.setattr("src.agent.main._npm_cache_key", lambda *a, **kw: "npm-fakehash")
        monkeypatch.setattr("boto3.client", lambda *a, **kw: _MockS3GenericError())

        spy = _PopenSpy()
        import subprocess
        monkeypatch.setattr(subprocess, "Popen", spy)
        _setup_health_check(monkeypatch)

        _start_dev_server(_default_profile())

        npm_ci_calls = [c for c in spy.calls if c["cmd"][:2] == ["npm", "ci"]]
        assert len(npm_ci_calls) == 1

    def test_no_lockfile_no_s3_calls(self, monkeypatch, tmp_path):
        _set_up_dev_server_test(monkeypatch, tmp_path, lockfile_exists=False)
        monkeypatch.setattr("src.agent.main.NPM_CACHE_ENABLED", True)
        boto3_calls = []
        monkeypatch.setattr("boto3.client", lambda *a, **kw: boto3_calls.append(1) or _MockS3Miss())

        spy = _PopenSpy()
        import subprocess
        monkeypatch.setattr(subprocess, "Popen", spy)
        _setup_health_check(monkeypatch)

        _start_dev_server(_default_profile())

        assert len(boto3_calls) == 0, "Should not call S3 without a lockfile"
        npm_ci_calls = [c for c in spy.calls if c["cmd"][:2] == ["npm", "ci"]]
        assert len(npm_ci_calls) == 1

    def test_cache_disabled_runs_npm_ci(self, monkeypatch, tmp_path):
        _set_up_dev_server_test(monkeypatch, tmp_path)
        monkeypatch.setattr("src.agent.main.NPM_CACHE_ENABLED", False)
        boto3_calls = []
        monkeypatch.setattr("boto3.client", lambda *a, **kw: boto3_calls.append(1) or _MockS3Miss())

        spy = _PopenSpy()
        import subprocess
        monkeypatch.setattr(subprocess, "Popen", spy)
        _setup_health_check(monkeypatch)

        _start_dev_server(_default_profile())

        assert len(boto3_calls) == 0
        npm_ci_calls = [c for c in spy.calls if c["cmd"][:2] == ["npm", "ci"]]
        assert len(npm_ci_calls) == 1


class TestNpmCacheKey:
    def test_returns_hash_of_lockfile(self, tmp_path):
        from src.agent.main import _npm_cache_key
        lockfile = tmp_path / "package-lock.json"
        lockfile.write_text("test content")
        result = _npm_cache_key(tmp_path)
        expected = "npm-" + hashlib.sha256(b"test content").hexdigest()
        assert result == expected

    def test_returns_none_if_no_lockfile(self, tmp_path):
        from src.agent.main import _npm_cache_key
        result = _npm_cache_key(tmp_path)
        assert result is None

    def test_keys_on_the_pnpm_lockfile(self, tmp_path):
        from src.agent.main import _npm_cache_key
        (tmp_path / "pnpm-lock.yaml").write_text("test content")
        result = _npm_cache_key(tmp_path, "pnpm")
        expected = "pnpm-" + hashlib.sha256(b"test content").hexdigest()
        assert result == expected

    def test_returns_none_when_pm_lockfile_absent(self, tmp_path):
        from src.agent.main import _npm_cache_key
        (tmp_path / "package-lock.json").write_text("x")
        # Asking for pnpm's key when only npm's lockfile exists -> no key.
        assert _npm_cache_key(tmp_path, "pnpm") is None


class TestNpmCacheStore:
    def test_stores_tarball_in_s3(self, monkeypatch, tmp_path):
        from src.agent.main import _try_npm_cache_store
        install_cwd = tmp_path / "proj"
        install_cwd.mkdir()
        (install_cwd / "node_modules").mkdir()
        (install_cwd / "node_modules" / "pkg.json").write_text("{}")
        cache_key = "abcd1234"

        upload_calls = []

        class MockS3:
            def upload_file(self, file_path, bucket, key):
                upload_calls.append({"file_path": str(file_path), "bucket": bucket, "key": key})

        monkeypatch.setattr("boto3.client", lambda *a, **kw: MockS3())
        monkeypatch.setenv("SCREENSHOT_BUCKET", "my-bucket")

        _try_npm_cache_store(install_cwd, cache_key)

        assert len(upload_calls) == 1
        call = upload_calls[0]
        assert call["bucket"] == "my-bucket"
        assert call["key"] == "npm-cache/abcd1234.tar.gz"
        assert "node_modules" not in Path(call["file_path"]).name
        # Tarball should exist during the upload (may be cleaned up after)

    def test_no_upload_without_cache_key(self, monkeypatch, tmp_path):
        from src.agent.main import _try_npm_cache_store
        install_cwd = tmp_path / "proj"
        install_cwd.mkdir()

        upload_calls = []

        class MockS3:
            def upload_file(self, *a, **kw):
                upload_calls.append(1)

        monkeypatch.setattr("boto3.client", lambda *a, **kw: MockS3())
        monkeypatch.setenv("SCREENSHOT_BUCKET", "my-bucket")

        _try_npm_cache_store(install_cwd, None)
        assert len(upload_calls) == 0

    def test_upload_failure_does_not_raise(self, monkeypatch, tmp_path):
        from src.agent.main import _try_npm_cache_store
        install_cwd = tmp_path / "proj"
        install_cwd.mkdir()
        (install_cwd / "node_modules").mkdir()

        class FailingS3:
            def upload_file(self, *a, **kw):
                raise Exception("S3 down")

        monkeypatch.setattr("boto3.client", lambda *a, **kw: FailingS3())
        monkeypatch.setenv("SCREENSHOT_BUCKET", "my-bucket")

        _try_npm_cache_store(install_cwd, "somekey")

    def test_corrupted_tarball_triggers_delete_from_s3(self, monkeypatch, tmp_path):
        from src.agent.main import _try_npm_cache_restore
        install_cwd = tmp_path / "proj"
        install_cwd.mkdir()
        lockfile = install_cwd / "package-lock.json"
        lockfile.write_text("dummy")

        deletes = []

        class MockS3:
            def __init__(self):
                self.tarball = tmp_path / "bad.tar.gz"
                self.tarball.write_bytes(b"not a real tarball")

            def head_object(self, Bucket=None, Key=None):
                return {"ResponseMetadata": {"HTTPStatusCode": 200}}

            def download_file(self, bucket, key, path):
                Path(path).write_bytes(b"not a real tarball")

            def delete_object(self, Bucket=None, Key=None):
                deletes.append({"Bucket": Bucket, "Key": Key})

        s3 = MockS3()
        monkeypatch.setattr("boto3.client", lambda *a, **kw: s3)
        monkeypatch.setenv("SCREENSHOT_BUCKET", "my-bucket")

        result = _try_npm_cache_restore(install_cwd, "abc123", s3)

        assert result is False
        assert len(deletes) == 1
        assert deletes[0]["Key"] == "npm-cache/abc123.tar.gz"


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

class TestBuildScreenshotGrid:
    def test_empty_pairs_returns_empty_string(self):
        from src.agent.main import _build_screenshot_grid
        assert _build_screenshot_grid([]) == ""

    def test_returns_table_with_images(self):
        from src.agent.main import _build_screenshot_grid
        pairs = [
            ("https://bucket.s3.amazonaws.com/mobile.png", "Mobile XS"),
            ("https://bucket.s3.amazonaws.com/tablet.png", "Tablet"),
        ]
        result = _build_screenshot_grid(pairs)
        assert "<table>" in result
        assert "<img" in result
        assert "mobile.png" in result
        assert "tablet.png" in result
        assert "Mobile XS" in result
        assert "Tablet" in result

    def test_4_urls_creates_2_rows(self):
        from src.agent.main import _build_screenshot_grid
        pairs = [
            ("url1", "Mobile XS"),
            ("url2", "Tablet"),
            ("url3", "Desktop"),
            ("url4", "Desktop XL"),
        ]
        result = _build_screenshot_grid(pairs)
        assert result.count("<tr>") == 2
        assert result.count("<td>") == 4

    def test_run_posts_review_directly(self, monkeypatch):
        posted = []
        _mock_all_deps(monkeypatch, posted_body=posted)
        run()

        # First a placeholder is posted, then the same comment is edited into the review.
        assert "is reviewing this PR" in posted[0]
        assert any("## Review" in body for body in posted)


class TestCaptureScreenshotsMockWiring:
    def test_mocks_passed_to_capture_screenshots(self, monkeypatch):

        captured_kwargs = {}
        def fake_infer_routes(diff, tree, key, framework="next"):
            return (
                [{"path": "/", "reason": "test", "actions": []}],
                {"api.example.com": {"/api/users": {"body": {"ok": True}}}},
            )

        def fake_capture(url, screenshot_dir=None, routes=None, mocks=None, **kw):
            captured_kwargs["mocks"] = mocks
            return [("/tmp/test.png", "Desktop - /")]

        monkeypatch.setattr("src.agent.routes.infer_routes", fake_infer_routes)
        monkeypatch.setattr("src.agent.visual.capture_screenshots", fake_capture)
        monkeypatch.setattr("src.agent.visual.upload_screenshots", lambda *a, **kw: [])
        monkeypatch.setattr("src.agent.main._dev_server_url", "http://localhost:3000")
        monkeypatch.setattr("src.agent.main.REPO_DIR", "/tmp")

        from src.agent.main import _capture_screenshots

        _capture_screenshots("diff content", {"openrouter_api_key": "sk-or-fake"})

        assert captured_kwargs.get("mocks") == {
            "api.example.com": {"/api/users": {"body": {"ok": True}}},
        }

    def test_server_mocks_written_before_capture(self, monkeypatch):
        captured = {"generated": None, "capture_called": False}

        def fake_infer_routes(diff, tree, key, framework="next"):
            return (
                [{"path": "/users", "reason": "test", "actions": []}],
                {"localhost": {"/api/users": {"body": [{"id": 1}], "status": 200}}},
            )

        def fake_write(repo_dir, mocks, framework="next", diff=None):
            captured["generated"] = (str(repo_dir), mocks)
            return ["src/app/api/users/route.ts"]

        def fake_capture(url, screenshot_dir=None, routes=None, mocks=None, **kw):
            captured["capture_called"] = True
            return []

        monkeypatch.setattr("src.agent.routes.infer_routes", fake_infer_routes)
        monkeypatch.setattr("src.agent.main.write_server_mocks", fake_write)
        monkeypatch.setattr("src.agent.visual.capture_screenshots", fake_capture)
        monkeypatch.setattr("src.agent.visual.upload_screenshots", lambda *a, **kw: [])
        monkeypatch.setattr("src.agent.main._dev_server_url", "http://localhost:3000")
        monkeypatch.setattr("src.agent.main.REPO_DIR", "/app/repo")

        from src.agent.main import _capture_screenshots, _runtime_generated_files
        _runtime_generated_files.clear()

        _capture_screenshots("diff content", {"openrouter_api_key": "sk-or-fake"})

        assert captured["generated"] == (
            "/app/repo",
            {"localhost": {"/api/users": {"body": [{"id": 1}], "status": 200}}},
        )
        assert captured["capture_called"] is True
        assert "src/app/api/users/route.ts" in _runtime_generated_files


class TestPostComment:
    def test_append_live_preview_link(self):
        from src.agent.main import _append_live_preview_link

        body = _append_live_preview_link("## Review\n\nLooks good.", "54.1.2.3")

        assert body == "## Review\n\nLooks good.\n\n---\n\nLive app: http://54.1.2.3:3000"

    def test_append_live_preview_link_skips_missing_ip(self):
        from src.agent.main import _append_live_preview_link

        body = _append_live_preview_link("## Review\n\nLooks good.", "")

        assert body == "## Review\n\nLooks good."

    def test_post_comment_success_returns_id(self, monkeypatch):
        import httpx
        mock_resp = httpx.Response(201, json={"id": 1})
        monkeypatch.setattr(httpx, "Client", lambda *a, **kw: _mock_client(mock_resp))

        comment_id = _post_comment(
            token="fake-token",
            repo_full_name="owner/repo",
            pr_number="42",
            body="## Review\n\nLooks good.",
        )

        assert comment_id == 1

    def test_post_comment_failure_returns_none(self, monkeypatch):
        import httpx
        mock_resp = httpx.Response(400, json={"message": "Error"})
        monkeypatch.setattr(httpx, "Client", lambda *a, **kw: _mock_client(mock_resp))

        # A failed post no longer kills the agent; the review continues without observability.
        result = _post_comment(
            token="fake-token",
            repo_full_name="owner/repo",
            pr_number="42",
            body="## Review",
        )

        assert result is None

    def test_update_comment_success(self, monkeypatch):
        import httpx
        mock_resp = httpx.Response(200, json={"id": 7})
        monkeypatch.setattr(httpx, "Client", lambda *a, **kw: _mock_client(mock_resp))

        assert _update_comment(
            token="fake-token",
            repo_full_name="owner/repo",
            comment_id=7,
            body="updated",
        ) is True

    def test_update_comment_failure_is_best_effort(self, monkeypatch):
        import httpx
        mock_resp = httpx.Response(404, json={"message": "Not Found"})
        monkeypatch.setattr(httpx, "Client", lambda *a, **kw: _mock_client(mock_resp))

        # Best-effort: a failed edit must not raise, just report failure.
        assert _update_comment(
            token="fake-token",
            repo_full_name="owner/repo",
            comment_id=7,
            body="updated",
        ) is False


class TestProgressComment:
    def test_render_progress_marks_current_and_done(self):
        body = _render_progress(2)
        assert "is reviewing this PR" in body
        assert "✅ Setting up preview environment" in body
        assert "✅ Installing dependencies & starting dev server" in body
        assert "🔄 Capturing screenshots" in body
        assert "⬜ Generating review" in body

    def test_render_progress_failure_marks_stage_and_stops(self):
        body = _render_progress(1, failed_at=1)
        assert "review failed" in body.lower()
        assert "✅ Setting up preview environment" in body
        assert "❌ Installing dependencies & starting dev server" in body
        assert "⬜ Capturing screenshots" in body
        assert "⬜ Generating review" in body

    def test_run_edits_comment_to_failed_on_crash(self, monkeypatch):
        _mock_all_deps(monkeypatch)
        updates = []
        monkeypatch.setattr("src.agent.main._post_comment", lambda *a, **kw: 777)
        monkeypatch.setattr("src.agent.main._update_comment", lambda *a, body, **kw: updates.append(body) or True)

        def boom(*a, **kw):
            raise RuntimeError("dev server exploded")
        monkeypatch.setattr("src.agent.main._start_dev_server", boom)

        with pytest.raises(RuntimeError):
            run()

        # The placeholder is edited into a failure state rather than left hanging.
        assert any("review failed" in body.lower() for body in updates)


_BACKEND_DIFF = "diff --git a/main.py b/main.py\n--- a/main.py\n+++ b/main.py\n@@ -1 +1 @@\n-old\n+new"


class TestDiscoveryIntegration:
    def test_no_frontend_skips_and_posts_comment(self, monkeypatch):
        monkeypatch.setattr("src.agent.main._fetch_secrets", lambda: {"app_id": "1", "private_key": "k", "openrouter_api_key": "o"})
        monkeypatch.setattr("src.agent.main._get_installation_token", lambda *a, **kw: "fake-token")
        monkeypatch.setattr("src.agent.main._clone_repo", lambda *a, **kw: None)
        monkeypatch.setattr("src.agent.main._fetch_diff", lambda *a, **kw: _BACKEND_DIFF)

        posted = []
        monkeypatch.setattr("src.agent.main._post_comment", lambda *a, **kw: 123)
        monkeypatch.setattr("src.agent.main._update_comment", lambda *a, body, **kw: posted.append(body) or True)
        monkeypatch.setattr("src.agent.main._start_dev_server", lambda *a, **kw: None)
        monkeypatch.setattr("src.agent.main._capture_screenshots", lambda *a, **kw: ([], [], []))

        run()

        # The skip reason replaces the in-progress placeholder via an edit.
        assert len(posted) == 1
        assert "no frontend" in posted[0].lower()

    def test_frontend_without_package_skips_and_posts(self, monkeypatch, tmp_path):
        monkeypatch.setattr("src.agent.main._fetch_secrets", lambda: {"app_id": "1", "private_key": "k", "openrouter_api_key": "o"})
        monkeypatch.setattr("src.agent.main._get_installation_token", lambda *a, **kw: "fake-token")
        monkeypatch.setattr("src.agent.main._clone_repo", lambda *a, **kw: None)
        monkeypatch.setattr("src.agent.discovery.REPO_DIR", str(tmp_path))
        monkeypatch.setattr("src.agent.main._fetch_diff", lambda *a, **kw: _FRONTEND_DIFF)

        posted = []
        monkeypatch.setattr("src.agent.main._post_comment", lambda *a, **kw: 123)
        monkeypatch.setattr("src.agent.main._update_comment", lambda *a, body, **kw: posted.append(body) or True)
        monkeypatch.setattr("src.agent.main._start_dev_server", lambda *a, **kw: None)
        monkeypatch.setattr("src.agent.main._capture_screenshots", lambda *a, **kw: ([], [], []))

        run()

        assert len(posted) == 1
        assert "no package.json" in posted[0].lower()

    def test_frontend_with_package_proceeds_normally(self, monkeypatch, tmp_path):
        monkeypatch.setattr("src.agent.main._fetch_secrets", lambda: {"app_id": "1", "private_key": "k", "openrouter_api_key": "o"})
        monkeypatch.setattr("src.agent.main._get_installation_token", lambda *a, **kw: "fake-token")
        monkeypatch.setattr("src.agent.main._clone_repo", lambda *a, **kw: None)

        pkg = tmp_path / "package.json"
        pkg.write_text('{"scripts": {"dev": "next dev"}}')
        monkeypatch.setattr("src.agent.discovery.REPO_DIR", str(tmp_path))
        monkeypatch.setattr("src.agent.main._fetch_diff", lambda *a, **kw: _FRONTEND_DIFF)
        monkeypatch.setattr("src.agent.main._fetch_pr_meta", lambda *a, **kw: {"head_ref": "review-pr", "head_sha": "abc123", "is_fork": False, "base": {"repo": {"full_name": "test-owner/test-repo"}}})
        monkeypatch.setattr("src.agent.command_server.CommandServer", _MockCommandServer)
        monkeypatch.setenv("RENDERPR_PUBLIC_IP", "127.0.0.1")

        started_with = {}
        def track_start(package_dir=None, install_dir=None, **kw):
            started_with["package_dir"] = package_dir
            started_with["install_dir"] = install_dir
        monkeypatch.setattr("src.agent.main._start_dev_server", track_start)

        monkeypatch.setattr("src.agent.main._capture_screenshots", lambda *a, **kw: ([], [], []))
        monkeypatch.setattr("src.agent.network.get_public_ip", lambda: "54.1.2.3")
        monkeypatch.setattr("src.agent.main.write_dev_origin_allowlist", lambda *a, **kw: [])
        monkeypatch.setattr("src.agent.review.run_review", lambda *a, **kw: "## Review")
        posted = []
        monkeypatch.setattr("src.agent.main._post_comment", lambda *a, **kw: 456)
        monkeypatch.setattr("src.agent.main._update_comment", lambda *a, body, **kw: posted.append(body) or True)

        run()

        # The review is delivered by editing the placeholder comment, not a fresh post.
        assert any("## Review" in body for body in posted)


def test_env_injection_writes_dotenv_and_passes_injected_env(tmp_path, monkeypatch):
    monkeypatch.setenv("INSTALLATION_ID", "999")
    monkeypatch.setenv("REPO_FULL_NAME", "test-owner/test-repo")
    monkeypatch.setenv("PR_NUMBER", "42")
    _mock_all_deps(monkeypatch)

    # Point the repo + frontend at a writable tmp dir.
    monkeypatch.setattr("src.agent.main.REPO_DIR", str(tmp_path))
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / ".env.example").write_text("NEXT_PUBLIC_X=\nMISSING_ONE=\n")
    monkeypatch.setattr(
        "src.agent.main.discover_frontend",
        lambda *a, **k: {
            "has_frontend": True,
            "package_json_path": str(tmp_path / "package.json"),
            "workspace_root": None,
            "dev_command": "npm run dev",
            "launch_profile": _default_profile(),
            "reason": None,
        },
    )
    monkeypatch.setattr("src.agent.main.load_repo_secrets", lambda *a, **k: {"NEXT_PUBLIC_X": "hello"})

    captured = {}
    monkeypatch.setattr("src.agent.main._start_dev_server", lambda **kw: captured.update(kw))

    run()

    assert captured["injected_env"] == {"NEXT_PUBLIC_X": "hello"}
    assert (tmp_path / ".env.local").read_text().strip() == 'NEXT_PUBLIC_X="hello"'


def test_invalid_renderpr_yml_degrades(tmp_path, monkeypatch):
    monkeypatch.setenv("INSTALLATION_ID", "999")
    monkeypatch.setenv("REPO_FULL_NAME", "test-owner/test-repo")
    monkeypatch.setenv("PR_NUMBER", "42")
    posted = []
    _mock_all_deps(monkeypatch, posted_body=posted)
    monkeypatch.setattr("src.agent.main.REPO_DIR", str(tmp_path))
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / ".renderpr.yml").write_text("auth:\n  type: not-a-real-provider\n")
    monkeypatch.setattr(
        "src.agent.main.discover_frontend",
        lambda *a, **k: {
            "has_frontend": True,
            "package_json_path": str(tmp_path / "package.json"),
            "workspace_root": None,
            "dev_command": "npm run dev",
            "launch_profile": _default_profile(),
            "reason": None,
        },
    )
    started = []
    monkeypatch.setattr("src.agent.main._start_dev_server", lambda **kw: started.append(kw))

    run()

    assert not started  # never boots the dev server on a config error
    assert any("Invalid `.renderpr.yml`" in body for body in posted)


def test_login_wall_without_auth_degrades(tmp_path, monkeypatch):
    monkeypatch.setenv("INSTALLATION_ID", "999")
    monkeypatch.setenv("REPO_FULL_NAME", "test-owner/test-repo")
    monkeypatch.setenv("PR_NUMBER", "42")
    posted = []
    _mock_all_deps(monkeypatch, posted_body=posted)
    # Simulate screenshots landing on a login wall, with no auth configured.
    monkeypatch.setattr(
        "src.agent.main._capture_screenshots",
        lambda *a, **kw: ([], [], [{"path": "/", "url": "http://localhost:3000/login"}]),
    )

    run()

    assert any("appears to require **login**" in body for body in posted)
    # The degraded run must not post an actual review.
    assert not any("Looks good." in body for body in posted)


def test_auth_session_storage_state_forwarded(monkeypatch):
    from src.agent.auth import AuthSession

    monkeypatch.setattr("src.agent.routes.build_repo_tree", lambda *a, **k: {})
    monkeypatch.setattr("src.agent.routes.infer_routes",
                        lambda *a, **k: ([{"path": "/", "actions": [], "reason": "t"}], None))
    monkeypatch.setattr("src.agent.main._dev_server_url", "http://localhost:3000")
    monkeypatch.setattr("src.agent.main.REPO_DIR", "/tmp")
    monkeypatch.setattr("src.agent.visual.upload_screenshots", lambda *a, **k: [])

    captured = {}

    def fake_capture(url, screenshot_dir=None, routes=None, mocks=None, **kw):
        captured.update(kw)
        return []

    monkeypatch.setattr("src.agent.visual.capture_screenshots", fake_capture)

    from src.agent.main import _capture_screenshots
    session = AuthSession(storage_state={"cookies": [{"name": "x"}], "origins": []}, entry_url="http://e")
    _capture_screenshots("diff", {"openrouter_api_key": "k"}, session)

    assert captured["storage_state"] == {"cookies": [{"name": "x"}], "origins": []}
    assert captured["entry_url"] == "http://e"
    assert captured["login_signals"] == []


class TestNpmCacheStoreTarExit:
    """The cache store must work for every package manager. bun/yarn/npm all
    produced a non-fatal `tar` exit 1 (node_modules churning, e.g. Vite's dep
    cache) which the old check=True turned into a hard 'no cache stored'.

    NOTE: split out from TestNpmCacheStore — origin/main defined that class name
    twice, so the second definition silently shadowed the first and dropped its
    tests. Renaming here lets both sets run."""

    def _patch(self, tmp_path, monkeypatch, returncode):
        from src.agent import main as m

        monkeypatch.setenv("SCREENSHOT_BUCKET", "test-bucket")
        (tmp_path / "node_modules").mkdir()
        uploaded = []

        class _FakeS3:
            def upload_file(self, path, bucket, key):
                uploaded.append(key)

        monkeypatch.setattr(m.boto3, "client", lambda svc: _FakeS3())

        class _Res:
            def __init__(self, rc):
                self.returncode = rc
                self.stderr = "tar: file changed as we read it"

        monkeypatch.setattr(m.subprocess, "run", lambda *a, **kw: _Res(returncode))
        return m, uploaded

    def test_tar_warning_exit1_still_stores(self, tmp_path, monkeypatch):
        m, uploaded = self._patch(tmp_path, monkeypatch, 1)
        m._try_npm_cache_store(tmp_path, "bun-abc123def456")
        assert uploaded == ["npm-cache/bun-abc123def456.tar.gz"]

    def test_tar_fatal_exit2_skips_store(self, tmp_path, monkeypatch):
        m, uploaded = self._patch(tmp_path, monkeypatch, 2)
        m._try_npm_cache_store(tmp_path, "yarn-abc123def456")
        assert uploaded == []

    def test_missing_node_modules_skips_store(self, tmp_path, monkeypatch):
        from src.agent import main as m

        monkeypatch.setenv("SCREENSHOT_BUCKET", "test-bucket")
        uploaded = []

        class _FakeS3:
            def upload_file(self, path, bucket, key):
                uploaded.append(key)

        monkeypatch.setattr(m.boto3, "client", lambda svc: _FakeS3())
        m._try_npm_cache_store(tmp_path, "npm-abc123def456")  # no node_modules
        assert uploaded == []
