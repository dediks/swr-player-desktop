import sys
from pathlib import Path

from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import audio_manager as am  # noqa: E402


def test_youtube_audio_selector_strictly_anti_drc():
    # Every tier in the selector must strictly guard against -drc streams
    tiers = am.YOUTUBE_AUDIO_SELECTOR.split("/")
    assert any("[format_id=141]" in t and "[format_id!*=-drc]" in t for t in tiers)
    assert any("[format_id=774]" in t and "[format_id!*=-drc]" in t for t in tiers)
    assert any("[format_id=251]" in t and "[format_id!*=-drc]" in t for t in tiers)
    assert any("[format_id=140]" in t and "[format_id!*=-drc]" in t for t in tiers)
    assert "ba[format_id!*=-drc]" in tiers


def test_youtube_opus_selector_prioritizes_opus_lossless():
    tiers = am.YOUTUBE_OPUS_SELECTOR.split("/")
    # Format 774 and 251 must come before 141 so Opus is stream-copied without re-encode
    idx_774 = next(i for i, t in enumerate(tiers) if "[format_id=774]" in t)
    idx_251 = next(i for i, t in enumerate(tiers) if "[format_id=251]" in t)
    idx_141 = next(i for i, t in enumerate(tiers) if "[format_id=141]" in t)
    assert idx_774 < idx_141
    assert idx_251 < idx_141


def test_build_ytdlp_command_performance_and_quality_flags(tmp_path):
    archive = tmp_path / "downloaded_ids.txt"
    cmd, desc = am.build_ytdlp_command(
        url="https://music.youtube.com/watch?v=test1234567",
        output_template="%(title)s.%(ext)s",
        archive_file=archive,
        audio_format="opus",
    )

    # Performance flags
    assert "--concurrent-fragments" in cmd
    idx_cf = cmd.index("--concurrent-fragments")
    assert cmd[idx_cf + 1] == "4"

    assert "--socket-timeout" in cmd
    assert cmd[cmd.index("--socket-timeout") + 1] == "30"

    assert "--retries" in cmd
    assert cmd[cmd.index("--retries") + 1] == "5"

    assert "--fragment-retries" in cmd
    assert cmd[cmd.index("--fragment-retries") + 1] == "5"

    # Cover art embedding only when mutagen is available
    if am.is_mutagen_available():
        assert "--embed-thumbnail" in cmd
        assert "--convert-thumbnails" in cmd
        assert cmd[cmd.index("--convert-thumbnails") + 1] == "jpg"
    else:
        assert "--embed-thumbnail" not in cmd

    # Lossless remux to .opus
    assert "--extract-audio" in cmd
    assert cmd[cmd.index("--audio-format") + 1] == "opus"

    assert "Anti-DRC: Active" in desc
    assert "Multi-thread: 4x" in desc


def test_thumbnail_guarded_by_mutagen(tmp_path):
    archive = tmp_path / "downloaded_ids.txt"
    with mock.patch.object(am, "is_mutagen_available", return_value=False):
        cmd, _ = am.build_ytdlp_command("https://youtube.com/watch?v=xyz", "out", archive, "opus")
        assert "--embed-thumbnail" not in cmd
    with mock.patch.object(am, "is_mutagen_available", return_value=True):
        cmd, _ = am.build_ytdlp_command("https://youtube.com/watch?v=xyz", "out", archive, "opus")
        assert "--embed-thumbnail" in cmd


def test_describe_format_tier():
    assert "256 kbps" in am.describe_format_tier("141")
    assert "160 kbps" in am.describe_format_tier("251")
    assert "WARNING" in am.describe_format_tier("251-drc")


