
from src.agent.editor import apply_edit, wait_for_dev_server, revert_edit


class TestApplyEdit:
    def _repo(self, tmp_path, monkeypatch):
        # The traversal guard resolves paths under code_edit.REPO_DIR, so edits
        # must be repo-relative; point REPO_DIR at the test's tmp dir.
        monkeypatch.setattr("src.agent.code_edit.REPO_DIR", str(tmp_path))
        monkeypatch.setattr("src.agent.editor.REPO_DIR", str(tmp_path))

    def test_successful_replacement(self, tmp_path, monkeypatch):
        self._repo(tmp_path, monkeypatch)
        file = tmp_path / "test.tsx"
        file.write_text('className="bg-blue-500"')
        edit = {"file": "test.tsx", "line": 1, "oldString": "bg-blue-500", "newString": "bg-orange-500"}
        assert apply_edit(edit)
        assert file.read_text() == 'className="bg-orange-500"'

    def test_file_not_found(self, tmp_path, monkeypatch):
        self._repo(tmp_path, monkeypatch)
        edit = {"file": "nonexistent/file.tsx", "line": 1, "oldString": "x", "newString": "y"}
        assert not apply_edit(edit)

    def test_rejects_path_traversal(self, tmp_path, monkeypatch):
        self._repo(tmp_path, monkeypatch)
        secret = tmp_path.parent / "secret.txt"
        secret.write_text("token=abc")
        edit = {"file": "../secret.txt", "line": 1, "oldString": "token", "newString": "X"}
        assert not apply_edit(edit)
        assert secret.read_text() == "token=abc"  # untouched

    def test_old_string_not_found(self, tmp_path, monkeypatch):
        self._repo(tmp_path, monkeypatch)
        file = tmp_path / "test.tsx"
        file.write_text("something else")
        edit = {"file": "test.tsx", "line": 1, "oldString": "nonexistent", "newString": "whatever"}
        assert not apply_edit(edit)

    def test_disambiguates_by_line(self, tmp_path, monkeypatch):
        self._repo(tmp_path, monkeypatch)
        file = tmp_path / "test.tsx"
        file.write_text("a\na\na\n")
        edit = {"file": "test.tsx", "line": 3, "oldString": "a", "newString": "b"}
        assert apply_edit(edit)
        assert file.read_text() == "a\na\nb\n"

    def test_disambiguates_to_nearest_line(self, tmp_path, monkeypatch):
        self._repo(tmp_path, monkeypatch)
        file = tmp_path / "test.tsx"
        file.write_text("a\na\na\n")
        edit = {"file": "test.tsx", "line": 2, "oldString": "a", "newString": "b"}
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
        def mock_get(*a, **kw):
            return type("R", (), {"status_code": 200, "text": "<html>nextjs__container_errors</html>"})()

        monkeypatch.setattr("httpx.get", mock_get)
        assert not wait_for_dev_server("http://localhost:3000", timeout=0.3, interval=0.05)


class TestErrorOverlayMarkers:
    def test_vite_overlay_detected_only_for_vite(self):
        from src.agent.editor import _has_dev_error_overlay
        body = "<vite-error-overlay>boom</vite-error-overlay>"
        assert _has_dev_error_overlay(body, "vite") is True
        # Vite-specific marker shouldn't trip the Next profile.
        assert _has_dev_error_overlay(body, "next") is False

    def test_generic_markers_apply_to_all_frameworks(self):
        from src.agent.editor import _has_dev_error_overlay
        assert _has_dev_error_overlay("Failed to compile", "sveltekit") is True
        assert _has_dev_error_overlay("Failed to compile", "vite") is True

    def test_next_marker_for_next(self):
        from src.agent.editor import _has_dev_error_overlay
        assert _has_dev_error_overlay("__next_error__", "next") is True


class TestRevertEdit:
    def test_calls_git_checkout(self, monkeypatch):
        calls = []

        def mock_run(*a, **kw):
            calls.append(a[0])
            return type("R", (), {"returncode": 0})()

        monkeypatch.setattr("subprocess.run", mock_run)
        revert_edit({"file": "src/page.tsx"})
        assert ["git", "checkout", "src/page.tsx"] in calls


