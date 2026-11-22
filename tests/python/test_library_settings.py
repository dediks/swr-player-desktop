"""Cross-platform universal default library paths, persistence and security tests."""
from __future__ import annotations

import contextlib
import http.client
import json
import os
import threading
from pathlib import Path
from unittest.mock import patch

from swararia.core.fs_browser import browse as fb_browse
from swararia.core.fs_browser import safe_paths as fb_safe
from swararia.server import app as srv_app
from swararia.server import handler as srv_handler
from swararia.server import hub as srv_hub
from swararia.server import network as srv_network
from swararia.server import state as srv_state


def test_cross_platform_default_paths_unify_source_and_frozen(tmp_path: Path):
    fake_home = tmp_path / "home_user"
    fake_local_app = fake_home / "AppData" / "Local"
    fake_repo = tmp_path / "repo"
    fake_exe_dir = tmp_path / "dist_exe"
    for d in (fake_home / "Music", fake_local_app, fake_repo, fake_exe_dir):
        d.mkdir(parents=True, exist_ok=True)

    # 1. Native Linux: source and frozen resolve to the exact same ~/Music/SwarariaAudioLab & ~/.local/share
    clean_env = {"PATH": os.environ.get("PATH", "")}
    with patch.dict(os.environ, clean_env, clear=True), \
            patch.object(fb_safe, "_is_wsl", return_value=False), \
            patch.object(fb_safe, "_is_portable_mode", return_value=False), \
            patch.object(fb_safe.Path, "home", return_value=fake_home), \
            patch.object(fb_safe.sys, "platform", "linux"):
        app_data_src = fb_safe._resolve_app_data_dir(fake_repo, portable=False)
        lib_src = fb_safe._resolve_os_default_library_dir(fake_repo, app_data_src, portable=False)
        app_data_exe = fb_safe._resolve_app_data_dir(fake_exe_dir, portable=False)
        lib_exe = fb_safe._resolve_os_default_library_dir(fake_exe_dir, app_data_exe, portable=False)
        assert app_data_src == app_data_exe == (fake_home / ".local" / "share" / "swararia-audio-lab").resolve()
        assert lib_src == lib_exe == (fake_home / "Music" / "SwarariaAudioLab").resolve()

    # 2. macOS: ~/Library/Application Support/SwarariaAudioLab & ~/Music/SwarariaAudioLab
    with patch.dict(os.environ, clean_env, clear=True), \
            patch.object(fb_safe, "_is_wsl", return_value=False), \
            patch.object(fb_safe, "_is_portable_mode", return_value=False), \
            patch.object(fb_safe.Path, "home", return_value=fake_home), \
            patch.object(fb_safe.sys, "platform", "darwin"):
        app_data_mac = fb_safe._resolve_app_data_dir(fake_exe_dir, portable=False)
        lib_mac = fb_safe._resolve_os_default_library_dir(fake_exe_dir, app_data_mac, portable=False)
        assert app_data_mac == (fake_home / "Library" / "Application Support" / "SwarariaAudioLab").resolve()
        assert lib_mac == (fake_home / "Music" / "SwarariaAudioLab").resolve()

    # 3. WSL shares Windows user Music folder and Windows AppData settings.json while keeping native POSIX app data
    fake_win_home = tmp_path / "win_users" / "activewin"
    (fake_win_home / "Music").mkdir(parents=True)
    (fake_win_home / "AppData" / "Local").mkdir(parents=True)
    with patch.dict(os.environ, clean_env, clear=True), \
            patch.object(fb_safe, "_is_wsl", return_value=True), \
            patch.object(fb_safe, "detect_wsl_windows_home", return_value=fake_win_home), \
            patch.object(fb_safe.Path, "home", return_value=fake_home), \
            patch.object(fb_safe.sys, "platform", "linux"):
        wsl_app_data = fb_safe._resolve_app_data_dir(fake_repo, portable=False)
        wsl_settings = fb_safe._resolve_settings_file(wsl_app_data, portable=False)
        wsl_lib = fb_safe._resolve_os_default_library_dir(fake_repo, wsl_app_data, portable=False)
        assert wsl_app_data == (fake_home / ".local" / "share" / "swararia-audio-lab").resolve()
        assert wsl_settings == (fake_win_home / "AppData" / "Local" / "SwarariaAudioLab" / "settings.json").resolve()
        assert wsl_lib == (fake_win_home / "Music" / "SwarariaAudioLab").resolve()

    # 4. Portable mode keeps data inside base_dir/library
    with patch.dict(os.environ, clean_env, clear=True):
        port_data = fb_safe._resolve_app_data_dir(fake_exe_dir, portable=True)
        port_settings = fb_safe._resolve_settings_file(port_data, portable=True)
        port_lib = fb_safe._resolve_os_default_library_dir(fake_exe_dir, port_data, portable=True)
        assert port_data == (fake_exe_dir / "library").resolve()
        assert port_settings == (fake_exe_dir / "library" / "settings.json").resolve()
        assert port_lib == (fake_exe_dir / "library" / "inbox").resolve()


