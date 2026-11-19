"""Security and functional tests for core audio transfer validation and LAN PIN phone uploads."""
from __future__ import annotations

import http.client
import json
import os
import stat
import subprocess
import threading
from pathlib import Path
from unittest.mock import patch


def _make_sample_audio_bytes(tmp_path: Path, name: str, codec_args: list[str]) -> bytes:
    out_file = tmp_path / name
    proc = subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=0.15",
            *codec_args,
            str(out_file),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"ffmpeg failed: {proc.stderr}"
    return out_file.read_bytes()


def _build_multipart_body(boundary: str, fields: list[tuple[str, str]], files: list[tuple[str, str, bytes]]) -> bytes:
    chunks: list[bytes] = []
    b_bytes = boundary.encode("ascii")
    for name, val in fields:
        chunks.append(b"--" + b_bytes + b"\r\n")
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        chunks.append(val.encode("utf-8") + b"\r\n")
    for field_name, filename, payload in files:
        chunks.append(b"--" + b_bytes + b"\r\n")
        chunks.append(
            f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode("utf-8")
        )
        chunks.append(payload + b"\r\n")
    chunks.append(b"--" + b_bytes + b"--\r\n")
    return b"".join(chunks)


def test_transfer_validation_rules_and_wav_flac_exclusion(tmp_path: Path):
    from swararia.core import transfer

    inbox = tmp_path / "inbox"
    inbox.mkdir()

    # Path traversal and hidden folder rejection
    for bad_sub in ("../escape", ".hidden", "$Recycle.Bin", "a/../b", "C:\\Windows"):
        try:
            transfer.resolve_transfer_destination(str(inbox), bad_sub, default_inbox=inbox)
            assert False, f"must reject unsafe subfolder: {bad_sub}"
        except transfer.TransferValidationError as exc:
            assert exc.code == "invalid_subfolder"

    # WAV/FLAC excluded with dedicated warning code in this phase
    for ext in (".wav", ".flac", ".WAV"):
        try:
            transfer.sanitize_transfer_filename(f"track{ext}")
            assert False, f"must exclude {ext} in this phase"
        except transfer.TransferValidationError as exc:
            assert exc.code == "wav_flac_excluded"
            assert "WAV" in exc.message or "FLAC" in exc.message

    for bad_ext in ("song.exe", "index.html", "payload.js", "archive.zip"):
        try:
            transfer.sanitize_transfer_filename(bad_ext)
            assert False
        except transfer.TransferValidationError as exc:
            assert exc.code == "unsupported_format"

    # Magic-byte & ffprobe content verification rejects polyglot HTML/script disguised as .mp3
    fake_part = transfer.create_staged_part_file(inbox)
    fake_part.write_bytes(b"<!doctype html><script>alert(1)</script>")
    try:
        transfer.verify_staged_audio_file(fake_part, ".mp3")
        assert False, "disguised HTML script must be rejected"
    except transfer.TransferValidationError as exc:
        assert exc.code == "invalid_audio_content"
    finally:
        fake_part.unlink(missing_ok=True)

    # Real compressed MP3 passes, sets 0644 mode, skips exact SHA-256 duplicates, auto-renames collisions
    mp3_1 = _make_sample_audio_bytes(tmp_path, "gen1.mp3", ["-c:a", "libmp3lame", "-b:a", "128k"])
    mp3_2 = _make_sample_audio_bytes(tmp_path, "gen2.mp3", ["-c:a", "libmp3lame", "-b:a", "192k"])
    dest = transfer.resolve_transfer_destination(str(inbox), "Opening/Set 1", default_inbox=inbox)

    p1 = transfer.create_staged_part_file(dest)
    p1.write_bytes(mp3_1)
    transfer.verify_staged_audio_file(p1, ".mp3")
    res1 = transfer.commit_staged_audio(p1, dest, "Anthem.mp3", root_for_rel=inbox)
    assert res1["status"] == "saved" and res1["filename"] == "Anthem.mp3"
    saved_file = dest / "Anthem.mp3"
    assert stat.S_IMODE(os.stat(saved_file).st_mode) == 0o644

    # Exact duplicate bytes -> skipped
    p_dup = transfer.create_staged_part_file(dest)
    p_dup.write_bytes(mp3_1)
    transfer.verify_staged_audio_file(p_dup, ".mp3")
    res_dup = transfer.commit_staged_audio(p_dup, dest, "CopyOfAnthem.mp3", root_for_rel=inbox)
    assert res_dup["status"] == "skipped_duplicate"
    assert not (dest / "CopyOfAnthem.mp3").exists()

    # Different audio with same filename -> auto-renamed without overwriting original
    p_col = transfer.create_staged_part_file(dest)
    p_col.write_bytes(mp3_2)
    transfer.verify_staged_audio_file(p_col, ".mp3")
    res_col = transfer.commit_staged_audio(p_col, dest, "Anthem.mp3", root_for_rel=inbox)
    assert res_col["status"] == "saved" and res_col["renamed"] is True
    assert res_col["filename"] == "Anthem (2).mp3"
    assert saved_file.read_bytes() == mp3_1
    assert (dest / "Anthem (2).mp3").read_bytes() == mp3_2


