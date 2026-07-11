import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from backend.human_approval.router import handle_request

logger = logging.getLogger("afos.backend.human_approval")


class _ApprovalRequestHandler(BaseHTTPRequestHandler):
    server_version = "AFOSHumanApproval/1.0"

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        logger.info("human_approval http: " + format, *args)

    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query)
        try:
            status, content_type, body = handle_request("GET", parsed.path, query)
        except Exception as exc:
            logger.error("human_approval http: unhandled error for %s: %s", parsed.path, exc)
            status, content_type, body = 500, "application/json; charset=utf-8", b'{"error": "internal server error"}'
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def build_server(host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), _ApprovalRequestHandler)


def run_server(host: str = "127.0.0.1", port: int = 8601) -> None:
    server = build_server(host, port)
    logger.info("AFOS human approval API serving on http://%s:%d", host, server.server_address[1])
    try:
        server.serve_forever()
    finally:
        server.server_close()
