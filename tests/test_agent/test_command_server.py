import json
import time
from http.client import HTTPConnection

from src.agent.command_server import CommandServer


class TestCommandServer:
    def test_dispatch_change_async(self):
        results = []

        def on_change(q):
            results.append(q)
            return {"status": "success", "message": f"changed {q}"}

        server = CommandServer(
            handle_change_fn=on_change,
            handle_apply_fn=lambda: {"status": "ok"},
            host="127.0.0.1",
            port=0,
            token=b"test-token",
        )
        server.start()
        port = server._httpd.server_port

        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        body = json.dumps({"command": "change", "query": "make it blue"})
        conn.request(
            "POST",
            "/__renderpr/command",
            body,
            {"Content-Type": "application/json", "X-RenderPR-Token": "test-token"},
        )
        resp = conn.getresponse()
        assert resp.status == 202
        data = json.loads(resp.read())
        conn.close()

        assert data["status"] == "accepted"
        time.sleep(0.1)
        assert results == ["make it blue"]
        server._httpd.shutdown()

    def test_dispatch_apply_async(self):
        called = {"apply": False}

        def on_apply():
            called["apply"] = True
            return {"status": "ok"}

        server = CommandServer(
            handle_change_fn=lambda q: {},
            handle_apply_fn=on_apply,
            host="127.0.0.1",
            port=0,
            token=b"test-token",
        )
        server.start()
        port = server._httpd.server_port

        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST",
            "/__renderpr/command",
            json.dumps({"command": "apply"}),
            {"Content-Type": "application/json", "X-RenderPR-Token": "test-token"},
        )
        resp = conn.getresponse()
        assert resp.status == 202
        resp.read()
        conn.close()

        time.sleep(0.1)
        assert called["apply"]
        server._httpd.shutdown()

    def test_missing_token_returns_401(self):
        server = CommandServer(
            handle_change_fn=lambda q: {},
            handle_apply_fn=lambda: {},
            host="127.0.0.1",
            port=0,
            token=b"secret-token",
        )
        server.start()
        port = server._httpd.server_port

        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST",
            "/__renderpr/command",
            json.dumps({"command": "apply"}),
            {"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        assert resp.status == 401
        resp.read()
        conn.close()
        server._httpd.shutdown()

    def test_wrong_token_returns_401(self):
        server = CommandServer(
            handle_change_fn=lambda q: {},
            handle_apply_fn=lambda: {},
            host="127.0.0.1",
            port=0,
            token=b"secret-token",
        )
        server.start()
        port = server._httpd.server_port

        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST",
            "/__renderpr/command",
            json.dumps({"command": "apply"}),
            {"Content-Type": "application/json", "X-RenderPR-Token": "wrong"},
        )
        resp = conn.getresponse()
        assert resp.status == 401
        resp.read()
        conn.close()
        server._httpd.shutdown()

    def test_no_token_set_rejects_all(self):
        server = CommandServer(
            handle_change_fn=lambda q: {},
            handle_apply_fn=lambda: {},
            host="127.0.0.1",
            port=0,
            token=b"",
        )
        server.start()
        port = server._httpd.server_port

        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST",
            "/__renderpr/command",
            json.dumps({"command": "apply"}),
            {"Content-Type": "application/json", "X-RenderPR-Token": "anything"},
        )
        resp = conn.getresponse()
        assert resp.status == 401
        resp.read()
        conn.close()
        server._httpd.shutdown()

    def test_invalid_json_returns_400(self):
        server = CommandServer(
            handle_change_fn=lambda q: {},
            handle_apply_fn=lambda: {},
            host="127.0.0.1",
            port=0,
            token=b"test-token",
        )
        server.start()
        port = server._httpd.server_port

        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST",
            "/__renderpr/command",
            b"not json",
            {"Content-Type": "application/json", "X-RenderPR-Token": "test-token"},
        )
        resp = conn.getresponse()
        assert resp.status == 400
        resp.read()
        conn.close()
        server._httpd.shutdown()

    def test_wrong_path_returns_404(self):
        server = CommandServer(
            handle_change_fn=lambda q: {},
            handle_apply_fn=lambda: {},
            host="127.0.0.1",
            port=0,
            token=b"test-token",
        )
        server.start()
        port = server._httpd.server_port

        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST",
            "/wrong-path",
            json.dumps({"command": "apply"}),
            {"Content-Type": "application/json", "X-RenderPR-Token": "test-token"},
        )
        resp = conn.getresponse()
        assert resp.status == 404
        resp.read()
        conn.close()
        server._httpd.shutdown()

    def test_reset_idle_timer(self):
        server = CommandServer(
            handle_change_fn=lambda q: {},
            handle_apply_fn=lambda: {},
        )
        old = server._last_interaction
        time.sleep(0.01)
        server.reset_idle_timer()
        assert server._last_interaction > old

    def test_idle_timeout_runs_shutdown_callback(self, monkeypatch):
        import pytest

        from src.agent import command_server as cs

        called = {"shutdown": False}

        server = CommandServer(
            handle_change_fn=lambda q: {},
            handle_apply_fn=lambda: {},
            idle_timeout=1,
            on_idle_shutdown=lambda: called.__setitem__("shutdown", True),
        )
        # Force the timeout to be already exceeded, and skip the real poll sleep.
        server._last_interaction = time.time() - 100
        monkeypatch.setattr(cs.time, "sleep", lambda _s: None)

        with pytest.raises(SystemExit):
            server.run_idle_loop()

        assert called["shutdown"] is True

    def test_idle_timeout_without_callback_still_exits(self, monkeypatch):
        import pytest

        from src.agent import command_server as cs

        server = CommandServer(
            handle_change_fn=lambda q: {},
            handle_apply_fn=lambda: {},
            idle_timeout=1,
        )
        server._last_interaction = time.time() - 100
        monkeypatch.setattr(cs.time, "sleep", lambda _s: None)

        with pytest.raises(SystemExit):
            server.run_idle_loop()