def test_netscape_cookie_filtering_domain_expiry_and_httponly(tmp_path):
    import time

    now_s = int(time.time())
    cookies_file = tmp_path / "cookies.txt"
    cookies_file.write_text(
        "# Netscape HTTP Cookie File\n"
        f"#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t{now_s + 3600}\tSID\tsecret_yt_1\n"
        f"music.youtube.com\tFALSE\t/\tTRUE\t{now_s + 3600}\tYSC\tsecret_music_2\n"
        f"__Host-1PSID\tFALSE\t/\tTRUE\t{now_s + 3600}\tWRONG\tskip_host\n"
        f"music.youtube.com\tFALSE\t/\tTRUE\t{now_s + 3600}\t__Host-1PSID\tsecret_host_ok\n"
        f".youtube.com\tTRUE\t/\tTRUE\t{now_s - 60}\tOLD\texpired_val\n"
        f".evil.example\tTRUE\t/\tTRUE\t{now_s + 3600}\tEVIL\tnever_send\n",
        encoding="utf-8",
    )

    hdr_music = am._parse_netscape_cookies_for_url(cookies_file, "https://music.youtube.com/")
    assert "SID=secret_yt_1" in hdr_music
    assert "YSC=secret_music_2" in hdr_music
    assert "__Host-1PSID=secret_host_ok" in hdr_music
    assert "OLD=" not in hdr_music
    assert "EVIL=" not in hdr_music
    assert "WRONG=" not in hdr_music

    hdr_http = am._parse_netscape_cookies_for_url(cookies_file, "http://music.youtube.com/")
    assert hdr_http == ""


def test_youtube_cookies_tier_detection_variants(tmp_path):
    import time

    now_s = int(time.time())
    cookies_file = tmp_path / "cookies.txt"
    cookies_file.write_text(
        f".youtube.com\tTRUE\t/\tTRUE\t{now_s + 3600}\tSAPISID\tdummy_secret_value\n",
        encoding="utf-8",
    )

    with am._COOKIES_TIER_CACHE_LOCK:
        am._COOKIES_TIER_CACHE.clear()

    # Case 1: YT Music Premium (www shows YOUTUBE_LOGO, music shows IS_SUBSCRIBER:true)
    def _mock_ytm_premium(start_url, _path, max_hops=3):
        if "music.youtube.com" in start_url:
            return (200, '{"LOGGED_IN":true,"IS_SUBSCRIBER":true}', False)
        return (200, '{"LOGGED_IN":true,"icon":"YOUTUBE_LOGO"}', False)

    with mock.patch.object(am, "_fetch_youtube_html_with_cookies", side_effect=_mock_ytm_premium):
        res = am.check_youtube_cookies_tier(cookies_file, force_refresh=True)
        assert res["status"] == "premium"
        assert res["tier"] == "premium"
        assert "YT Music Premium" in res["label"]
        assert "dummy_secret_value" not in str(res)

    # Case 2: Standard free logged-in account
    def _mock_free(start_url, _path, max_hops=3):
        if "music.youtube.com" in start_url:
            return (200, '{"LOGGED_IN":true,"IS_SUBSCRIBER":false}', False)
        return (200, '{"LOGGED_IN":true,"icon":"YOUTUBE_LOGO"}', False)

    with mock.patch.object(am, "_fetch_youtube_html_with_cookies", side_effect=_mock_free):
        res = am.check_youtube_cookies_tier(cookies_file, force_refresh=True)
        assert res["status"] == "free"
        assert res["tier"] == "free"

    # Case 3: Expired / logged out cookies
    def _mock_expired(start_url, _path, max_hops=3):
        return (200, '{"LOGGED_IN":false,"IS_SUBSCRIBER":false}', False)

    with mock.patch.object(am, "_fetch_youtube_html_with_cookies", side_effect=_mock_expired):
        res = am.check_youtube_cookies_tier(cookies_file, force_refresh=True)
        assert res["status"] == "unauthenticated"
        assert res["tier"] == "free"


def test_build_ytdlp_command_premium_extractor_args(tmp_path):
    archive = tmp_path / "downloaded_ids.txt"
    cookies_file = tmp_path / "cookies.txt"
    cookies_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")

    cmd_prem, desc_prem = am.build_ytdlp_command(
        "https://music.youtube.com/watch?v=qmOLtTGvsbM",
        "out",
        archive,
        "opus",
        cookies_file=cookies_file,
        premium_active=True,
    )
    assert "--extractor-args" in cmd_prem
    ext_arg = cmd_prem[cmd_prem.index("--extractor-args") + 1]
    assert "web_music" in ext_arg and "formats=missing_pot" in ext_arg
    assert "YT Music Premium" in desc_prem

    cmd_std, desc_std = am.build_ytdlp_command(
        "https://music.youtube.com/watch?v=qmOLtTGvsbM",
        "out",
        archive,
        "opus",
        cookies_file=cookies_file,
        premium_active=False,
    )
    assert "--extractor-args" not in cmd_std
    assert "Standar" in desc_std