def test_phone_upload_gates_multipart_streaming_and_dedup(tmp_path: Path):
    from swararia.server import app as srv_app, handler as srv_handler, hub as srv_hub, network as srv_network, state as srv_state

    inbox = tmp_path / "library" / "inbox"
    inbox.mkdir(parents=True)
    mp3_bytes = _make_sample_audio_bytes(tmp_path, "valid.mp3", ["-c:a", "libmp3lame", "-b:a", "128k"])
    mp3_alt = _make_sample_audio_bytes(tmp_path, "valid_alt.mp3", ["-c:a", "libmp3lame", "-b:a", "192k"])
    wav_bytes = _make_sample_audio_bytes(tmp_path, "lossless.wav", ["-c:a", "pcm_s16le"])

    test_hub = srv_hub.RemotePlayerHub()
    with patch.object(srv_hub, "remote_hub", test_hub), \
            patch.object(srv_state, "INBOX_DIR", inbox), \
            patch.object(srv_network, "_cleanup_stale_wsl_listen_port"):
        server, port = srv_app.create_server("127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f"http://127.0.0.1:{port}"
        boundary = "----SwarariaTestBoundary7890"

        def upload_req(body: bytes, *, headers=None, content_type=None):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=6)
            h = {
                "Content-Type": content_type or f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
            }
            if headers:
                h.update(headers)
            conn.request("POST", "/api/remote/upload", body, h)
            resp = conn.getresponse()
            data = json.loads(resp.read())
            conn.close()
            return resp.status, data

        def local_json(path: str, payload: dict, extra_headers=None):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
            h = {"Content-Type": "application/json", "Origin": origin}
            if extra_headers:
                h.update(extra_headers)
            conn.request("POST", path, json.dumps(payload), h)
            resp = conn.getresponse()
            data = json.loads(resp.read())
            conn.close()
            return resp.status, data

        try:
            body_one = _build_multipart_body(
                boundary,
                [("subfolder", "Resepsi / Pagi")],
                [("files", "Lagu Pertama.mp3", mp3_bytes)],
            )

            # Default OFF: upload disabled
            st, res = upload_req(body_one, headers={"X-FOH-PIN": test_hub.pin, "X-FOH-Client-ID": "hp1"})
            assert st == 403 and res["code"] == "UPLOAD_DISABLED"

            # Config requires local_origin_required
            assert local_json("/api/remote/upload-config", {"enabled": True}, {"Origin": "http://evil.test"})[0] == 403
            st_cfg, cfg = local_json("/api/remote/upload-config", {"enabled": True})
            assert st_cfg == 200 and cfg["upload_enabled"] is True

            # Auth gates: bad PIN, cross-site header, view_only role, kicked client
            assert upload_req(body_one, headers={"X-FOH-PIN": "0000"})[0] == 401
            assert upload_req(body_one, headers={"X-FOH-PIN": test_hub.pin, "Sec-Fetch-Site": "cross-site"})[0] == 403
            assert upload_req(body_one, headers={"X-FOH-PIN": test_hub.pin}, content_type="application/json")[0] == 415

            test_hub.set_client_role("127.0.0.1:hp_view", "view_only")
            st_v, res_v = upload_req(body_one, headers={"X-FOH-PIN": test_hub.pin, "X-FOH-Client-ID": "hp_view"})
            assert st_v == 403 and res_v["code"] == "VIEW_ONLY"

            test_hub.kick_client("127.0.0.1:hp_kicked")
            st_k, res_k = upload_req(body_one, headers={"X-FOH-PIN": test_hub.pin, "X-FOH-Client-ID": "hp_kicked"})
            assert st_k == 403 and res_k["code"] == "KICKED"

            # Multi-file upload: valid .mp3 + excluded .wav + disguised HTML .mp3
            mixed_body = _build_multipart_body(
                boundary,
                [("subfolder", "Resepsi/Pagi")],
                [
                    ("files", "Lagu Pertama.mp3", mp3_bytes),
                    ("files", "Audio Raw.wav", wav_bytes),
                    ("files", "Evil.mp3", b"<script>alert(1)</script>"),
                ],
            )
            st_ok, res_ok = upload_req(mixed_body, headers={"X-FOH-PIN": test_hub.pin, "X-FOH-Client-ID": "hp_ctrl"})
            assert st_ok == 200 and res_ok["ok"] is True
            assert res_ok["saved_count"] == 1
            assert res_ok["wav_flac_skipped_count"] == 1
            assert res_ok["rejected_count"] == 2
            assert any("WAV" in w for w in res_ok["warnings"])
            dest_file = inbox / "Resepsi" / "Pagi" / "Lagu Pertama.mp3"
            assert dest_file.is_file() and dest_file.read_bytes() == mp3_bytes

            # Repeat exact SHA-256 -> skipped; colliding filename with different audio -> auto-rename
            repeat_body = _build_multipart_body(
                boundary,
                [("subfolder", "Resepsi/Pagi")],
                [
                    ("files", "Sama Persis.mp3", mp3_bytes),
                    ("files", "Lagu Pertama.mp3", mp3_alt),
                ],
            )
            st_rep, res_rep = upload_req(repeat_body, headers={"X-FOH-PIN": test_hub.pin, "X-FOH-Client-ID": "hp_ctrl"})
            assert st_rep == 200
            assert res_rep["duplicate_count"] == 1
            assert res_rep["saved_count"] == 1
            assert (inbox / "Resepsi" / "Pagi" / "Lagu Pertama (2).mp3").is_file()
            assert list((inbox / "Resepsi" / "Pagi").glob("*.part")) == []

            # Simulate LAN peer: upload allowed with PIN, upload-config still blocked 403
            class LanPeer(srv_handler.AudioServerHandler):
                def __init__(self, req, addr, owner):
                    super().__init__(req, ("192.168.1.77", addr[1]), owner)

            server.RequestHandlerClass = LanPeer
            assert local_json("/api/remote/upload-config", {"enabled": False})[0] == 403
            lan_body = _build_multipart_body(
                boundary,
                [("subfolder", "DariHP")],
                [("files", "Lagu LAN.mp3", mp3_alt)],
            )
            st_lan, res_lan = upload_req(
                lan_body,
                headers={"Host": f"192.168.1.10:{port}", "X-FOH-PIN": test_hub.pin, "X-FOH-Client-ID": "iphone"},
            )
            assert st_lan == 200 and res_lan["saved_count"] == 1
            assert (inbox / "DariHP" / "Lagu LAN.mp3").is_file()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=4)


