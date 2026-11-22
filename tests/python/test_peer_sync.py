"""Security and end-to-end tests for PC <-> PC pinned HTTPS folder sync."""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import patch


def _gen_audio(path: Path, codec_args: list[str]) -> bytes:
    proc = subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "sine=frequency=523.25:duration=0.12",
            *codec_args,
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"ffmpeg failed: {proc.stderr}"
    return path.read_bytes()


def _make_pairing_string(url: str, fp_hex: str, invite: str) -> str:
    host_port = url.replace("https://", "", 1)
    fp_b64 = base64.urlsafe_b64encode(bytes.fromhex(fp_hex)).decode("ascii").rstrip("=")
    return f"swararia1|{host_port}|{fp_b64}|{invite}"


def test_peer_sync_ssrf_and_fingerprint_pinning_rejections(tmp_path: Path):
    from swararia.core import transfer
    from swararia.remote.peer_sync_client import PeerSyncManager
    from swararia.remote.secure_remote import RemoteError, SecureRemote
    from swararia.server.hub import RemotePlayerHub

    remote = SecureRemote(RemotePlayerHub(), default_inbox=tmp_path, sync_scanner=transfer.scan_sync_folders)
    remote.start("127.0.0.1", 0)
    try:
        invite = remote.open_invite()
        fp_hex = remote.connection_info["fingerprint_sha256"]
        url = remote.connection_info["url"]
        mgr = PeerSyncManager()

        # SSRF prevention: public, metadata, and wildcard IPv4 rejected before connect
        fp_b64 = base64.urlsafe_b64encode(bytes.fromhex(fp_hex)).decode("ascii").rstrip("=")
        for bad_host in ("8.8.8.8:8766", "169.254.169.254:8766", "0.0.0.0:8766", "127.0.0.1:0", "localhost:8766"):
            try:
                mgr.pair_with_host(f"swararia1|{bad_host}|{fp_b64}|{invite}")
                assert False, f"must reject unsafe host: {bad_host}"
            except RemoteError as exc:
                assert exc.code in ("invalid_pairing_host", "invalid_bind")

        # Tampered certificate fingerprint must fail closed before transmitting invite/HTTP bytes
        wrong_fp_hex = ("00" if not fp_hex.startswith("00") else "ff") + fp_hex[2:]
        bad_code = _make_pairing_string(url, wrong_fp_hex, invite)
        try:
            mgr.pair_with_host(bad_code, "MitmTest")
            assert False, "fingerprint mismatch must abort before HTTP exchange"
        except RemoteError as exc:
            assert exc.code == "fingerprint_mismatch"

        assert remote.devices == {}, "invite must never reach host when fingerprint does not match"
    finally:
        remote.stop()


