"""Local HTTP server and static asset host for the Phase 12 dashboard.

Security posture: loopback-only by default, same-origin requests only, a strict
CSP for scripts (no inline JavaScript), no CORS headers, no directory listing,
path-traversal checks and explicit request-size limits. There is no
authentication, so the CLI refuses a non-loopback bind unless the operator
explicitly opts in; the UI states that it is a local single-user tool.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from exam_engine.dashboard.api import ApiResponse, DashboardAPI
from exam_engine.dashboard.errors import ApiError

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    # Hairline injects a scoped <style> element; styles may be inline, scripts
    # may not. User data is always written with textContent, never innerHTML.
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    ),
}

_UPLOAD_ROUTE = re.compile(r"^/api/courses/([^/]+)/documents/?$")
_BOUNDARY_RE = re.compile(r"boundary=(?:\"([^\"]+)\"|([^;]+))", re.IGNORECASE)


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, default=str, allow_nan=False).encode("utf-8")


class DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second process bind a port that is already
    # listening: the CLI would report success while the older server keeps
    # answering. POSIX needs the option to rebind after TIME_WAIT.
    allow_reuse_address = os.name != "nt"

    def __init__(self, address: tuple[str, int], api: DashboardAPI) -> None:
        self.api = api
        super().__init__(address, DashboardRequestHandler)

    @property
    def origin(self) -> str:
        raw_host, raw_port = self.server_address[0], self.server_address[1]
        host = raw_host.decode("ascii") if isinstance(raw_host, bytes) else raw_host
        return f"http://{host}:{int(raw_port)}"


class DashboardRequestHandler(BaseHTTPRequestHandler):
    server: DashboardHTTPServer
    server_version = "ExamEngineDashboard/0.1"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # -- HTTP verbs --------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._handle("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle("HEAD")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_OPTIONS(self) -> None:  # noqa: N802
        request_id = uuid.uuid4().hex[:12]
        self._send_json(
            ApiResponse(405, {"error": {"code": "method_not_allowed", "message": "no CORS"}}),
            request_id=request_id,
        )

    # -- core dispatch -----------------------------------------------------
    def _handle(self, method: str) -> None:
        request_id = uuid.uuid4().hex[:12]
        parsed = urlsplit(self.path)
        path = parsed.path
        query = parse_qs(parsed.query, keep_blank_values=True)
        try:
            if path == "/api" or path.startswith("/api/"):
                self._reject_cross_origin()
                if method == "HEAD":
                    raise ApiError(405, "method_not_allowed", "HEAD is not supported for the API")
                upload = _UPLOAD_ROUTE.fullmatch(path)
                if method == "POST" and upload:
                    fields, filename, data = self._read_upload()
                    response = self.server.api.ingest_upload(
                        unquote(upload.group(1)), filename, data, fields
                    )
                else:
                    body = self._read_json_body() if method == "POST" else b""
                    response = self.server.api.dispatch(method, path, query, body)
                self._send_json(response, request_id=request_id)
                return
            if method not in {"GET", "HEAD"}:
                raise ApiError(405, "method_not_allowed", "static assets are read-only")
            self._serve_static(path, head_only=method == "HEAD")
        except ApiError as exc:
            logger.info("dashboard request %s %s rejected: %s", method, path, exc.code)
            self._send_json(
                ApiResponse(exc.status, exc.as_dict(request_id=request_id)),
                request_id=request_id,
            )
        except (BrokenPipeError, ConnectionResetError):
            logger.debug("dashboard client disconnected during %s %s", method, path)
        except Exception:  # noqa: BLE001 - server fault boundary
            logger.exception("dashboard request failed: %s %s", method, path)
            self._send_json(
                ApiResponse(
                    500,
                    {
                        "error": {
                            "code": "internal_error",
                            "message": "the dashboard hit an unexpected error",
                            "detail": {},
                        },
                        "request_id": request_id,
                    },
                ),
                request_id=request_id,
            )

    # -- request validation ------------------------------------------------
    def _reject_cross_origin(self) -> None:
        origin = self.headers.get("Origin")
        if not origin:
            return
        parsed = urlsplit(origin)
        host = self.headers.get("Host", "")
        if parsed.scheme not in {"http", "https"} or parsed.netloc != host:
            raise ApiError(
                403,
                "cross_origin_rejected",
                "this dashboard accepts same-origin requests only",
            )

    def _body_limit(self) -> int:
        limits = self.server.api.settings.limits
        return min(max(limits.max_file_bytes + 2 * 1024 * 1024, 4 * 1024 * 1024), 64 * 1024 * 1024)

    def _content_length(self) -> int:
        transfer = (self.headers.get("Transfer-Encoding") or "").strip().casefold()
        if transfer and transfer != "identity":
            raise ApiError(501, "unsupported_transfer", "chunked request bodies are not supported")
        raw = self.headers.get("Content-Length")
        if raw is None:
            raise ApiError(411, "length_required", "Content-Length is required")
        try:
            length = int(raw)
        except ValueError as exc:
            raise ApiError(400, "invalid_request", "Content-Length must be an integer") from exc
        if length < 0:
            raise ApiError(400, "invalid_request", "Content-Length must not be negative")
        if length > self._body_limit():
            raise ApiError(413, "request_too_large", "the request body is too large")
        return length

    def _read_exact(self, length: int) -> bytes:
        data = self.rfile.read(length)
        if len(data) != length:
            raise ApiError(400, "incomplete_request", "the request body ended early")
        return data

    def _read_json_body(self) -> bytes:
        length = self._content_length()
        if length == 0:
            return b""
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().casefold()
        if content_type != "application/json":
            raise ApiError(415, "unsupported_media_type", "send application/json")
        return self._read_exact(length)

    def _read_upload(self) -> tuple[dict[str, str], str, bytes]:
        content_type = self.headers.get("Content-Type") or ""
        if not content_type.casefold().startswith("multipart/form-data"):
            raise ApiError(415, "unsupported_media_type", "send multipart/form-data")
        length = self._content_length()
        fields, filename, data = parse_multipart(self._read_exact(length), content_type)
        if filename is None or data is None:
            raise ApiError(400, "missing_file", "include one file field named 'file'")
        return fields, filename, data

    # -- responses ---------------------------------------------------------
    def _send_json(self, response: ApiResponse, *, request_id: str) -> None:
        try:
            body = _json_bytes(response.payload)
        except (TypeError, ValueError):
            logger.exception("dashboard response was not JSON serialisable")
            response = ApiResponse(
                500,
                {
                    "error": {
                        "code": "serialization_error",
                        "message": "the response could not be serialised",
                    }
                },
            )
            body = _json_bytes(response.payload)
        self.send_response(response.status)
        self._security_headers(request_id)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _serve_static(self, path: str, *, head_only: bool) -> None:
        relative = "index.html" if path in {"", "/", "/index.html"} else None
        if relative is None and path.startswith("/assets/"):
            relative = path[len("/assets/") :]
        if relative is None:
            raise ApiError(404, "not_found", "no such asset")
        if not relative or relative.endswith("/"):
            raise ApiError(404, "not_found", "no such asset")
        root = STATIC_DIR.resolve()
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ApiError(404, "not_found", "no such asset") from exc
        if not candidate.is_file():
            raise ApiError(404, "not_found", "no such asset")
        body = candidate.read_bytes()
        content_type = _CONTENT_TYPES.get(candidate.suffix.lower(), "application/octet-stream")
        self.send_response(200)
        self._security_headers(uuid.uuid4().hex[:12])
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        cache = "no-cache" if candidate.suffix == ".html" else "public, max-age=300"
        if "/vendor/" in candidate.as_posix():
            cache = "public, max-age=3600"
        self.send_header("Cache-Control", cache)
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _security_headers(self, request_id: str) -> None:
        for key, value in _SECURITY_HEADERS.items():
            self.send_header(key, value)
        self.send_header("X-Request-ID", request_id)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        logger.info("%s - %s", self.address_string(), format % args)


def parse_multipart(
    body: bytes,
    content_type: str,
) -> tuple[dict[str, str], str | None, bytes | None]:
    """Parse a small multipart/form-data body without third-party dependencies.

    Only one file part named ``file`` is accepted. Fields are decoded as UTF-8;
    the returned filename is still untrusted and is sanitised by the API.
    """

    match = _BOUNDARY_RE.search(content_type)
    if not match:
        raise ApiError(400, "invalid_multipart", "the multipart boundary is missing")
    boundary = match.group(1) or match.group(2) or ""
    boundary = boundary.strip()
    if not boundary or len(boundary) > 200:
        raise ApiError(400, "invalid_multipart", "the multipart boundary is invalid")

    delimiter = b"--" + boundary.encode("latin-1")
    if body.count(delimiter) > 64:
        raise ApiError(400, "invalid_multipart", "too many multipart sections")
    fields: dict[str, str] = {}
    filename: str | None = None
    file_data: bytes | None = None

    for chunk in body.split(delimiter)[1:]:
        if chunk.startswith(b"--"):
            break
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        header_blob, separator, content = chunk.partition(b"\r\n\r\n")
        if not separator:
            continue
        if content.endswith(b"\r\n"):
            content = content[:-2]
        headers: dict[str, str] = {}
        for line in header_blob.split(b"\r\n"):
            name, colon, value = line.partition(b":")
            if not colon:
                continue
            headers[name.decode("latin-1").strip().casefold()] = value.decode("latin-1").strip()
        disposition = headers.get("content-disposition", "")
        name_match = re.search(r'name="([^"]*)"', disposition, re.IGNORECASE)
        filename_match = re.search(r'filename="([^"]*)"', disposition, re.IGNORECASE)
        field_name = name_match.group(1) if name_match else ""
        if filename_match is not None and field_name == "file":
            if file_data is not None:
                raise ApiError(400, "invalid_multipart", "send exactly one file")
            filename = filename_match.group(1)
            file_data = content
        elif field_name and not filename_match:
            fields[field_name] = content.decode("utf-8", errors="replace")
    return fields, filename, file_data


def create_server(
    api: DashboardAPI,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> DashboardHTTPServer:
    return DashboardHTTPServer((host, port), api)


def serve(api: DashboardAPI, host: str = "127.0.0.1", port: int = 8765) -> None:
    server = create_server(api, host, port)
    try:
        logger.info("dashboard listening on %s", server.origin)
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()


__all__ = [
    "DashboardHTTPServer",
    "STATIC_DIR",
    "create_server",
    "parse_multipart",
    "serve",
]
