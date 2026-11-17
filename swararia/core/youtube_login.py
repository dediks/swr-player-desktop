#!/usr/bin/env python3
"""Interactive browser login session manager for YouTube Music Premium on Desktop.

Launches an isolated Chromium/Chrome/Edge/Brave profile window via Chrome DevTools
Protocol (CDP) on 127.0.0.1, detects completed Google/YouTube authentication cleanly,
verifies active account tier via audio_manager.check_youtube_cookies_tier(), persists
canonical Netscape cookies.txt (0600 on POSIX), and closes the login window automatically.
"""
from __future__ import annotations

import atexit
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from swararia.core import audio_manager

_STRONG_YT_AUTH_COOKIES = frozenset({
    "SAPISID",
    "SID",
    "__Secure-1PSID",
    "__Secure-3PSID",
    "LOGIN_INFO",
})
_CDP_MAX_FRAME_BYTES = 8 * 1024 * 1024


def find_chromium_browser() -> tuple[str | None, str | None]:
    """Finds an available Chromium-family browser executable and friendly display name."""
    candidates: list[tuple[str, str]] = []
    if os.name == "nt":
        program_files = [
            os.environ.get("PROGRAMFILES", r"C:\Program Files"),
            os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", ""),
        ]
        rel_map = [
            (r"Microsoft\Edge\Application\msedge.exe", "Microsoft Edge"),
            (r"Google\Chrome\Application\chrome.exe", "Google Chrome"),
            (r"BraveSoftware\Brave-Browser\Application\brave.exe", "Brave"),
        ]
        for base in program_files:
            if not base:
                continue
            for rel, label in rel_map:
                p = Path(base) / rel
                if p.is_file():
                    return str(p), label
    elif sys.platform == "darwin":
        candidates.extend([
            ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "Google Chrome"),
            ("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge", "Microsoft Edge"),
            ("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser", "Brave"),
            ("/Applications/Chromium.app/Contents/MacOS/Chromium", "Chromium"),
        ])
        for path_str, label in candidates:
            if os.path.isfile(path_str) and os.access(path_str, os.X_OK):
                return path_str, label

    linux_bins = [
        ("google-chrome", "Google Chrome"),
        ("google-chrome-stable", "Google Chrome"),
        ("chromium-browser", "Chromium"),
        ("chromium", "Chromium"),
        ("microsoft-edge", "Microsoft Edge"),
        ("microsoft-edge-stable", "Microsoft Edge"),
        ("brave-browser", "Brave"),
    ]
    for bin_name, label in linux_bins:
        found = shutil.which(bin_name)
        if found:
            return found, label
    return None, None


def _is_allowed_cookie_domain(domain: str) -> bool:
    clean = (domain or "").strip().lower().lstrip(".")
    return (
        clean == "youtube.com"
        or clean.endswith(".youtube.com")
        or clean == "google.com"
        or clean.endswith(".google.com")
    )


def _is_youtube_cookie_domain(domain: str) -> bool:
    clean = (domain or "").strip().lower().lstrip(".")
    return clean == "youtube.com" or clean.endswith(".youtube.com")


def _clean_netscape_field(val: object) -> str | None:
    if not isinstance(val, str):
        return None
    if "\r" in val or "\n" in val or "\t" in val:
        return None
    return val


def has_youtube_auth_cookies(cookies: list[dict]) -> bool:
    """Returns True when CDP cookies include at least one active YouTube auth token."""
    for c in cookies or []:
        if not isinstance(c, dict):
            continue
        domain = str(c.get("domain") or "")
        name = str(c.get("name") or "")
        val = str(c.get("value") or "")
        if val and name in _STRONG_YT_AUTH_COOKIES and _is_youtube_cookie_domain(domain):
            return True
    return False