def test_metadata_derives_bitrate_when_ffprobe_omits_it(tmp_path):
    import json
    from types import SimpleNamespace

    sample = tmp_path / "track.opus"
    sample.write_bytes(b"0" * 3_200_000)  # ~256 kbps over 100s
    ffprobe_json = json.dumps({
        "streams": [{"codec_type": "audio", "codec_name": "opus", "sample_rate": "48000", "channels": 2, "duration": "100.0"}],
        "format": {"duration": "100.0"},
    })
    with mock.patch.object(am.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=ffprobe_json)):
        info = am.metadata(sample)
        assert info["codec"] == "opus"
        assert info["bitrate_kbps"] == 256.0


def test_web_ui_cookies_endpoint_and_system_status(tmp_path):
    from email.message import Message
    import json
    import time
    import web_ui

    fake_cookies = tmp_path / "cookies.txt"
    now_s = int(time.time())
    payload_content = f".youtube.com\tTRUE\t/\tTRUE\t{now_s + 3600}\tSAPISID\ttop_secret_cookie_val\n"

    hdrs = Message()
    hdrs["Host"] = "127.0.0.1:8765"
    hdrs["Origin"] = "http://127.0.0.1:8765"

    handler = web_ui.AudioServerHandler.__new__(web_ui.AudioServerHandler)
    handler.client_address = ("127.0.0.1", 55555)
    handler.server = type("Srv", (), {"server_address": ("127.0.0.1", 8765)})()
    handler.headers = hdrs
    sent = []
    handler.send_json = lambda code, obj: sent.append((code, obj))

    def _mock_prem(start_url, _path, max_hops=3):
        return (200, '{"LOGGED_IN":true,"IS_SUBSCRIBER":true}', False)

    with mock.patch.object(web_ui, "COOKIES_FILE", fake_cookies), \
            mock.patch.object(am, "_fetch_youtube_html_with_cookies", side_effect=_mock_prem):
        handler._handle_post_cookies({"action": "save", "content": payload_content})
        assert len(sent) == 1
        code, body = sent[0]
        assert code == 200 and body["ok"] is True
        assert body["cookies_present"] is True
        assert body["cookies_status"] == "premium"
        assert "top_secret_cookie_val" not in json.dumps(body)
        assert fake_cookies.is_file()

        sent.clear()
        handler._handle_post_cookies({"action": "delete"})
        assert sent[0][0] == 200
        assert not fake_cookies.is_file()

        # Non-local client or missing Origin rejected with 403
        sent.clear()
        handler.client_address = ("192.168.1.50", 55555)
        handler._handle_post_cookies({"action": "verify"})
        assert sent[0][0] == 403


def test_youtube_cookie_fetch_does_not_leak_to_non_youtube_redirects(tmp_path):
    import io
    import time
    import urllib.error

    now_s = int(time.time())
    cookies_file = tmp_path / "cookies.txt"
    cookies_file.write_text(
        f".youtube.com\tTRUE\t/\tTRUE\t{now_s + 3600}\tSAPISID\tdummy_secret_value\n",
        encoding="utf-8",
    )
    seen_urls = []

    def _fake_open(req, timeout=6.0):
        seen_urls.append(req.full_url)
        raise urllib.error.HTTPError(
            req.full_url,
            302,
            "Found",
            {"Location": "https://accounts.google.com/ServiceLogin?continue=https://music.youtube.com/"},
            io.BytesIO(b""),
        )

    with mock.patch.object(am, "_open_url_no_redirect", side_effect=_fake_open):
        status, html, login_redir = am._fetch_youtube_html_with_cookies("https://music.youtube.com/", cookies_file)
        assert status == 302
        assert html == ""
        assert login_redir is True
        assert seen_urls == ["https://music.youtube.com/"]


