"""Local PC <-> PC folder sync endpoints wrapping PeerSyncManager and TaskQueueManager."""
from __future__ import annotations

from swararia.core import transfer
from swararia.remote.secure_remote import RemoteError
from swararia.server import state


_SYNC_ERROR_MESSAGES = {
    'invalid_pairing_code': 'Format kode pairing tidak valid (harus dimulai dengan swararia1|...).',
    'invalid_pairing_host': 'Host pairing harus berupa IPv4 LAN privat/lokal dengan port 1-65535.',
    'invalid_pairing_fingerprint': 'Fingerprint SHA-256 pada kode pairing tidak valid.',
    'invalid_invite': 'Kode undangan tidak valid, sudah dipakai, atau kedaluwarsa di PC sumber.',
    'fingerprint_mismatch': 'Fingerprint sertifikat TLS PC sumber tidak cocok — koneksi dibatalkan demi keamanan.',
    'peer_unreachable': 'PC sumber tidak dapat dijangkau. Pastikan Android/Native Remote aktif dan firewall mengizinkan.',
    'pending': 'Menunggu persetujuan operator di PC sumber (pilih Izinkan Kontrol).',
    'view_only': 'Perangkat ini disetujui sebagai Pantau Saja. Ubah ke Kontrol Penuh di PC sumber untuk sinkronisasi.',
    'revoked': 'Akses perangkat ini telah dicabut oleh PC sumber.',
    'denied': 'Permintaan pairing ditolak oleh PC sumber.',
    'expired': 'Sesi pairing telah kedaluwarsa. Buat kode pairing baru di PC sumber.',
    'sync_disabled': 'Sinkronisasi folder PC <-> PC belum diaktifkan pada PC sumber.',
    'unknown_peer': 'Sesi peer sinkronisasi tidak ditemukan.',
    'unknown_folder': 'Folder yang dipilih tidak ditemukan pada PC sumber.',
}


