import json
import threading
import time
from http.client import HTTPConnection

from src.agent.command_server import CommandServer


class TestCommandServer:
    def test_dispatch_change(self):
        results = []

        def on_change(q):
            results.append(q)
            return {"status": "success", "message": f"changed {q}"}

        server = CommandServer(
            handle_change_fn=on_change,
            handle_apply_fn=lambda: {"status": "ok"},
            handle_reject_fn=lambda: {"status": "ok"},
            host="127.0.0.1",
            port=0,
        )
        server.start()
        # Get the actual port
        port = server._httpd.server_port

        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        body = json.dumps({"command": "change", "query": "make it blue"})
        conn.request("POST", "/__renderpr/command", body, {"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()

        assert data["status"] == "success"
        assert results == ["make it blue"]
        server._httpd.shutdown()

    def test_dispatch_apply(self):
        called = {"apply": False}

        def on_apply():
            called["apply"] = True
            return {"status": "ok"}

        server = CommandServer(
            handle_change_fn=lambda q: {},
            handle_apply_fn=on_apply,
            handle_reject_fn=lambda: {},
            host="127.0.0.1",
            port=0,
        )
        server.start()
        port = server._httpd.server_port

        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/__renderpr/command", json.dumps({"command": "apply"}), {"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        conn.close()

        assert called["apply"]
        server._httpd.shutdown()

    def test_dispatch_reject(self):
        called = {"reject": False}

        def on_reject():
            called["reject"] = True
            return {"status": "ok"}

        server = CommandServer(
            handle_change_fn=lambda q: {},
            handle_apply_fn=lambda: {},
            handle_reject_fn=on_reject,
            host="127.0.0.1",
            port=0,
        )
        server.start()
        port = server._httpd.server_port

        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/__renderpr/command", json.dumps({"command": "reject"}), {"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        conn.close()

        assert called["reject"]
        server._httpd.shutdown()

    def test_unknown_command(self):
        server = CommandServer(
            handle_change_fn=lambda q: {},
            handle_apply_fn=lambda: {},
            handle_reject_fn=lambda: {},
            host="127.0.0.1",
            port=0,
        )
        server.start()
        port = server._httpd.server_port

        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        body = json.dumps({"command": "unknown"})
        conn.request("POST", "/__renderpr/command", body, {"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()

        assert "error" in data["status"] or "error" in str(data)
        server._httpd.shutdown()

    def test_invalid_json_returns_400(self):
        server = CommandServer(
            handle_change_fn=lambda q: {},
            handle_apply_fn=lambda: {},
            handle_reject_fn=lambda: {},
            host="127.0.0.1",
            port=0,
        )
        server.start()
        port = server._httpd.server_port

        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/__renderpr/command", b"not json", {"Content-Type": "application/json"})
        resp = conn.getresponse()
        assert resp.status == 400
        resp.read()
        conn.close()
        server._httpd.shutdown()

    def test_wrong_path_returns_404(self):
        server = CommandServer(
            handle_change_fn=lambda q: {},
            handle_apply_fn=lambda: {},
            handle_reject_fn=lambda: {},
            host="127.0.0.1",
            port=0,
        )
        server.start()
        port = server._httpd.server_port

        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/wrong-path", json.dumps({"command": "apply"}), {"Content-Type": "application/json"})
        resp = conn.getresponse()
        assert resp.status == 404
        resp.read()
        conn.close()
        server._httpd.shutdown()

    def test_reset_idle_timer(self):
        server = CommandServer(
            handle_change_fn=lambda q: {},
            handle_apply_fn=lambda: {},
            handle_reject_fn=lambda: {},
        )
        old = server._last_interaction
        time.sleep(0.01)
        server.reset_idle_timer()
        assert server._last_interaction > old