def test_native_tls_library_upload_and_admin_dir_selection(tmp_path: Path):
    import hashlib
    import ssl
    import urllib.parse
    from swararia.server import app as srv_app, hub as srv_hub, network as srv_network, state as srv_state

    inbox = tmp_path / "library" / "inbox"
    custom_src = inbox / "custom_setlist"
    inbox.mkdir(parents=True)
    custom_src.mkdir(parents=True)

    mp3_bytes = _make_sample_audio_bytes(tmp_path, "native_one.mp3", ["-c:a", "libmp3lame", "-b:a", "128k"])
    mp3_alt = _make_sample_audio_bytes(tmp_path, "native_two.mp3", ["-c:a", "libmp3lame", "-b:a", "192k"])
    wav_bytes = _make_sample_audio_bytes(tmp_path, "native_raw.wav", ["-c:a", "pcm_s16le"])

    test_hub = srv_hub.RemotePlayerHub()
    probed_batches: list[list[Path]] = []

    def fake_probe_fast(files, include_spectral=False):
        probed_batches.append([Path(p) for p in files])

    with patch.object(srv_hub, "remote_hub", test_hub), \
            patch.object(srv_state, "INBOX_DIR", inbox), \
            patch.object(srv_state, "probe_files_fast", side_effect=fake_probe_fast), \
            patch.object(srv_network, "_cleanup_stale_wsl_listen_port"):
        server, port = srv_app.create_server("127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f"http://127.0.0.1:{port}"

        def admin_post(payload: dict):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
            conn.request("POST", "/api/native-remote", json.dumps(payload), {
                "Content-Type": "application/json",
                "Origin": origin,
            })
            resp = conn.getresponse()
            data = json.loads(resp.read())
            conn.close()
            return resp.status, data

        try:
            st, info = admin_post({"action": "start", "bind_ip": "127.0.0.1", "port": 0})
            assert st == 200 and info["enabled"]
            _, info = admin_post({"action": "invite_open"})
            remote = server.secure_remote
            tls_port = remote.listener.server_address[1]
            ssl_ctx = ssl.create_default_context(cadata=remote.certificate)

            def native_req(method: str, path: str, token: str, body: bytes | None = None, headers: dict | None = None):
                conn = http.client.HTTPSConnection("127.0.0.1", tls_port, timeout=6, context=ssl_ctx)
                h = {"Authorization": f"Bearer {token}"}
                if headers:
                    h.update(headers)
                if body is not None:
                    h.setdefault("Content-Length", str(len(body)))
                conn.request(method, path, body, h)
                resp = conn.getresponse()
                raw = resp.read()
                conn.close()
                return resp.status, json.loads(raw) if raw else None

            pair = remote.pair(info["invite"], "Android Test")
            assert admin_post({"action": "approve", "device_id": pair["device_id"], "role": "control"})[0] == 200
            token = pair["token"]

            mp3_sha = hashlib.sha256(mp3_bytes).hexdigest()
            upload_hdrs = {
                "Content-Type": "application/octet-stream",
                "X-Swararia-Capabilities": "extended-v1",
                "X-Swararia-Filename": urllib.parse.quote("Intro + Lagu.mp3", safe=""),
                "X-Swararia-Sha256": mp3_sha,
                "X-Swararia-Playlist": urllib.parse.quote("Show Malam", safe=""),
            }

            # Missing capabilities -> 400; default upload_enabled False -> 403
            st_nc, res_nc = native_req("POST", "/v1/library/upload", token, mp3_bytes, {"Content-Type": "application/octet-stream"})
            assert st_nc == 400 and res_nc["code"] == "unsupported_capabilities"
            assert native_req("POST", "/v1/library/upload", token, mp3_bytes, upload_hdrs)[0] == 403
            caps_off = native_req("GET", "/v1/capabilities", token, headers={"X-Swararia-Capabilities": "extended-v1"})[1]
            assert caps_off["upload"] is False

            # Admin configures sync source_dir and upload dest_dir
            st_s, admin_sync = admin_post({"action": "set_sync_enabled", "enabled": True, "source_dir": str(custom_src)})
            assert st_s == 200 and admin_sync["sync_enabled"] is True and admin_sync["sync_source_label"] == "custom_setlist"
            st_u, admin_up = admin_post({"action": "set_upload_enabled", "enabled": True, "dest_dir": str(custom_src)})
            assert st_u == 200 and admin_up["upload_enabled"] is True and admin_up["upload_dest_label"] == "custom_setlist"

            caps_on = native_req("GET", "/v1/capabilities", token, headers={"X-Swararia-Capabilities": "extended-v1"})[1]
            assert caps_on["upload"] is True and caps_on["sync"] is True

            # View-only device denied
            _, viewer_inv = admin_post({"action": "invite_open"})
            viewer = remote.pair(viewer_inv["invite"], "Viewer Android")
            admin_post({"action": "approve", "device_id": viewer["device_id"], "role": "view_only"})
            assert native_req("POST", "/v1/library/upload", viewer["token"], mp3_bytes, upload_hdrs)[0] == 403

            # WAV excluded (415 wav_flac_excluded), SHA mismatch (422), disguised HTML (415 invalid_audio_content)
            wav_hdrs = {
                **upload_hdrs,
                "X-Swararia-Filename": urllib.parse.quote("Raw.wav", safe=""),
                "X-Swararia-Sha256": hashlib.sha256(wav_bytes).hexdigest(),
            }
            st_w, res_w = native_req("POST", "/v1/library/upload", token, wav_bytes, wav_hdrs)
            assert st_w == 415 and res_w["code"] == "wav_flac_excluded"

            bad_sha_hdrs = {**upload_hdrs, "X-Swararia-Sha256": "0" * 64}
            assert native_req("POST", "/v1/library/upload", token, mp3_bytes, bad_sha_hdrs)[0] == 422

            evil_bytes = b"<script>alert(1)</script>"
            evil_hdrs = {**upload_hdrs, "X-Swararia-Sha256": hashlib.sha256(evil_bytes).hexdigest()}
            assert native_req("POST", "/v1/library/upload", token, evil_bytes, evil_hdrs)[0] == 415

            # Valid upload creates file in custom_src/Show Malam, sets 0o644, triggers probe_files_fast
            st_ok, res_ok = native_req("POST", "/v1/library/upload", token, mp3_bytes, upload_hdrs)
            assert st_ok == 201 and res_ok["status"] == "saved" and res_ok["deduplicated"] is False
            saved_file = custom_src / "Show Malam" / "Intro + Lagu.mp3"
            assert saved_file.is_file() and saved_file.read_bytes() == mp3_bytes
            assert stat.S_IMODE(os.stat(saved_file).st_mode) == 0o644

            # Same SHA-256 -> 200 deduplicated; colliding name with different audio -> auto-rename
            st_dup, res_dup = native_req("POST", "/v1/library/upload", token, mp3_bytes, {
                **upload_hdrs,
                "X-Swararia-Filename": urllib.parse.quote("DifferentNameSameBytes.mp3", safe=""),
            })
            assert st_dup == 200 and res_dup["deduplicated"] is True

            st_col, res_col = native_req("POST", "/v1/library/upload", token, mp3_alt, {
                **upload_hdrs,
                "X-Swararia-Sha256": hashlib.sha256(mp3_alt).hexdigest(),
            })
            assert st_col == 201 and res_col["renamed"] is True
            assert (custom_src / "Show Malam" / "Intro + Lagu (2).mp3").is_file()
            # Allow daemon probe thread a moment to run cleanly
            import time as _time
            deadline = _time.monotonic() + 2.0
            while len(probed_batches) < 2 and _time.monotonic() < deadline:
                _time.sleep(0.02)
            assert any(saved_file in batch for batch in probed_batches)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=4)