def test_wsl_windows_home_detection_skips_inaccessible_sibling_accounts(tmp_path: Path):
    mnt_root = tmp_path / "mnt"
    users_dir = mnt_root / "c" / "Users"
    (users_dir / "Public").mkdir(parents=True)
    (users_dir / "Default").mkdir(parents=True)

    locked_user = users_dir / "otheruser"
    locked_user.mkdir(parents=True)
    active_user = users_dir / "realwinuser"
    (active_user / "Music").mkdir(parents=True)

    orig_access = os.access

    def fake_access(path, mode):
        p = Path(path)
        if p == locked_user or locked_user in p.parents:
            return False
        return orig_access(path, mode)

    with patch.dict(os.environ, {"USERPROFILE": ""}, clear=False), \
            patch.object(fb_safe.os, "access", side_effect=fake_access):
        detected = fb_safe.detect_wsl_windows_home(mnt_root=mnt_root)
        assert detected == active_user


def test_settings_persistence_priority_and_fail_closed_fallback(tmp_path: Path):
    os_default = (tmp_path / "Music" / "SwarariaAudioLab").resolve()
    os_default.mkdir(parents=True)
    app_data = (tmp_path / "appdata").resolve()
    app_data.mkdir(parents=True)
    settings_file = app_data / "settings.json"
    custom_dir = (tmp_path / "FOH_Audio").resolve()
    custom_dir.mkdir(parents=True)
    env_dir = (tmp_path / "Env_Audio").resolve()

    with patch.object(fb_safe, "DEFAULT_LIBRARY_DIR", os_default), \
            patch.object(fb_safe, "APP_DATA_DIR", app_data), \
            patch.object(fb_safe, "SETTINGS_FILE", settings_file), \
            patch.object(fb_safe, "INBOX_DIR", os_default), \
            patch.object(fb_safe, "get_allowed_roots", return_value=[tmp_path.resolve()]), \
            patch.dict(os.environ, {fb_safe.ENV_LIBRARY_DIR: ""}, clear=False):
        # Default when settings.json absent
        eff, src = fb_safe.resolve_effective_library_dir(default_dir=os_default, settings_file=settings_file)
        assert eff == os_default and src == "default"

        # Save custom directory -> persists to settings.json
        saved, src = fb_safe.set_configured_library_dir(str(custom_dir), settings_file=settings_file)
        assert saved == custom_dir and src == "custom"
        assert settings_file.is_file()
        eff, src = fb_safe.resolve_effective_library_dir(default_dir=os_default, settings_file=settings_file)
        assert eff == custom_dir and src == "custom"

        # SWARARIA_LIBRARY_DIR env var takes highest priority and auto-creates safely
        with patch.dict(os.environ, {fb_safe.ENV_LIBRARY_DIR: str(env_dir)}, clear=False):
            eff_env, src_env = fb_safe.resolve_effective_library_dir(default_dir=os_default, settings_file=settings_file)
            assert eff_env == env_dir and src_env == "env" and env_dir.is_dir()
            try:
                fb_safe.set_configured_library_dir(str(custom_dir), settings_file=settings_file)
                raise AssertionError("Expected ValueError when env override locks library dir")
            except ValueError as exc:
                assert "SWARARIA_LIBRARY_DIR" in str(exc)

        # Corrupt JSON in settings.json fails closed to os_default
        settings_file.write_text("{corrupt-json", encoding="utf-8")
        eff, src = fb_safe.resolve_effective_library_dir(default_dir=os_default, settings_file=settings_file)
        assert eff == os_default and src == "default"

        # Missing or unsafe path in settings.json fails closed to os_default
        settings_file.write_text(json.dumps({"library_dir": str(tmp_path / "does_not_exist")}), encoding="utf-8")
        eff, src = fb_safe.resolve_effective_library_dir(default_dir=os_default, settings_file=settings_file)
        assert eff == os_default and src == "default"

        # Reset restores OS default and clears saved key
        fb_safe.set_configured_library_dir(str(custom_dir), settings_file=settings_file)
        restored, src = fb_safe.set_configured_library_dir(None, reset=True, settings_file=settings_file)
        assert restored == os_default and src == "default"
        assert "library_dir" not in json.loads(settings_file.read_text(encoding="utf-8"))