def format_chrome_cookies_to_netscape(cookies: list[dict]) -> str:
    """Formats CDP cookie dicts into strict canonical Netscape cookies.txt for yt-dlp."""
    lines = [
        "# Netscape HTTP Cookie File",
        "# Generated automatically by Swararia Audio Lab Desktop Login.",
        "",
    ]
    dedup: dict[tuple[str, str, str], str] = {}
    for c in cookies or []:
        if not isinstance(c, dict):
            continue
        domain = _clean_netscape_field(c.get("domain"))
        path_val = _clean_netscape_field(c.get("path") or "/")
        name = _clean_netscape_field(c.get("name"))
        value = _clean_netscape_field(c.get("value"))
        if not domain or not path_val or not name or value is None:
            continue
        if not _is_allowed_cookie_domain(domain):
            continue

        include_sub = "TRUE" if domain.startswith(".") else "FALSE"
        secure_flag = "TRUE" if bool(c.get("secure")) else "FALSE"
        is_session = bool(c.get("session"))
        raw_exp = c.get("expires")
        exp_ts = 0
        if not is_session and isinstance(raw_exp, (int, float)) and raw_exp > 0:
            exp_ts = int(raw_exp)
        line = "\t".join((domain, include_sub, path_val, secure_flag, str(exp_ts), name, value))
        dedup[(domain, path_val, name)] = line

    lines.extend(dedup.values())
    return "\n".join(lines) + "\n"


def _cdp_ws_call(ws_url: str, method: str, params: dict | None = None, timeout: float = 4.0) -> dict:
    """Executes a single JSON-RPC call over a loopback CDP WebSocket using Python stdlib."""
    parsed = urllib.parse.urlparse(ws_url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "ws" or host not in ("127.0.0.1", "localhost") or not parsed.port:
        raise ValueError("Invalid local CDP WebSocket URL")
    path = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")

    sock = socket.create_connection((host, int(parsed.port)), timeout=timeout)
    try:
        sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{parsed.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        sock.sendall(req)

        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk or len(buf) > 32768:
                raise RuntimeError("Invalid CDP WebSocket handshake")
            buf += chunk
        hdr_end = buf.index(b"\r\n\r\n") + 4
        if b"101 " not in buf[:hdr_end]:
            raise RuntimeError("CDP WebSocket upgrade rejected")
        extra = buf[hdr_end:]

        req_id = 1
        payload = json.dumps({"id": req_id, "method": method, "params": params or {}}).encode("utf-8")
        _send_ws_frame(sock, 0x1, payload)

        recv_buf = bytearray(extra)
        while True:
            opcode, body = _read_ws_frame(sock, recv_buf)
            if opcode == 0x8:
                raise RuntimeError("CDP WebSocket closed")
            if opcode == 0x9:
                _send_ws_frame(sock, 0xA, body)
                continue
            if opcode != 0x1:
                continue
            msg = json.loads(body.decode("utf-8"))
            if msg.get("id") == req_id:
                if "error" in msg:
                    raise RuntimeError(str(msg["error"]))
                res = msg.get("result")
                return res if isinstance(res, dict) else {}
    finally:
        sock.close()


def _send_ws_frame(sock: socket.socket, opcode: int, payload: bytes) -> None:
    mask = os.urandom(4)
    length = len(payload)
    header = bytearray([0x80 | (opcode & 0x0F)])
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))
    header.extend(mask)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(bytes(header) + masked)


def _read_exact(sock: socket.socket, buf: bytearray, count: int) -> bytes:
    while len(buf) < count:
        chunk = sock.recv(max(4096, count - len(buf)))
        if not chunk:
            raise RuntimeError("Unexpected EOF on CDP WebSocket")
        buf.extend(chunk)
    out = bytes(buf[:count])
    del buf[:count]
    return out


def _read_ws_frame(sock: socket.socket, buf: bytearray) -> tuple[int, bytes]:
    b1, b2 = _read_exact(sock, buf, 2)
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack("!H", _read_exact(sock, buf, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _read_exact(sock, buf, 8))[0]
    if length > _CDP_MAX_FRAME_BYTES:
        raise RuntimeError("CDP frame exceeds limit")
    mask = _read_exact(sock, buf, 4) if masked else b""
    body = _read_exact(sock, buf, length)
    if masked:
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(body))
    return opcode, body


def _fetch_cdp_json(url: str, timeout: float = 0.8):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read(1_048_576).decode("utf-8", errors="replace"))


def _cdp_has_youtube_tab(cdp_port: int) -> bool:
    """Returns True when an open browser tab has returned to youtube.com after Google Sign-In."""
    try:
        targets = _fetch_cdp_json(f"http://127.0.0.1:{cdp_port}/json/list", timeout=0.8)
    except Exception:
        return False
    if not isinstance(targets, list):
        return False
    for tgt in targets:
        if not isinstance(tgt, dict) or tgt.get("type") != "page":
            continue
        url = str(tgt.get("url") or "")
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        if host == "youtube.com" or host.endswith(".youtube.com"):
            return True
    return False


