"""Session-only native remote v1 + opt-in local catalog media transfer.

Owned by the local HTTP server. Secrets exist only in RAM; TLS key material is
created in a private temporary directory and removed immediately after loading.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import threading
import time


class RemoteError(Exception):
    def __init__(self, code, status=403):
        self.code, self.status = code, status
        super().__init__(code)


MAX_CATALOG_BYTES = 98_304


def encode_remote_json(data):
    """One serialization policy for catalog budgeting and the actual TLS reply."""
    return json.dumps(data, allow_nan=False).encode('utf-8')


def _digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


class SecureRemote:
    INVITE_TTL = 90
    PENDING_TTL = 120
    SESSION_TTL = 8 * 3600
    MAX_DEVICES = 32
    COMMAND_TTL = 4
    RECORD_TTL = 300
    MAX_RECORDS = 256
    MAX_TRACKS = 250
    MAX_GROUPS = 64
    TRACK_ID_RE = re.compile(r'^[A-Za-z0-9_-]{3,32}$')
    GROUP_ID_RE = re.compile(r'^[A-Za-z0-9_-]{2,32}$')
    ALLOWED_ACTIONS = ('play', 'pause', 'stop', 'fade_out', 'prev', 'next', 'select_track', 'select_group')
    MAX_MEDIA_BYTES = 150 * 1024 * 1024
    MAX_SHA_CACHE = 512
    DOWNLOADABLE_EXTENSIONS = frozenset({'.mp3', '.m4a', '.mp4', '.aac', '.opus', '.ogg', '.webm', '.flac', '.wav'})
    SYNC_EXTENSIONS = frozenset({'.mp3', '.m4a', '.mp4', '.aac', '.opus', '.ogg', '.webm'})
    EXCLUDED_SYNC_EXTENSIONS = frozenset({'.wav', '.flac'})
    SYNC_FOLDER_ID_RE = re.compile(r'^(root|fld_[0-9a-f]{16})$')
    SYNC_FILE_ID_RE = re.compile(r'^fil_[0-9a-f]{20}$')
    MEDIA_MIME_FALLBACKS = {
        '.mp3': 'audio/mpeg',
        '.m4a': 'audio/mp4',
        '.mp4': 'audio/mp4',
        '.aac': 'audio/aac',
        '.opus': 'audio/ogg',
        '.ogg': 'audio/ogg',
        '.webm': 'audio/webm',
        '.flac': 'audio/flac',
        '.wav': 'audio/wav',
    }

    def __init__(self, hub, clock=time.time, media_resolver=None, default_inbox=None, sync_scanner=None, upload_handler=None):
        self.hub, self.clock = hub, clock
        self.media_resolver = media_resolver
        self.default_inbox = default_inbox
        self.sync_scanner = sync_scanner
        self.upload_handler = upload_handler
        self.lock = threading.RLock()
        self.enabled = False
        self.media_transfer_enabled = False
        self.sync_enabled = False
        self.sync_source_dir = None
        self.sync_source_label = 'library/inbox'
        self.upload_enabled = False
        self.upload_dest_dir = None
        self.upload_dest_label = 'library/inbox'
        self._sync_cache = {}
        self.session_id = None
        self.invite = None
        self.devices = {}
        self.records = {}
        self.catalog_tracks = []
        self.track_ids = set()
        self.catalog_media = {}
        self._sha_cache = {}
        self.catalog_groups = []
        self.group_ids = set()
        self.active_group_id = None
        self.total_tracks = 0
        self.current_track_id = None
        self.playlist_label = None
        self.catalog_version = 0
        self.tracks_synced = False
        self.master_instance = None
        self.master_seen = 0
        self.listener = None
        self.listener_thread = None
        self.certificate = None
        self.connection_info = {}
        self.bind_info = {}
        self.lifecycle_lock = threading.Lock()

    def start(self, host, port=8766, advertise_ip=None, advertise_port=None):
        """Start the local mTLS/HTTPS remote listener, optionally advertising a bridged public endpoint."""
        from swararia.remote.secure_remote_tls import NativeTLSServer, tls_context, validate_bind
        validate_bind(host, port)
        public_ip = host if advertise_ip is None else advertise_ip
        if advertise_ip is not None or advertise_port is not None:
            public_check_port = port if advertise_port is None else advertise_port
            validate_bind(public_ip, public_check_port)
        with self.lifecycle_lock:
            if self.listener:
                raise RemoteError('already_enabled', 409)
            try:
                extra_ips = (public_ip,) if public_ip != host else ()
                context, certificate, fingerprint = tls_context(host, extra_ips=extra_ips)
                listener = NativeTLSServer((host, port), self, context)
            except (OSError, ValueError):
                raise RemoteError('listener_unavailable', 409) from None
            self.activate()
            self.certificate = certificate
            actual_bind_port = listener.server_address[1]
            actual_public_port = actual_bind_port if advertise_port is None else advertise_port
            self.bind_info = {'ip': host, 'port': actual_bind_port}
            self.connection_info = {'url': f'https://{public_ip}:{actual_public_port}',
                                    'fingerprint_sha256': fingerprint}
            self.listener = listener
            self.listener_thread = threading.Thread(target=listener.serve_forever,
                                                    kwargs={'poll_interval': 0.05}, daemon=True)
            self.listener_thread.start()

    def stop(self):
        with self.lifecycle_lock:
            self.deactivate()
            if self.listener:
                self.listener.shutdown()
                self.listener.server_close()
                self.listener_thread.join(timeout=4)
                self.listener = self.listener_thread = None
            self.connection_info = {}
            self.bind_info = {}
            self.certificate = None

    def activate(self):
        with self.lock:
            self.enabled = True
            self.session_id = secrets.token_urlsafe(24)
            self.devices.clear()
            self.records.clear()
            self._clear_catalog()
            self._discard_queue()
            self.invite = None

    def deactivate(self):
        with self.lock:
            self.enabled = False
            self.media_transfer_enabled = False
            self.sync_enabled = False
            self.upload_enabled = False
            self._sync_cache.clear()
            self.invite = None
            self.devices.clear()
            self.records.clear()
            self._clear_catalog()
            self._discard_queue()
            self.session_id = None

    def _clear_catalog(self):
        self.catalog_tracks = []
        self.track_ids = set()
        self.catalog_media = {}
        self._sha_cache.clear()
        self.catalog_groups = []
        self.group_ids = set()
        self.active_group_id = None
        self.total_tracks = 0
        self.current_track_id = None
        self.playlist_label = None
        self.tracks_synced = False

    @staticmethod
    def _sanitize_title(raw, max_len=160):
        if not isinstance(raw, str) or '://' in raw:
            return None
        title = re.split(r'[/\\]', raw)[-1]
        title = ''.join(c for c in title if c.isprintable()).strip()
        # Android (including released v1 clients) bounds strings in UTF-16 units.
        # Dropping an incomplete final pair keeps whole Unicode scalar values.
        return title.encode('utf-16-le')[:max_len * 2].decode('utf-16-le', errors='ignore') or None

    def _update_catalog(self, catalog):
        if not isinstance(catalog, dict):
            return
        raw_items = catalog.get('items')
        if not isinstance(raw_items, list):
            return
        sanitized_groups = []
        seen_group_ids = set()
        raw_groups = catalog.get('groups')
        if isinstance(raw_groups, list):
            for entry in raw_groups[:self.MAX_GROUPS]:
                if not isinstance(entry, dict):
                    continue
                gid = entry.get('id')
                if not isinstance(gid, str) or not self.GROUP_ID_RE.fullmatch(gid) or gid in seen_group_ids:
                    continue
                name = self._sanitize_title(entry.get('name'), max_len=60)
                if not name:
                    continue
                raw_count = entry.get('count', 0)
                count = raw_count if (type(raw_count) is int and raw_count >= 0) else 0
                seen_group_ids.add(gid)
                sanitized_groups.append({'id': gid, 'name': name, 'count': min(count, 100000)})
        sanitized = []
        seen_ids = set()
        media_refs = {}
        raw_source_dir = catalog.get('source_dir')
        source_dir = raw_source_dir.strip() if isinstance(raw_source_dir, str) and raw_source_dir.strip() else None
        for item in raw_items[:self.MAX_TRACKS]:
            if not isinstance(item, dict):
                continue
            tid = item.get('id')
            if not isinstance(tid, str) or not self.TRACK_ID_RE.fullmatch(tid) or tid in seen_ids:
                continue
            title = self._sanitize_title(item.get('title'), max_len=140)
            if not title:
                continue
            seen_ids.add(tid)
            entry = {'id': tid, 'title': title}
            item_gid = item.get('group_id')
            if isinstance(item_gid, str) and item_gid in seen_group_ids:
                entry['group_id'] = item_gid
            sanitized.append(entry)
            rel_path = item.get('rel_path')
            if isinstance(rel_path, str) and rel_path.strip():
                media_refs[tid] = (source_dir, rel_path.strip())
        self.catalog_groups = sanitized_groups
        self.group_ids = seen_group_ids
        raw_active_group = catalog.get('active_group_id')
        self.active_group_id = raw_active_group if (isinstance(raw_active_group, str) and raw_active_group in seen_group_ids) else None
        raw_total = catalog.get('total_tracks')
        self.total_tracks = min(raw_total, 100000) if (type(raw_total) is int and raw_total >= len(sanitized)) else len(sanitized)
        self.catalog_tracks = sanitized
        self.track_ids = seen_ids
        self.catalog_media = media_refs
        self.playlist_label = self._sanitize_title(catalog.get('playlist'), max_len=60)
        if self.current_track_id not in self.track_ids:
            self.current_track_id = None
        self.catalog_version += 1
        self.tracks_synced = True

    def _enabled(self):
        if not self.enabled:
            raise RemoteError('disabled')

    def open_invite(self):
        with self.lock:
            self._enabled()
            self.invite = (secrets.token_urlsafe(32), self.clock() + self.INVITE_TTL)
            return self.invite[0]

    def close_invite(self):
        with self.lock:
            self.invite = None

    def pair(self, invite, label):
        with self.lock:
            self._enabled()
            if (not isinstance(invite, str) or not self.invite or
                    self.clock() >= self.invite[1] or
                    not secrets.compare_digest(invite, self.invite[0])):
                raise RemoteError('invalid_invite', 401)
            if not isinstance(label, str) or not 1 <= len(label) <= 80:
                raise RemoteError('invalid_label', 400)
            if len(self.devices) >= self.MAX_DEVICES:
                raise RemoteError('device_limit', 429)
            self.invite = None
            token = secrets.token_urlsafe(32)
            device = {'device_id': secrets.token_urlsafe(18), 'label': label,
                      'status': 'pending', 'role': None,
                      'expires_at': self.clock() + self.PENDING_TTL,
                      'token_hash': _digest(token)}
            self.devices[device['device_id']] = device
            return {'device_id': device['device_id'], 'token': token, 'status': 'pending'}

    def _device(self, token, active=False, control=False):
        self._enabled()
        if not isinstance(token, str) or not 20 <= len(token) <= 128:
            raise RemoteError('unauthorized', 401)
        hashed = _digest(token)
        device = next((d for d in self.devices.values()
                       if secrets.compare_digest(d['token_hash'], hashed)), None)
        if device is None:
            raise RemoteError('unauthorized', 401)
        if self.clock() >= device['expires_at']:
            device['status'] = 'expired'
        if device['status'] in ('revoked', 'denied', 'expired'):
            raise RemoteError(device['status'])
        if active and device['status'] != 'active':
            raise RemoteError('pending')
        if control and device['role'] != 'control':
            raise RemoteError('view_only')
        return device

    def session(self, token):
        with self.lock:
            d = self._device(token)
            return {k: d[k] for k in ('device_id', 'status', 'role', 'expires_at')}

    def approve(self, device_id, role):
        with self.lock:
            self._enabled()
            if role not in ('control', 'view_only'):
                raise RemoteError('invalid_role', 400)
            d = self.devices.get(device_id)
            if not d or d['status'] not in ('pending', 'active') or self.clock() >= d['expires_at']:
                raise RemoteError('unknown_device', 404)
            if role == 'view_only':
                self._cancel_device_commands(device_id)
            d.update(status='active', role=role, expires_at=self.clock() + self.SESSION_TTL)

    def revoke(self, device_id, deny=False):
        with self.lock:
            d = self.devices.get(device_id)
            if not d:
                raise RemoteError('unknown_device', 404)
            d['status'] = 'denied' if deny else 'revoked'
            self._cancel_device_commands(device_id)

    def _cancel_device_commands(self, device_id):
        for record in self.records.values():
            if record['device_id'] == device_id:
                if record['status'] == 'accepted':
                    record['status'] = 'rejected'
                elif record['status'] == 'pending':
                    record['status'] = 'unknown'
        with self.hub.lock:
            self.hub.command_queue = [c for c in self.hub.command_queue
                                      if not (c.get('native_id') and c.get('client_id') == device_id)]

    def _discard_queue(self):
        with self.hub.lock:
            self.hub.command_queue = [c for c in self.hub.command_queue if not c.get('native_id')]

    def _prune_records(self):
        now = self.clock()
        for cid, r in list(self.records.items()):
            if now >= r['expires_at'] and r['status'] in ('accepted', 'pending'):
                r['status'] = 'expired' if r['status'] == 'accepted' else 'unknown'
            if now >= r['created_at'] + self.RECORD_TTL:
                del self.records[cid]

    @staticmethod
    def _result(r):
        return {k: r[k] for k in ('command_id', 'action', 'status', 'expires_at')}

    def command(self, token, action, key, track_id=None, auto_play=False, group_id=None):
        with self.lock:
            d = self._device(token, active=True, control=True)
            if action not in self.ALLOWED_ACTIONS or not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', key):
                raise RemoteError('invalid_command', 400)
            if action == 'select_track':
                if not isinstance(track_id, str) or not self.TRACK_ID_RE.fullmatch(track_id) or type(auto_play) is not bool or group_id is not None:
                    raise RemoteError('invalid_command', 400)
                cmd_params = {'track_id': track_id, 'auto_play': auto_play}
            elif action == 'select_group':
                if not isinstance(group_id, str) or not self.GROUP_ID_RE.fullmatch(group_id) or track_id is not None or auto_play:
                    raise RemoteError('invalid_command', 400)
                cmd_params = {'group_id': group_id}
            else:
                if track_id is not None or auto_play or group_id is not None:
                    raise RemoteError('invalid_command', 400)
                cmd_params = {}
            self._prune_records()
            for r in self.records.values():
                if r['device_id'] == d['device_id'] and r['key'] == key:
                    if (r['action'] != action or r.get('track_id') != track_id or
                            bool(r.get('auto_play', False)) != bool(auto_play) or
                            r.get('group_id') != group_id):
                        raise RemoteError('idempotency_conflict', 409)
                    return self._result(r)
            if len(self.records) >= self.MAX_RECORDS:
                raise RemoteError('command_limit', 429)
            online = self.master_instance is not None and self.clock() - self.master_seen < 2.0
            if not online:
                raise RemoteError('master_unavailable', 409)
            if action == 'select_track' and track_id not in self.track_ids:
                raise RemoteError('unknown_track', 409)
            if action == 'select_group' and group_id not in self.group_ids:
                raise RemoteError('unknown_group', 409)
            cid = secrets.token_urlsafe(18)
            now = self.clock()
            r = {'command_id': cid, 'device_id': d['device_id'], 'key': key,
                 'action': action, 'track_id': track_id, 'auto_play': bool(auto_play),
                 'group_id': group_id, 'status': 'accepted', 'created_at': now, 'expires_at': now + self.COMMAND_TTL}
            self.records[cid] = r
            # Reuse the existing hub queue; mark within its lock before any poll.
            with self.hub.lock:
                cmd = {'id': self.hub.next_cmd_id, 'native_id': cid, 'action': action,
                       'params': cmd_params, 'client_id': d['device_id'], 'created_at': now,
                       'expires_at': r['expires_at']}
                self.hub.next_cmd_id += 1
                self.hub.command_queue.append(cmd)
            return self._result(r)

    def command_status(self, token, cid):
        with self.lock:
            d = self._device(token, active=True)
            self._prune_records()
            r = self.records.get(cid)
            if not r or r['device_id'] != d['device_id']:
                return {'command_id': cid, 'status': 'unknown'}
            return self._result(r)

    def tracks(self, token):
        with self.lock:
            self._device(token, active=True)
            online = self.master_instance is not None and self.clock() - self.master_seen < 2.0
            current = self.current_track_id if (online and self.current_track_id in self.track_ids) else None
            payload = {
                'protocol': 'v1',
                'master_online': online,
                'catalog_version': self.catalog_version,
                'playlist': self.playlist_label if online else None,
                'active_group_id': self.active_group_id if online else None,
                'total_tracks': self.total_tracks if online else 0,
                'groups': list(self.catalog_groups) if online else [],
                'current_track_id': current,
                'tracks': list(self.catalog_tracks) if online else [],
            }
            # Fixed bounded metadata and at most 64 bounded groups fit on their
            # own. Select the largest deterministic track prefix fitting the
            # *actual* reply encoding, without changing total_tracks or IDs used
            # for command validation. The current ID may be outside that prefix.
            if len(encode_remote_json(payload)) > MAX_CATALOG_BYTES:
                tracks = payload['tracks']
                low, high = 0, len(tracks)
                while low < high:
                    mid = (low + high + 1) // 2
                    payload['tracks'] = tracks[:mid]
                    if len(encode_remote_json(payload)) <= MAX_CATALOG_BYTES:
                        low = mid
                    else:
                        high = mid - 1
                payload['tracks'] = tracks[:low]
            return payload

    def state(self, token):
        with self.lock:
            d = self._device(token, active=True)
            with self.hub.lock:
                state = self.hub.master_state
                # Only bounded display text; never forward track paths, URLs or QC.
                snapshot = {k: state.get(k) for k in ('is_playing', 'transport_state', 'current_time', 'duration')}
                track = state.get('track')
                title = self._sanitize_title(track.get('name') if isinstance(track, dict) else None, max_len=160)
                snapshot['track_title'] = title
                online = self.master_instance is not None and self.clock() - self.master_seen < 2.0
            return {'protocol': 'v1', 'role': d['role'], 'master_online': online, 'state': snapshot}

    def master_poll(self, state, acknowledgements, instance, catalog=None):
        with self.lock:
            if not isinstance(instance, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', instance) or not isinstance(state, dict) or not state:
                raise RemoteError('invalid_master_state', 400)
            # A new page (or a poll gap) is not permission to replay old work.
            if self.master_instance != instance or self.clock() - self.master_seen >= 2.0:
                for d in self.devices.values():
                    self._cancel_device_commands(d['device_id'])
                self._clear_catalog()
            self.master_instance = instance
            self.master_seen = self.clock()
            if catalog is not None:
                self._update_catalog(catalog)
            if 'native_track_id' in state:
                tid = state.get('native_track_id')
                self.current_track_id = tid if (isinstance(tid, str) and tid in self.track_ids) else None
            queue, *_ = self.hub.update_master_state(state)
            return self.master_exchange(queue, acknowledgements)

    def claim(self, cid, instance):
        """Last local authorization check, immediately before the JS consumer acts."""
        with self.lock:
            self._prune_records()
            r = self.records.get(cid) if isinstance(cid, str) else None
            d = self.devices.get(r['device_id']) if r else None
            allowed = bool(self.enabled and r and r['status'] == 'pending' and d and
                           d['status'] == 'active' and d['role'] == 'control' and
                           self.clock() < d['expires_at'] and instance == self.master_instance and
                           self.clock() - self.master_seen < 2.0)
            return {'authorized': allowed}

    def master_exchange(self, queue, acknowledgements):
        with self.lock:
            self._prune_records()
            for ack in acknowledgements[:64] if isinstance(acknowledgements, list) else []:
                if not isinstance(ack, dict):
                    continue
                r = self.records.get(ack.get('command_id'))
                if not r or r['status'] != 'pending':
                    continue
                observed = ack.get('observed')
                if ack.get('status') == 'rejected':
                    r['status'] = 'rejected'
                elif ack.get('status') == 'applied' and isinstance(observed, dict):
                    if r['action'] == 'play':
                        matches = observed.get('is_playing') is True
                    elif r['action'] == 'stop':
                        matches = (observed.get('is_playing') is False and
                                   observed.get('transport_state') == 'STOPPED' and
                                   observed.get('current_time') == 0)
                    elif r['action'] == 'fade_out':
                        matches = (observed.get('transport_state') == 'FADING' and
                                   observed.get('is_playing') is True)
                    elif r['action'] == 'select_track':
                        matches = (observed.get('track_id') == r.get('track_id') and
                                   (observed.get('is_playing') is True if r.get('auto_play')
                                    else observed.get('is_playing') is False))
                        if matches and isinstance(r.get('track_id'), str):
                            self.current_track_id = r['track_id']
                    elif r['action'] == 'select_group':
                        matches = observed.get('active_group_id') == r.get('group_id')
                        if matches and isinstance(r.get('group_id'), str):
                            self.active_group_id = r['group_id']
                    elif r['action'] in ('prev', 'next'):
                        tid = observed.get('track_id')
                        matches = (isinstance(tid, str) and bool(self.TRACK_ID_RE.fullmatch(tid)) and
                                   isinstance(observed.get('is_playing'), bool))
                        if matches:
                            self.current_track_id = tid
                    else:
                        matches = observed.get('is_playing') is False
                    if matches:
                        r['status'] = 'applied'
            deliver = []
            for cmd in queue:
                if not cmd.get('native_id'):
                    deliver.append(cmd)
                    continue
                r = self.records.get(cmd['native_id'])
                if not r or r['status'] != 'accepted':
                    continue
                d = self.devices.get(r['device_id'])
                if (not self.enabled or not d or d['status'] != 'active' or
                        d['role'] != 'control' or self.clock() >= d['expires_at']):
                    r['status'] = 'rejected'
                    continue
                r['status'] = 'pending'
                deliver.append(cmd)
            return deliver

    def set_media_transfer(self, enabled):
        if type(enabled) is not bool:
            raise RemoteError('invalid_media_flag', 400)
        with self.lock:
            self.media_transfer_enabled = enabled
            if not enabled and not self.sync_enabled:
                self._sha_cache.clear()

    def set_sync_enabled(self, enabled, source_dir=None, source_label=None):
        if type(enabled) is not bool:
            raise RemoteError('invalid_sync_flag', 400)
        with self.lock:
            self.sync_enabled = enabled
            if source_dir is not None:
                clean = str(source_dir).strip()
                self.sync_source_dir = clean or None
            if source_label is not None:
                self.sync_source_label = str(source_label).strip() or 'library/inbox'
            self._sync_cache.clear()
            if not enabled and not self.media_transfer_enabled:
                self._sha_cache.clear()

    def set_upload_enabled(self, enabled, dest_dir=None, dest_label=None):
        if type(enabled) is not bool:
            raise RemoteError('invalid_upload_flag', 400)
        with self.lock:
            self.upload_enabled = enabled
            if dest_dir is not None:
                clean = str(dest_dir).strip()
                self.upload_dest_dir = clean or None
            if dest_label is not None:
                self.upload_dest_label = str(dest_label).strip() or 'library/inbox'

    def authorize_upload(self, token):
        with self.lock:
            self._device(token, active=True, control=True)
            if not self.upload_enabled or self.upload_handler is None:
                raise RemoteError('upload_disabled', 403)
            dest_root = self.upload_dest_dir or (str(self.default_inbox) if self.default_inbox is not None else None)
            if not dest_root:
                raise RemoteError('upload_disabled', 403)
            return dest_root, self.upload_handler

    def capabilities_for(self, token, extended=False):
        with self.lock:
            device = self._device(token, active=True)
            actions = list(self.ALLOWED_ACTIONS) if extended else ['play', 'pause', 'stop']
            can_download = bool(
                extended
                and self.media_transfer_enabled
                and self.media_resolver is not None
                and device['role'] == 'control'
            )
            can_sync = bool(
                extended
                and self.sync_enabled
                and self.sync_scanner is not None
                and self.media_resolver is not None
                and device['role'] == 'control'
            )
            can_upload = bool(
                extended
                and self.upload_enabled
                and self.upload_handler is not None
                and device['role'] == 'control'
            )
            return {
                'protocol': 'v1',
                'commands': actions,
                'media': can_download,
                'sync': can_sync,
                'upload': can_upload,
                'media_max_bytes': self.MAX_MEDIA_BYTES,
                'command_ttl_seconds': self.COMMAND_TTL,
                'idempotency_ttl_seconds': self.RECORD_TTL,
            }

    def _refresh_sync_index(self, expected_session_id, sync_root):
        folder_list, folder_files, summary = self.sync_scanner(sync_root)
        files_by_id = {}
        root_str = str(sync_root)
        for fid, entries in folder_files.items():
            for entry in entries:
                files_by_id[entry['file_id']] = {
                    **entry,
                    'source_dir': root_str,
                }
        folder_map = {f['folder_id']: f for f in folder_list}
        snapshot = {
            'session_id': expected_session_id,
            'root_str': root_str,
            'folders': folder_list,
            'folder_map': folder_map,
            'folder_files': folder_files,
            'files_by_id': files_by_id,
            'summary': summary,
            'cached_at': self.clock(),
        }
        with self.lock:
            if self.enabled and self.session_id == expected_session_id and self.sync_enabled:
                self._sync_cache = snapshot
        return snapshot

    def _get_sync_snapshot(self, force_refresh=False):
        with self.lock:
            if not self.sync_enabled or self.sync_scanner is None or self.media_resolver is None:
                raise RemoteError('sync_disabled', 403)
            sync_root = self.sync_source_dir or self.default_inbox
            if sync_root is None:
                raise RemoteError('sync_disabled', 403)
            session_id = self.session_id
            cached = self._sync_cache
            if (
                not force_refresh
                and cached
                and cached.get('session_id') == session_id
                and cached.get('root_str') == str(sync_root)
                and (self.clock() - float(cached.get('cached_at') or 0.0)) < 2.0
            ):
                return cached
        try:
            return self._refresh_sync_index(session_id, sync_root)
        except (ValueError, OSError):
            raise RemoteError('sync_unavailable', 409) from None

    @staticmethod
    def _sync_warnings_for(wav_flac_count, oversized_count):
        warnings = []
        if wav_flac_count > 0:
            warnings.append(
                f"{wav_flac_count} file .wav/.flac dilewati karena WAV/FLAC belum didukung pada fase sinkronisasi ini."
            )
        if oversized_count > 0:
            warnings.append(
                f"{oversized_count} file melebihi batas 150 MiB per file sehingga tidak disertakan."
            )
        return warnings

    def sync_folders(self, token):
        with self.lock:
            self._device(token, active=True, control=True)
        snap = self._get_sync_snapshot(force_refresh=True)
        summary = snap.get('summary') or {}
        wav_count = int(summary.get('excluded_wav_flac_count') or 0)
        over_count = int(summary.get('oversized_count') or 0)
        folders = [
            {
                'folder_id': f['folder_id'],
                'rel_folder': f['rel_folder'],
                'name': f['name'],
                'track_count': f['track_count'],
                'total_bytes': f['total_bytes'],
                'excluded_wav_flac_count': f['excluded_wav_flac_count'],
                'oversized_count': f['oversized_count'],
            }
            for f in snap.get('folders', [])
        ]
        return {
            'protocol': 'v1',
            'folders': folders,
            'excluded_wav_flac_count': wav_count,
            'oversized_count': over_count,
            'warnings': self._sync_warnings_for(wav_count, over_count),
        }

    def _compute_entry_sha256(self, expected_session_id, entry):
        path_obj = entry.get('_abs_path')
        if path_obj is None:
            raise RemoteError('media_unavailable', 404)
        cache_key = str(path_obj)
        size_bytes = int(entry['size_bytes'])
        mtime_ns = int(entry['mtime_ns'])
        with self.lock:
            if not self.enabled or self.session_id != expected_session_id or not self.sync_enabled:
                raise RemoteError('sync_disabled', 403)
            cached = self._sha_cache.get(cache_key)
            if cached and cached[0] == size_bytes and cached[1] == mtime_ns:
                return cached[2]

        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
        try:
            fd = os.open(path_obj, flags)
        except OSError:
            raise RemoteError('media_unavailable', 404) from None
        hasher = hashlib.sha256()
        try:
            with os.fdopen(fd, 'rb') as stream:
                st = os.fstat(stream.fileno())
                if not stat.S_ISREG(st.st_mode) or st.st_size <= 0 or st.st_size > self.MAX_MEDIA_BYTES:
                    raise RemoteError('media_unavailable', 404)
                while True:
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    hasher.update(chunk)
        except RemoteError:
            raise
        except OSError:
            raise RemoteError('media_unavailable', 404) from None
        digest = hasher.hexdigest()
        with self.lock:
            if self.enabled and self.session_id == expected_session_id and self.sync_enabled:
                if len(self._sha_cache) >= self.MAX_SHA_CACHE:
                    self._sha_cache.clear()
                self._sha_cache[cache_key] = (size_bytes, mtime_ns, digest)
        return digest

    def sync_manifest(self, token, folder_id, offset=0, limit=100):
        if not isinstance(folder_id, str) or not self.SYNC_FOLDER_ID_RE.fullmatch(folder_id):
            raise RemoteError('invalid_folder', 400)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise RemoteError('invalid_pagination', 400)
        with self.lock:
            self._device(token, active=True, control=True)
            expected_session_id = self.session_id
        snap = self._get_sync_snapshot(force_refresh=(offset == 0))
        folder_meta = (snap.get('folder_map') or {}).get(folder_id)
        if folder_meta is None:
            raise RemoteError('unknown_folder', 404)
        all_entries = (snap.get('folder_files') or {}).get(folder_id, [])
        total = len(all_entries)
        page_entries = all_entries[offset:offset + limit]
        items = []
        for entry in page_entries:
            sha256_hex = self._compute_entry_sha256(expected_session_id, entry)
            items.append({
                'file_id': entry['file_id'],
                'name': entry['name'],
                'rel_path': entry['rel_path'],
                'ext': entry['ext'],
                'size_bytes': entry['size_bytes'],
                'sha256': sha256_hex,
            })
        next_offset = offset + len(page_entries) if (offset + len(page_entries)) < total else None
        wav_count = int(folder_meta.get('excluded_wav_flac_count') or 0)
        over_count = int(folder_meta.get('oversized_count') or 0)
        return {
            'protocol': 'v1',
            'folder_id': folder_id,
            'rel_folder': folder_meta['rel_folder'],
            'folder_name': folder_meta['name'],
            'total': total,
            'offset': offset,
            'next_offset': next_offset,
            'excluded_wav_flac_count': wav_count,
            'oversized_count': over_count,
            'warnings': self._sync_warnings_for(wav_count, over_count),
            'files': items,
        }

    def authorize_sync_file(self, token, file_id):
        if not isinstance(file_id, str) or not self.SYNC_FILE_ID_RE.fullmatch(file_id):
            raise RemoteError('unknown_route', 404)
        with self.lock:
            self._device(token, active=True, control=True)
            if not self.sync_enabled or self.sync_scanner is None or self.media_resolver is None:
                raise RemoteError('sync_disabled', 403)
            entry = (self._sync_cache.get('files_by_id') or {}).get(file_id)
            session_id = self.session_id
        if entry is None:
            snap = self._get_sync_snapshot(force_refresh=True)
            entry = (snap.get('files_by_id') or {}).get(file_id)
        if not entry:
            raise RemoteError('media_unavailable', 404)
        return entry['source_dir'], entry['rel_path'], session_id, 'sync'

    def authorize_media(self, token, track_id):
        if not isinstance(track_id, str) or not self.TRACK_ID_RE.fullmatch(track_id):
            raise RemoteError('unknown_route', 404)
        with self.lock:
            self._device(token, active=True, control=True)
            if not self.media_transfer_enabled or self.media_resolver is None:
                raise RemoteError('media_disabled', 403)
            if self.master_instance is None or self.clock() - self.master_seen >= 2.0:
                raise RemoteError('master_unavailable', 409)
            if track_id not in self.track_ids:
                raise RemoteError('unknown_track', 404)
            media_ref = self.catalog_media.get(track_id)
            if not media_ref:
                raise RemoteError('media_unavailable', 404)
            return media_ref[0], media_ref[1], self.session_id

    @staticmethod
    def _sanitize_download_filename(name):
        cleaned = ''.join(ch if ch.isprintable() and ch not in '/\\:"<>|?*' else '_' for ch in str(name)).strip()
        return cleaned or 'track.audio'

    def open_media_file(self, media_ref, need_stream=True):
        if len(media_ref) == 4:
            source_dir, rel_path, expected_session_id, mode = media_ref
        else:
            source_dir, rel_path, expected_session_id = media_ref
            mode = 'media'
        if self.media_resolver is None:
            raise RemoteError('sync_disabled' if mode == 'sync' else 'media_disabled', 403)
        try:
            target_path, mime_type = self.media_resolver(source_dir, rel_path, self.default_inbox)
        except FileNotFoundError:
            raise RemoteError('media_unavailable', 404) from None
        except (ValueError, OSError):
            raise RemoteError('media_forbidden', 403) from None

        ext = target_path.suffix.lower()
        if mode == 'sync':
            if ext in self.EXCLUDED_SYNC_EXTENSIONS:
                raise RemoteError('wav_flac_excluded', 415)
            if ext not in self.SYNC_EXTENSIONS:
                raise RemoteError('unsupported_media_format', 415)
        elif ext not in self.DOWNLOADABLE_EXTENSIONS:
            raise RemoteError('unsupported_media_format', 415)
        if not isinstance(mime_type, str) or not mime_type.lower().startswith('audio/'):
            mime_type = self.MEDIA_MIME_FALLBACKS[ext]

        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
        try:
            fd = os.open(target_path, flags)
        except FileNotFoundError:
            raise RemoteError('media_unavailable', 404) from None
        except OSError:
            raise RemoteError('media_forbidden', 403) from None

        stream = None
        try:
            stream = os.fdopen(fd, 'rb')
        except OSError:
            os.close(fd)
            raise RemoteError('media_unavailable', 404) from None

        try:
            st = os.fstat(stream.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_size <= 0:
                raise RemoteError('media_unavailable', 404)
            if st.st_size > self.MAX_MEDIA_BYTES:
                raise RemoteError('media_too_large', 413)

            cache_key = str(target_path)
            sha256_hex = None
            with self.lock:
                feature_ok = self.sync_enabled if mode == 'sync' else self.media_transfer_enabled
                if not self.enabled or self.session_id != expected_session_id or not feature_ok:
                    raise RemoteError('sync_disabled' if mode == 'sync' else 'media_disabled', 403)
                cached = self._sha_cache.get(cache_key)
                if cached and cached[0] == st.st_size and cached[1] == st.st_mtime_ns:
                    sha256_hex = cached[2]

            if sha256_hex is None:
                hasher = hashlib.sha256()
                while True:
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    hasher.update(chunk)
                sha256_hex = hasher.hexdigest()
                with self.lock:
                    feature_ok = self.sync_enabled if mode == 'sync' else self.media_transfer_enabled
                    if self.enabled and self.session_id == expected_session_id and feature_ok:
                        if len(self._sha_cache) >= self.MAX_SHA_CACHE:
                            self._sha_cache.clear()
                        self._sha_cache[cache_key] = (st.st_size, st.st_mtime_ns, sha256_hex)
                if need_stream:
                    stream.seek(0)

            safe_name = self._sanitize_download_filename(target_path.name)
            if not need_stream:
                stream.close()
                stream = None
            return stream, mime_type, st.st_size, sha256_hex, safe_name
        except Exception:
            if stream is not None:
                stream.close()
            raise

    def admin_status(self):
        with self.lock:
            if self.invite and self.clock() >= self.invite[1]:
                self.invite = None
            for d in self.devices.values():
                if d['status'] in ('pending', 'active') and self.clock() >= d['expires_at']:
                    d['status'] = 'expired'
                    self._cancel_device_commands(d['device_id'])
            status = {'enabled': self.enabled, 'protocol': 'v1', **self.connection_info,
                      'media_transfer_enabled': bool(self.media_transfer_enabled),
                      'sync_enabled': bool(self.sync_enabled),
                      'sync_source_dir': self.sync_source_dir or '',
                      'sync_source_label': self.sync_source_label,
                      'upload_enabled': bool(self.upload_enabled),
                      'upload_dest_dir': self.upload_dest_dir or '',
                      'upload_dest_label': self.upload_dest_label,
                      'invite': self.invite[0] if self.invite else None,
                      'invite_expires_at': self.invite[1] if self.invite else None,
                      'devices': [{k: d[k] for k in ('device_id', 'label', 'status', 'role', 'expires_at')}
                                  for d in self.devices.values()]}
            if self.enabled and self.bind_info:
                bind_url = f"https://{self.bind_info['ip']}:{self.bind_info['port']}"
                if bind_url != self.connection_info.get('url'):
                    status['bind_url'] = bind_url
            return status
