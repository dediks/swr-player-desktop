"""App directories, allowed roots, path normalization/validation and URL allowlist."""
from __future__ import annotations

import os
import re
import shutil
import string
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from swararia.config import REPO_ROOT
from swararia.core import audio_manager


def _determine_app_dirs() -> tuple[Path, Path]:
    """Returns (RESOURCE_DIR, BASE_DIR) supporting normal execution and PyInstaller frozen bundles."""
    here = REPO_ROOT
    if getattr(sys, "frozen", False):
        resource_dir = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent)).resolve()
        exe_dir = Path(sys.executable).resolve().parent
        candidates = [
            exe_dir,
            (Path.home() / "SwarariaAudioLab").resolve(),
            here,
            Path("/tmp/SwarariaAudioLab").resolve(),
        ]
        for cand in candidates:
            try:
                (cand / "library" / "inbox").mkdir(parents=True, exist_ok=True)
                probe_file = cand / "library" / ".write_test"
                probe_file.write_text("ok", encoding="utf-8")
                probe_file.unlink(missing_ok=True)
                return resource_dir, cand
            except OSError:
                continue
        return resource_dir, exe_dir

    try:
        (here / "library" / "inbox").mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return here, here


RESOURCE_DIR, BASE_DIR = _determine_app_dirs()
INBOX_DIR = BASE_DIR / "library" / "inbox"

SKIP_SYSTEM_DIRS = {
    "$Recycle.Bin",
    "$WinREAgent",
    "System Volume Information",
    "Documents and Settings",
    "Recovery",
    "PerfLogs",
    "MSOCache",
    "AppData",
    "Application Data",
    "Local Settings",
    "Windows",
    "Program Files",
    "Program Files (x86)",
    "ProgramData",
    "node_modules",
}

ALLOWED_YOUTUBE_DOMAINS = (
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
)


def get_allowed_roots() -> list[Path]:
    """Returns allowed base directories for local folder browsing and conversion."""
    roots = [BASE_DIR.resolve(), RESOURCE_DIR.resolve(), Path.home().resolve()]
    if os.name == "nt":
        for letter in string.ascii_uppercase:
            drive_root = Path(f"{letter}:\\")
            try:
                if drive_root.exists():
                    roots.append(drive_root.resolve())
            except OSError:
                pass
    else:
        for extra in ("/tmp", "/media", "/mnt", "/run/media", "/Volumes"):
            ep = Path(extra)
            try:
                if ep.exists():
                    roots.append(ep.resolve())
            except OSError:
                pass
    return roots


def normalize_windows_or_wsl_path(raw_path: str) -> str:
    """Normalizes paths across Native Windows, WSL (/mnt/<drive>), and POSIX systems."""
    cleaned = raw_path.strip().strip('"').strip("'")
    if os.name == "nt":
        mnt_match = re.match(r"^/mnt/([a-zA-Z])(?:/(.*))?$", cleaned)
        if mnt_match:
            drive = mnt_match.group(1).upper()
            rest = (mnt_match.group(2) or "").replace("/", "\\")
            return f"{drive}:\\{rest}" if rest else f"{drive}:\\"
        return cleaned

    win_match = re.match(r"^([a-zA-Z]):(?:[\\/](.*))?$", cleaned)
    if win_match:
        drive = win_match.group(1).lower()
        rest = (win_match.group(2) or "").replace("\\", "/").strip("/")
        wsl_drive = Path(f"/mnt/{drive}")
        if wsl_drive.exists():
            return f"/mnt/{drive}/{rest}" if rest else f"/mnt/{drive}"
    return cleaned


