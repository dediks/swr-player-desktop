"""Phone browser -> PC audio upload & local upload configuration endpoints."""
from __future__ import annotations

import os
import re
import threading
from email.parser import BytesParser
from email.policy import compat32 as COMPACT_EMAIL_POLICY
from email.utils import collapse_rfc2231_value
from pathlib import Path
from urllib.parse import unquote

from swararia.core import transfer
from swararia.server import hub, state


MAX_UPLOAD_REQUEST_BYTES = 320 * 1024 * 1024
_BOUNDARY_RE = re.compile(r'^[!#$%&\'*+\-.^_`|~0-9A-Za-z(),/:=?]+$')
_UPLOAD_SEMAPHORE = threading.BoundedSemaphore(2)


def _extract_boundary(content_type: str) -> bytes | None:
    if not content_type:
        return None
    parts = [p.strip() for p in content_type.split(";")]
    if not parts or parts[0].lower() != "multipart/form-data":
        return None
    for param in parts[1:]:
        if "=" not in param:
            continue
        k, v = param.split("=", 1)
        if k.strip().lower() == "boundary":
            val = v.strip()
            if len(val) >= 2 and val.startswith('"') and val.endswith('"'):
                val = val[1:-1]
            if 1 <= len(val) <= 70 and _BOUNDARY_RE.fullmatch(val):
                return val.encode("ascii")
    return None


def _decode_header_param(raw_val) -> str | None:
    if raw_val is None:
        return None
    if isinstance(raw_val, tuple):
        return collapse_rfc2231_value(raw_val)
    return str(raw_val)


def _parse_content_disposition(raw_header: str) -> tuple[str | None, str | None]:
    msg = BytesParser(policy=COMPACT_EMAIL_POLICY).parsebytes(
        f"Content-Disposition: {raw_header}\r\n\r\n".encode("utf-8", errors="replace")
    )
    field_name = _decode_header_param(msg.get_param("name", header="content-disposition", failobj=None))
    filename = _decode_header_param(msg.get_param("filename", header="content-disposition", failobj=None))
    return field_name, filename


class _BoundedReader:
    """Streams chunks from an underlying socket reader without exceeding Content-Length."""

    def __init__(self, rfile, remaining: int) -> None:
        self.rfile = rfile
        self.remaining = remaining

    def read_chunk(self, size: int = 65536) -> bytes:
        if self.remaining <= 0:
            return b""
        to_read = min(size, self.remaining)
        data = self.rfile.read(to_read)
        if not data:
            self.remaining = 0
            return b""
        self.remaining -= len(data)
        return data


