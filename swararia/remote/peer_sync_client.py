"""Pinned TLS client and in-memory peer session manager for PC <-> PC folder sync."""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import re
import secrets
import ssl
import threading
import time

from swararia.remote.secure_remote import RemoteError
from swararia.remote.secure_remote_tls import validate_bind


_FILE_ID_RE = re.compile(r'^fil_[0-9a-f]{20}$')
_FOLDER_ID_RE = re.compile(r'^(root|fld_[0-9a-f]{16})$')


def parse_pairing_string(raw: str) -> tuple[str, int, bytes, str, str]:
    """Parses and strictly validates `swararia1|<ipv4>:<port>|<b64url_sha256>|<invite>`."""
    if not isinstance(raw, str):
        raise RemoteError('invalid_pairing_code', 400)
    parts = raw.strip().split('|')
    if len(parts) != 4 or parts[0] != 'swararia1':
        raise RemoteError('invalid_pairing_code', 400)

    host_port, fp_b64, invite = parts[1].strip(), parts[2].strip(), parts[3].strip()
    if ':' not in host_port or host_port.count(':') != 1:
        raise RemoteError('invalid_pairing_host', 400)
    host, port_str = host_port.split(':', 1)
    try:
        ipaddress.IPv4Address(host)
        port = int(port_str)
    except (ValueError, ipaddress.AddressValueError):
        raise RemoteError('invalid_pairing_host', 400) from None
    if not (1 <= port <= 65535):
        raise RemoteError('invalid_pairing_host', 400)

    validate_bind(host, port)

    if not re.fullmatch(r'[A-Za-z0-9_-]{43,44}={0,2}', fp_b64):
        raise RemoteError('invalid_pairing_fingerprint', 400)
    padded = fp_b64 + '=' * (-len(fp_b64) % 4)
    try:
        fp_bytes = base64.urlsafe_b64decode(padded.encode('ascii'))
    except ValueError:
        raise RemoteError('invalid_pairing_fingerprint', 400) from None
    if len(fp_bytes) != 32:
        raise RemoteError('invalid_pairing_fingerprint', 400)

    if not (16 <= len(invite) <= 128) or any(not ch.isprintable() or ch.isspace() for ch in invite):
        raise RemoteError('invalid_invite', 400)

    return host, port, fp_bytes, fp_bytes.hex(), invite


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection verifying the peer leaf certificate SHA-256 before any HTTP byte is sent."""

    def __init__(self, host: str, port: int, *, expected_digest: bytes, timeout: float = 10.0) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = False
        # Ephemeral self-signed leaf certs lack a shared CA; verified strictly by SHA-256 below.
        context.verify_mode = ssl.CERT_NONE
        super().__init__(host, port, timeout=timeout, context=context)
        self._expected_digest = expected_digest

    def connect(self) -> None:
        super().connect()
        try:
            peer_der = self.sock.getpeercert(binary_form=True) if self.sock else b''
        except OSError:
            peer_der = b''
        if not peer_der:
            self.close()
            raise RemoteError('fingerprint_mismatch', 495)
        actual_digest = hashlib.sha256(peer_der).digest()
        if not hmac.compare_digest(actual_digest, self._expected_digest):
            self.close()
            raise RemoteError('fingerprint_mismatch', 495)


class PeerSyncManager:
    """Thread-safe RAM-only store of paired Host PCs and pinned HTTPS sync operations."""

    MAX_PEERS = 8

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.peers: dict[str, dict] = {}

    @staticmethod
    def _public_peer_view(peer: dict) -> dict:
        return {
            'peer_id': peer['peer_id'],
            'url': peer['url'],
            'host': peer['host'],
            'port': peer['port'],
            'fingerprint_sha256': peer['fingerprint_hex'],
            'device_id': peer['device_id'],
            'label': peer['label'],
            'status': peer['status'],
            'role': peer['role'],
            'expires_at': peer['expires_at'],
            'sync_capable': bool(peer.get('sync_capable')),
            'media_capable': bool(peer.get('media_capable')),
        }

    def _get_peer(self, peer_id: str) -> dict:
        if not isinstance(peer_id, str) or not peer_id.strip():
            raise RemoteError('unknown_peer', 404)
        with self.lock:
            peer = self.peers.get(peer_id)
            if peer is None:
                raise RemoteError('unknown_peer', 404)
            return dict(peer)

    @staticmethod
    def _request_json(
        host: str,
        port: int,
        fp_bytes: bytes,
        method: str,
        path: str,
        token: str,
        payload: dict | None = None,
        *,
        extended: bool = True,
        timeout: float = 10.0,
    ) -> dict:
        conn = _PinnedHTTPSConnection(host, port, expected_digest=fp_bytes, timeout=timeout)
        try:
            headers = {
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json',
            }
            if extended:
                headers['X-Swararia-Capabilities'] = 'extended-v1'
            body_bytes = json.dumps(payload).encode('utf-8') if payload is not None else None
            conn.request(method, path, body=body_bytes, headers=headers)
            resp = conn.getresponse()
            raw = resp.read(2 * 1024 * 1024)
            try:
                data = json.loads(raw.decode('utf-8')) if raw else {}
            except (ValueError, UnicodeError):
                data = {}
            if resp.status >= 400:
                code = str(data.get('code') or 'peer_request_failed')
                raise RemoteError(code, resp.status)
            if not isinstance(data, dict):
                raise RemoteError('invalid_peer_response', 502)
            return data
        except RemoteError:
            raise
        except (OSError, http.client.HTTPException, ssl.SSLError):
            raise RemoteError('peer_unreachable', 502) from None
        finally:
            conn.close()

    def pair_with_host(self, pairing_code: str, client_label: str = 'Swararia PC Sync') -> dict:
        host, port, fp_bytes, fp_hex, invite = parse_pairing_string(pairing_code)
        safe_label = ''.join(ch for ch in str(client_label or 'Swararia PC Sync') if ch.isprintable()).strip()[:80]
        if not safe_label:
            safe_label = 'Swararia PC Sync'

        data = self._request_json(
            host,
            port,
            fp_bytes,
            'POST',
            '/v1/pair',
            invite,
            {'label': safe_label},
            extended=False,
        )
        token = str(data.get('token') or '')
        device_id = str(data.get('device_id') or '')
        if not token or not device_id:
            raise RemoteError('invalid_peer_response', 502)

        peer_id = 'peer_' + secrets.token_urlsafe(12)
        peer = {
            'peer_id': peer_id,
            'host': host,
            'port': port,
            'url': f'https://{host}:{port}',
            'fp_bytes': fp_bytes,
            'fingerprint_hex': fp_hex,
            'device_id': device_id,
            'token': token,
            'label': safe_label,
            'status': str(data.get('status') or 'pending'),
            'role': None,
            'expires_at': 0,
            'sync_capable': False,
            'media_capable': False,
            'created_at': time.time(),
        }
        with self.lock:
            if len(self.peers) >= self.MAX_PEERS:
                oldest = min(self.peers, key=lambda pid: self.peers[pid].get('created_at', 0))
                self.peers.pop(oldest, None)
            self.peers[peer_id] = peer
        return self._public_peer_view(peer)

    def refresh_status(self, peer_id: str) -> dict:
        peer = self._get_peer(peer_id)
        sess = self._request_json(
            peer['host'],
            peer['port'],
            peer['fp_bytes'],
            'GET',
            '/v1/session',
            peer['token'],
            extended=False,
        )
        sync_capable = False
        media_capable = False
        if sess.get('status') == 'active':
            try:
                caps = self._request_json(
                    peer['host'],
                    peer['port'],
                    peer['fp_bytes'],
                    'GET',
                    '/v1/capabilities',
                    peer['token'],
                    extended=True,
                )
                sync_capable = bool(caps.get('sync'))
                media_capable = bool(caps.get('media'))
            except RemoteError:
                pass
        with self.lock:
            stored = self.peers.get(peer_id)
            if stored is None:
                raise RemoteError('unknown_peer', 404)
            stored['status'] = str(sess.get('status') or stored['status'])
            stored['role'] = sess.get('role')
            stored['expires_at'] = sess.get('expires_at') or 0
            stored['sync_capable'] = sync_capable
            stored['media_capable'] = media_capable
            return self._public_peer_view(stored)

    def list_peers(self) -> list[dict]:
        with self.lock:
            return [self._public_peer_view(p) for p in self.peers.values()]

    def disconnect(self, peer_id: str | None = None) -> None:
        with self.lock:
            if peer_id is None:
                self.peers.clear()
            else:
                self.peers.pop(peer_id, None)

    def fetch_folders(self, peer_id: str) -> dict:
        peer = self._get_peer(peer_id)
        return self._request_json(
            peer['host'],
            peer['port'],
            peer['fp_bytes'],
            'GET',
            '/v1/sync/folders',
            peer['token'],
            extended=True,
            timeout=15.0,
        )

    def fetch_folder_manifest(self, peer_id: str, folder_id: str, max_pages: int = 100) -> dict:
        if not isinstance(folder_id, str) or not _FOLDER_ID_RE.fullmatch(folder_id):
            raise RemoteError('invalid_folder', 400)
        peer = self._get_peer(peer_id)
        all_files: list[dict] = []
        offset = 0
        meta: dict = {}
        for _ in range(max_pages):
            page = self._request_json(
                peer['host'],
                peer['port'],
                peer['fp_bytes'],
                'POST',
                '/v1/sync/manifest',
                peer['token'],
                {'folder_id': folder_id, 'offset': offset, 'limit': 100},
                extended=True,
                timeout=30.0,
            )
            if not meta:
                meta = {
                    'folder_id': page.get('folder_id', folder_id),
                    'rel_folder': page.get('rel_folder', ''),
                    'folder_name': page.get('folder_name', ''),
                    'total': int(page.get('total') or 0),
                    'excluded_wav_flac_count': int(page.get('excluded_wav_flac_count') or 0),
                    'oversized_count': int(page.get('oversized_count') or 0),
                    'warnings': list(page.get('warnings') or []),
                }
            all_files.extend(page.get('files') or [])
            next_offset = page.get('next_offset')
            if next_offset is None or next_offset <= offset:
                break
            offset = int(next_offset)
        meta['files'] = all_files
        return meta

    def download_sync_file_to_part(
        self,
        peer_id: str,
        file_id: str,
        dest_part_path,
        *,
        max_bytes: int,
        expected_sha256: str | None = None,
        progress_cb=None,
    ) -> dict:
        if not isinstance(file_id, str) or not _FILE_ID_RE.fullmatch(file_id):
            raise RemoteError('invalid_file_id', 400)
        peer = self._get_peer(peer_id)
        conn = _PinnedHTTPSConnection(peer['host'], peer['port'], expected_digest=peer['fp_bytes'], timeout=30.0)
        try:
            conn.request(
                'GET',
                f'/v1/sync/files/{file_id}',
                headers={
                    'Authorization': f"Bearer {peer['token']}",
                    'X-Swararia-Capabilities': 'extended-v1',
                },
            )
            resp = conn.getresponse()
            if resp.status >= 400:
                raw_err = resp.read(4096)
                try:
                    code = str(json.loads(raw_err.decode('utf-8')).get('code') or 'media_unavailable')
                except (ValueError, UnicodeError):
                    code = 'media_unavailable'
                raise RemoteError(code, resp.status)

            raw_len = resp.getheader('Content-Length')
            if raw_len is not None:
                try:
                    announced_len = int(raw_len)
                except ValueError:
                    raise RemoteError('invalid_length', 502) from None
                if announced_len <= 0 or announced_len > max_bytes:
                    raise RemoteError('media_too_large', 413)
            else:
                announced_len = 0

            server_sha = str(resp.getheader('X-Swararia-Sha256') or '').strip().lower()
            hasher = hashlib.sha256()
            written = 0
            flags = os.O_WRONLY | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0)
            fd = os.open(dest_part_path, flags)
            try:
                with os.fdopen(fd, 'wb') as out_stream:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > max_bytes:
                            raise RemoteError('media_too_large', 413)
                        out_stream.write(chunk)
                        hasher.update(chunk)
                        if progress_cb:
                            progress_cb(written, announced_len)
            except Exception:
                try:
                    os.unlink(dest_part_path)
                except OSError:
                    pass
                raise

            actual_sha = hasher.hexdigest()
            if server_sha and not hmac.compare_digest(actual_sha, server_sha):
                os.unlink(dest_part_path)
                raise RemoteError('sha256_mismatch', 422)
            if expected_sha256 and not hmac.compare_digest(actual_sha, str(expected_sha256).strip().lower()):
                os.unlink(dest_part_path)
                raise RemoteError('sha256_mismatch', 422)

            return {'size_bytes': written, 'sha256': actual_sha}
        except RemoteError:
            raise
        except (OSError, http.client.HTTPException, ssl.SSLError):
            try:
                os.unlink(dest_part_path)
            except OSError:
                pass
            raise RemoteError('peer_unreachable', 502) from None
        finally:
            conn.close()