def test_youtube_login_netscape_formatter_and_ytdlp_jar_compatibility(tmp_path):
    import time
    import youtube_login as yl
    from yt_dlp.cookies import YoutubeDLCookieJar

    now_s = int(time.time())
    cookies = [
        {"domain": ".youtube.com", "path": "/", "secure": True, "session": False, "expires": now_s + 7200.5, "name": "SAPISID", "value": "yt_sapi_secret"},
        {"domain": "music.youtube.com", "path": "/", "secure": True, "session": True, "expires": -1, "name": "YSC", "value": "sess_val"},
        {"domain": ".google.com", "path": "/", "secure": True, "session": False, "expires": now_s + 7200, "name": "SID", "value": "g_sid"},
        {"domain": ".evil.example", "path": "/", "secure": True, "session": False, "expires": now_s + 7200, "name": "EVIL", "value": "skip_me"},
        {"domain": ".youtube.com", "path": "/", "secure": True, "session": False, "expires": now_s + 7200, "name": "BAD\tNAME", "value": "inject\n"},
    ]

    # Logged-out anonymous cookies do not qualify as auth cookies
    assert yl.has_youtube_auth_cookies([cookies[1], cookies[2]]) is False
    # .youtube.com auth cookie does qualify
    assert yl.has_youtube_auth_cookies(cookies) is True

    text = yl.format_chrome_cookies_to_netscape(cookies)
    assert "yt_sapi_secret" in text
    assert "skip_me" not in text
    assert "BAD" not in text

    out = tmp_path / "netscape_cookies.txt"
    out.write_text(text, encoding="utf-8")
    jar = YoutubeDLCookieJar()
    jar.load(str(out), ignore_discard=True, ignore_expires=True)
    names = {c.name for c in jar}
    assert "SAPISID" in names and "YSC" in names and "SID" in names


def test_youtube_browser_login_session_lifecycle(tmp_path):
    import json
    import time
    import youtube_login as yl

    cookies_file = tmp_path / "library" / "cookies.txt"
    now_s = int(time.time())

    # 1. Missing browser returns 400 gracefully
    sess = yl.YoutubeBrowserLoginSession(cookies_file)
    with mock.patch.object(yl, "find_chromium_browser", return_value=(None, None)):
        code, resp = sess.start_login()
        assert code == 400 and resp["ok"] is False and resp["state"] == "failed"

    # 2. CDP capture -> verify Premium -> save cookies.txt -> close browser
    class _FakeProc:
        def __init__(self):
            self.terminated = False
            self.exited = False
        def poll(self):
            return 0 if self.exited else None
        def wait(self, timeout=None):
            self.exited = True
            return 0
        def terminate(self):
            self.terminated = True
            self.exited = True
        def kill(self):
            self.exited = True

    fake_proc = _FakeProc()
    cdp_methods = []

    def _fake_cdp_ws(ws_url, method, params=None, timeout=4.0):
        cdp_methods.append(method)
        if method == "Storage.getCookies":
            return {
                "cookies": [
                    {
                        "domain": ".youtube.com",
                        "path": "/",
                        "secure": True,
                        "session": False,
                        "expires": now_s + 3600,
                        "name": "SAPISID",
                        "value": "super_secret_token_xyz",
                    }
                ]
            }
        if method == "Browser.close":
            fake_proc.exited = True
        return {}

    def _fake_fetch_json(url, timeout=0.8):
        if "/json/version" in url:
            return {"webSocketDebuggerUrl": "ws://127.0.0.1:9229/devtools/browser/abc"}
        if "/json/list" in url:
            return [{"type": "page", "url": "https://music.youtube.com/"}]
        return {}

    def _fake_check_tier(path, force_refresh=False):
        return {
            "present": True,
            "status": "premium",
            "tier": "premium",
            "label": "YT Music Premium",
            "note": "Sesi aktif • stream 256 kbps siap dipakai.",
        }

    sess2 = yl.YoutubeBrowserLoginSession(cookies_file, timeout_seconds=10.0)
    with mock.patch.object(yl, "find_chromium_browser", return_value=("/fake/chromium", "Chromium")), \
            mock.patch.object(yl.subprocess, "Popen", return_value=fake_proc), \
            mock.patch.object(yl, "_fetch_cdp_json", side_effect=_fake_fetch_json), \
            mock.patch.object(yl, "_cdp_ws_call", side_effect=_fake_cdp_ws), \
            mock.patch.object(am, "check_youtube_cookies_tier", side_effect=_fake_check_tier):
        code, init_resp = sess2.start_login()
        assert code == 200 and init_resp["ok"] is True
        deadline = time.monotonic() + 4.0
        st = sess2.get_status()
        while st["active"] and time.monotonic() < deadline:
            time.sleep(0.05)
            st = sess2.get_status()
        assert st["state"] == "authenticated", st
        assert st["system_status"]["cookies_status"] == "premium"
        assert "super_secret_token_xyz" not in json.dumps(st)
        assert cookies_file.is_file()
        assert "super_secret_token_xyz" in cookies_file.read_text(encoding="utf-8")
        assert "Browser.close" in cdp_methods


