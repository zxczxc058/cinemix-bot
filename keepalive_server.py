"""Minimal HTTP health endpoint for the Telegram bot workflow."""

import logging
import os
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

DEFAULT_KEEPALIVE_PORT = 8082


class _KeepAliveHandler(BaseHTTPRequestHandler):
    RESPONSE_BODY = b"OK"

    def do_GET(self):
        from cinemix_stream_gateway import handle_portal_stream
        if handle_portal_stream(self):
            return
        from web_streaming import handle_web_request
        if handle_web_request(self):
            return
        self._send_health_response(include_body=True)

    def do_HEAD(self):
        from cinemix_stream_gateway import handle_portal_stream
        if handle_portal_stream(self, head=True):
            return
        from web_streaming import handle_web_request
        if handle_web_request(self, head=True):
            return
        self._send_health_response(include_body=False)

    def _send_health_response(self, include_body):
        path = self.path.partition("?")[0]
        if path in {"/status", "/readyz", "/health/polling"}:
            health = getattr(self.server, "polling_health", None)
            status = health.snapshot() if health is not None else {
                "state": "unavailable", "ready": False,
            }
            body = json.dumps(status).encode("utf-8")
            code = 200 if path == "/status" or status["ready"] else 503
            content_type = "application/json"
        else:
            body = getattr(self.server, "health_response_body", self.RESPONSE_BODY)
            code = 200
            content_type = "text/plain; charset=utf-8"
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def log_message(self, format_string, *args):
        return


def start_keepalive_server(port=None, *, response_body=b"OK", polling_health=None):
    """Start the HTTP health endpoint on a daemon thread and return its server."""
    if port is None:
        port = int(os.environ.get("PORT", DEFAULT_KEEPALIVE_PORT))
    server = ThreadingHTTPServer(("0.0.0.0", port), _KeepAliveHandler)
    server.health_response_body = response_body
    server.polling_health = polling_health
    thread = Thread(target=server.serve_forever, name="keepalive-http", daemon=True)
    server.keepalive_thread = thread
    thread.start()
    logging.info("Keep-alive HTTP server listening on 0.0.0.0:%s.", port)
    return server


def stop_keepalive_server(server):
    """Stop and close a server started by start_keepalive_server."""
    if server is None:
        return
    server.shutdown()
    server.server_close()
    thread = getattr(server, "keepalive_thread", None)
    if thread is not None:
        thread.join(timeout=2)