class UploadPostMixin:
    """Handlers for local upload settings and PIN-gated LAN phone uploads."""

    def _handle_post_upload_config(self, payload: dict) -> None:
        if not self.local_request_allowed(write=True):
            self.send_json(403, {"ok": False, "code": "local_origin_required", "error": "Permintaan tulis lokal tidak diizinkan."})
            return

        enabled = payload.get("enabled") if "enabled" in payload else None
        if enabled is not None and type(enabled) is not bool:
            self.send_json(400, {"ok": False, "error": "Parameter 'enabled' harus bertipe boolean."})
            return

        dest_dir_arg = None
        dest_label_arg = None
        if "dest_dir" in payload:
            raw_dest = payload.get("dest_dir")
            if raw_dest is None or (isinstance(raw_dest, str) and not raw_dest.strip()):
                dest_dir_arg = ""
                dest_label_arg = "library/inbox"
            else:
                try:
                    resolved = state.resolve_safe_dir(str(raw_dest), state.INBOX_DIR, must_exist=True)
                    if resolved.is_symlink():
                        raise ValueError("Folder tujuan symlink tidak diizinkan.")
                except Exception as exc:
                    self.send_json(400, {"ok": False, "error": str(exc)})
                    return
                dest_dir_arg = str(resolved)
                inbox_res = state.INBOX_DIR.resolve()
                dest_label_arg = "library/inbox" if resolved == inbox_res else resolved.name

        cfg = hub.remote_hub.set_upload_config(
            enabled=enabled,
            dest_dir=dest_dir_arg,
            dest_label=dest_label_arg,
        )
        self.send_json(200, {"ok": True, **cfg})

    def _validate_upload_csrf_headers(self) -> bool:
        sec_fetch = self.headers.get("Sec-Fetch-Site")
        if sec_fetch is not None and sec_fetch not in ("same-origin", "none"):
            return False
        origin = self.headers.get("Origin")
        host = self.headers.get("Host")
        if origin and host:
            from urllib.parse import urlparse

            parsed_origin = urlparse(origin)
            orig_netloc = (parsed_origin.netloc or "").lower()
            if not orig_netloc or orig_netloc != host.strip().lower():
                return False
        return True

    def _handle_post_remote_upload(self) -> None:
        self.close_connection = True
        if not hub.remote_hub.enabled:
            self.send_json(403, {"ok": False, "code": "DISABLED", "error": "FOH Mobile Remote sedang dinonaktifkan."})
            return
        if not hub.remote_hub.upload_enabled:
            self.send_json(403, {"ok": False, "code": "UPLOAD_DISABLED", "error": "Fitur Upload dari HP belum diaktifkan di PC Master Deck."})
            return
        if not self._validate_upload_csrf_headers():
            self.send_json(403, {"ok": False, "code": "ORIGIN_FORBIDDEN", "error": "Permintaan lintas situs ditolak."})
            return

        pin = self.headers.get("X-FOH-PIN")
        if not hub.remote_hub.verify_pin(pin):
            self.send_json(401, {"ok": False, "code": "INVALID_PIN", "error": "PIN FOH Remote tidak valid."})
            return

        client_ip = str(self.client_address[0])
        raw_tag = (self.headers.get("X-FOH-Client-ID") or "mobile").strip()
        client_tag = re.sub(r"[^a-zA-Z0-9_\-]", "", raw_tag)[:32] or "mobile"
        client_id = f"{client_ip}:{client_tag}"
        if hub.remote_hub.is_client_blocked(client_id):
            self.send_json(403, {"ok": False, "code": "KICKED", "error": "Koneksi HP ini telah diputus oleh operator Master Deck."})
            return
        if hub.remote_hub.get_client_role(client_id) == "view_only":
            self.send_json(403, {"ok": False, "code": "VIEW_ONLY", "error": "HP dalam mode Pantau Saja (View-Only) tidak diizinkan mengunggah."})
            return
        if not hub.remote_hub.check_upload_rate_limit(client_id):
            self.send_json(429, {"ok": False, "code": "RATE_LIMITED", "error": "Terlalu banyak permintaan upload. Coba beberapa detik lagi."})
            return

        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get_all("Transfer-Encoding") or len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,10}", lengths[0]):
            self.send_json(400, {"ok": False, "code": "invalid_length", "error": "Header Content-Length tidak valid."})
            return
        content_length = int(lengths[0])
        if content_length <= 0 or content_length > MAX_UPLOAD_REQUEST_BYTES:
            self.send_json(413, {"ok": False, "code": "body_too_large", "error": "Ukuran total upload melebihi batas request (maks. 320 MiB per batch)."})
            return

        boundary = _extract_boundary(self.headers.get("Content-Type", ""))
        if boundary is None:
            self.send_json(415, {"ok": False, "code": "multipart_required", "error": "Gunakan multipart/form-data untuk unggah file dari HP."})
            return

        if not _UPLOAD_SEMAPHORE.acquire(blocking=False):
            self.send_json(429, {"ok": False, "code": "UPLOAD_BUSY", "error": "Slot upload sedang penuh. Tunggu upload lain selesai."})
            return

        try:
            self._process_multipart_upload(content_length, boundary)
        finally:
            _UPLOAD_SEMAPHORE.release()

    def _process_multipart_upload(self, content_length: int, boundary: bytes) -> None:
        cfg = hub.remote_hub.get_upload_config()
        raw_root = cfg.get("upload_dest_dir") or None
        raw_header_sub = unquote(self.headers.get("X-Swararia-Subfolder") or "").strip()

        try:
            base_root = state.resolve_safe_dir(raw_root, state.INBOX_DIR, must_exist=True)
            header_dest = transfer.resolve_transfer_destination(
                str(base_root),
                raw_header_sub,
                default_inbox=state.INBOX_DIR,
                create=True,
            )
        except transfer.TransferValidationError as exc:
            self.send_json(exc.status, {"ok": False, "code": exc.code, "error": exc.message})
            return
        except Exception as exc:
            self.send_json(400, {"ok": False, "error": str(exc)})
            return

        reader = _BoundedReader(self.rfile, content_length)
        buf = b""
        first_delim = b"--" + boundary
        next_delim = b"\r\n--" + boundary

        def _fill_until(min_bytes: int) -> bool:
            nonlocal buf
            while len(buf) < min_bytes:
                chunk = reader.read_chunk(65536)
                if not chunk:
                    return False
                buf += chunk
            return True

        while True:
            idx = buf.find(first_delim)
            if idx != -1:
                buf = buf[idx + len(first_delim):]
                break
            if len(buf) > 4096:
                buf = buf[-(len(first_delim) + 4):]
            if not _fill_until(len(buf) + 1):
                self.send_json(400, {"ok": False, "code": "invalid_multipart", "error": "Boundary multipart tidak ditemukan."})
                return

        if not _fill_until(2):
            self.send_json(400, {"ok": False, "code": "invalid_multipart", "error": "Format multipart tidak lengkap."})
            return
        if buf[:2] == b"--":
            self.send_json(400, {"ok": False, "code": "no_files", "error": "Tidak ada file yang dikirim."})
            return
        if buf[:2] != b"\r\n":
            self.send_json(400, {"ok": False, "code": "invalid_multipart", "error": "Delimiter multipart tidak valid."})
            return
        buf = buf[2:]

        target_dir = header_dest
        subfolder_locked = False
        sha_index: dict[str, Path] | None = None
        results: list[dict] = []
        warnings: list[str] = []
        saved_paths: list[Path] = []
        saved_count = 0
        duplicate_count = 0
        rejected_count = 0
        wav_flac_count = 0
        file_parts_seen = 0

        while True:
            while b"\r\n\r\n" not in buf:
                if len(buf) > 16384:
                    self.send_json(400, {"ok": False, "code": "headers_too_large", "error": "Header part multipart terlalu besar."})
                    return
                if not _fill_until(len(buf) + 1):
                    self.send_json(400, {"ok": False, "code": "truncated_upload", "error": "Koneksi upload terputus."})
                    return
            hdr_end = buf.index(b"\r\n\r\n")
            raw_hdr = buf[:hdr_end]
            buf = buf[hdr_end + 4:]

            disp_line = ""
            for line in raw_hdr.split(b"\r\n"):
                if line.lower().startswith(b"content-disposition:"):
                    disp_line = line.split(b":", 1)[1].decode("utf-8", errors="replace").strip()
                    break
            field_name, raw_filename = _parse_content_disposition(disp_line)

            is_file_part = raw_filename is not None
            part_path: Path | None = None
            part_stream = None
            clean_name = ""
            ext = ""
            skip_error: tuple[str, str] | None = None
            field_bytes = bytearray()
            part_written = 0

            def _sink_chunk(chunk_out: bytes) -> None:
                nonlocal part_written, part_stream, part_path, skip_error
                if not chunk_out:
                    return
                part_written += len(chunk_out)
                if is_file_part:
                    if part_stream is None:
                        return
                    if part_written > transfer.MAX_TRANSFER_BYTES:
                        part_stream.close()
                        part_stream = None
                        if part_path is not None:
                            part_path.unlink(missing_ok=True)
                            part_path = None
                        skip_error = ("file_too_large", "Ukuran file melebihi batas 150 MiB.")
                    else:
                        part_stream.write(chunk_out)
                elif len(field_bytes) < 1024:
                    field_bytes.extend(chunk_out[: 1024 - len(field_bytes)])

            if is_file_part:
                file_parts_seen += 1
                if file_parts_seen > transfer.MAX_UPLOAD_FILES_PER_REQUEST:
                    self.send_json(
                        400,
                        {"ok": False, "code": "too_many_files", "error": f"Maksimal {transfer.MAX_UPLOAD_FILES_PER_REQUEST} file per pengiriman."},
                    )
                    return
                subfolder_locked = True
                try:
                    clean_name, ext = transfer.sanitize_transfer_filename(raw_filename)
                    part_path = transfer.create_staged_part_file(target_dir)
                    fd = os.open(part_path, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0))
                    part_stream = os.fdopen(fd, "wb")
                except transfer.TransferValidationError as exc:
                    skip_error = (exc.code, exc.message)
                    if exc.code == "wav_flac_excluded":
                        wav_flac_count += 1
                        warnings.append(f"{Path(raw_filename or 'file').name}: {exc.message}")

            try:
                keep_tail = len(next_delim) + 2
                while True:
                    d_idx = buf.find(next_delim)
                    if d_idx != -1:
                        _sink_chunk(buf[:d_idx])
                        buf = buf[d_idx + len(next_delim):]
                        break
                    if len(buf) > keep_tail:
                        flush_len = len(buf) - keep_tail
                        _sink_chunk(buf[:flush_len])
                        buf = buf[flush_len:]
                    if not _fill_until(len(buf) + 1):
                        self.send_json(400, {"ok": False, "code": "truncated_upload", "error": "Stream multipart terputus sebelum akhir."})
                        return

                if part_stream is not None:
                    part_stream.close()
                    part_stream = None

                if is_file_part:
                    display_name = Path(raw_filename or clean_name or "audio").name
                    if skip_error is not None:
                        rejected_count += 1
                        results.append({
                            "name": display_name,
                            "status": "skipped_wav_flac" if skip_error[0] == "wav_flac_excluded" else "rejected",
                            "code": skip_error[0],
                            "message": skip_error[1],
                        })
                    elif part_path is not None:
                        try:
                            transfer.verify_staged_audio_file(part_path, ext)
                            if sha_index is None:
                                sha_index = transfer.build_folder_sha256_index(target_dir)
                            committed = transfer.commit_staged_audio(
                                part_path,
                                target_dir,
                                clean_name,
                                existing_hashes=sha_index,
                                root_for_rel=base_root,
                            )
                            part_path = None
                            if committed["status"] == "skipped_duplicate":
                                duplicate_count += 1
                            else:
                                saved_count += 1
                                saved_paths.append(target_dir / committed["filename"])
                            results.append({"name": display_name, **committed})
                        except transfer.TransferValidationError as exc:
                            rejected_count += 1
                            results.append({
                                "name": display_name,
                                "status": "rejected",
                                "code": exc.code,
                                "message": exc.message,
                            })
                elif field_name == "subfolder" and not subfolder_locked and not raw_header_sub:
                    sub_val = field_bytes.decode("utf-8", errors="replace").strip()
                    try:
                        target_dir = transfer.resolve_transfer_destination(
                            str(base_root),
                            sub_val,
                            default_inbox=state.INBOX_DIR,
                            create=True,
                        )
                        sha_index = None
                    except transfer.TransferValidationError as exc:
                        self.send_json(exc.status, {"ok": False, "code": exc.code, "error": exc.message})
                        return
            finally:
                if part_stream is not None:
                    try:
                        part_stream.close()
                    except OSError:
                        pass
                if part_path is not None:
                    part_path.unlink(missing_ok=True)

            if not _fill_until(2):
                self.send_json(400, {"ok": False, "code": "truncated_upload", "error": "Akhir stream multipart tidak ditemukan."})
                return
            if buf[:2] == b"--":
                break
            if buf[:2] != b"\r\n":
                self.send_json(400, {"ok": False, "code": "invalid_multipart", "error": "Pembatas antar file multipart tidak valid."})
                return
            buf = buf[2:]

        if saved_count > 0:
            state.metadata_cache.invalidate_dir_cache(base_root)
            hub.remote_hub.bump_library_revision()
            if saved_paths:
                threading.Thread(
                    target=state.probe_files_fast,
                    args=(saved_paths,),
                    kwargs={"include_spectral": False},
                    daemon=True,
                ).start()

        if file_parts_seen == 0:
            self.send_json(400, {"ok": False, "code": "no_files", "error": "Pilih minimal satu file audio untuk diunggah."})
            return

        rel_dest = "."
        if target_dir != base_root and target_dir.is_relative_to(base_root):
            rel_dest = target_dir.relative_to(base_root).as_posix()

        status_code = 200 if (saved_count > 0 or duplicate_count > 0) else (415 if wav_flac_count == file_parts_seen else 400)
        resp_body = {
            "ok": status_code == 200,
            "saved_count": saved_count,
            "duplicate_count": duplicate_count,
            "rejected_count": rejected_count,
            "wav_flac_skipped_count": wav_flac_count,
            "subfolder": rel_dest,
            "warnings": warnings,
            "files": results,
        }
        if status_code != 200:
            first_msg = next((r.get("message") for r in results if r.get("message")), None)
            resp_body["error"] = first_msg or (warnings[0] if warnings else "Upload file audio ditolak.")
        self.send_json(status_code, resp_body)
