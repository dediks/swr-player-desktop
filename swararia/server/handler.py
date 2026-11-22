"""Secure HTTP request handler: gates, headers and JSON helpers."""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

from swararia.server.routes.native_admin import NativeAdminMixin
from swararia.server.routes.get import GetRoutesMixin
from swararia.server.routes.post import PostRoutesMixin
from swararia.server.routes.post_ingest import IngestPostMixin
from swararia.server.routes.post_library import LibraryPostMixin
from swararia.server.routes.post_splitter import SplitterPostMixin
from swararia.server.routes.post_sync import SyncRoutesMixin
from swararia.server.routes.post_upload import UploadPostMixin
from swararia.server.routes.streaming import StreamingMixin


class AudioServerHandler(
    NativeAdminMixin,
    GetRoutesMixin,
    PostRoutesMixin,
    IngestPostMixin,
    LibraryPostMixin,
    SplitterPostMixin,
    UploadPostMixin,
    SyncRoutesMixin,
    StreamingMixin,
    BaseHTTPRequestHandler,
):
    """Secure HTTP Request Handler with FOH Mobile Remote LAN Gate."""

    protocol_version = "HTTP/1.1"
    timeout = 60.0

    def is_localhost_client(self) -> bool:
        # Never authorize from Host, X-Forwarded-For or client-provided tags.
        return str(self.client_address[0]) in ('127.0.0.1', '::1', '::ffff:127.0.0.1')

    def local_request_allowed(self, write=False):
        if not self.is_localhost_client():
            return False
        port = self.server.server_address[1]
        host = self.headers.get('Host', '')
        if len(self.headers.get_all('Host', [])) != 1:
            return False
        try:
            parsed_host = urlparse(f'http://{host}')
            host_port = parsed_host.port
        except ValueError:
            return False
        if (parsed_host.username or parsed_host.password or parsed_host.path
                or parsed_host.query or parsed_host.fragment or '@' in host):
            return False
        host_name = (parsed_host.hostname or '').strip().lower()
        if host_name not in ('127.0.0.1', 'localhost', '::1'):
            return False
        if host_port is not None and host_port != port:
            return False
        if host_port is None and port != 80:
            return False
        origin = self.headers.get('Origin')
        if len(self.headers.get_all('Origin', [])) > 1 or (write and not origin):
            return False
        if origin:
            parsed = urlparse(origin)
            orig_host = (parsed.hostname or '').strip().lower()
            if orig_host not in ('127.0.0.1', 'localhost', '::1'):
                return False
            if parsed.port is not None and parsed.port != port:
                return False
        if self.headers.get('Sec-Fetch-Site') not in (None, 'same-origin', 'none'):
            return False
        return True

    def parse_request(self):
        valid = super().parse_request()
        if valid and self.is_localhost_client() and not self.local_request_allowed():
            self.close_connection = True
            self.send_json(403, {'code': 'local_origin_required'})
            return False
        return valid

    def log_error(self, format, *args):
        # In HTTP/1.1, persistent keep-alive client sockets naturally close when idle.
        # Suppress benign socket timeout log so it does not pollute the server console.
        msg = format % args
        if "Request timed out" in msg:
            return
        self.log_message(format, *args)

    def log_message(self, format, *args):
        # Request lines and parser errors may contain credentials supplied by
        # clients, including rejected query strings. Do not log their contents.
        return

    def send_security_headers(self, content_type: str = "application/json"):
        self.send_header("Content-Type", content_type)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data: https://*.googleusercontent.com https://*.ytimg.com https://*.ggpht.com; "
            "media-src 'self' blob:; "
            "frame-src 'self' https://www.youtube-nocookie.com https://www.youtube.com; "
            "connect-src 'self'; "
            "frame-ancestors 'none';",
        )

    def send_json(self, status: int, data: dict):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_security_headers("application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