class SyncRoutesMixin:
    """Handlers for local PC <-> PC folder pairing, diff preview, and background pull."""

    def _sync_error_reply(self, exc: RemoteError) -> None:
        self.send_json(exc.status, {
            'ok': False,
            'code': exc.code,
            'error': _SYNC_ERROR_MESSAGES.get(exc.code, f'Kesalahan sinkronisasi ({exc.code}).'),
        })

    def _handle_get_sync_status(self) -> None:
        if not self.local_request_allowed(write=False):
            self.send_json(403, {'ok': False, 'code': 'local_origin_required'})
            return
        self.send_json(200, {'ok': True, 'peers': self.server.peer_sync.list_peers()})

    def _handle_post_sync_pair(self, payload: dict) -> None:
        if not self.local_request_allowed(write=True):
            self.send_json(403, {'ok': False, 'code': 'local_origin_required'})
            return
        pairing_code = str(payload.get('pairing_code') or '')
        label = str(payload.get('label') or 'Swararia PC Sync')
        try:
            peer = self.server.peer_sync.pair_with_host(pairing_code, client_label=label)
            self.send_json(200, {'ok': True, 'peer': peer})
        except RemoteError as exc:
            self._sync_error_reply(exc)

    def _handle_post_sync_refresh(self, payload: dict) -> None:
        if not self.local_request_allowed(write=True):
            self.send_json(403, {'ok': False, 'code': 'local_origin_required'})
            return
        peer_id = str(payload.get('peer_id') or '')
        try:
            peer = self.server.peer_sync.refresh_status(peer_id)
            folders_data = None
            if peer.get('status') == 'active' and peer.get('sync_capable'):
                folders_data = self.server.peer_sync.fetch_folders(peer_id)
            self.send_json(200, {'ok': True, 'peer': peer, 'folders_info': folders_data})
        except RemoteError as exc:
            self._sync_error_reply(exc)

    def _handle_post_sync_disconnect(self, payload: dict) -> None:
        if not self.local_request_allowed(write=True):
            self.send_json(403, {'ok': False, 'code': 'local_origin_required'})
            return
        peer_id = payload.get('peer_id')
        self.server.peer_sync.disconnect(str(peer_id) if peer_id else None)
        self.send_json(200, {'ok': True, 'peers': self.server.peer_sync.list_peers()})

    def _collect_sync_plan(self, peer_id: str, folder_ids: list[str], raw_dest: str | None) -> tuple[dict, object]:
        dest_root = state.resolve_safe_dir(raw_dest, state.INBOX_DIR, must_exist=True)
        if dest_root.is_symlink():
            raise ValueError('Folder tujuan symlink tidak diizinkan.')

        peer = self.server.peer_sync.refresh_status(peer_id)
        if peer.get('status') != 'active':
            raise RemoteError(str(peer.get('status') or 'pending'), 403)
        if peer.get('role') != 'control':
            raise RemoteError('view_only', 403)
        if not peer.get('sync_capable'):
            raise RemoteError('sync_disabled', 403)

        clean_ids = [str(fid).strip() for fid in folder_ids if isinstance(fid, str) and fid.strip()]
        if not clean_ids or len(clean_ids) > 64:
            raise ValueError('Pilih antara 1 hingga 64 folder playlist untuk disinkronkan.')

        # Deduplicate while preserving order
        seen_fids: list[str] = list(dict.fromkeys(clean_ids))
        local_hash_cache: dict[str, dict] = {}
        new_count = 0
        skip_dup_count = 0
        collision_rename_count = 0
        total_wav_flac = 0
        total_oversized = 0
        transfer_bytes = 0
        items_for_queue: list[dict] = []
        preview_items: list[dict] = []

        for fid in seen_fids:
            manifest = self.server.peer_sync.fetch_folder_manifest(peer_id, fid)
            rel_folder = str(manifest.get('rel_folder') or '').strip()
            total_wav_flac += int(manifest.get('excluded_wav_flac_count') or 0)
            total_oversized += int(manifest.get('oversized_count') or 0)

            target_dir = transfer.resolve_transfer_destination(
                str(dest_root),
                rel_folder,
                default_inbox=state.INBOX_DIR,
                create=False,
            )
            dir_key = str(target_dir)
            if dir_key not in local_hash_cache:
                local_hash_cache[dir_key] = (
                    transfer.build_folder_sha256_index(target_dir) if target_dir.is_dir() else {}
                )
            local_hashes = local_hash_cache[dir_key]

            for entry in manifest.get('files') or []:
                fname = str(entry.get('name') or '')
                sha = str(entry.get('sha256') or '').lower()
                size_bytes = int(entry.get('size_bytes') or 0)
                if sha and sha in local_hashes:
                    action = 'skip_duplicate'
                    skip_dup_count += 1
                elif target_dir.is_dir() and (target_dir / fname).exists():
                    action = 'rename_collision'
                    collision_rename_count += 1
                    transfer_bytes += size_bytes
                    items_for_queue.append({**entry, 'rel_folder': rel_folder})
                else:
                    action = 'new'
                    new_count += 1
                    transfer_bytes += size_bytes
                    items_for_queue.append({**entry, 'rel_folder': rel_folder})
                if len(preview_items) < 500:
                    preview_items.append({
                        'name': fname,
                        'rel_path': entry.get('rel_path', fname),
                        'size_bytes': size_bytes,
                        'action': action,
                    })

        warnings = []
        if total_wav_flac > 0:
            warnings.append(
                f"{total_wav_flac} file .wav/.flac di PC sumber dilewati karena WAV/FLAC belum didukung pada fase sinkronisasi ini."
            )
        if total_oversized > 0:
            warnings.append(
                f"{total_oversized} file di PC sumber melebihi batas 150 MiB/file dan dilewati."
            )

        summary = {
            'peer': peer,
            'dest_dir': str(dest_root),
            'new_count': new_count,
            'rename_collision_count': collision_rename_count,
            'duplicate_skip_count': skip_dup_count,
            'wav_flac_skipped_count': total_wav_flac,
            'oversized_skipped_count': total_oversized,
            'transfer_count': len(items_for_queue),
            'transfer_bytes': transfer_bytes,
            'warnings': warnings,
            'items_preview': preview_items,
        }
        return summary, (dest_root, items_for_queue)

    def _handle_post_sync_preview(self, payload: dict) -> None:
        if not self.local_request_allowed(write=True):
            self.send_json(403, {'ok': False, 'code': 'local_origin_required'})
            return
        peer_id = str(payload.get('peer_id') or '')
        folder_ids = payload.get('folder_ids') or []
        raw_dest = payload.get('dest_dir')
        try:
            summary, _ = self._collect_sync_plan(peer_id, folder_ids, raw_dest)
            self.send_json(200, {'ok': True, **summary})
        except RemoteError as exc:
            self._sync_error_reply(exc)
        except Exception as exc:
            self.send_json(400, {'ok': False, 'error': str(exc)})

    def _handle_post_sync_start(self, payload: dict) -> None:
        if not self.local_request_allowed(write=True):
            self.send_json(403, {'ok': False, 'code': 'local_origin_required'})
            return
        peer_id = str(payload.get('peer_id') or '')
        folder_ids = payload.get('folder_ids') or []
        raw_dest = payload.get('dest_dir')
        try:
            summary, (dest_root, items_for_queue) = self._collect_sync_plan(peer_id, folder_ids, raw_dest)
            peer = summary['peer']

            def _pull(file_id: str, part_path, *, expected_sha256: str | None = None) -> dict:
                return self.server.peer_sync.download_sync_file_to_part(
                    peer_id,
                    file_id,
                    part_path,
                    max_bytes=transfer.MAX_TRANSFER_BYTES,
                    expected_sha256=expected_sha256,
                )

            status_code, resp = state.task_manager.enqueue_sync(
                peer_label=peer.get('host') or peer_id,
                dest_dir=dest_root,
                items=items_for_queue,
                wav_flac_skipped=summary['wav_flac_skipped_count'],
                oversized_skipped=summary['oversized_skipped_count'],
                pull_file_fn=_pull,
            )
            self.send_json(status_code, {'ok': status_code == 200, **resp, 'warnings': summary['warnings']})
        except RemoteError as exc:
            self._sync_error_reply(exc)
        except Exception as exc:
            self.send_json(400, {'ok': False, 'error': str(exc)})
