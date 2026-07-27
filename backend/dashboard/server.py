import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from backend.dashboard.router import handle_request

logger = logging.getLogger("afos.backend.dashboard")


class _DashboardRequestHandler(BaseHTTPRequestHandler):
    server_version = "AFOSDashboard/1.0"

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        logger.info("dashboard http: " + format, *args)

    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query)
        try:
            status, content_type, body = handle_request("GET", parsed.path, query)
        except Exception as exc:  # a handler bug must not crash the server thread
            logger.error("dashboard http: unhandled error for %s: %s", parsed.path, exc)
            status, content_type, body = 500, "application/json; charset=utf-8", b'{"error": "internal server error"}'
        self._respond(status, content_type, body)

    def do_POST(self) -> None:  # noqa: N802 - stdlib method name
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query)
        content_length = int(self.headers.get("Content-Length") or 0)
        request_body = self.rfile.read(content_length) if content_length > 0 else b""
        try:
            status, content_type, body = handle_request("POST", parsed.path, query, request_body)
        except Exception as exc:  # a handler bug must not crash the server thread
            logger.error("dashboard http: unhandled error for %s: %s", parsed.path, exc)
            status, content_type, body = 500, "application/json; charset=utf-8", b'{"error": "internal server error"}'
        self._respond(status, content_type, body)

    def _respond(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def build_server(host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    """Builds (but does not start) a real stdlib HTTP server backing the REST
    API defined in backend/dashboard/router.py - no new dependency (no
    FastAPI/Flask installed in this environment), but a genuine, servable
    REST API: port=0 lets the OS assign a free port, useful for tests.
    """
    return ThreadingHTTPServer((host, port), _DashboardRequestHandler)


def run_server(host: str = "127.0.0.1", port: int = 8600) -> None:
    server = build_server(host, port)
    logger.info("AFOS dashboard serving on http://%s:%d", host, server.server_address[1])
    try:
        server.serve_forever()
    finally:
        server.server_close()