def resolve_safe_dir(raw_path: str | None, default_dir: Path = INBOX_DIR, must_exist: bool = True) -> Path:
    """Validates and resolves a directory path within allowed local roots (WSL + Windows /mnt/*)."""
    if not raw_path or not isinstance(raw_path, str) or not raw_path.strip():
        return default_dir.resolve()
    cleaned = normalize_windows_or_wsl_path(raw_path)
    p = Path(cleaned).expanduser()
    if not p.is_absolute():
        p = BASE_DIR / p
    resolved = p.resolve()

    allowed_roots = get_allowed_roots()
    matched_root = next((r for r in allowed_roots if resolved.is_relative_to(r)), None)
    if matched_root is None:
        raise ValueError(f"Folder access denied (outside Home / Windows /mnt / Media / Project): {resolved}")

    rel_parts = resolved.relative_to(matched_root).parts
    if any(part.startswith(".") for part in rel_parts):
        raise ValueError("Access to hidden system folders (dot-folders) is not allowed.")

    if must_exist and not resolved.is_dir():
        raise ValueError(f"Folder not found: {resolved}")
    return resolved


def validate_url(url_str: str) -> bool:
    """Strictly validates input URL for allowed scheme and YouTube domain."""
    if not url_str or not isinstance(url_str, str):
        return False
    try:
        parsed = urlparse(url_str.strip())
        if parsed.scheme not in ("http", "https"):
            return False
        netloc = parsed.netloc.lower()
        return any(netloc == domain or netloc.endswith("." + domain) for domain in ALLOWED_YOUTUBE_DOMAINS)
    except Exception:
        return False


def to_windows_path(linux_path: Path | str) -> str:
    """Converts a Linux/WSL path to a Windows-compatible path (or returns native Windows path)."""
    p_str = str(linux_path).strip()
    if os.name == "nt":
        return str(Path(p_str).resolve())

    wslpath_bin = shutil.which("wslpath")
    if wslpath_bin:
        try:
            res = subprocess.run([wslpath_bin, "-w", p_str], capture_output=True, text=True, timeout=3)
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
        except Exception:
            pass

    # Fallback 1: Drive mapping for /mnt/<letter>/...
    mnt_match = re.match(r"^/mnt/([a-zA-Z])(?:/(.*))?$", p_str)
    if mnt_match:
        drive = mnt_match.group(1).upper()
        rest = (mnt_match.group(2) or "").replace("/", "\\")
        return f"{drive}:\\{rest}" if rest else f"{drive}:\\"

    # Fallback 2: UNC path for WSL2 distro
    distro = os.environ.get("WSL_DISTRO_NAME", "Ubuntu")
    win_rest = p_str.replace("/", "\\")
    return f"\\\\wsl.localhost\\{distro}{win_rest}"


AUDIO_MIME_TYPES: dict[str, str] = {
    ".opus": "audio/ogg",
    ".ogg": "audio/ogg",
    ".webm": "audio/webm",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
}


def resolve_safe_audio_file(
    raw_source_dir: str | None,
    rel_path: str | None,
    default_inbox: Path = INBOX_DIR,
) -> tuple[Path, str]:
    """Strictly validates and resolves a local audio file within source_dir for playback streaming."""
    if not rel_path or not isinstance(rel_path, str) or not rel_path.strip():
        raise ValueError("Jalur file audio tidak boleh kosong.")

    source_dir = resolve_safe_dir(raw_source_dir, default_inbox, must_exist=True)
    clean_rel = rel_path.strip().replace("\\", "/").lstrip("/")
    if not clean_rel or clean_rel in (".", ".."):
        raise ValueError("Jalur file audio tidak valid.")

    target_path = (source_dir / clean_rel).resolve()
    if not target_path.is_relative_to(source_dir) or target_path == source_dir:
        raise ValueError("Akses jalur file ditolak (di luar batas direktori sumber).")

    rel_parts = target_path.relative_to(source_dir).parts
    if any(part in (".", "..") or part.startswith(".") or part.startswith("$") or part in SKIP_SYSTEM_DIRS for part in rel_parts):
        raise ValueError("Tidak diizinkan mengakses file sistem atau tersembunyi.")

    if target_path.is_symlink():
        raise ValueError("Tidak diizinkan memutar symbolic link.")

    if not target_path.is_file():
        raise FileNotFoundError(f"File audio tidak ditemukan: {clean_rel}")

    ext = target_path.suffix.lower()
    if ext not in audio_manager.EXTENSIONS:
        raise ValueError(f"Format file '{ext}' tidak didukung untuk pemutaran audio.")

    mime_type = AUDIO_MIME_TYPES.get(ext, "application/octet-stream")
    return target_path, mime_type