class TestRunEditPreview:
    def test_drops_unvalidated_edit_actions(self, tmp_path, monkeypatch):
        from src.agent import editor

        target = tmp_path / "app" / "users" / "page.tsx"
        target.parent.mkdir(parents=True)
        target.write_text("<td>Admin</td>")

        captured = {}

        monkeypatch.setattr("src.agent.editor.REPO_DIR", str(tmp_path))
        monkeypatch.setattr("src.agent.code_edit.REPO_DIR", str(tmp_path))
        monkeypatch.setattr(
            "src.agent.code_edit.request_edit",
            lambda *a, **kw: {
                "edits": [{
                    "file": "app/users/page.tsx",
                    "line": 1,
                    "oldString": "Admin",
                    "newString": "Administrator",
                }],
                "actions": [{"type": "click", "selector": "text=Admin"}],
            },
        )
        monkeypatch.setattr(editor, "wait_for_dev_server", lambda *a, **kw: True)
        monkeypatch.setattr("src.agent.routes.build_repo_tree", lambda: "app/users/page.tsx")
        monkeypatch.setattr(
            "src.agent.routes.infer_routes",
            lambda *a, **kw: ([{"path": "/users", "actions": [], "reason": "test"}], {}),
        )
        monkeypatch.setattr("src.agent.visual.upload_screenshots", lambda *a, **kw: [])

        calls = {"n": 0}

        def fake_capture_screenshots(*a, **kw):
            captured["routes"] = kw["routes"]
            # Return a distinct (non-empty, differing) result per call so the
            # before/after diff registers a visible change.
            calls["n"] += 1
            return [(tmp_path / f"shot-{calls['n']}.png", "Desktop - /users")]

        monkeypatch.setattr("src.agent.visual.capture_screenshots", fake_capture_screenshots)
        for n in (1, 2):
            (tmp_path / f"shot-{n}.png").write_bytes(str(n).encode())

        result = editor.execute_change(
            "change role text",
            "sk-or-fake",
            "http://localhost:3000",
            "diff",
            bucket="",
            pr_number="1",
        )

        assert result["status"] == "success"
        assert captured["routes"] == [{"path": "/users", "actions": [], "reason": "test"}]


