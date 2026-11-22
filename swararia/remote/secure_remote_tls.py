"""Bounded private-IPv4 TLS transport. No static files, redirects or request logs."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import sys
import tempfile
import threading
import time
import urllib.parse

from swararia.remote.secure_remote import RemoteError, encode_remote_json


def tls_context(host, extra_ips=()):
    # CPython 3.12.4 fixed Windows mkdtemp permissions (CVE-2024-4030).
    # Never write session keys using an older Windows runtime's inherited ACLs.
    if os.name == 'nt' and tuple(sys.version_info[:3]) < (3, 12, 4):
        raise RemoteError('secure_temp_runtime_required', 503)
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
    san_ips = list(dict.fromkeys(ipaddress.ip_address(ip) for ip in (host, *extra_ips) if ip))
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Swararia session remote')])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=15)).not_valid_after(now + timedelta(hours=12))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(addr) for addr in san_ips]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(key, hashes.SHA256()))
    pem = cert.public_bytes(serialization.Encoding.PEM)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    # Windows inherits owner ACLs from its user temp directory; POSIX also sets
    # restrictive modes. Files are gone before the listener accepts any clients.
    with tempfile.TemporaryDirectory(prefix='swararia-tls-') as directory:
        os.chmod(directory, 0o700)
        paths = []
        for name, data in [('cert.pem', pem), ('key.pem', key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))]:
            path = Path(directory) / name
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
            paths.append(str(path))
        context.load_cert_chain(*paths)
    return context, pem.decode('ascii'), cert.fingerprint(hashes.SHA256()).hex()


def validate_bind(host, port):
    try:
        ip = ipaddress.IPv4Address(host)
        allowed = ip.is_loopback or any(ip in ipaddress.ip_network(n) for n in
                                        ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
        if not allowed or type(port) is not int or not (1 <= port <= 65535 or port == 0 and ip.is_loopback):
            raise ValueError()
    except (ValueError, TypeError, ipaddress.AddressValueError):
        raise RemoteError('invalid_bind', 400) from None


class NativeTLSServer(HTTPServer):
    # POSIX SO_REUSEADDR allows immediate rebind after TIME_WAIT without permitting
    # another live socket to hijack the port; Windows keeps SO_REUSEADDR disabled.
    allow_reuse_address = os.name == 'posix'
    request_queue_size = 16
    CONNECTION_TTL = 6
    MEDIA_CONNECTION_TTL = 120

    def __init__(self, address, owner, context):
        self.owner, self.context = owner, context
        self.slots = threading.BoundedSemaphore(16)
        self.media_slots = threading.BoundedSemaphore(2)
        self.sockets = set()
        self.workers = set()
        self.connection_timers = {}
        self.guard = threading.Lock()
        self.closing = False
        self.buckets = {'all': [120.0, time.monotonic()], 'pair': [10.0, time.monotonic()]}
        super().__init__(address, NativeHandler)

    def rate(self, category):
        with self.guard:
            capacity, refill = (120, 20) if category == 'all' else (10, 1 / 6)
            bucket = self.buckets[category]
            now = time.monotonic()
            bucket[0] = min(capacity, bucket[0] + (now - bucket[1]) * refill)
            bucket[1] = now
            if bucket[0] < 1:
                raise RemoteError('rate_limited', 429)
            bucket[0] -= 1

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        worker = threading.Thread(target=self._serve_client, args=(request, address), daemon=True)
        with self.guard:
            if self.closing:
                self.slots.release()
                request.close()
                return
            self.sockets.add(request)
            self.workers.add(worker)
            # Publish/start atomically with respect to shutdown's worker snapshot.
            worker.start()

    def _serve_client(self, raw, address):
        connection = raw
        def deadline():
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        timer_box = [None]
        tid = threading.get_ident()
        with self.guard:
            self.connection_timers[tid] = (deadline, timer_box)
        timer = threading.Timer(self.CONNECTION_TTL, deadline)
        timer.daemon = True
        timer_box[0] = timer
        timer.start()
        try:
            raw.settimeout(3)
            connection = self.context.wrap_socket(raw, server_side=True, do_handshake_on_connect=False)
            with self.guard:
                self.sockets.discard(raw)
                self.sockets.add(connection)
                if self.closing:
                    return
            connection.do_handshake()
            self.finish_request(connection, address)
        except (OSError, ValueError):
            pass  # Never print raw requests, credentials, SSL or parser exceptions.
        finally:
            with self.guard:
                entry = self.connection_timers.pop(tid, None)
            active_timer = entry[1][0] if entry else timer
            if active_timer is not None:
                active_timer.cancel()
            # Send TLS close_notify before closing a connection whose rejected
            # body may still be in flight. Never drain unbounded attacker data.
            if isinstance(connection, ssl.SSLSocket):
                try:
                    connection.settimeout(0.2)
                    connection.unwrap().close()
                except (OSError, ValueError):
                    pass
            connection.close()
            raw.close()
            with self.guard:
                self.sockets.discard(raw)
                self.sockets.discard(connection)
                self.workers.discard(threading.current_thread())
            self.slots.release()

    def extend_deadline(self, ttl_seconds):
        tid = threading.get_ident()
        with self.guard:
            if self.closing:
                return
            entry = self.connection_timers.get(tid)
            if not entry:
                return
            deadline_fn, timer_box = entry
            if timer_box[0] is not None:
                timer_box[0].cancel()
            new_timer = threading.Timer(ttl_seconds, deadline_fn)
            new_timer.daemon = True
            timer_box[0] = new_timer
            new_timer.start()

    def server_close(self):
        with self.guard:
            self.closing = True
            for connection in list(self.sockets):
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()
            workers = list(self.workers)
        super().server_close()
        for worker in workers:
            worker.join(timeout=4)

    def handle_error(self, request, client_address):
        pass


class NativeHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'
    timeout = 3
    server_version = 'SwarariaNative/1'
    sys_version = ''

    def log_message(self, *args):
        pass

    def log_error(self, *args):
        pass

    def send_error(self, code, message=None, explain=None):
        self.reply(code, {'code': 'invalid_request'})

    def reply(self, status, data):
        self.close_connection = True
        body = encode_remote_json(data)
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Connection', 'close')
        self.end_headers()
        if getattr(self, 'command', None) != 'HEAD':
            self.wfile.write(body)

    def parse_request(self):
        valid = super().parse_request()
        if valid:
            # BaseHTTPRequestHandler normalizes // to /. Reject the original.
            pieces = self.raw_requestline.split()
            if len(pieces) != 3 or pieces[1].startswith(b'//'):
                self.reply(404, {'code': 'unknown_route'})
                return False
        return valid

    def _token(self):
        values = self.headers.get_all('Authorization', [])
        if len(values) != 1 or not values[0].startswith('Bearer '):
            raise RemoteError('unauthorized', 401)
        token = values[0][7:]
        if not 20 <= len(token) <= 128:
            raise RemoteError('unauthorized', 401)
        return token

    def _body(self):
        lengths = self.headers.get_all('Content-Length', [])
        if self.headers.get_all('Transfer-Encoding') or len(lengths) != 1 or not re.fullmatch(r'[0-9]{1,8}', lengths[0]):
            raise RemoteError('invalid_length', 400)
        length = int(lengths[0])
        if length > 4096:
            raise RemoteError('body_too_large', 413)
        data = self.rfile.read(length)
        if self.headers.get('Content-Type', '').lower() != 'application/json':
            raise RemoteError('json_required', 415)
        try:
            def unique(pairs):
                value = {}
                for k, v in pairs:
                    if k in value:
                        raise ValueError()
                    value[k] = v
                return value
            if len(data) != length:
                raise ValueError()
            payload = json.loads(data, object_pairs_hook=unique,
                                 parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(payload, dict):
                raise ValueError()
            return payload
        except (ValueError, UnicodeError, RecursionError):
            raise RemoteError('invalid_json', 400) from None

    def _serve_authorized_media(self, media_ref, owner):
        if not self.server.media_slots.acquire(blocking=False):
            raise RemoteError('media_busy', 429)
        stream = None
        try:
            self.server.extend_deadline(self.server.MEDIA_CONNECTION_TTL)
            try:
                self.connection.settimeout(15)
            except (OSError, ValueError):
                pass
            stream, mime_type, size_bytes, sha256_hex, filename = owner.open_media_file(
                media_ref,
                need_stream=(self.command == 'GET'),
            )
            ascii_name = ''.join(
                ch if 32 <= ord(ch) <= 126 and ch not in ('"', '\\', ';') else '_'
                for ch in filename
            ) or 'audio'
            encoded_name = urllib.parse.quote(filename, safe='')
            self.close_connection = True
            self.send_response(200)
            self.send_header('Content-Type', mime_type)
            self.send_header('Content-Length', str(size_bytes))
            self.send_header('X-Swararia-Sha256', sha256_hex)
            self.send_header('X-Swararia-Filename', encoded_name)
            self.send_header('Content-Disposition', f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded_name}')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Connection', 'close')
            self.end_headers()
            if stream is not None:
                try:
                    remaining = size_bytes
                    while remaining > 0:
                        chunk = stream.read(min(65536, remaining))
                        if not chunk:
                            self.connection.close()
                            return
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                    self.wfile.flush()
                except OSError:
                    self.close_connection = True
                    return
        finally:
            if stream is not None:
                stream.close()
            self.server.media_slots.release()

    def _serve_media(self, token, track_id, owner):
        media_ref = owner.authorize_media(token, track_id)
        self._serve_authorized_media(media_ref, owner)

    def _serve_sync_file(self, token, file_id, owner):
        media_ref = owner.authorize_sync_file(token, file_id)
        self._serve_authorized_media(media_ref, owner)

    @staticmethod
    def _decode_percent_header(raw_value, error_code):
        if raw_value is None:
            return None
        try:
            decoded = urllib.parse.unquote(str(raw_value), encoding='utf-8', errors='strict').strip()
        except UnicodeDecodeError:
            raise RemoteError(error_code, 400) from None
        if '\x00' in decoded:
            raise RemoteError(error_code, 400)
        return decoded

    def _handle_library_upload(self, owner):
        token = self._token()
        dest_root, upload_handler = owner.authorize_upload(token)

        content_type = self.headers.get('Content-Type', '').split(';')[0].strip().lower()
        if content_type != 'application/octet-stream':
            raise RemoteError('binary_required', 415)

        lengths = self.headers.get_all('Content-Length', [])
        if self.headers.get_all('Transfer-Encoding') or len(lengths) != 1 or not re.fullmatch(r'[0-9]{1,10}', lengths[0]):
            raise RemoteError('invalid_length', 400)
        content_length = int(lengths[0])
        if content_length <= 0:
            raise RemoteError('invalid_length', 400)
        if content_length > owner.MAX_MEDIA_BYTES:
            self.close_connection = True
            raise RemoteError('media_too_large', 413)

        raw_name = self.headers.get('X-Swararia-Filename')
        if not raw_name:
            raise RemoteError('missing_filename', 400)
        filename = self._decode_percent_header(raw_name, 'invalid_filename')
        if not filename:
            raise RemoteError('invalid_filename', 400)

        sha_header = (self.headers.get('X-Swararia-Sha256') or '').strip().lower()
        if not sha_header:
            raise RemoteError('missing_sha256', 400)
        if not re.fullmatch(r'[0-9a-f]{64}', sha_header):
            raise RemoteError('invalid_sha256', 400)

        raw_playlist = self.headers.get('X-Swararia-Playlist')
        playlist_name = self._decode_percent_header(raw_playlist, 'invalid_playlist') if raw_playlist is not None else None
        if playlist_name == '':
            playlist_name = None

        if not self.server.media_slots.acquire(blocking=False):
            raise RemoteError('media_busy', 429)
        try:
            self.server.extend_deadline(self.server.MEDIA_CONNECTION_TTL)
            try:
                self.connection.settimeout(15)
            except (OSError, ValueError):
                pass
            return upload_handler(self.rfile, content_length, dest_root, filename, sha_header, playlist_name)
        finally:
            self.server.media_slots.release()

    def _dispatch(self):
        owner = self.server.owner
        self.server.rate('all')
        fixed = {
            '/v1/pair', '/v1/session', '/v1/capabilities', '/v1/state',
            '/v1/tracks', '/v1/commands', '/v1/sync/folders', '/v1/sync/manifest',
            '/v1/library/upload',
        }
        status_route = re.fullmatch(r'/v1/commands/([A-Za-z0-9_-]{24})', self.path)
        media_route = re.fullmatch(r'/v1/tracks/([A-Za-z0-9_-]{3,32})/media', self.path)
        sync_file_route = re.fullmatch(r'/v1/sync/files/(fil_[0-9a-f]{20})', self.path)
        if self.path not in fixed and not status_route and not media_route and not sync_file_route:
            raise RemoteError('unknown_route', 404)
        if self.headers.get('Origin') or self.headers.get('Sec-Fetch-Site'):
            raise RemoteError('native_clients_only')
        extended = self.headers.get_all('X-Swararia-Capabilities', []) == ['extended-v1']
        if (self.path.startswith('/v1/sync/') or sync_file_route is not None or self.path == '/v1/library/upload') and not extended:
            raise RemoteError('unsupported_capabilities', 400)
        if self.path == '/v1/library/upload':
            if self.command != 'POST':
                raise RemoteError('method_not_allowed', 405)
            return self._handle_library_upload(owner)
        if self.command == 'POST':
            payload = self._body()
            if self.path == '/v1/pair':
                self.server.rate('pair')
                if set(payload) != {'label'}:
                    raise RemoteError('invalid_fields', 400)
                return 202, owner.pair(self._token(), payload['label'])
            if self.path == '/v1/sync/manifest':
                if not {'folder_id'} <= set(payload) or (set(payload) - {'folder_id', 'offset', 'limit'}):
                    raise RemoteError('invalid_fields', 400)
                return 200, owner.sync_manifest(
                    self._token(),
                    payload['folder_id'],
                    offset=payload.get('offset', 0),
                    limit=payload.get('limit', 100),
                )
            if self.path == '/v1/commands':
                action = payload.get('action')
                if action == 'select_track':
                    if set(payload) not in ({'action', 'idempotency_key', 'track_id'},
                                            {'action', 'idempotency_key', 'track_id', 'auto_play'}):
                        raise RemoteError('invalid_fields', 400)
                    return 202, owner.command(
                        self._token(), action, payload['idempotency_key'],
                        track_id=payload['track_id'], auto_play=payload.get('auto_play', False),
                    )
                if action == 'select_group':
                    if set(payload) != {'action', 'idempotency_key', 'group_id'}:
                        raise RemoteError('invalid_fields', 400)
                    return 202, owner.command(
                        self._token(), action, payload['idempotency_key'],
                        group_id=payload['group_id'],
                    )
                if set(payload) != {'action', 'idempotency_key'}:
                    raise RemoteError('invalid_fields', 400)
                return 202, owner.command(self._token(), action, payload['idempotency_key'])
            raise RemoteError('method_not_allowed', 405)
        allowed_read = self.command == 'GET' or (self.command == 'HEAD' and (media_route is not None or sync_file_route is not None))
        if not allowed_read:
            raise RemoteError('method_not_allowed', 405)
        if self.headers.get_all('Transfer-Encoding') or self.headers.get('Content-Length', '0') != '0':
            raise RemoteError('invalid_length', 400)
        token = self._token()
        if media_route:
            self._serve_media(token, media_route[1], owner)
            return None
        if sync_file_route:
            self._serve_sync_file(token, sync_file_route[1], owner)
            return None
        if self.path == '/v1/sync/folders':
            return 200, owner.sync_folders(token)
        if self.path == '/v1/session':
            return 200, owner.session(token)
        if self.path == '/v1/state':
            return 200, owner.state(token)
        if self.path == '/v1/tracks':
            return 200, owner.tracks(token)
        if status_route:
            return 200, owner.command_status(token, status_route[1])
        if self.path == '/v1/capabilities':
            return 200, owner.capabilities_for(token, extended=extended)
        raise RemoteError('method_not_allowed', 405)

    def do_GET(self):
        try:
            res = self._dispatch()
            if res is not None:
                self.reply(res[0], res[1])
        except RemoteError as exc:
            self.reply(exc.status, {'code': exc.code})
        except (OSError, ValueError, TypeError, RecursionError):
            self.close_connection = True
            self.reply(400, {'code': 'invalid_request'})

    do_POST = do_GET
    do_PUT = do_GET
    do_DELETE = do_GET
    do_OPTIONS = do_GET
    do_HEAD = do_GET
