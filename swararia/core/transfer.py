"""Safe LAN transfer validation, audio container inspection, and SHA-256 deduplication."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import shutil
import stat
from pathlib import Path

from swararia.core import audio_manager
from swararia.core import fs_browser


MAX_TRANSFER_BYTES = 150 * 1024 * 1024
MAX_UPLOAD_FILES_PER_REQUEST = 20
EXCLUDED_PHASE_EXTENSIONS = frozenset({".wav", ".flac"})
ALLOWED_TRANSFER_EXTENSIONS = frozenset({".mp3", ".m4a", ".mp4", ".aac", ".opus", ".ogg", ".webm"})

_DANGEROUS_MAGIC_PREFIXES = (
    b"\x7fELF",
    b"MZ",
    b"#!",
    b"PK\x03\x04",
    b"%PDF-",
    b"<?php",
    b"<?xml",
    b"<html",
    b"<!doctype",
    b"<script",
    b"<svg",
)


class TransferValidationError(ValueError):
    """Structured transfer validation failure mapped cleanly to HTTP error codes."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)


def sanitize_subfolder_parts(raw_subfolder: str | None) -> list[str]:
    """Splits and sanitizes an optional relative playlist subfolder path."""
    if not raw_subfolder or not isinstance(raw_subfolder, str):
        return []
    cleaned = raw_subfolder.strip().replace("\\", "/")
    if not cleaned or cleaned in ("/", "__ROOT__", "__ALL__", "ALL"):
        return []
    if re.match(r"^[a-zA-Z]:", cleaned):
        raise TransferValidationError("invalid_subfolder", "Nama folder tujuan tidak valid.")

    raw_parts = [seg for seg in cleaned.split("/") if seg != ""]
    if len(raw_parts) > 8:
        raise TransferValidationError("invalid_subfolder", "Kedalaman subfolder melebihi batas (maks. 8 level).")

    safe_parts: list[str] = []
    for seg in raw_parts:
        if seg in (".", "..") or seg.startswith(".") or seg.startswith("$") or seg in fs_browser.SKIP_SYSTEM_DIRS:
            raise TransferValidationError("invalid_subfolder", "Nama subfolder sistem atau tersembunyi tidak diizinkan.")
        try:
            safe_seg = fs_browser.sanitize_entry_name(seg)
        except ValueError as exc:
            raise TransferValidationError("invalid_subfolder", str(exc)) from None
        safe_parts.append(safe_seg)
    return safe_parts


def resolve_transfer_destination(
    raw_dest_dir: str | None,
    subfolder_rel: str | None = None,
    default_inbox: Path = fs_browser.INBOX_DIR,
    *,
    create: bool = True,
) -> Path:
    """Resolves a safe destination directory within allowed local roots without symlink traversal."""
    try:
        root = fs_browser.resolve_safe_dir(raw_dest_dir, default_inbox, must_exist=True)
    except ValueError as exc:
        raise TransferValidationError("invalid_destination", str(exc), status=400) from None

    if root.is_symlink():
        raise TransferValidationError("invalid_destination", "Folder tujuan symlink tidak diizinkan.", status=403)

    current = root
    for seg in sanitize_subfolder_parts(subfolder_rel):
        cand = current / seg
        if cand.exists() or cand.is_symlink():
            if cand.is_symlink():
                raise TransferValidationError("invalid_destination", "Subfolder tujuan symlink tidak diizinkan.", status=403)
            if not cand.is_dir():
                raise TransferValidationError("invalid_destination", f"Bukan direktori: {seg}", status=409)
        elif create:
            cand.mkdir(parents=False, exist_ok=True)
        current = cand

    resolved = current.resolve() if (current.exists() or create) else current
    if not resolved.is_relative_to(root):
        raise TransferValidationError("invalid_destination", "Tujuan di luar batas direktori yang diizinkan.", status=403)
    return resolved


def sanitize_transfer_filename(raw_name: str | None) -> tuple[str, str]:
    """Normalizes an incoming audio filename and enforces phase extension allowlists."""
    if not raw_name or not isinstance(raw_name, str):
        raise TransferValidationError("invalid_filename", "Nama file tidak boleh kosong.", status=400)
    base = re.split(r"[/\\]", raw_name.strip())[-1].strip()
    try:
        cleaned = fs_browser.sanitize_entry_name(base)
    except ValueError as exc:
        raise TransferValidationError("invalid_filename", str(exc), status=400) from None

    stem, ext = os.path.splitext(cleaned)
    ext_low = ext.lower()
    if ext_low in EXCLUDED_PHASE_EXTENSIONS:
        raise TransferValidationError(
            "wav_flac_excluded",
            f"Format {ext_low.upper()} sementara tidak didukung pada fase ini — gunakan .mp3/.m4a/.aac/.opus/.ogg/.webm (maks. 150 MiB).",
            status=415,
        )
    if ext_low not in ALLOWED_TRANSFER_EXTENSIONS:
        raise TransferValidationError(
            "unsupported_format",
            f"Format file '{ext_low or '(tanpa ekstensi)'}' tidak diizinkan.",
            status=415,
        )

    safe_stem = stem.strip()[:180].rstrip(". ") or "audio"
    return f"{safe_stem}{ext_low}", ext_low