class TestRouteSetConsistency:
    """Regression: a code-change run must screenshot the SAME route set the
    initial review used for a given diff. The review infers routes once and
    hands that set to execute_change as base_routes; the run must reuse it
    verbatim (no re-inference, which is non-deterministic) and only ever add
    edit-target routes for files the edit touched but the diff didn't cover.

    The original bug: the code-change run re-inferred routes and screenshotted
    e.g. /users when the review never did, so the two capture sets diverged.
    """

    def _run(self, tmp_path, monkeypatch, base_routes, edit_file, edit_route_dir):
        from src.agent import editor

        target = tmp_path / edit_route_dir / "page.tsx"
        target.parent.mkdir(parents=True)
        target.write_text("<td>Admin</td>")

        monkeypatch.setattr("src.agent.editor.REPO_DIR", str(tmp_path))
        monkeypatch.setattr("src.agent.code_edit.REPO_DIR", str(tmp_path))
        monkeypatch.setattr(
            "src.agent.code_edit.request_edit",
            lambda *a, **kw: {
                "edits": [{"file": edit_file, "line": 1, "oldString": "Admin", "newString": "Administrator"}],
                "actions": [],
            },
        )
        monkeypatch.setattr(editor, "wait_for_dev_server", lambda *a, **kw: True)
        monkeypatch.setattr("src.agent.visual.upload_screenshots", lambda *a, **kw: [])

        # If execute_change ever re-infers routes, the capture set could drift
        # from the review's — so make any inference attempt fail the test loudly.
        def _must_not_infer(*a, **kw):
            raise AssertionError("execute_change re-inferred routes instead of reusing base_routes")

        monkeypatch.setattr("src.agent.routes.infer_routes", _must_not_infer)

        captured: dict = {}
        calls = {"n": 0}

        def fake_capture_screenshots(*a, **kw):
            captured["routes"] = kw["routes"]
            calls["n"] += 1  # distinct bytes per call => before/after differ
            return [(tmp_path / f"shot-{calls['n']}.png", "Desktop - /x")]

        monkeypatch.setattr("src.agent.visual.capture_screenshots", fake_capture_screenshots)
        for n in (1, 2):
            (tmp_path / f"shot-{n}.png").write_bytes(str(n).encode())

        result = editor.execute_change(
            "change role text", "sk-or-fake", "http://localhost:3000", "diff",
            bucket="", pr_number="1", base_routes=base_routes,
        )
        return result, captured["routes"]

    def test_reuses_review_base_set_and_adds_only_edit_target(self, tmp_path, monkeypatch):
        # Review inferred /dashboard for this diff; the edit touches /users.
        review_routes = [{"path": "/dashboard", "actions": [], "reason": "deterministic"}]
        result, captured = self._run(
            tmp_path, monkeypatch, review_routes, "app/users/page.tsx", "app/users"
        )

        assert result["status"] == "success"
        captured_paths = [r["path"] for r in captured]
        # The review's base route is preserved unchanged...
        assert {"path": "/dashboard", "actions": [], "reason": "deterministic"} in captured
        # ...and the ONLY route added beyond the review's set is the edit target.
        added = [r for r in captured if r["path"] not in {"/dashboard"}]
        assert added == [{"path": "/users", "actions": [], "reason": "edit-target"}]
        assert set(captured_paths) - {"/dashboard"} == {"/users"}

    def test_edit_within_base_set_adds_no_routes(self, tmp_path, monkeypatch):
        # When the edit lands on a route the review already covers, the capture
        # set is identical to the review's — no duplicate, no drift.
        review_routes = [{"path": "/users", "actions": [], "reason": "deterministic"}]
        result, captured = self._run(
            tmp_path, monkeypatch, review_routes, "app/users/page.tsx", "app/users"
        )

        assert result["status"] == "success"
        assert captured == [{"path": "/users", "actions": [], "reason": "deterministic"}]