def test_cdp_ws_call_loopback_guard_and_exchange():
    import base64
    import hashlib
    import socket
    import struct
    import threading
    import youtube_login as yl

    # Reject non-loopback CDP WebSocket targets
    for bad in ("ws://192.168.1.10:9222/devtools/browser/1", "wss://127.0.0.1:9222/x"):
        raised = False
        try:
            yl._cdp_ws_call(bad, "Browser.getVersion")
        except ValueError:
            raised = True
        assert raised

    # Localhost round-trip WebSocket test
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def _serve_once():
        conn, _ = srv.accept()
        with conn:
            req = conn.recv(4096).decode("ascii", errors="ignore")
            key_line = [ln for ln in req.splitlines() if ln.lower().startswith("sec-websocket-key:")][0]
            key = key_line.split(":", 1)[1].strip()
            accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            conn.sendall(
                f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n".encode("ascii")
            )
            # Consume client frame header+payload
            b1, b2 = conn.recv(2)
            ln = b2 & 0x7F
            _mask = conn.recv(4)
            _pay = conn.recv(ln)
            resp = b'{"id":1,"result":{"product":"Chrome/156"}}'
            conn.sendall(bytes([0x81, len(resp)]) + resp)

    t = threading.Thread(target=_serve_once, daemon=True)
    t.start()
    try:
        res = yl._cdp_ws_call(f"ws://127.0.0.1:{port}/devtools/browser/1", "Browser.getVersion")
        assert res["product"] == "Chrome/156"
    finally:
        t.join(timeout=2.0)
        srv.close()


def test_web_ui_login_routes_enforce_localhost_and_origin(tmp_path):
    from email.message import Message
    import io
    import json
    import web_ui

    body_bytes = b'{"reset_profile": false}'
    hdrs = Message()
    hdrs["Host"] = "127.0.0.1:8765"
    hdrs["Content-Type"] = "application/json"
    hdrs["Content-Length"] = str(len(body_bytes))

    handler = web_ui.AudioServerHandler.__new__(web_ui.AudioServerHandler)
    handler.client_address = ("127.0.0.1", 55555)
    handler.server = type("Srv", (), {"server_address": ("127.0.0.1", 8765)})()
    handler.headers = hdrs
    handler.path = "/api/cookies/login/start"
    handler.rfile = io.BytesIO(body_bytes)
    sent = []
    handler.send_json = lambda code, obj: sent.append((code, obj))

    # Missing Origin header on write must be rejected with 403
    handler.do_POST()
    assert sent[0][0] == 403

    # Non-local GET to /api/cookies/login/status must be rejected with 403
    sent.clear()
    handler.client_address = ("192.168.1.40", 55555)
    handler.path = "/api/cookies/login/status"
    handler.do_GET()
    assert sent[0][0] == 403

    # Localhost GET returns 200 status without secrets
    sent.clear()
    handler.client_address = ("127.0.0.1", 55555)
    handler.do_GET()
    assert sent[0][0] == 200 and sent[0][1]["ok"] is True





