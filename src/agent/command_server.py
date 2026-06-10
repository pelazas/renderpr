import json
import logging
import os
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any

from src.agent.config import IDLE_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)


class CommandHandler(BaseHTTPRequestHandler):
    server_instance: "CommandServer | None" = None

    def log_message(self, format, *args):
        logger.debug("HTTP: %s %s", self.client_address, format % args)

    def do_POST(self):
        if self.path != "/__renderpr/command":
            self._respond(404, {"error": "Not found"})
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        try:
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._respond(400, {"error": "Invalid JSON"})
            return

        cmd = data.get("command")
        query = data.get("query")

        result = self._dispatch(cmd, query)
        self._respond(200, result)

    def _dispatch(self, command: str | None, query: str | None) -> dict:
        server = self.server_instance
        if server is None:
            return {"status": "error", "message": "Server not initialized"}

        server.reset_idle_timer()

        if command == "change":
            return server.handle_change(query or "")
        elif command == "apply":
            return server.handle_apply()
        elif command == "reject":
            return server.handle_reject()
        else:
            return {"status": "error", "message": f"Unknown command: {command}"}

    def _respond(self, status: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class CommandServer:
    def __init__(
        self,
        handle_change_fn: Any,
        handle_apply_fn: Any,
        handle_reject_fn: Any,
        host: str = "0.0.0.0",
        port: int = 3001,
        idle_timeout: int | None = None,
    ):
        self._handle_change = handle_change_fn
        self._handle_apply = handle_apply_fn
        self._handle_reject = handle_reject_fn
        self._host = host
        self._port = port
        self._idle_timeout = idle_timeout or IDLE_TIMEOUT_SECONDS
        self._last_interaction = time.time()
        self._httpd: HTTPServer | None = None

    def reset_idle_timer(self) -> None:
        self._last_interaction = time.time()

    def handle_change(self, query: str) -> dict:
        try:
            return self._handle_change(query)
        except Exception:
            logger.exception("handle_change failed")
            return {"status": "error", "message": "Internal error processing change"}

    def handle_apply(self) -> dict:
        try:
            return self._handle_apply()
        except Exception:
            logger.exception("handle_apply failed")
            return {"status": "error", "message": "Internal error applying changes"}

    def handle_reject(self) -> dict:
        try:
            return self._handle_reject()
        except Exception:
            logger.exception("handle_reject failed")
            return {"status": "error", "message": "Internal error rejecting changes"}

    def start(self) -> None:
        CommandHandler.server_instance = self
        self._httpd = HTTPServer((self._host, self._port), CommandHandler)
        thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        thread.start()
        logger.info("Command server listening on %s:%s", self._host, self._port)

    def run_idle_loop(self) -> None:
        logger.info("Idle timeout set to %d seconds", self._idle_timeout)
        while True:
            time.sleep(10)
            elapsed = time.time() - self._last_interaction
            if elapsed >= self._idle_timeout:
                logger.info("Idle timeout reached (%ds). Shutting down.", self._idle_timeout)
                if self._httpd:
                    self._httpd.shutdown()
                sys.exit(0)

    def wait_for_command(self) -> None:
        idle_thread = threading.Thread(target=self.run_idle_loop, daemon=True)
        idle_thread.start()
        idle_thread.join()
