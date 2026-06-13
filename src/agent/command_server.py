import hmac
import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from src.agent.config import IDLE_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

COMMAND_TOKEN_ENV = "RENDERPR_COMMAND_TOKEN"


def _get_command_token() -> bytes:
    token = os.environ.get(COMMAND_TOKEN_ENV, "")
    return token.encode("utf-8") if token else b""


class CommandHandler(BaseHTTPRequestHandler):
    server_instance: "CommandServer | None" = None

    def log_message(self, format, *args):
        logger.debug("HTTP: %s %s", self.client_address, format % args)

    def do_POST(self):
        if self.path != "/__renderpr/command":
            self._respond(404, {"error": "Not found"})
            return

        if not self._verify_token():
            self._respond(401, {"error": "Invalid token"})
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

        self._respond(202, {"status": "accepted", "command": cmd})
        self._run_async(cmd, query)

    def _verify_token(self) -> bool:
        server = self.server_instance
        if server is None or not server._token:
            return False
        provided = self.headers.get("X-RenderPR-Token", "")
        if not provided:
            return False
        return hmac.compare_digest(provided.encode("utf-8"), server._token)

    def _run_async(self, command: str | None, query: str | None) -> None:
        server = self.server_instance
        if server is None:
            return
        server.reset_idle_timer()

        def runner():
            try:
                server.dispatch(command, query)
            except Exception:
                logger.exception("Async command dispatch failed")

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()

    def _respond(self, status: int, data: dict) -> None:
        body = json.dumps(data).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


class CommandServer:
    def __init__(
        self,
        handle_change_fn: Callable,
        handle_apply_fn: Callable,
        handle_reject_fn: Callable,
        handle_review_fn: Callable | None = None,
        host: str = "0.0.0.0",
        port: int = 3001,
        idle_timeout: int | None = None,
        token: bytes | None = None,
    ):
        self._handle_change = handle_change_fn
        self._handle_apply = handle_apply_fn
        self._handle_reject = handle_reject_fn
        self._handle_review = handle_review_fn
        self._host = host
        self._port = port
        self._idle_timeout = idle_timeout or IDLE_TIMEOUT_SECONDS
        self._token = token if token is not None else _get_command_token()
        self._last_interaction = time.time()
        self._httpd: ThreadingHTTPServer | None = None
        self._last_interaction_lock = threading.Lock()

    def reset_idle_timer(self) -> None:
        with self._last_interaction_lock:
            self._last_interaction = time.time()

    def dispatch(self, command: str | None, query: str | None) -> None:
        if command == "change":
            self.handle_change(query or "")
        elif command == "apply":
            self.handle_apply()
        elif command == "reject":
            self.handle_reject()
        elif command == "review":
            self.handle_review()
        else:
            logger.warning("Unknown command: %s", command)

    def handle_change(self, query: str) -> None:
        try:
            self._handle_change(query)
        except Exception:
            logger.exception("handle_change failed")

    def handle_apply(self) -> None:
        try:
            self._handle_apply()
        except Exception:
            logger.exception("handle_apply failed")

    def handle_reject(self) -> None:
        try:
            self._handle_reject()
        except Exception:
            logger.exception("handle_reject failed")

    def handle_review(self) -> None:
        if self._handle_review is None:
            logger.warning("handle_review not configured, ignoring")
            return
        try:
            self._handle_review()
        except Exception:
            logger.exception("handle_review failed")

    def start(self) -> None:
        CommandHandler.server_instance = self
        if not self._token:
            logger.warning(
                "RENDERPR_COMMAND_TOKEN is not set — all dispatch requests will be rejected with 401"
            )
        self._httpd = ThreadingHTTPServer((self._host, self._port), CommandHandler)
        thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        thread.start()
        logger.info("Command server listening on %s:%s", self._host, self._port)

    def run_idle_loop(self) -> None:
        logger.info("Idle timeout set to %d seconds", self._idle_timeout)
        while True:
            time.sleep(10)
            with self._last_interaction_lock:
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