def test_app_data_dir_not_in_allowed_roots_and_shortcuts_include_os_default_and_legacy(tmp_path: Path):
    os_default = (tmp_path / "Music" / "SwarariaAudioLab").resolve()
    custom_dir = (tmp_path / "FOH_Audio").resolve()
    app_data = (tmp_path / "private_appdata").resolve()
    legacy_base = (tmp_path / "legacy_repo").resolve()
    legacy_inbox = (legacy_base / "library" / "inbox").resolve()
    for d in (os_default, custom_dir, app_data, legacy_inbox):
        d.mkdir(parents=True)

    with patch.object(fb_safe, "DEFAULT_LIBRARY_DIR", os_default), \
            patch.object(fb_safe, "INBOX_DIR", custom_dir), \
            patch.object(fb_safe, "APP_DATA_DIR", app_data), \
            patch.object(fb_safe, "BASE_DIR", legacy_base), \
            patch.object(fb_safe, "RESOURCE_DIR", legacy_base):
        roots = fb_safe.get_allowed_roots()
        assert app_data not in roots
        assert custom_dir in roots
        assert os_default in roots

        shortcuts = fb_browse.get_windows_shortcuts(custom_dir)
        paths = [s["path"] for s in shortcuts]
        labels = [s["label"] for s in shortcuts]
        assert str(custom_dir) in paths
        assert str(os_default) in paths
        assert str(legacy_inbox) in paths
        assert any("Default OS" in lbl for lbl in labels)
        assert any("Legacy Inbox" in lbl for lbl in labels)


