"""GET dispatch: pages, static files, streams and read-only API."""
from __future__ import annotations

import mimetypes
import re
import time
from urllib.parse import parse_qs, urlparse

from swararia.core import audio_manager
from swararia.core import fs_browser
from swararia.server import hub, network, state, system, updates


class GetRoutesMixin:
    """GET dispatch: pages, static files, streams and read-only API."""


    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        is_local = self.is_localhost_client()

        # Security Gate for LAN / Wi-Fi devices (Smartphone Remote)
        if not is_local:
            if not hub.remote_hub.enabled:
                self.send_json(403, {"error": "FOH Mobile Remote sedang dinonaktifkan di Master Deck."})
                return
            if path in ("/", "/index.html"):
                self.send_response(302)
                self.send_header("Location", "/player?remote=1")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            allowed_lan_get = path in (
                "/player",
                "/player.html",
                "/api/system",
                "/api/library",
                "/api/remote/info",
                "/api/remote/state",
            ) or path.startswith("/static/")
            if not allowed_lan_get:
                self.send_json(403, {"error": "Akses jaringan lokal hanya diizinkan untuk FOH Mobile Remote (/player)."})
                return

        if path == '/api/sync/status':
            self._handle_get_sync_status()
            return

        if path == '/api/native-remote':
            if not self.local_request_allowed():
                self.send_json(403, {'code': 'local_origin_required'})
            elif self.path != path:
                self.send_json(404, {'code': 'unknown_route'})
            else:
                self.send_json(200, self._build_native_admin_status(force_refresh=False))
            return

        if path == '/api/native-remote/wsl-bridge':
            if not self.local_request_allowed():
                self.send_json(403, {'code': 'local_origin_required'})
            elif self.path != path:
                self.send_json(404, {'code': 'unknown_route'})
            else:
                http_port, eff_port, eff_ip, enabled, bind_port = self._resolve_native_bridge_target()
                self.send_json(200, network.get_native_remote_bridge_status(
                    http_port,
                    public_port=eff_port,
                    wifi_ip=eff_ip,
                    native_enabled=enabled,
                    native_local_port=bind_port,
                ))
            return

        if path in ("/", "/index.html"):
            self.serve_file(state.STATIC_DIR / "pages" / "ingest" / "index.html", "text/html; charset=utf-8")
        elif path in ("/qc", "/qc.html"):
            self.serve_file(state.STATIC_DIR / "pages" / "qc" / "index.html", "text/html; charset=utf-8")
        elif path in ("/converter", "/converter.html"):
            self.serve_file(state.STATIC_DIR / "pages" / "converter" / "index.html", "text/html; charset=utf-8")
        elif path in ("/organizer", "/organizer.html"):
            self.serve_file(state.STATIC_DIR / "pages" / "organizer" / "index.html", "text/html; charset=utf-8")
        elif path in ("/multitrack", "/multitrack.html", "/splitter", "/splitter.html"):
            self.serve_file(state.STATIC_DIR / "pages" / "multitrack" / "index.html", "text/html; charset=utf-8")
        elif path in ("/video-split", "/video-split.html", "/videosplit", "/videosplit.html"):
            self.serve_file(state.STATIC_DIR / "pages" / "video-split" / "index.html", "text/html; charset=utf-8")
        elif path in ("/player", "/player.html"):
            self.serve_file(state.STATIC_DIR / "pages" / "player" / "index.html", "text/html; charset=utf-8")
        elif path.startswith("/static/"):
            rel_path = path[len("/static/"):]
            safe_file = (state.STATIC_DIR / rel_path).resolve()
            if safe_file.is_relative_to(state.STATIC_DIR) and safe_file.is_file():
                ext = safe_file.suffix.lower()
                if ext == ".js":
                    mime = "text/javascript; charset=utf-8"
                elif ext == ".css":
                    mime = "text/css; charset=utf-8"
                else:
                    mime, _ = mimetypes.guess_type(str(safe_file))
                self.serve_file(safe_file, mime or "application/octet-stream")
            else:
                self.send_error(404, "File not found")
        elif path == "/api/system":
            qs = parse_qs(parsed.query)
            verify_cookies = qs.get("verify_cookies", ["0"])[0] == "1"
            self.send_json(200, system.check_system_binaries(verify_cookies=verify_cookies))
        elif path == "/api/cookies/login/status":
            if not is_local:
                self.send_json(403, {"ok": False, "error": "Hanya Master Deck lokal yang dapat memeriksa sesi login."})
                return
            self.send_json(200, system.login_session.get_status())
        elif path == "/api/check-update":
            self.send_json(200, updates.check_for_updates())
        elif path == "/api/undo-rename":
            self.send_json(200, {"ok": True, "last": fs_browser.peek_rename_history()})
        elif path == "/api/remote/info":
            qs = parse_qs(parsed.query)
            force_ip = qs.get("refresh", ["0"])[0] == "1"
            interfaces = network.get_network_interfaces_detail(force_refresh=force_ip)
            lan_ips = network.get_local_lan_ips(force_refresh=False)
            actual_port = int(self.server.server_address[1])
            remote_port = network._get_wsl_remote_bridge_port(actual_port)
            active_list, blocked_list = hub.remote_hub.get_clients_lists()
            upload_cfg = hub.remote_hub.get_upload_config()
            info: dict = {
                "ok": True,
                "is_localhost": is_local,
                "enabled": hub.remote_hub.enabled,
                "upload_enabled": upload_cfg["upload_enabled"],
                "upload_dest_label": upload_cfg["upload_dest_label"],
                "port": remote_port,
                "lan_ips": lan_ips,
                "master_online": (time.time() - hub.remote_hub.master_updated_at) < 5.0,
                "active_remotes_count": len(active_list),
            }
            if is_local:
                info["pin"] = hub.remote_hub.pin
                info["upload_dest_dir"] = upload_cfg["upload_dest_dir"]
                info["active_remotes"] = active_list
                info["blocked_remotes"] = blocked_list
                info["interfaces"] = [
                    {
                        **iface,
                        "url": f"http://{iface['ip']}:{remote_port}/player?remote=1&pin={hub.remote_hub.pin}",
                    }
                    for iface in interfaces
                ]
                info["remote_urls"] = [
                    f"http://{ip}:{remote_port}/player?remote=1&pin={hub.remote_hub.pin}"
                    for ip in lan_ips
                ]
                info["short_urls"] = [
                    f"http://{ip}:{remote_port}"
                    for ip in lan_ips
                ]
                info["wsl_bridge"] = network.get_wsl_bridge_status(actual_port)
            self.send_json(200, info)
        elif path == "/api/remote/state":
            if not hub.remote_hub.enabled:
                self.send_json(403, {"ok": False, "error": "FOH Mobile Remote sedang dinonaktifkan.", "code": "DISABLED"})
                return
            qs = parse_qs(parsed.query)
            pin = self.headers.get("X-FOH-PIN") or qs.get("pin", [None])[0]
            if not hub.remote_hub.verify_pin(pin):
                self.send_json(401, {"ok": False, "error": "PIN FOH Remote tidak valid atau belum dimasukkan.", "code": "INVALID_PIN"})
                return
            client_ip = str(self.client_address[0])
            raw_tag = (self.headers.get("X-FOH-Client-ID") or "mobile").strip()
            client_tag = re.sub(r"[^a-zA-Z0-9_\-]", "", raw_tag)[:32] or "mobile"
            client_id = f"{client_ip}:{client_tag}"
            if hub.remote_hub.is_client_blocked(client_id):
                self.send_json(403, {
                    "ok": False,
                    "error": "Koneksi HP ini telah diputus oleh operator Master Deck.",
                    "code": "KICKED",
                })
                return
            ua = self.headers.get("User-Agent") or ""
            self.send_json(200, hub.remote_hub.get_remote_state(client_id, client_ip, client_tag, ua))
        elif path == "/api/queue":
            self.send_json(200, state.task_manager.get_snapshot(history_limit=10))
        elif path == "/api/library":
            qs = parse_qs(parsed.query)
            raw_src = qs.get("source_dir", [None])[0]
            try:
                src_dir = state.resolve_safe_dir(raw_src, state.INBOX_DIR, must_exist=True) if raw_src else state.INBOX_DIR.resolve()
                files_data, playlists_data = self.get_library_files(src_dir)
                self.send_json(200, {
                    "files": files_data,
                    "playlists": playlists_data,
                    "source_dir": str(src_dir),
                    "default_inbox": str(state.INBOX_DIR.resolve()),
                })
            except Exception as exc:
                self.send_json(400, {"error": str(exc)})
        elif path == "/api/stream":
            qs = parse_qs(parsed.query)
            raw_src = qs.get("source_dir", [None])[0]
            rel_path = qs.get("rel_path", [None])[0]
            try:
                target_file, mime_type = fs_browser.resolve_safe_audio_file(raw_src, rel_path, state.INBOX_DIR)
                self.serve_audio_stream(target_file, mime_type)
            except FileNotFoundError as exc:
                self.send_json(404, {"error": str(exc)})
            except Exception as exc:
                self.send_json(400, {"error": str(exc)})
        elif path == "/api/stream-preview":
            qs = parse_qs(parsed.query)
            target_url = qs.get("url", [None])[0]
            if not target_url:
                self.send_json(400, {"error": "Parameter url diperlukan."})
                return
            cookies = state.COOKIES_FILE if state.COOKIES_FILE.is_file() else None
            stream_info = audio_manager.get_preview_stream_info(target_url, cookies_file=cookies)
            if not stream_info:
                self.send_json(404, {"error": "Stream audio pratinjau tidak ditemukan atau gagal diekstrak."})
                return
            direct_url, headers = stream_info
            self.serve_proxied_audio_stream(direct_url, headers)
        elif path == "/api/playlist/inspect":
            qs = parse_qs(parsed.query)
            target_url = qs.get("url", [None])[0]
            if not target_url:
                self.send_json(400, {"ok": False, "error": "Parameter url diperlukan."})
                return
            try:
                limit = min(max(int(qs.get("limit", [100])[0]), 1), 300)
            except Exception:
                limit = 100
            use_cookies = qs.get("use_cookies", ["1"])[0] != "0"
            cookies = state.COOKIES_FILE if use_cookies and state.COOKIES_FILE.is_file() else None
            try:
                data = audio_manager.inspect_playlist(target_url, limit=limit, cookies_file=cookies)
                if not data:
                    self.send_json(404, {"ok": False, "error": "Tidak dapat memuat daftar lagu dari playlist/album ini."})
                    return
                self.send_json(200, {"ok": True, **data})
            except Exception as exc:
                self.send_json(500, {"ok": False, "error": f"Gagal membaca playlist: {str(exc)}"})
        else:
            self.send_error(404, "Endpoint not found")