def _allocate_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _write_secure_file_atomic(target_path: Path, text: str) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    raw = text.encode("utf-8", errors="ignore")
    fd, tmp_name = tempfile.mkstemp(prefix=".cookies.", suffix=".tmp", dir=str(target_path.parent))
    try:
        os.write(fd, raw)
    finally:
        os.close(fd)
    if os.name == "posix":
        os.chmod(tmp_name, 0o600)
    os.replace(tmp_name, target_path)


class YoutubeBrowserLoginSession:
    """Coordinates an isolated Chromium-based login window to capture active YouTube cookies."""

    def __init__(
        self,
        cookies_file: Path,
        status_builder: Callable[[bool], dict] | None = None,
        timeout_seconds: float = 300.0,
    ) -> None:
        self.cookies_file = Path(cookies_file)
        self._status_builder = status_builder
        self._timeout_seconds = timeout_seconds
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._ws_url: str | None = None
        self._state = "idle"
        self._message = "Siap membuka jendela login YouTube Music."
        self._browser_name: str | None = None
        self._system_status: dict | None = None
        self._thread: threading.Thread | None = None
        atexit.register(self.cleanup)

    def get_status(self) -> dict:
        browser_bin, browser_name = find_chromium_browser()
        with self._lock:
            return {
                "ok": True,
                "active": self._state in ("launching", "waiting_login", "verifying"),
                "state": self._state,
                "message": self._message,
                "browser_name": self._browser_name or browser_name,
                "browser_available": browser_bin is not None,
                "system_status": dict(self._system_status) if isinstance(self._system_status, dict) else None,
            }

    def start_login(self, reset_profile: bool = False) -> tuple[int, dict]:
        browser_bin, browser_name = find_chromium_browser()
        if not browser_bin:
            with self._lock:
                self._state = "failed"
                self._message = (
                    "Browser Google Chrome, Chromium, atau Microsoft Edge tidak ditemukan di sistem. "
                    "Gunakan menu import file cookies.txt manual."
                )
            return 400, {**self.get_status(), "ok": False, "error": self._message}

        with self._lock:
            if self._state in ("launching", "waiting_login", "verifying") and self._proc and self._proc.poll() is None:
                return 200, self.get_status()

            self._stop_event = threading.Event()
            self._ws_url = None
            self._browser_name = browser_name
            self._state = "launching"
            self._message = f"Membuka jendela login YouTube Music ({browser_name})..."
            self._system_status = None

        profile_dir = self.cookies_file.parent / "ytm_browser_profile"
        if reset_profile and profile_dir.is_dir():
            shutil.rmtree(profile_dir, ignore_errors=True)
        profile_dir.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            try:
                os.chmod(profile_dir, 0o700)
            except OSError:
                pass

        cdp_port = _allocate_loopback_port()
        cmd = [
            browser_bin,
            f"--remote-debugging-port={cdp_port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=980,760",
            "--new-window",
            "https://music.youtube.com/",
        ]
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            with self._lock:
                self._state = "failed"
                self._message = f"Gagal menjalankan {browser_name}: {exc}"
            return 500, {**self.get_status(), "ok": False, "error": self._message}

        with self._lock:
            self._proc = proc
            worker = threading.Thread(
                target=self._watch_login_loop,
                args=(proc, cdp_port, self._stop_event),
                daemon=True,
            )
            self._thread = worker
        worker.start()
        return 200, self.get_status()

    def cancel_login(self) -> dict:
        with self._lock:
            stop_ev = self._stop_event
            proc = self._proc
            ws_url = self._ws_url
            was_active = self._state in ("launching", "waiting_login", "verifying")
            if was_active:
                self._state = "cancelled"
                self._message = "Login YouTube dibatalkan oleh pengguna."
        stop_ev.set()
        self._shutdown_browser(proc, ws_url)
        return self.get_status()

    def cleanup(self) -> None:
        with self._lock:
            stop_ev = self._stop_event
            proc = self._proc
            ws_url = self._ws_url
        stop_ev.set()
        self._shutdown_browser(proc, ws_url)

    def _shutdown_browser(self, proc: subprocess.Popen | None, ws_url: str | None) -> None:
        if ws_url:
            try:
                _cdp_ws_call(ws_url, "Browser.close", timeout=1.5)
            except Exception:
                pass
        if proc and proc.poll() is None:
            try:
                proc.wait(timeout=2.0)
            except Exception:
                try:
                    proc.terminate()
                    proc.wait(timeout=2.0)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

    def _watch_login_loop(
        self,
        proc: subprocess.Popen,
        cdp_port: int,
        stop_event: threading.Event,
    ) -> None:
        deadline = time.monotonic() + self._timeout_seconds
        ws_url: str | None = None

        while not stop_event.is_set() and time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            try:
                ver = _fetch_cdp_json(f"http://127.0.0.1:{cdp_port}/json/version", timeout=0.6)
                cand_ws = ver.get("webSocketDebuggerUrl") if isinstance(ver, dict) else None
                if isinstance(cand_ws, str) and cand_ws.startswith("ws://"):
                    ws_url = cand_ws
                    break
            except Exception:
                pass
            time.sleep(0.25)

        if stop_event.is_set():
            self._shutdown_browser(proc, ws_url)
            return

        if not ws_url:
            self._shutdown_browser(proc, None)
            with self._lock:
                if self._state != "cancelled":
                    self._state = "failed"
                    self._message = "Jendela browser tidak dapat dihubungi atau ditutup sebelum siap."
            return

        with self._lock:
            self._ws_url = ws_url
            if self._state == "launching":
                self._state = "waiting_login"
                self._message = (
                    "Silakan klik 'Sign in / Login' di jendela YouTube Music yang terbuka. "
                    "Jendela akan tertutup otomatis setelah login berhasil."
                )

        last_stale_sig = ""
        while not stop_event.is_set() and time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            try:
                res = _cdp_ws_call(ws_url, "Storage.getCookies", timeout=2.5)
                cookies = res.get("cookies", []) if isinstance(res, dict) else []
                if has_youtube_auth_cookies(cookies) and _cdp_has_youtube_tab(cdp_port):
                    netscape_text = format_chrome_cookies_to_netscape(cookies)
                    sig = f"{len(netscape_text)}:{hash(netscape_text)}"
                    if sig != last_stale_sig:
                        last_stale_sig = sig
                        verified, tier_info = self._verify_netscape_text(netscape_text)
                        if verified:
                            with self._lock:
                                self._state = "verifying"
                                self._message = "Login terdeteksi! Menyimpan sesi ke library/cookies.txt..."
                            _write_secure_file_atomic(self.cookies_file, netscape_text)
                            final_tier = audio_manager.check_youtube_cookies_tier(
                                self.cookies_file, force_refresh=True
                            )
                            sys_status = (
                                self._status_builder(False)
                                if self._status_builder is not None
                                else {
                                    "cookies_present": True,
                                    "cookies_status": final_tier.get("status"),
                                    "cookies_tier": final_tier.get("tier"),
                                    "cookies_label": final_tier.get("label"),
                                    "cookies_note": final_tier.get("note"),
                                }
                            )
                            self._shutdown_browser(proc, ws_url)
                            with self._lock:
                                self._ws_url = None
                                self._proc = None
                                self._system_status = sys_status
                                self._state = "authenticated"
                                self._message = (
                                    f"Login berhasil: {final_tier.get('label', 'Aktif')} — "
                                    f"{final_tier.get('note', '')}"
                                )
                            return
            except Exception:
                pass
            time.sleep(1.0)

        self._shutdown_browser(proc, ws_url)
        with self._lock:
            self._ws_url = None
            self._proc = None
            if self._state in ("cancelled", "authenticated"):
                return
            if time.monotonic() >= deadline:
                self._state = "timeout"
                self._message = "Batas waktu login habis (5 menit). Silakan coba klik Login lagi."
            elif proc.poll() is not None:
                self._state = "cancelled"
                self._message = "Jendela browser ditutup sebelum proses login selesai."
            else:
                self._state = "failed"
                self._message = "Proses login belum berhasil diselesaikan."

    def _verify_netscape_text(self, netscape_text: str) -> tuple[bool, dict | None]:
        """Tests captured Netscape cookies against YouTube via temporary 0600 file before saving."""
        self.cookies_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".verify.", suffix=".tmp", dir=str(self.cookies_file.parent))
        tmp_path = Path(tmp_name)
        try:
            try:
                os.write(fd, netscape_text.encode("utf-8", errors="ignore"))
            finally:
                os.close(fd)
            if os.name == "posix":
                os.chmod(tmp_path, 0o600)
            tier = audio_manager.check_youtube_cookies_tier(tmp_path, force_refresh=True)
            is_valid = tier.get("status") in ("premium", "free")
            return is_valid, tier
        except Exception:
            return False, None
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