@contextlib.contextmanager
def _running_isolated_server(tmp_path: Path):
    os_default = (tmp_path / "Music" / "SwarariaAudioLab").resolve()
    custom_dir = (tmp_path / "SharedAudio").resolve()
    app_data = (tmp_path / "appdata").resolve()
    settings_file = app_data / "settings.json"
    for d in (os_default, custom_dir, app_data):
        d.mkdir(parents=True)

    # Create distinct dummy audio files so GET /api/library proves which folder is active
    (os_default / "default_track.mp3").write_bytes(b"ID3" + b"\x00" * 64)
    (custom_dir / "custom_track.mp3").write_bytes(b"ID3" + b"\x00" * 64)

    orig_task_inbox = srv_state.task_manager.inbox_dir
    with patch.object(fb_safe, "DEFAULT_LIBRARY_DIR", os_default), \
            patch.object(fb_safe, "APP_DATA_DIR", app_data), \
            patch.object(fb_safe, "SETTINGS_FILE", settings_file), \
            patch.object(fb_safe, "INBOX_DIR", os_default), \
            patch.object(fb_safe, "get_allowed_roots", return_value=[tmp_path.resolve()]), \
            patch.object(srv_state, "DEFAULT_LIBRARY_DIR", os_default), \
            patch.object(srv_state, "APP_DATA_DIR", app_data), \
            patch.object(srv_state, "SETTINGS_FILE", settings_file), \
            patch.object(srv_state, "INBOX_DIR", os_default), \
            patch.object(srv_hub, "remote_hub", srv_hub.RemotePlayerHub()), \
            patch.object(srv_network, "_cleanup_stale_wsl_listen_port"), \
            patch.dict(os.environ, {fb_safe.ENV_LIBRARY_DIR: ""}, clear=False):
        srv_state.task_manager.inbox_dir = os_default
        srv_state.metadata_cache.invalidate_dir_cache()
        # Security: test servers strictly bind 127.0.0.1, never 0.0.0.0.
        server, port = srv_app.create_server("127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def req(method: str, path: str, payload=None, headers=None):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
            h = {"Content-Type": "application/json"}
            h.update(headers or {})
            body = json.dumps(payload) if payload is not None else None
            conn.request(method, path, body, h)
            resp = conn.getresponse()
            raw = resp.read()
            conn.close()
            return resp.status, json.loads(raw) if raw else None

        origin = {"Origin": f"http://127.0.0.1:{port}"}
        try:
            yield server, req, origin, os_default, custom_dir, app_data, settings_file
        finally:
            server.shutdown()
            server.server_close()
            thread.join(4)
            srv_state.task_manager.inbox_dir = orig_task_inbox
            srv_state.metadata_cache.invalidate_dir_cache()


def test_http_library_settings_live_update_and_security(tmp_path: Path):
    with _running_isolated_server(tmp_path) as (server, req, origin, os_default, custom_dir, app_data, settings_file):
        # 1. GET /api/settings/library returns OS default initially
        status, cfg = req("GET", "/api/settings/library")
        assert status == 200 and cfg["ok"] is True
        assert cfg["library_dir"] == str(os_default)
        assert cfg["os_default_dir"] == str(os_default)
        assert cfg["source"] == "default" and cfg["is_custom"] is False

        # Initial /api/library serves default_track.mp3
        status, lib = req("GET", "/api/library")
        assert status == 200
        assert [f["name"] for f in lib["files"]] == ["default_track.mp3"]

        # 2. Security: POST without Origin header must fail closed (403)
        status, err = req("POST", "/api/settings/library", {"library_dir": str(custom_dir)})
        assert status == 403 and err["code"] == "local_origin_required"

        # 3. Security: POST with foreign Origin must fail closed (403)
        status, _ = req(
            "POST",
            "/api/settings/library",
            {"library_dir": str(custom_dir)},
            {"Origin": "https://evil.example"},
        )
        assert status == 403

        # 4. Security: unknown fields, traversal, dot-folder, symlink, AppData, system dir rejected (400)
        assert req("POST", "/api/settings/library", {"library_dir": str(custom_dir), "extra": 1}, origin)[0] == 400
        assert req("POST", "/api/settings/library", {"library_dir": "../../etc"}, origin)[0] == 400

        dot_dir = tmp_path / ".hidden_audio"
        dot_dir.mkdir()
        assert req("POST", "/api/settings/library", {"library_dir": str(dot_dir)}, origin)[0] == 400

        sys_dir = tmp_path / "AppData"
        sys_dir.mkdir()
        assert req("POST", "/api/settings/library", {"library_dir": str(sys_dir)}, origin)[0] == 400

        assert req("POST", "/api/settings/library", {"library_dir": str(app_data)}, origin)[0] == 400

        sym_dir = tmp_path / "symlink_audio"
        sym_dir.symlink_to(custom_dir)
        assert req("POST", "/api/settings/library", {"library_dir": str(sym_dir)}, origin)[0] == 400

        assert req("POST", "/api/settings/library", {"library_dir": str(tmp_path / "missing")}, origin)[0] == 400
        assert not settings_file.exists(), "Rejected requests must never write settings.json"

        # 5. Valid POST updates state.INBOX_DIR, task_manager.inbox_dir, secure_remote.default_inbox live
        status, updated = req("POST", "/api/settings/library", {"library_dir": str(custom_dir)}, origin)
        assert status == 200 and updated["ok"] is True
        assert updated["library_dir"] == str(custom_dir)
        assert updated["source"] == "custom" and updated["is_custom"] is True
        assert srv_state.INBOX_DIR == custom_dir
        assert srv_state.task_manager.inbox_dir == custom_dir
        assert server.secure_remote.default_inbox == custom_dir
        assert settings_file.is_file()

        # Subsequent GET /api/library without source_dir immediately lists custom_track.mp3
        status, lib2 = req("GET", "/api/library")
        assert status == 200
        assert lib2["default_inbox"] == str(custom_dir)
        assert lib2["default_inbox_source"] == "custom"
        assert [f["name"] for f in lib2["files"]] == ["custom_track.mp3"]

        # 6. Reset restores OS default live
        status, reset_res = req("POST", "/api/settings/library", {"reset": True}, origin)
        assert status == 200 and reset_res["source"] == "default"
        assert srv_state.INBOX_DIR == os_default
        assert srv_state.task_manager.inbox_dir == os_default
        assert server.secure_remote.default_inbox == os_default

        # 7. Security: LAN clients cannot read or modify library settings
        class LanPeer(srv_handler.AudioServerHandler):
            def __init__(self, request, address, owner):
                super().__init__(request, ("192.168.1.50", address[1]), owner)

        server.RequestHandlerClass = LanPeer
        lan_headers = {**origin, "X-FOH-PIN": srv_hub.remote_hub.pin}
        assert req("GET", "/api/settings/library", headers=lan_headers)[0] == 403
        assert req("POST", "/api/settings/library", {"library_dir": str(custom_dir)}, lan_headers)[0] == 403