class TestApplyEdits:
    def _repo(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.agent.code_edit.REPO_DIR", str(tmp_path))
        monkeypatch.setattr("src.agent.editor.REPO_DIR", str(tmp_path))

    def test_applies_all_edits(self, tmp_path, monkeypatch):
        from src.agent.editor import apply_edits

        self._repo(tmp_path, monkeypatch)
        f = tmp_path / "page.tsx"
        f.write_text("via-indigo-700 to-violet-600")
        edits = [
            {"file": "page.tsx", "line": 1, "oldString": "via-indigo-700", "newString": "via-orange-500"},
            {"file": "page.tsx", "line": 1, "oldString": "to-violet-600", "newString": "to-orange-600"},
        ]
        assert apply_edits(edits)
        assert f.read_text() == "via-orange-500 to-orange-600"

    def test_rolls_back_when_one_edit_fails(self, tmp_path, monkeypatch):
        from src.agent.editor import apply_edits

        self._repo(tmp_path, monkeypatch)
        f = tmp_path / "page.tsx"
        original = "via-indigo-700 to-violet-600"
        f.write_text(original)
        edits = [
            {"file": "page.tsx", "line": 1, "oldString": "via-indigo-700", "newString": "via-orange-500"},
            {"file": "page.tsx", "line": 1, "oldString": "DOES-NOT-EXIST", "newString": "x"},
        ]
        assert not apply_edits(edits)
        assert f.read_text() == original  # first edit was rolled back


class TestScreenshotsIdentical:
    def test_identical_bytes_detected(self, tmp_path):
        from src.agent.editor import _screenshots_identical

        a = tmp_path / "a.png"
        a.write_bytes(b"same")
        b = tmp_path / "b.png"
        b.write_bytes(b"same")
        before = [(a, "Desktop - /")]
        after = [(b, "Desktop - /")]
        assert _screenshots_identical(before, after)

    def test_differing_bytes_not_identical(self, tmp_path):
        from src.agent.editor import _screenshots_identical

        a = tmp_path / "a.png"
        a.write_bytes(b"before")
        b = tmp_path / "b.png"
        b.write_bytes(b"after")
        assert not _screenshots_identical([(a, "Desktop - /")], [(b, "Desktop - /")])

    def test_empty_is_not_identical(self, tmp_path):
        from src.agent.editor import _screenshots_identical

        assert not _screenshots_identical([], [])


class TestSelectGroundingImages:
    def test_prefers_desktop_baselines(self, tmp_path):
        from src.agent.editor import _select_grounding_images

        desktop = tmp_path / "d.png"
        desktop.write_bytes(b"desktop")
        mobile = tmp_path / "m.png"
        mobile.write_bytes(b"mobile")
        interacted = tmp_path / "i.png"
        interacted.write_bytes(b"interacted")
        results = [
            (mobile, "Mobile XS - /"),
            (desktop, "Desktop - /"),
            (interacted, "Desktop - / after interaction"),
        ]
        assert _select_grounding_images(results) == [b"desktop"]

    def test_falls_back_to_any_when_no_desktop(self, tmp_path):
        from src.agent.editor import _select_grounding_images

        mobile = tmp_path / "m.png"
        mobile.write_bytes(b"mobile")
        assert _select_grounding_images([(mobile, "Mobile XS - /")]) == [b"mobile"]


class TestVisionGrounding:
    def test_before_screenshots_passed_to_edit_model(self, tmp_path, monkeypatch):
        from src.agent import editor

        target = tmp_path / "app" / "page.tsx"
        target.parent.mkdir(parents=True)
        target.write_text("via-indigo-700")

        monkeypatch.setattr("src.agent.editor.REPO_DIR", str(tmp_path))
        monkeypatch.setattr("src.agent.code_edit.REPO_DIR", str(tmp_path))

        seen = {}

        def fake_request_edit(query, api_key, frontend_root=None, images=None, feedback=None):
            seen["images"] = images
            return {
                "edits": [{"file": "app/page.tsx", "line": 1, "oldString": "via-indigo-700", "newString": "via-orange-500"}],
                "actions": [],
            }

        monkeypatch.setattr("src.agent.code_edit.request_edit", fake_request_edit)
        monkeypatch.setattr(editor, "wait_for_dev_server", lambda *a, **kw: True)
        monkeypatch.setattr("src.agent.routes.build_repo_tree", lambda: "app/page.tsx")
        monkeypatch.setattr(
            "src.agent.routes.infer_routes",
            lambda *a, **kw: ([{"path": "/", "actions": [], "reason": "test"}], {}),
        )
        monkeypatch.setattr("src.agent.visual.upload_screenshots", lambda *a, **kw: [])

        calls = {"n": 0}

        def fake_capture(*a, **kw):
            calls["n"] += 1
            shot = tmp_path / f"shot-{calls['n']}.png"
            shot.write_bytes(f"img-{calls['n']}".encode())
            return [(shot, "Desktop - /")]

        monkeypatch.setattr("src.agent.visual.capture_screenshots", fake_capture)

        result = editor.execute_change(
            "make the headline orange", "sk-or-fake", "http://localhost:3000", "diff",
            bucket="", pr_number="1",
        )

        assert result["status"] == "success"
        assert seen["images"] == [b"img-1"]  # before-screenshot handed to the model


class TestRetryLoop:
    def test_retries_with_feedback_then_succeeds(self, tmp_path, monkeypatch):
        from src.agent import editor

        target = tmp_path / "app" / "page.tsx"
        target.parent.mkdir(parents=True)
        target.write_text("via-indigo-700")

        monkeypatch.setattr("src.agent.editor.REPO_DIR", str(tmp_path))
        monkeypatch.setattr("src.agent.code_edit.REPO_DIR", str(tmp_path))

        calls = {"n": 0, "feedback_seen": []}

        def fake_request_edit(query, api_key, frontend_root=None, images=None, feedback=None):
            calls["n"] += 1
            calls["feedback_seen"].append(list(feedback or []))
            return {
                "edits": [{"file": "app/page.tsx", "line": 1, "oldString": "via-indigo-700", "newString": "via-orange-500"}],
                "actions": [],
            }

        monkeypatch.setattr("src.agent.code_edit.request_edit", fake_request_edit)
        monkeypatch.setattr(editor, "wait_for_dev_server", lambda *a, **kw: True)
        # Restore the file on revert so the next attempt's edit applies again.
        monkeypatch.setattr(editor, "revert_edits", lambda edits: target.write_text("via-indigo-700"))
        monkeypatch.setattr("src.agent.routes.build_repo_tree", lambda: "app/page.tsx")
        monkeypatch.setattr(
            "src.agent.routes.infer_routes",
            lambda *a, **kw: ([{"path": "/", "actions": [], "reason": "test"}], {}),
        )
        monkeypatch.setattr("src.agent.visual.upload_screenshots", lambda *a, **kw: [])

        cap = {"n": 0}

        def fake_capture(*a, **kw):
            cap["n"] += 1
            # before (call 1) and attempt-1 after (call 2) identical; attempt-2 after (call 3) differs.
            content = b"same" if cap["n"] <= 2 else b"changed"
            shot = tmp_path / f"s{cap['n']}.png"
            shot.write_bytes(content)
            return [(shot, "Desktop - /")]

        monkeypatch.setattr("src.agent.visual.capture_screenshots", fake_capture)

        result = editor.execute_change(
            "make the headline orange", "sk-or-fake", "http://localhost:3000", "diff",
            bucket="", pr_number="1",
        )

        assert result["status"] == "success"
        assert calls["n"] == 2  # second attempt succeeded
        assert calls["feedback_seen"][0] == []  # first attempt: no feedback
        assert calls["feedback_seen"][1]  # second attempt: got feedback about the no-op


class TestNoVisibleChange:
    def test_unchanged_screenshots_revert_and_report(self, tmp_path, monkeypatch):
        from src.agent import editor

        target = tmp_path / "app" / "page.tsx"
        target.parent.mkdir(parents=True)
        target.write_text('className="via-indigo-700"')

        monkeypatch.setattr("src.agent.editor.REPO_DIR", str(tmp_path))
        monkeypatch.setattr("src.agent.code_edit.REPO_DIR", str(tmp_path))
        monkeypatch.setattr(
            "src.agent.code_edit.request_edit",
            lambda *a, **kw: {
                "edits": [{
                    "file": "app/page.tsx",
                    "line": 1,
                    "oldString": "via-indigo-700",
                    "newString": "via-orange-500",
                }],
                "actions": [],
            },
        )
        monkeypatch.setattr(editor, "wait_for_dev_server", lambda *a, **kw: True)
        monkeypatch.setattr("src.agent.routes.build_repo_tree", lambda: "app/page.tsx")
        monkeypatch.setattr(
            "src.agent.routes.infer_routes",
            lambda *a, **kw: ([{"path": "/", "actions": [], "reason": "test"}], {}),
        )

        reverted = {"called": False}
        monkeypatch.setattr(editor, "revert_edits", lambda edits: reverted.update(called=True))

        # Same byte content before and after -> no visible change.
        shot = tmp_path / "shot.png"
        shot.write_bytes(b"identical")
        monkeypatch.setattr(
            "src.agent.visual.capture_screenshots",
            lambda *a, **kw: [(shot, "Desktop - /")],
        )
        uploaded = {"called": False}
        monkeypatch.setattr(
            "src.agent.visual.upload_screenshots",
            lambda *a, **kw: uploaded.update(called=True) or [],
        )

        result = editor.execute_change(
            "make it orange", "sk-or-fake", "http://localhost:3000", "diff",
            bucket="bucket", pr_number="1",
        )

        assert result["status"] == "no_visible_change"
        assert reverted["called"] is True
        assert uploaded["called"] is False  # never upload a no-op