def _is_valid_mpeg_audio_sync(header: bytes) -> bool:
    for offset in range(max(0, min(len(header) - 2, 16))):
        b0, b1 = header[offset], header[offset + 1]
        if b0 == 0xFF and (b1 & 0xE0) == 0xE0:
            version_bits = (b1 >> 3) & 0x03
            layer_bits = (b1 >> 1) & 0x03
            if version_bits != 0x01 and layer_bits != 0x00:
                return True
    return False


def validate_audio_magic(header: bytes, ext: str) -> bool:
    """Validates leading file signature against allowed compressed audio containers."""
    if not header or len(header) < 4:
        return False
    low_head = header[:64].lstrip().lower()
    for danger in _DANGEROUS_MAGIC_PREFIXES:
        if header.startswith(danger) or low_head.startswith(danger.lower()):
            return False

    ext_low = ext.lower()
    if ext_low == ".mp3":
        return header.startswith(b"ID3") or _is_valid_mpeg_audio_sync(header)
    if ext_low == ".aac":
        return header.startswith((b"ADIF", b"ID3")) or (header[0] == 0xFF and (header[1] & 0xF0) == 0xF0)
    if ext_low in (".m4a", ".mp4"):
        return len(header) >= 8 and header[4:8] == b"ftyp"
    if ext_low in (".ogg", ".opus"):
        return header.startswith(b"OggS") or header.startswith(b"\x1a\x45\xdf\xa3")
    if ext_low == ".webm":
        return header.startswith(b"\x1a\x45\xdf\xa3")
    return False


def verify_staged_audio_file(part_path: Path, ext: str, *, use_ffprobe: bool = True) -> dict | None:
    """Verifies a staged temporary upload/sync file via symlink check, size, magic bytes, and ffprobe."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(part_path, flags)
    except OSError as exc:
        raise TransferValidationError("staged_file_error", f"File sementara tidak dapat dibuka: {exc}", status=400) from None

    try:
        with os.fdopen(fd, "rb") as stream:
            st = os.fstat(stream.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_size <= 0:
                raise TransferValidationError("empty_file", "File audio kosong atau bukan file reguler.", status=400)
            if st.st_size > MAX_TRANSFER_BYTES:
                raise TransferValidationError("file_too_large", "Ukuran file melebihi batas 150 MiB.", status=413)
            header = stream.read(64)
    except TransferValidationError:
        raise
    except OSError as exc:
        raise TransferValidationError("staged_file_error", f"Gagal membaca file: {exc}", status=400) from None

    if not validate_audio_magic(header, ext):
        raise TransferValidationError(
            "invalid_audio_content",
            f"Isi file tidak cocok dengan header container audio {ext.upper()}.",
            status=415,
        )

    # TODO(security): Cloud/local antivirus and Office CDR do not apply to this offline local-first audio tool;
    # mitigated via strict container magic-byte inspection, ffprobe audio decode validation, and non-executable 0644 storage.
    if use_ffprobe and shutil.which("ffprobe"):
        try:
            meta = audio_manager.metadata(part_path)
            return meta
        except Exception as exc:
            raise TransferValidationError(
                "invalid_audio_stream",
                f"Verifikasi stream audio ffprobe gagal: {exc}",
                status=415,
            ) from None
    return None


def create_staged_part_file(target_dir: Path) -> Path:
    """Creates a hidden non-executable exclusive temp file inside target_dir for atomic commit."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    for _ in range(8):
        candidate = target_dir / f".swararia_xfer_{secrets.token_hex(8)}.part"
        try:
            fd = os.open(candidate, flags, 0o600)
            os.close(fd)
            return candidate
        except FileExistsError:
            continue
    raise TransferValidationError("staging_failed", "Gagal menyiapkan file sementara di folder tujuan.", status=500)


