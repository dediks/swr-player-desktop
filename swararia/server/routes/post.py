"""POST dispatch: security gates, body parsing and API routing."""
from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from swararia.core import fs_browser
from swararia.remote.secure_remote import RemoteError
from swararia.server import hub, network, state, system, updates


class PostRoutesMixin:
    """POST dispatch: security gates, body parsing and API routing."""


    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        is_local = self.is_localhost_client()

        if not is_local:
            if not hub.remote_hub.enabled:
                self.send_json(403, {"error": "FOH Mobile Remote sedang dinonaktifkan di Master Deck."})
                return
            if path not in ("/api/remote/command", "/api/remote/upload"):
                self.send_json(403, {"error": "Operasi ini hanya diizinkan langsung dari Master Laptop/PC."})
                return

        if path == "/api/remote/upload":
            self._handle_post_remote_upload()
            return

        self.close_connection = True
        if path.startswith('/api/native-remote') and not self.local_request_allowed(write=True):
            self.send_json(403, {'code': 'local_origin_required'})
            return
        if path.startswith('/api/native-remote') and self.path not in ('/api/native-remote', '/api/native-remote/claim', '/api/native-remote/wsl-bridge'):
            self.send_json(404, {'code': 'unknown_route'})
            return
        if self.headers.get('Content-Type', '').split(';')[0].lower() != 'application/json':
            self.send_json(415, {'code': 'json_required'})
            return
        lengths = self.headers.get_all('Content-Length', [])
        if self.headers.get_all('Transfer-Encoding') or len(lengths) != 1 or not re.fullmatch(r'[0-9]{1,8}', lengths[0]):
            self.send_json(400, {'code': 'invalid_length'})
            return
        content_length = int(lengths[0])
        if content_length > (262144 if path == '/api/remote/state' else 4096 if path.startswith('/api/native-remote') else 1024 * 1024):
            self.send_json(413, {'code': 'body_too_large'})
            return
        body = self.rfile.read(content_length)
        try:
            payload = json.loads(body.decode('utf-8'))
            if not isinstance(payload, dict):
                raise ValueError()
        except (ValueError, UnicodeError, RecursionError):
            self.send_json(400, {'code': 'invalid_json'})
            return
        if path == '/api/native-remote/claim':
            if set(payload) != {'command_id', 'master_instance'}:
                self.send_json(400, {'code': 'invalid_fields'})
            else:
                self.send_json(200, self.server.secure_remote.claim(payload['command_id'], payload['master_instance']))
            return
        if path == '/api/native-remote/wsl-bridge':
            op = payload.get('action', 'status')
            if op not in ('status', 'enable', 'disable') or (set(payload) - {'action', 'bind_ip', 'port'}):
                self.send_json(400, {'code': 'invalid_fields'})
                return
            http_port, eff_port, eff_ip, enabled, bind_port = self._resolve_native_bridge_target(payload)
            if op == 'status':
                bridge = network.get_native_remote_bridge_status(
                    http_port,
                    public_port=eff_port,
                    wifi_ip=eff_ip,
                    native_enabled=enabled,
                    native_local_port=bind_port,
                )
                self.send_json(200, {'ok': True, 'bridge': bridge})
            elif op == 'disable':
                ok_bridge, msg_bridge, bridge = network.disable_native_remote_bridge(
                    http_port,
                    public_port=eff_port,
                    wifi_ip=eff_ip,
                    native_enabled=enabled,
                    native_local_port=bind_port,
                )
                self.send_json(200 if ok_bridge else 409, {'ok': ok_bridge, 'message': msg_bridge, 'bridge': bridge})
            else:
                ok_bridge, msg_bridge, bridge = network.enable_native_remote_bridge(
                    http_port,
                    public_port=eff_port,
                    wifi_ip=eff_ip,
                    native_enabled=enabled,
                    native_local_port=bind_port,
                )
                self.send_json(200 if ok_bridge else 409, {'ok': ok_bridge, 'message': msg_bridge, 'bridge': bridge})
            return
        if path == '/api/native-remote':
            self.native_admin(payload)
            return

        if path == "/api/remote/wsl-bridge":
            if not is_local:
                self.send_json(403, {"error": "Hanya Master Deck lokal yang dapat mengatur bridge WSL2."})
                return
            actual_port = int(self.server.server_address[1])
            if payload.get("action") == "disable":
                ok_bridge, msg_bridge, status_bridge = network.disable_wsl_port_bridge(actual_port)
            else:
                ok_bridge, msg_bridge, status_bridge = network.enable_wsl_port_bridge(actual_port)
            self.send_json(200 if ok_bridge else 400, {
                "ok": ok_bridge,
                "message": msg_bridge,
                "wsl_bridge": status_bridge,
            })
        elif path == "/api/remote/state":
            if not is_local:
                self.send_json(403, {"error": "Hanya Master Deck lokal yang dapat memperbarui state."})
                return
            native_master = 'master_instance' in payload or 'native_acks' in payload
            if native_master and not self.local_request_allowed(write=True):
                self.send_json(403, {'code': 'local_origin_required'})
                return
            if payload.get("regenerate_pin"):
                hub.remote_hub.regenerate_pin()
            if "enabled" in payload and isinstance(payload["enabled"], bool):
                hub.remote_hub.set_enabled(payload["enabled"])
            if native_master:
                try:
                    commands = self.server.secure_remote.master_poll(
                        payload.get('state'), payload.get('native_acks', []),
                        payload.get('master_instance'), catalog=payload.get('native_catalog'),
                    )
                except RemoteError as exc:
                    self.send_json(exc.status, {'code': exc.code})
                    return
                active_count = hub.remote_hub.get_active_remotes_count()
                current_pin, is_enabled = hub.remote_hub.pin, hub.remote_hub.enabled
            else:
                commands, active_count, current_pin, is_enabled = hub.remote_hub.update_master_state(payload.get("state"), include_native=False)
            active_list, blocked_list = hub.remote_hub.get_clients_lists()
            response_body = {
                "ok": True,
                "commands": commands,
                "active_remotes_count": active_count,
                "active_remotes": active_list,
                "blocked_remotes": blocked_list,
                "pin": current_pin,
                "enabled": is_enabled,
                "upload_library_version": hub.remote_hub.get_library_revision(),
                "port": int(self.server.server_address[1]),
            }
            if native_master:
                response_body["native_catalog_synced"] = bool(self.server.secure_remote.tracks_synced)
            self.send_json(200, response_body)
        elif path == "/api/remote/clients":
            if not is_local:
                self.send_json(403, {"error": "Hanya Master Deck lokal yang dapat mengelola koneksi HP."})
                return
            op = str(payload.get("action") or "").strip().lower()
            target_cid = str(payload.get("client_id") or "").strip()
            if op == "kick" and target_cid:
                hub.remote_hub.kick_client(target_cid)
            elif op == "kick_all":
                hub.remote_hub.kick_all_clients()
            elif op == "unblock" and target_cid:
                hub.remote_hub.unblock_client(target_cid)
            elif op == "set_role" and target_cid:
                hub.remote_hub.set_client_role(target_cid, str(payload.get("role") or "control"))
            else:
                self.send_json(400, {"ok": False, "error": "Perintah manajemen perangkat tidak valid."})
                return
            active_list, blocked_list = hub.remote_hub.get_clients_lists()
            self.send_json(200, {
                "ok": True,
                "active_remotes_count": len(active_list),
                "active_remotes": active_list,
                "blocked_remotes": blocked_list,
            })
        elif path == "/api/remote/upload-config":
            self._handle_post_upload_config(payload)
        elif path == "/api/sync/pair":
            self._handle_post_sync_pair(payload)
        elif path == "/api/sync/refresh":
            self._handle_post_sync_refresh(payload)
        elif path == "/api/sync/disconnect":
            self._handle_post_sync_disconnect(payload)
        elif path == "/api/sync/preview":
            self._handle_post_sync_preview(payload)
        elif path == "/api/sync/start":
            self._handle_post_sync_start(payload)
        elif path == "/api/remote/command":
            if not hub.remote_hub.enabled:
                self.send_json(403, {"ok": False, "error": "FOH Mobile Remote sedang dinonaktifkan.", "code": "DISABLED"})
                return
            pin = self.headers.get("X-FOH-PIN") or payload.get("pin")
            if not hub.remote_hub.verify_pin(pin):
                self.send_json(401, {"ok": False, "error": "PIN FOH Remote tidak valid.", "code": "INVALID_PIN"})
                return
            action = str(payload.get("action") or "").strip()
            if action not in hub.ALLOWED_REMOTE_ACTIONS:
                self.send_json(400, {"ok": False, "error": f"Action '{action}' tidak dikenali."})
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
            if hub.remote_hub.get_client_role(client_id) == "view_only":
                self.send_json(403, {
                    "ok": False,
                    "error": "HP ini diatur ke mode Pantau Saja (View-Only) oleh Master Deck.",
                    "code": "VIEW_ONLY",
                })
                return
            ua = self.headers.get("User-Agent") or ""
            cmd = hub.remote_hub.enqueue_command(action, payload.get("params"), client_id, client_ip, client_tag, ua)
            self.send_json(200, {"ok": True, "command_id": cmd["id"]})
        elif path == "/api/download":
            status, resp = state.task_manager.enqueue_downloads(payload)
            self.send_json(status, resp)
        elif path == "/api/cookies":
            self._handle_post_cookies(payload)
        elif path == "/api/cookies/login/start":
            if not self.local_request_allowed(write=True):
                self.send_json(403, {"ok": False, "error": "Permintaan tulis lokal tidak diizinkan."})
                return
            reset_profile = bool((payload or {}).get("reset_profile", False))
            status_code, resp = system.login_session.start_login(reset_profile=reset_profile)
            self.send_json(status_code, resp)
        elif path == "/api/cookies/login/cancel":
            if not self.local_request_allowed(write=True):
                self.send_json(403, {"ok": False, "error": "Permintaan tulis lokal tidak diizinkan."})
                return
            self.send_json(200, system.login_session.cancel_login())
        elif path == "/api/search":
            self._handle_post_search(payload)
        elif path == "/api/playlist/inspect":
            self._handle_post_playlist_inspect(payload)
        elif path == "/api/scan":
            self._handle_post_scan(payload)
        elif path == "/api/browse-dirs":
            self._handle_post_browse_dirs(payload)
        elif path == "/api/probe":
            self._handle_post_probe(payload)
        elif path == "/api/resample":
            status, resp = state.task_manager.enqueue_resample(payload)
            self.send_json(status, resp)
        elif path == "/api/open-folder":
            self._handle_post_open_folder(payload)
        elif path == "/api/rename":
            self._handle_post_rename(payload)
        elif path == "/api/batch-rename":
            self._handle_post_batch_rename(payload)
        elif path == "/api/organize-folders":
            self._handle_post_organize_folders(payload)
        elif path == "/api/undo-rename":
            ok, msg, pairs = fs_browser.undo_last_batch_rename()
            if ok:
                for cur, restored in pairs:
                    state.metadata_cache.rename_entry(cur, restored)
            self.send_json(200 if ok else 409, {"ok": ok, "message": msg} if ok else {"error": msg})
        elif path == "/api/delete":
            self._handle_post_delete(payload)
        elif path == "/api/splitter/inspect":
            self._handle_post_splitter_inspect(payload)
        elif path == "/api/splitter/detect":
            self._handle_post_splitter_detect(payload)
        elif path == "/api/splitter/youtube-info":
            self._handle_post_splitter_youtube_info(payload)
        elif path == "/api/splitter/execute":
            status, resp = state.task_manager.enqueue_split(payload)
            self.send_json(status, resp)
        elif path == "/api/install-update":
            if not is_local:
                self.send_json(403, {"ok": False, "error": "Hanya Master Deck lokal yang dapat memasang pembaruan."})
                return
            download_url = str(payload.get("download_url") or "").strip()
            ok_inst, msg_inst = updates.install_update_package(download_url)
            self.send_json(200 if ok_inst else 400, {
                "ok": ok_inst,
                "message": msg_inst,
            })
        else:
            self.send_error(404, "Endpoint not found")