def test_pc_to_pc_pinned_https_folder_sync_end_to_end(tmp_path: Path):
    from swararia.core.task_engine import AudioMetadataCache
    from swararia.server import app as srv_app, network as srv_network, state as srv_state

    host_inbox = tmp_path / "host_inbox"
    recv_inbox = tmp_path / "recv_inbox"
    (host_inbox / "SetlistA").mkdir(parents=True)
    (recv_inbox / "SetlistA").mkdir(parents=True)

    mp3_new = _gen_audio(host_inbox / "SetlistA" / "01 Opening.mp3", ["-c:a", "libmp3lame", "-b:a", "128k"])
    mp3_dup = _gen_audio(host_inbox / "SetlistA" / "02 Same.mp3", ["-c:a", "libmp3lame", "-b:a", "192k"])
    mp3_col_src = _gen_audio(host_inbox / "SetlistA" / "03 Clash.mp3", ["-c:a", "libmp3lame", "-b:a", "160k"])
    mp3_col_dst = _gen_audio(recv_inbox / "SetlistA" / "03 Clash.mp3", ["-c:a", "libmp3lame", "-b:a", "96k"])
    (recv_inbox / "SetlistA" / "02 Same.mp3").write_bytes(mp3_dup)

    # Excluded WAV & FLAC files on Host PC must be omitted cleanly with warnings
    _gen_audio(host_inbox / "SetlistA" / "99 Studio.wav", ["-c:a", "pcm_s16le"])
    _gen_audio(host_inbox / "SetlistA" / "98 Master.flac", ["-c:a", "flac"])

    recv_cache = AudioMetadataCache()
    with patch.object(srv_state, "INBOX_DIR", host_inbox), \
            patch.object(srv_network, "_cleanup_stale_wsl_listen_port"):
        host_server, host_port = srv_app.create_server("127.0.0.1", 0)

    # Swap INBOX_DIR & task_manager attributes cleanly so the active worker loop serves Receiver
    with patch.object(srv_state, "INBOX_DIR", recv_inbox), \
            patch.object(srv_state, "metadata_cache", recv_cache), \
            patch.object(srv_state.task_manager, "inbox_dir", recv_inbox), \
            patch.object(srv_state.task_manager, "metadata_cache", recv_cache), \
            patch.object(srv_network, "_cleanup_stale_wsl_listen_port"):
        recv_server, recv_port = srv_app.create_server("127.0.0.1", 0)
        host_thread = threading.Thread(target=host_server.serve_forever, daemon=True)
        recv_thread = threading.Thread(target=recv_server.serve_forever, daemon=True)
        host_thread.start()
        recv_thread.start()

        def call_local(target_port: int, method: str, path: str, payload=None, origin_override=None):
            conn = http.client.HTTPConnection("127.0.0.1", target_port, timeout=6)
            h = {"Content-Type": "application/json", "Origin": origin_override or f"http://127.0.0.1:{target_port}"}
            conn.request(method, path, json.dumps(payload) if payload is not None else None, h)
            resp = conn.getresponse()
            data = json.loads(resp.read())
            conn.close()
            return resp.status, data

        try:
            # Host starts Native HTTPS on 127.0.0.1:0 (default sync_enabled=False)
            st, host_admin = call_local(host_port, "POST", "/api/native-remote", {"action": "start", "bind_ip": "127.0.0.1", "port": 0})
            assert st == 200 and host_admin["enabled"] and host_admin["sync_enabled"] is False
            _, host_admin = call_local(host_port, "POST", "/api/native-remote", {"action": "invite_open"})

            pairing_code = _make_pairing_string(
                host_admin["url"],
                host_admin["fingerprint_sha256"],
                host_admin["invite"],
            )

            # Security gate on Receiver: non-local Origin rejected 403
            assert call_local(recv_port, "POST", "/api/sync/pair", {"pairing_code": pairing_code}, origin_override="http://evil.test")[0] == 403

            # Receiver pairs; response never leaks bearer token or invite
            st_pair, pair_res = call_local(recv_port, "POST", "/api/sync/pair", {"pairing_code": pairing_code, "label": "FOH Backup PC"})
            assert st_pair == 200 and pair_res["ok"]
            assert "token" not in json.dumps(pair_res)
            peer_id = pair_res["peer"]["peer_id"]
            assert pair_res["peer"]["status"] == "pending"

            # Host approves as view_only first -> Receiver must see sync_capable=False and preview must reject 403
            _, host_admin = call_local(host_port, "GET", "/api/native-remote")
            dev_id = host_admin["devices"][0]["device_id"]
            call_local(host_port, "POST", "/api/native-remote", {"action": "approve", "device_id": dev_id, "role": "view_only"})
            st_ref1, ref1 = call_local(recv_port, "POST", "/api/sync/refresh", {"peer_id": peer_id})
            assert st_ref1 == 200 and ref1["peer"]["sync_capable"] is False

            # Upgrade to control, but sync still OFF -> still sync_capable=False
            call_local(host_port, "POST", "/api/native-remote", {"action": "approve", "device_id": dev_id, "role": "control"})
            st_ref2, ref2 = call_local(recv_port, "POST", "/api/sync/refresh", {"peer_id": peer_id})
            assert st_ref2 == 200 and ref2["peer"]["sync_capable"] is False

            # Enable Host PC folder sync via native-remote admin
            st_en, host_admin = call_local(host_port, "POST", "/api/native-remote", {"action": "set_sync_enabled", "enabled": True, "source_dir": str(host_inbox)})
            assert st_en == 200 and host_admin["sync_enabled"] is True

            # Refresh on Receiver sees sanitized folder list & WAV/FLAC warning (no host OS paths leaked)
            st_ref3, ref3 = call_local(recv_port, "POST", "/api/sync/refresh", {"peer_id": peer_id})
            assert st_ref3 == 200 and ref3["peer"]["sync_capable"] is True
            folders = ref3["folders_info"]["folders"]
            assert len(folders) == 1 and folders[0]["rel_folder"] == "SetlistA"
            assert folders[0]["track_count"] == 3
            assert folders[0]["excluded_wav_flac_count"] == 2
            assert str(host_inbox) not in json.dumps(ref3)

            folder_id = folders[0]["folder_id"]

            # Preview diff checks deduplication, auto-rename collision, and WAV/FLAC warnings
            st_prev, prev = call_local(recv_port, "POST", "/api/sync/preview", {
                "peer_id": peer_id,
                "folder_ids": [folder_id],
                "dest_dir": str(recv_inbox),
            })
            assert st_prev == 200
            assert prev["new_count"] == 1
            assert prev["duplicate_skip_count"] == 1
            assert prev["rename_collision_count"] == 1
            assert prev["wav_flac_skipped_count"] == 2
            assert any(".wav/.flac" in w for w in prev["warnings"])

            # Start queued background sync task & wait for worker completion
            st_start, start_res = call_local(recv_port, "POST", "/api/sync/start", {
                "peer_id": peer_id,
                "folder_ids": [folder_id],
                "dest_dir": str(recv_inbox),
            })
            assert st_start == 200 and start_res["ok"] and start_res["job_id"]

            deadline = time.time() + 15.0
            finished = None
            while time.time() < deadline:
                _, q_snap = call_local(recv_port, "GET", "/api/queue")
                match = next((h for h in q_snap.get("history", []) if h.get("id") == start_res["job_id"]), None)
                if match is not None:
                    finished = match
                    break
                time.sleep(0.2)

            assert finished is not None and finished["status"] == "SUCCESS"
            assert finished["outcome"] == "WARNING"  # Due to 2 excluded WAV/FLAC files
            assert finished["stats"]["synced"] == 2
            assert finished["stats"]["renamed"] == 1
            assert finished["stats"]["wav_flac_skipped"] == 2

            # Verify final files on Receiver disk match SHA-256 and collision preserved original
            assert (recv_inbox / "SetlistA" / "01 Opening.mp3").read_bytes() == mp3_new
            assert (recv_inbox / "SetlistA" / "02 Same.mp3").read_bytes() == mp3_dup
            assert (recv_inbox / "SetlistA" / "03 Clash.mp3").read_bytes() == mp3_col_dst
            assert (recv_inbox / "SetlistA" / "03 Clash (2).mp3").read_bytes() == mp3_col_src
            assert not (recv_inbox / "SetlistA" / "99 Studio.wav").exists()
            assert not (recv_inbox / "SetlistA" / "98 Master.flac").exists()

            # Check SHA-256 integrity equals original host file bytes
            assert hashlib.sha256((recv_inbox / "SetlistA" / "01 Opening.mp3").read_bytes()).hexdigest() == hashlib.sha256(mp3_new).hexdigest()
        finally:
            host_server.shutdown()
            host_server.server_close()
            recv_server.shutdown()
            recv_server.server_close()
            host_thread.join(timeout=4)
            recv_thread.join(timeout=4)
