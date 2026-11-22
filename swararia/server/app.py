"""HTTP server classes, FOH bridge, server factory and process entry point."""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer

from swararia.core import fs_browser, transfer
from swararia.remote.peer_sync_client import PeerSyncManager
from swararia.remote.secure_remote import RemoteError, SecureRemote
from swararia.server import hub, network, state
from swararia.server.handler import AudioServerHandler


def _native_upload_handler(
    rfile,
    content_length: int,
    dest_root: str,
    filename: str,
    sha256_hex: str,
    playlist_name: str | None,
) -> tuple[int, dict]:
    """Bridges Native TLS octet-stream uploads to core.transfer validation and cache updates."""
    try:
        committed = transfer.stage_and_commit_stream(
            rfile,
            content_length,
            dest_root,
            filename,
            sha256_hex,
            subfolder=playlist_name,
            default_inbox=state.INBOX_DIR,
        )
    except transfer.TransferValidationError as exc:
        raise RemoteError(exc.code, exc.status) from None

    saved_path = committed.pop("_saved_path", "")
    if saved_path:
        state.metadata_cache.invalidate_dir_cache(Path(dest_root))
        hub.remote_hub.bump_library_revision()
        threading.Thread(
            target=state.probe_files_fast,
            args=([Path(saved_path)],),
            kwargs={"include_spectral": False},
            daemon=True,
        ).start()

    status_code = 200 if committed.get("deduplicated") else 201
    return status_code, committed


class LocalAudioServer(ThreadingHTTPServer):
    """Own both listeners, so either launcher has the same shutdown boundary."""
    daemon_threads = True

    def __init__(self, address, handler):
        self.secure_remote = SecureRemote(
            hub.remote_hub,
            media_resolver=fs_browser.resolve_safe_audio_file,
            default_inbox=state.INBOX_DIR,
            sync_scanner=transfer.scan_sync_folders,
            upload_handler=_native_upload_handler,
        )
        self.peer_sync = PeerSyncManager()
        self._wsl_foh_bridge_server = None
        self._wsl_foh_bridge_thread = None
        self.wsl_foh_internal_port: int | None = None
        super().__init__(address, handler)
        if network.is_wsl_environment() and address and str(address[0]) == "0.0.0.0":
            self._start_wsl_foh_bridge()

    def _start_wsl_foh_bridge(self) -> None:
        primary_port = int(self.server_address[1])
        preferred_port = network._get_wsl_foh_internal_port(primary_port)
        bridge_srv = None
        for candidate_port in (preferred_port, 0):
            try:
                bridge_srv = _FohBridgeServer(
                    ("127.0.0.1", candidate_port),
                    _FohBridgedHandler,
                    parent_server=self,
                )
                break
            except OSError:
                continue
        if bridge_srv is None:
            return
        actual_internal = int(bridge_srv.server_address[1])
        self._wsl_foh_bridge_server = bridge_srv
        self.wsl_foh_internal_port = actual_internal
        with network._wsl_foh_ports_lock:
            network._active_wsl_foh_ports[primary_port] = actual_internal
        started = threading.Event()

        def _serve() -> None:
            started.set()
            bridge_srv.serve_forever()

        thread = threading.Thread(target=_serve, daemon=True)
        self._wsl_foh_bridge_thread = thread
        thread.start()
        started.wait(timeout=1.0)

    def _stop_wsl_foh_bridge(self) -> None:
        bridge_srv = self._wsl_foh_bridge_server
        bridge_thread = self._wsl_foh_bridge_thread
        internal_port = self.wsl_foh_internal_port
        self._wsl_foh_bridge_server = None
        self._wsl_foh_bridge_thread = None
        self.wsl_foh_internal_port = None
        try:
            primary_port = int(self.server_address[1])
            with network._wsl_foh_ports_lock:
                if internal_port is not None and network._active_wsl_foh_ports.get(primary_port) == internal_port:
                    network._active_wsl_foh_ports.pop(primary_port, None)
        except Exception:
            pass
        if bridge_srv is not None:
            try:
                bridge_srv.shutdown()
            except Exception:
                pass
            try:
                bridge_srv.server_close()
            except Exception:
                pass
        if bridge_thread is not None and bridge_thread.is_alive():
            bridge_thread.join(timeout=2.0)

    def server_close(self):
        self._stop_wsl_foh_bridge()
        self.secure_remote.stop()
        self.peer_sync.disconnect()
        super().server_close()


class _FohBridgedHandler(AudioServerHandler):
    """Handles WSL2 Windows portproxy -> wslrelay FOH traffic strictly as a non-localhost LAN client."""

    class _ServerProxy:
        __slots__ = ("_inner", "server_address")

        def __init__(self, inner, primary_port: int) -> None:
            self._inner = inner
            self.server_address = ("0.0.0.0", primary_port)

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

    def __init__(self, request, client_address, server):
        proxy = self._ServerProxy(server, int(getattr(server, "primary_port", server.server_address[1])))
        super().__init__(request, client_address, proxy)

    def is_localhost_client(self) -> bool:
        return False


class _FohBridgeServer(ThreadingHTTPServer):
    """Loopback-only WSL2 companion HTTP listener for wslrelay FOH portproxy forwarding."""
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, parent_server: LocalAudioServer):
        self.secure_remote = parent_server.secure_remote
        self.primary_port = int(parent_server.server_address[1])
        super().__init__(address, handler)


_worker_started = False
_worker_lock = threading.Lock()


def ensure_worker_started() -> None:
    """Starts the background FIFO task worker thread once."""
    global _worker_started
    with _worker_lock:
        if not _worker_started:
            worker_thread = threading.Thread(target=state.run_worker, daemon=True)
            worker_thread.start()
            _worker_started = True


def create_server(host: str = "0.0.0.0", port: int = 8765) -> tuple[ThreadingHTTPServer, int]:
    """Starts the background worker and creates the HTTP server, falling back to a free port if needed."""
    ensure_worker_started()
    network._cleanup_stale_wsl_listen_port(port)
    ThreadingHTTPServer.allow_reuse_address = True
    try:
        server = LocalAudioServer((host, port), AudioServerHandler)
    except OSError:
        try:
            server = LocalAudioServer((host, 0), AudioServerHandler)
        except OSError:
            server = LocalAudioServer(("127.0.0.1", 0), AudioServerHandler)
    actual_port = int(server.server_address[1])
    return server, actual_port


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "-m" and sys.argv[2] == "yt_dlp":
        sys.argv = ["yt-dlp"] + sys.argv[3:]
        import yt_dlp

        yt_dlp.main()
        return

    bind_host = "0.0.0.0"  # Listens on LAN for FOH Mobile Remote (gated by PIN & localhost checks)
    local_host = "127.0.0.1"
    port = 8765

    server, actual_port = create_server(bind_host, port)
    remote_port = network._get_wsl_remote_bridge_port(actual_port)
    lan_ips = network.get_local_lan_ips(force_refresh=True)
    print("=" * 65)
    print("  SWARARIA AUDIO LAB // BATCH INGESTION & SOUNDSYSTEM QC ENGINE")
    print("=" * 65)
    print(f"  Server running locally at : http://{local_host}:{actual_port}")
    if lan_ips:
        print(f"  FOH Mobile Remote (HP)    : http://{lan_ips[0]}:{remote_port}/player?remote=1")
        print(f"  FOH Remote Security PIN   : {hub.remote_hub.pin}")
    print(f"  Inbox Folder              : {state.INBOX_DIR}")
    print("  Press Ctrl+C to stop.")
    print("=" * 65)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server gracefully...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