def build_folder_sha256_index(target_dir: Path) -> dict[str, Path]:
    """Maps lowercase SHA-256 hex digests of existing audio files in target_dir to their paths."""
    index: dict[str, Path] = {}
    if not target_dir.is_dir():
        return index
    try:
        entries = sorted(target_dir.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return index

    for p in entries:
        if p.name.startswith(".") or p.is_symlink():
            continue
        try:
            if not p.is_file() or p.suffix.lower() not in ALLOWED_TRANSFER_EXTENSIONS:
                continue
            digest = audio_manager.sha256_file(p)
            index.setdefault(digest, p)
        except OSError:
            continue
    return index


def _next_available_path(target_dir: Path, filename: str) -> tuple[Path, bool]:
    candidate = target_dir / filename
    if not candidate.exists() and not candidate.is_symlink():
        return candidate, False
    stem, ext = os.path.splitext(filename)
    for idx in range(2, 10000):
        cand = target_dir / f"{stem} ({idx}){ext}"
        if not cand.exists() and not cand.is_symlink():
            return cand, True
    raise TransferValidationError("name_exhausted", f"Terlalu banyak duplikat nama untuk '{filename}'.", status=409)


def commit_staged_audio(
    part_path: Path,
    target_dir: Path,
    clean_filename: str,
    *,
    expected_sha256: str | None = None,
    existing_hashes: dict[str, Path] | None = None,
    root_for_rel: Path | None = None,
) -> dict:
    """Verifies SHA-256, skips byte-identical duplicates, auto-renames collisions, and commits atomically."""
    actual_sha = audio_manager.sha256_file(part_path)
    if expected_sha256 is not None:
        exp_clean = str(expected_sha256).strip().lower()
        if not exp_clean or not hmac.compare_digest(actual_sha, exp_clean):
            part_path.unlink(missing_ok=True)
            raise TransferValidationError("sha256_mismatch", "Checksum SHA-256 file tidak cocok.", status=422)

    hash_map = existing_hashes if existing_hashes is not None else build_folder_sha256_index(target_dir)
    existing_match = hash_map.get(actual_sha)
    if existing_match is not None and existing_match.exists():
        part_path.unlink(missing_ok=True)
        rel_base = root_for_rel or target_dir
        rel_str = str(existing_match.relative_to(rel_base)) if existing_match.is_relative_to(rel_base) else existing_match.name
        return {
            "status": "skipped_duplicate",
            "filename": existing_match.name,
            "rel_path": rel_str.replace("\\", "/"),
            "sha256": actual_sha,
            "renamed": False,
        }

    dest_path, was_renamed = _next_available_path(target_dir, clean_filename)
    try:
        os.chmod(part_path, 0o644)
    except OSError:
        pass
    os.replace(part_path, dest_path)
    try:
        os.chmod(dest_path, 0o644)
    except OSError:
        pass
    size_bytes = dest_path.stat().st_size
    hash_map.setdefault(actual_sha, dest_path)
    rel_base = root_for_rel or target_dir
    rel_str = str(dest_path.relative_to(rel_base)) if dest_path.is_relative_to(rel_base) else dest_path.name
    return {
        "status": "saved",
        "filename": dest_path.name,
        "rel_path": rel_str.replace("\\", "/"),
        "sha256": actual_sha,
        "size_bytes": size_bytes,
        "renamed": was_renamed,
    }


def stage_and_commit_stream(
    rfile,
    content_length: int,
    base_root: str | Path | None,
    raw_filename: str,
    expected_sha256: str,
    *,
    subfolder: str | None = None,
    default_inbox: Path = fs_browser.INBOX_DIR,
) -> dict:
    """Streams an octet-stream upload directly to .part, verifies SHA-256 + magic + ffprobe, and commits."""
    if type(content_length) is not int or content_length <= 0:
        raise TransferValidationError("empty_file", "File audio kosong.", status=400)
    if content_length > MAX_TRANSFER_BYTES:
        raise TransferValidationError("file_too_large", "Ukuran file melebihi batas 150 MiB.", status=413)

    exp_sha = str(expected_sha256 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", exp_sha):
        raise TransferValidationError("invalid_sha256", "Header SHA-256 tidak valid.", status=400)

    clean_name, ext = sanitize_transfer_filename(raw_filename)
    base_arg = str(base_root) if base_root is not None else None
    root_resolved = fs_browser.resolve_safe_dir(base_arg, default_inbox, must_exist=True)
    target_dir = resolve_transfer_destination(str(root_resolved), subfolder, default_inbox=default_inbox, create=True)

    part_path = create_staged_part_file(target_dir)
    part_stream = None
    try:
        fd = os.open(part_path, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0))
        part_stream = os.fdopen(fd, "wb")
        hasher = hashlib.sha256()
        remaining = content_length
        while remaining > 0:
            chunk = rfile.read(min(65536, remaining))
            if not chunk:
                raise TransferValidationError("incomplete_upload", "Stream upload terputus sebelum selesai.", status=400)
            part_stream.write(chunk)
            hasher.update(chunk)
            remaining -= len(chunk)
        part_stream.flush()
        part_stream.close()
        part_stream = None

        actual_sha = hasher.hexdigest()
        if not hmac.compare_digest(actual_sha, exp_sha):
            raise TransferValidationError("sha256_mismatch", "Checksum SHA-256 file tidak cocok.", status=422)

        verify_staged_audio_file(part_path, ext)
        committed = commit_staged_audio(
            part_path,
            target_dir,
            clean_name,
            expected_sha256=exp_sha,
            root_for_rel=root_resolved,
        )
        saved_abs = str(target_dir / committed["filename"]) if committed["status"] == "saved" else ""
        part_path = None
        rel_sub = "."
        if target_dir != root_resolved and target_dir.is_relative_to(root_resolved):
            rel_sub = target_dir.relative_to(root_resolved).as_posix()
        return {
            **committed,
            "deduplicated": committed["status"] == "skipped_duplicate",
            "subfolder": rel_sub,
            "_saved_path": saved_abs,
        }
    finally:
        if part_stream is not None:
            try:
                part_stream.close()
            except OSError:
                pass
        if part_path is not None:
            part_path.unlink(missing_ok=True)


def make_sync_folder_id(rel_folder: str) -> str:
    if not rel_folder:
        return "root"
    return "fld_" + hashlib.sha256(rel_folder.encode("utf-8")).hexdigest()[:16]


def make_sync_file_id(rel_path: str) -> str:
    return "fil_" + hashlib.sha256(rel_path.encode("utf-8")).hexdigest()[:20]


def scan_sync_folders(sync_root: Path) -> tuple[list[dict], dict[str, list[dict]], dict]:
    """Scans sync_root into sanitized folder summaries, bounded file descriptors, and phase warnings."""
    resolved_root = fs_browser.resolve_safe_dir(str(sync_root), fs_browser.INBOX_DIR, must_exist=True)
    folders_meta: dict[str, dict] = {}
    folder_files: dict[str, list[dict]] = {}
    total_wav_flac = 0
    total_oversized = 0

    for dirpath, dirnames, filenames in os.walk(resolved_root, followlinks=False, onerror=lambda _: None):
        cur_dir = Path(dirpath)
        dirnames[:] = sorted(
            d for d in dirnames
            if not d.startswith(".")
            and not d.startswith("$")
            and d not in fs_browser.SKIP_SYSTEM_DIRS
            and not (cur_dir / d).is_symlink()
        )
        rel_dir = cur_dir.relative_to(resolved_root)
        rel_folder = "" if rel_dir == Path(".") else rel_dir.as_posix()
        fid = make_sync_folder_id(rel_folder)
        folder_name = f"Root ({resolved_root.name})" if not rel_folder else rel_folder

        meta = folders_meta.setdefault(fid, {
            "folder_id": fid,
            "rel_folder": rel_folder,
            "name": folder_name,
            "track_count": 0,
            "total_bytes": 0,
            "excluded_wav_flac_count": 0,
            "oversized_count": 0,
        })
        entries = folder_files.setdefault(fid, [])

        for fname in sorted(filenames):
            if fname.startswith("."):
                continue
            fpath = cur_dir / fname
            if fpath.is_symlink():
                continue
            try:
                st = os.lstat(fpath)
                if not stat.S_ISREG(st.st_mode):
                    continue
            except OSError:
                continue

            ext = fpath.suffix.lower()
            if ext in EXCLUDED_PHASE_EXTENSIONS:
                meta["excluded_wav_flac_count"] += 1
                total_wav_flac += 1
                continue
            if ext not in ALLOWED_TRANSFER_EXTENSIONS:
                continue
            if st.st_size <= 0:
                continue
            if st.st_size > MAX_TRANSFER_BYTES:
                meta["oversized_count"] += 1
                total_oversized += 1
                continue

            rel_path = f"{rel_folder}/{fname}" if rel_folder else fname
            file_id = make_sync_file_id(rel_path)
            meta["track_count"] += 1
            meta["total_bytes"] += int(st.st_size)
            entries.append({
                "file_id": file_id,
                "folder_id": fid,
                "rel_folder": rel_folder,
                "rel_path": rel_path,
                "name": fname,
                "ext": ext,
                "size_bytes": int(st.st_size),
                "mtime_ns": int(st.st_mtime_ns),
                "_abs_path": fpath,
            })

    folder_list = [
        m for m in folders_meta.values()
        if m["track_count"] > 0 or m["excluded_wav_flac_count"] > 0 or m["oversized_count"] > 0
    ]
    folder_list.sort(key=lambda item: (0 if item["folder_id"] == "root" else 1, item["rel_folder"].lower()))
    summary = {
        "excluded_wav_flac_count": total_wav_flac,
        "oversized_count": total_oversized,
    }
    return folder_list, folder_files, summary
