import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from backend.deployment.router import handle_request

logger = logging.getLogger("afos.backend.deployment")


class _DeploymentRequestHandler(BaseHTTPRequestHandler):
    server_version = "AFOSDeployment/1.0"

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        logger.info("deployment http: " + format, *args)

    def _respond(self, method: str) -> None:
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query)
        body = b""
        if method == "POST":
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length) if length else b""
        try:
            status, content_type, response_body = handle_request(method, parsed.path, query, body)
        except Exception as exc:
            logger.error("deployment http: unhandled error for %s %s: %s", method, parsed.path, exc)
            status, content_type, response_body = 500, "application/json; charset=utf-8", b'{"error": "internal server error"}'
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(response_body)))
        self.end_headers()
        self.wfile.write(response_body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        self._respond("GET")

    def do_POST(self) -> None:  # noqa: N802 - stdlib method name
        self._respond("POST")


def build_server(host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), _DeploymentRequestHandler)


def run_server(host: str = "127.0.0.1", port: int = 8602) -> None:
    server = build_server(host, port)
    logger.info("AFOS deployment API serving on http://%s:%d", host, server.server_address[1])
    try:
        server.serve_forever()
    finally:
        server.server_close()
