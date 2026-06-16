"""The Phase 3 end-to-end harness runner.

:func:`run_fixture` drives ONE framework fixture through the REAL ``src.agent``
pipeline — detection, on-disk mock writers, dependency install, dev-server boot —
then HTTP-asserts the runtime behavior against the running dev server. It never
reimplements agent logic: it calls the same functions ``main.py`` uses in
production. Every stage is wrapped so a failure becomes a recorded ``FAIL`` (with
captured logs) rather than an exception that aborts the whole sweep, and teardown
always runs in ``finally``.

Wired against ``src.agent``:
  - ``discovery.discover_frontend(diff)`` for framework/PM/launch-profile detection
    (with ``discovery.REPO_DIR`` + ``routes.REPO_DIR`` pointed at the fixture clone).
  - ``mock_server.get_mock_writer(framework)`` → write_server_mocks /
    write_unmocked_fallbacks / write_banner / write_dev_origin_allowlist, and
    ``mock_server.restore_runtime_files`` for cleanup.
  - ``main._start_dev_process`` + ``main._resolve_ready_port`` to boot the server
    (NOT ``_start_dev_server``/`_run_install`, which sys.exit and touch boto3).
  - ``config.NPM_CI_TIMEOUT_SECONDS`` / ``config.DEV_SERVER_START_TIMEOUT``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import httpx

from src.agent import config, discovery, mock_server, routes
from src.agent.main import _resolve_ready_port, _start_dev_process

from tests.integration.report import FAIL, PASS, FixtureResult
from tests.integration.spec_types import FixtureSpec

# Per-request HTTP timeout. The first SSR compile of a route can take several
# seconds, so individual GETs are given a generous ceiling and pages are polled.
HTTP_TIMEOUT = 30.0
PAGE_POLL_ATTEMPTS = 8
PAGE_POLL_DELAY = 2.0


def _build_diff(diff_files: list[str]) -> str:
    """Synthesize a unified diff touching each frontend file so the real
    ``detect_frontend_changes`` / ``_get_changed_files`` see them as changed."""
    chunks: list[str] = []
    for f in diff_files:
        chunks.append(
            f"diff --git a/{f} b/{f}\n"
            f"--- a/{f}\n"
            f"+++ b/{f}\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
    return "".join(chunks)


def _tail(text: str, n: int = 30) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-n:])


def _provision_repo(spec: FixtureSpec, *, use_local: bool, workdir: Path) -> Path:
    """Materialize the fixture's app source into a fresh temp dir under workdir.

    ``use_local`` copies the committed fixture tree; otherwise clones the private
    mirror via the GH token (or ``gh repo clone`` fallback).
    """
    repo_dir = workdir / f"repo-{spec.name}"
    if repo_dir.exists():
        shutil.rmtree(repo_dir, ignore_errors=True)

    if use_local:
        shutil.copytree(spec.source_dir, repo_dir)
        return repo_dir

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        clone_url = f"https://x-access-token:{token}@github.com/{spec.repo}.git"
        cmd = ["git", "clone", "--depth", "1", clone_url, str(repo_dir)]
    else:
        cmd = ["gh", "repo", "clone", spec.repo, str(repo_dir)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(
            f"clone of {spec.repo} failed (code {proc.returncode}):\n"
            + _tail(proc.stdout + proc.stderr)
        )
    return repo_dir


def _run_install(install_cmd: list[str], install_cwd: Path, log_path: Path) -> tuple[bool, str]:
    """Install deps, retrying once. Returns (ok, captured_output)."""
    last_out = ""
    for attempt in (1, 2):
        proc = subprocess.run(
            install_cmd,
            cwd=str(install_cwd),
            capture_output=True,
            text=True,
            timeout=config.NPM_CI_TIMEOUT_SECONDS,
        )
        last_out = (proc.stdout or "") + (proc.stderr or "")
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(last_out)
        except OSError:
            pass
        if proc.returncode == 0:
            return True, last_out
    return False, last_out


def _get(client: httpx.Client, base: str, path: str) -> httpx.Response:
    return client.get(base + path)


def _poll_page(client: httpx.Client, base: str, path: str) -> httpx.Response | None:
    """GET a page a few times, tolerating the first-compile delay/5xx."""
    last: httpx.Response | None = None
    for _ in range(PAGE_POLL_ATTEMPTS):
        try:
            resp = client.get(base + path)
            last = resp
            if resp.status_code == 200:
                return resp
        except httpx.HTTPError:
            last = None
        time.sleep(PAGE_POLL_DELAY)
    return last


def _assert_http(spec: FixtureSpec, port: int, result: FixtureResult) -> None:
    """Run the runtime HTTP assertions, recording each as a cell."""
    base = f"http://localhost:{port}"
    with httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        # host-bind: dev server must answer on 0.0.0.0, not just localhost.
        try:
            resp = client.get(f"http://0.0.0.0:{port}/")
            ok = resp.status_code < 500
            result.set("host_bind", PASS if ok else FAIL)
            if not ok:
                result.logs["host_bind"] = f"GET 0.0.0.0:{port}/ -> {resp.status_code}"
        except httpx.HTTPError as e:
            result.set("host_bind", FAIL)
            result.logs["host_bind"] = f"0.0.0.0 bind check failed: {e}"

        # server-mock-renders: page SSR'd with mock data + mocked API returns it.
        try:
            page = _poll_page(client, base, spec.page_path)
            problems: list[str] = []
            if page is None or page.status_code != 200:
                problems.append(
                    f"GET {spec.page_path} -> {getattr(page, 'status_code', 'no response')}"
                )
                body = page.text if page is not None else ""
            else:
                body = page.text
                missing = [s for s in spec.expected_items if s not in body]
                if missing:
                    problems.append(f"missing items in HTML: {missing}")
            api = _get(client, base, spec.mocked_api)
            if api.status_code != 200:
                problems.append(f"GET {spec.mocked_api} -> {api.status_code}")
            else:
                try:
                    data = api.json()
                    if not isinstance(data, list) or len(data) != 5:
                        problems.append(
                            f"mocked api body not a 5-item list: {data!r}"
                        )
                except json.JSONDecodeError:
                    problems.append(f"mocked api not JSON: {api.text[:200]}")
            result.set("server_mock_renders", FAIL if problems else PASS)
            if problems:
                result.logs["server_mock_renders"] = "\n".join(problems) + (
                    "\n--- page head ---\n" + body[:1500] if body else ""
                )
        except httpx.HTTPError as e:
            result.set("server_mock_renders", FAIL)
            result.logs["server_mock_renders"] = f"request error: {e}"

        # unmocked-fallback: unmocked api degrades to [] + the unmocked header.
        try:
            resp = _get(client, base, spec.unmocked_api)
            problems = []
            if resp.status_code != 200:
                problems.append(f"GET {spec.unmocked_api} -> {resp.status_code}")
            if mock_server.API_FALLBACK_HEADER not in resp.headers:
                problems.append(
                    f"missing header {mock_server.API_FALLBACK_HEADER}; got {dict(resp.headers)}"
                )
            try:
                data = resp.json()
                if data != []:
                    problems.append(f"body not empty list: {data!r}")
            except json.JSONDecodeError:
                problems.append(f"body not JSON: {resp.text[:200]}")
            result.set("unmocked_fallback", FAIL if problems else PASS)
            if problems:
                result.logs["unmocked_fallback"] = "\n".join(problems)
        except httpx.HTTPError as e:
            result.set("unmocked_fallback", FAIL)
            result.logs["unmocked_fallback"] = f"request error: {e}"

        # banner: page references the script + the script serves the init guard.
        try:
            page = _poll_page(client, base, spec.page_path)
            problems = []
            page_body = page.text if page is not None else ""
            if "/__renderpr-unmocked.js" not in page_body:
                problems.append("page HTML missing /__renderpr-unmocked.js reference")
            script = _get(client, base, "/__renderpr-unmocked.js")
            if script.status_code != 200:
                problems.append(f"GET /__renderpr-unmocked.js -> {script.status_code}")
            elif "__renderprUnmockedInit" not in script.text:
                problems.append("banner script missing __renderprUnmockedInit")
            result.set("banner", FAIL if problems else PASS)
            if problems:
                result.logs["banner"] = "\n".join(problems)
        except httpx.HTTPError as e:
            result.set("banner", FAIL)
            result.logs["banner"] = f"request error: {e}"

        # routing sanity: a second page sharing the component renders.
        try:
            page = _poll_page(client, base, spec.second_page_path)
            problems = []
            if page is None or page.status_code != 200:
                problems.append(
                    f"GET {spec.second_page_path} -> {getattr(page, 'status_code', 'no response')}"
                )
            elif "ItemList component" not in page.text:
                problems.append("second page missing 'ItemList component' marker")
            result.set("routing", FAIL if problems else PASS)
            if problems:
                result.logs["routing"] = "\n".join(problems)
        except httpx.HTTPError as e:
            result.set("routing", FAIL)
            result.logs["routing"] = f"request error: {e}"


def run_fixture(spec: FixtureSpec, *, use_local: bool, workdir: Path) -> FixtureResult:
    result = FixtureResult(framework=spec.name, pm=spec.pm)
    workdir = Path(workdir)
    log_dir = workdir / "logs"

    repo_dir: Path | None = None
    proc = None
    generated: list[str] = []

    try:
        # 1. Provision the repo.
        try:
            repo_dir = _provision_repo(spec, use_local=use_local, workdir=workdir)
        except Exception as e:  # noqa: BLE001 — record, don't abort the sweep.
            result.error = f"provision failed: {e}"
            result.block_remaining("install")
            return result

        # 2. Detection via the REAL discover_frontend. REPO_DIR must be set on
        #    BOTH discovery and routes (routes._get_changed_files reads it too).
        diff = _build_diff(spec.diff_files)
        setattr(discovery, "REPO_DIR", str(repo_dir))
        setattr(routes, "REPO_DIR", str(repo_dir))

        try:
            info = discovery.discover_frontend(diff)
        except Exception as e:  # noqa: BLE001
            result.error = f"discover_frontend raised: {e}"
            result.set("install", FAIL)
            result.block_remaining("boot")
            return result

        if not info.get("has_frontend") or "launch_profile" not in info:
            result.error = f"no frontend detected: {info.get('reason')}"
            result.set("install", FAIL)
            result.block_remaining("boot")
            return result

        profile = info["launch_profile"]
        package_json_path = info["package_json_path"]
        workspace_root = info.get("workspace_root")

        # Detection pre-check folded into the install cell: a wrong PM/framework/
        # install command is a detection FAIL that blocks the rest.
        mismatches: list[str] = []
        if profile.package_manager != spec.expected_pm:
            mismatches.append(
                f"pm: got {profile.package_manager}, expected {spec.expected_pm}"
            )
        if profile.framework != spec.expected_framework:
            mismatches.append(
                f"framework: got {profile.framework}, expected {spec.expected_framework}"
            )
        if profile.install_command != spec.expected_install_command:
            mismatches.append(
                f"install_command: got {profile.install_command}, "
                f"expected {spec.expected_install_command}"
            )
        if mismatches:
            result.error = "detection mismatch:\n" + "\n".join(mismatches)
            result.set("install", FAIL)
            result.logs["install"] = result.error
            result.block_remaining("boot")
            return result

        # 3. Run the on-disk mock writers in order. Accumulate generated paths.
        writer = mock_server.get_mock_writer(profile.framework)
        try:
            generated += writer.write_server_mocks(repo_dir, spec.mocks, diff=diff)
            generated += writer.write_unmocked_fallbacks(repo_dir, mocks=spec.mocks, diff=diff)
            generated += writer.write_banner(repo_dir)
            generated += writer.write_dev_origin_allowlist(repo_dir, "10.0.0.5")
        except Exception as e:  # noqa: BLE001
            result.error = f"mock writers raised: {e}"
            result.set("install", FAIL)
            result.block_remaining("boot")
            return result

        # 4. Install dependencies (real install_command, retry once).
        install_cwd = (
            Path(workspace_root).parent if workspace_root
            else Path(package_json_path).parent
        )
        ok, out = _run_install(
            profile.install_command, install_cwd, log_dir / f"{spec.name}-install.log"
        )
        if not ok:
            result.set("install", FAIL)
            result.logs["install"] = _tail(out, 30)
            result.error = "dependency install failed"
            result.block_remaining("boot")
            return result
        result.set("install", PASS)

        # 5. Boot the dev server via the REAL _start_dev_process/_resolve_ready_port.
        dev_cwd = Path(package_json_path).parent
        dev_env = {**os.environ, **profile.dev_env}
        port: int | None = None
        for attempt in (1, 2):
            proc, sniffed = _start_dev_process(profile.dev_command, dev_cwd, dev_env)
            port = _resolve_ready_port(proc, sniffed, profile.default_port)
            if port is not None:
                break
            # Boot failed this attempt; tear down before retrying.
            _terminate(proc)
            proc = None

        if port is None:
            result.set("boot", FAIL)
            result.logs["boot"] = (
                f"dev server did not become ready within "
                f"{config.DEV_SERVER_START_TIMEOUT}s (cmd: {' '.join(profile.dev_command)})"
            )
            result.error = "dev server boot failed"
            result.block_remaining("host_bind")
            return result
        result.set("boot", PASS)

        # 6. Runtime HTTP assertions.
        _assert_http(spec, port, result)
        return result

    finally:
        # 7. Always teardown: stop the dev server, restore patched files, drop clone.
        if proc is not None:
            _terminate(proc)
        if repo_dir is not None:
            try:
                mock_server.restore_runtime_files(repo_dir, generated)
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass
            shutil.rmtree(repo_dir, ignore_errors=True)


def _terminate(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        pass
