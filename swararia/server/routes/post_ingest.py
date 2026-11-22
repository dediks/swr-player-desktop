"""Ingest endpoints: YouTube cookies, search and playlist inspection."""
from __future__ import annotations

import os
import tempfile

from swararia.core import audio_manager
from swararia.server import state, system


class IngestPostMixin:
    """Ingest endpoints: YouTube cookies, search and playlist inspection."""


    def _handle_post_cookies(self, payload: dict) -> None:
        if not self.local_request_allowed(write=True):
            self.send_json(403, {"ok": False, "error": "Permintaan tulis lokal tidak diizinkan."})
            return
        action = str((payload or {}).get("action") or "verify").strip().lower()
        if action == "verify":
            self.send_json(200, {"ok": True, **system.check_system_binaries(verify_cookies=True)})
            return
        if action == "delete":
            try:
                if state.COOKIES_FILE.is_file():
                    state.COOKIES_FILE.unlink()
            except OSError as exc:
                self.send_json(500, {"ok": False, "error": f"Gagal menghapus file cookies: {exc}"})
                return
            self.send_json(200, {"ok": True, **system.check_system_binaries(verify_cookies=False)})
            return
        if action == "save":
            raw_content = (payload or {}).get("content")
            if not isinstance(raw_content, str):
                self.send_json(400, {"ok": False, "error": "Konten cookies.txt harus berupa teks."})
                return
            normalized = raw_content.replace("\r\n", "\n").strip() + "\n"
            raw_bytes = normalized.encode("utf-8", errors="ignore")
            if len(raw_bytes) <= 2 or len(raw_bytes) > 524288:
                self.send_json(400, {"ok": False, "error": "Ukuran cookies.txt tidak valid (maksimal 512 KB)."})
                return
            has_valid_row = False
            for line in normalized.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped.startswith("#HttpOnly_"):
                    stripped = stripped[len("#HttpOnly_"):]
                elif stripped.startswith("#"):
                    continue
                parts = stripped.split("\t")
                if len(parts) >= 7:
                    dom = parts[0].strip().lower()
                    if "youtube.com" in dom or "google.com" in dom:
                        has_valid_row = True
                        break
            if not has_valid_row:
                self.send_json(400, {
                    "ok": False,
                    "error": "Format cookies.txt tidak valid atau tidak memuat cookie YouTube/Google format Netscape.",
                })
                return
            try:
                state.COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp_name = tempfile.mkstemp(prefix=".cookies.", suffix=".tmp", dir=str(state.COOKIES_FILE.parent))
                try:
                    os.write(fd, raw_bytes)
                finally:
                    os.close(fd)
                if os.name == "posix":
                    os.chmod(tmp_name, 0o600)
                os.replace(tmp_name, state.COOKIES_FILE)
            except Exception as exc:
                self.send_json(500, {"ok": False, "error": f"Gagal menyimpan cookies.txt: {exc}"})
                return
            self.send_json(200, {"ok": True, **system.check_system_binaries(verify_cookies=True)})
            return
        self.send_json(400, {"ok": False, "error": "Action /api/cookies tidak valid."})

    def _handle_post_search(self, payload: dict) -> None:
        query = str((payload or {}).get("query") or "").strip()
        if not query:
            self.send_json(400, {"ok": False, "error": "Kata kunci pencarian tidak boleh kosong."})
            return
        search_type = str((payload or {}).get("type") or "songs").strip().lower()
        limit = min(max(int((payload or {}).get("limit") or 15), 1), 50)
        use_cookies = bool((payload or {}).get("use_cookies", True))
        cookies = state.COOKIES_FILE if use_cookies and state.COOKIES_FILE.is_file() else None

        try:
            results = audio_manager.search_youtube_music(
                query=query,
                search_type=search_type,
                limit=limit,
                cookies_file=cookies,
            )
            self.send_json(200, {
                "ok": True,
                "query": query,
                "type": search_type,
                "count": len(results),
                "results": results,
            })
        except Exception as exc:
            self.send_json(500, {"ok": False, "error": f"Pencarian gagal: {str(exc)}"})

    def _handle_post_playlist_inspect(self, payload: dict) -> None:
        target_url = (payload or {}).get("url") or (payload or {}).get("playlist_url")
        if not target_url:
            self.send_json(400, {"ok": False, "error": "Parameter url diperlukan."})
            return
        try:
            limit = min(max(int((payload or {}).get("limit") or 100), 1), 300)
        except Exception:
            limit = 100
        use_cookies = bool((payload or {}).get("use_cookies", True))
        cookies = state.COOKIES_FILE if use_cookies and state.COOKIES_FILE.is_file() else None
        try:
            data = audio_manager.inspect_playlist(str(target_url).strip(), limit=limit, cookies_file=cookies)
            if not data:
                self.send_json(404, {"ok": False, "error": "Tidak dapat memuat daftar lagu dari playlist/album ini."})
                return
            self.send_json(200, {"ok": True, **data})
        except Exception as exc:
            self.send_json(500, {"ok": False, "error": f"Gagal membaca playlist: {str(exc)}"})
