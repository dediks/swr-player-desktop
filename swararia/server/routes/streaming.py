"""File and audio streaming plus library listing helpers."""
from __future__ import annotations

import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from swararia.server import state


class StreamingMixin:
    """File and audio streaming plus library listing helpers."""


    def serve_file(self, file_path: Path, content_type: str):
        try:
            with open(file_path, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_security_headers(content_type)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except Exception as exc:
            self.send_error(500, f"Error reading file: {exc}")

    def serve_audio_stream(self, file_path: Path, content_type: str):
        """Streams an audio file with HTTP Range (206 Partial Content) support for instant seeking."""
        try:
            file_size = file_path.stat().st_size
        except OSError:
            self.send_error(404, "Audio file not found")
            return

        range_header = self.headers.get("Range")
        start = 0
        end = max(0, file_size - 1)
        is_partial = False

        if range_header and range_header.strip().startswith("bytes=") and file_size > 0:
            raw_range = range_header.strip()[len("bytes="):].split(",")[0].strip()
            if "-" in raw_range:
                start_str, end_str = raw_range.split("-", 1)
                try:
                    if start_str == "":
                        suffix_len = int(end_str)
                        start = max(0, file_size - suffix_len)
                        end = file_size - 1
                    else:
                        start = int(start_str)
                        end = int(end_str) if end_str else (file_size - 1)
                    if 0 <= start <= end < file_size:
                        is_partial = True
                    else:
                        self.send_response(416)
                        self.send_security_headers(content_type)
                        self.send_header("Content-Range", f"bytes */{file_size}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                except ValueError:
                    is_partial = False

        content_length = (end - start + 1) if file_size > 0 else 0
        try:
            with open(file_path, "rb") as f:
                if is_partial:
                    self.send_response(206)
                    self.send_security_headers(content_type)
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                    self.send_header("Content-Length", str(content_length))
                    self.end_headers()
                    f.seek(start)
                else:
                    self.send_response(200)
                    self.send_security_headers(content_type)
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Length", str(file_size))
                    self.end_headers()

                remaining = content_length
                chunk_size = 65536
                while remaining > 0:
                    chunk = f.read(min(chunk_size, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            # Normal when browser/WebView seeks or switches tracks mid-stream
            self.close_connection = True
            return
        except Exception as exc:
            sys.stderr.write(f"[Stream Error] {exc}\n")

    def serve_proxied_audio_stream(self, direct_url: str, custom_headers: dict | None = None):
        """Proxies an upstream audio stream with HTTP Range forwarding for live web preview."""
        req_headers = dict(custom_headers or {})
        if not req_headers.get("User-Agent"):
            req_headers["User-Agent"] = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        req_headers["Accept"] = "*/*"

        client_range = self.headers.get("Range")
        if client_range:
            req_headers["Range"] = client_range

        req = urllib.request.Request(direct_url, headers=req_headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                status_code = resp.status
                self.send_response(status_code)
                raw_ct = resp.headers.get("Content-Type") or "audio/webm"
                # Coerce video/* to audio/* so browser <audio> tag with nosniff won't reject audio-only webm
                if raw_ct.startswith("video/webm"):
                    content_type = "audio/webm"
                elif raw_ct.startswith("video/mp4"):
                    content_type = "audio/mp4"
                elif raw_ct.startswith("video/"):
                    content_type = "audio/" + raw_ct.split("video/", 1)[-1]
                else:
                    content_type = raw_ct

                self.send_security_headers(content_type)
                self.send_header("Accept-Ranges", "bytes")

                content_length = resp.headers.get("Content-Length")
                if content_length:
                    self.send_header("Content-Length", content_length)

                content_range = resp.headers.get("Content-Range")
                if content_range:
                    self.send_header("Content-Range", content_range)

                self.end_headers()

                chunk_size = 65536
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
            return
        except urllib.error.HTTPError as exc:
            sys.stderr.write(f"[Proxy Stream HTTP Error] {exc.code}: {exc.reason}\n")
            self.send_error(exc.code, f"Upstream error: {exc.reason}")
        except Exception as exc:
            sys.stderr.write(f"[Proxy Stream Error] {exc}\n")
            self.send_error(500, str(exc))

    def get_library_files(self, base_dir: Path | None = None) -> tuple[list[dict], list[dict]]:
        return state.metadata_cache.get_library_files(base_dir or state.INBOX_DIR)

    def scan_library(self) -> list[dict]:
        return state.metadata_cache.scan_library(state.INBOX_DIR)
