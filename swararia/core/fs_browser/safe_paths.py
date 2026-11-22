"""App directories, allowed roots, path normalization/validation and URL allowlist."""
from __future__ import annotations

import getpass
import json
import os
import re
import shutil
import string
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlparse

from swararia.config import REPO_ROOT
from swararia.core import audio_manager

APP_FOLDER_NAME = "SwarariaAudioLab"
LINUX_APP_DATA_NAME = "swararia-audio-lab"
ENV_LIBRARY_DIR = "SWARARIA_LIBRARY_DIR"
ENV_DATA_DIR = "SWARARIA_DATA_DIR"
ENV_PORTABLE = "SWARARIA_PORTABLE"

_settings_lock = threading.Lock()

_WSL_SKIP_USERS = frozenset({
    "public",
    "default",
    "default user",
    "all users",
    "wsiaccount",
    "wdagutilityaccount",
    "administrator",
    "guest",
})


def _is_wsl() -> bool:
    """Detects WSL without spawning any subprocesses."""
    if os.name == "nt":
        return False
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8", errors="ignore").lower()
    except OSError:
        return False


def _dir_is_writable(path: Path) -> bool:
    """Creates path if needed and probes write access without leaving stray files."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        fd, probe_name = tempfile.mkstemp(prefix=".swararia_write_test_", dir=str(path))
        os.close(fd)
        Path(probe_name).unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _is_portable_mode() -> bool:
    """Returns True when portable mode is explicitly requested via env var or portable.flag."""
    if os.environ.get(ENV_PORTABLE, "").strip() == "1":
        return True
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        try:
            return (exe_dir / "portable.flag").is_file()
        except OSError:
            return False
    return False


def detect_wsl_windows_home(mnt_root: Path = Path("/mnt")) -> Path | None:
    """Detects the active accessible Windows user profile directory when running under WSL."""
    if os.name == "nt":
        return None

    env_profile = os.environ.get("USERPROFILE", "").strip()
    if env_profile:
        norm_profile = normalize_windows_or_wsl_path(env_profile)
        cand = Path(norm_profile)
        try:
            if cand.is_dir() and os.access(cand, os.R_OK | os.X_OK):
                return cand
        except OSError:
            pass

    win_users_dir = mnt_root / "c" / "Users"
    if not win_users_dir.is_dir():
        return None

    try:
        linux_user = getpass.getuser().lower()
    except Exception:
        linux_user = ""

    candidates: list[tuple[int, float, Path]] = []
    try:
        entries = sorted(win_users_dir.iterdir())
    except OSError:
        return None

    for udir in entries:
        name_low = udir.name.lower()
        if (
            udir.name.startswith(".")
            or name_low in _WSL_SKIP_USERS
            or name_low.startswith("defaultuser")
            or name_low.startswith("temp")
        ):
            continue
        try:
            if udir.is_symlink() or not udir.is_dir():
                continue
            # Require actual read+execute access so inaccessible sibling accounts
            # (e.g. d--x--x--x on multi-user Windows hosts) are never selected.
            if not os.access(udir, os.R_OK | os.X_OK):
                continue
            # Verify at least one standard user folder is accessible/writable.
            writable_score = 0
            for probe_sub in ("Music", "Downloads", "Desktop", "Documents"):
                sp = udir / probe_sub
                try:
                    if sp.is_dir() and os.access(sp, os.R_OK | os.W_OK | os.X_OK):
                        writable_score = 2
                        break
                except OSError:
                    pass
            if writable_score == 0:
                if os.access(udir, os.W_OK):
                    writable_score = 1
                else:
                    continue

            latest_mtime = udir.stat().st_mtime
            for probe_sub in ("Downloads", "Desktop", "Music", "NTUSER.DAT"):
                sp = udir / probe_sub
                try:
                    if sp.exists():
                        latest_mtime = max(latest_mtime, sp.stat().st_mtime)
                except OSError:
                    pass

            user_match = 1 if (linux_user and name_low == linux_user) else 0
            candidates.append((writable_score * 2 + user_match, latest_mtime, udir))
        except OSError:
            continue

    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return candidates[0][2]


def _resolve_linux_music_dir(home_dir: Path) -> Path:
    """Reads XDG_MUSIC_DIR from ~/.config/user-dirs.dirs on native Linux if safely inside home."""
    user_dirs_file = home_dir / ".config" / "user-dirs.dirs"
    try:
        if user_dirs_file.is_file():
            for line in user_dirs_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                stripped = line.strip()
                if not stripped.startswith("XDG_MUSIC_DIR="):
                    continue
                raw_val = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                if raw_val.startswith("$HOME/"):
                    cand = (home_dir / raw_val[len("$HOME/"):]).resolve()
                elif raw_val == "$HOME":
                    cand = home_dir.resolve()
                else:
                    cand = Path(raw_val).expanduser().resolve()
                if cand.is_relative_to(home_dir.resolve()):
                    rel_parts = cand.relative_to(home_dir.resolve()).parts
                    if not any(part.startswith(".") for part in rel_parts):
                        return cand
    except OSError:
        pass
    return home_dir / "Music"


def _resolve_os_music_dir() -> Path:
    """Returns the user-visible OS Music directory (Windows profile Music on WSL)."""
    home_dir = Path.home()
    if os.name == "nt" or sys.platform == "darwin":
        return home_dir / "Music"
    if _is_wsl():
        win_home = detect_wsl_windows_home()
        if win_home is not None:
            win_music = win_home / "Music"
            if _dir_is_writable(win_music):
                return win_music
    return _resolve_linux_music_dir(home_dir)


def _resolve_app_data_dir(base_dir: Path, portable: bool) -> Path:
    """Returns the persistent private application data directory (cookies, archive, history)."""
    env_data = os.environ.get(ENV_DATA_DIR, "").strip()
    if env_data:
        cand = Path(normalize_windows_or_wsl_path(env_data)).expanduser()
        if _dir_is_writable(cand):
            return cand.resolve()

    if portable:
        cand = base_dir / "library"
        if _dir_is_writable(cand):
            return cand.resolve()

    home_dir = Path.home()
    candidates: list[Path] = []
    if os.name == "nt":
        local_app = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app:
            candidates.append(Path(local_app) / APP_FOLDER_NAME)
        candidates.append(home_dir / "AppData" / "Local" / APP_FOLDER_NAME)
    elif sys.platform == "darwin":
        candidates.append(home_dir / "Library" / "Application Support" / APP_FOLDER_NAME)
    else:
        xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
        if xdg_data:
            candidates.append(Path(xdg_data).expanduser() / LINUX_APP_DATA_NAME)
        candidates.append(home_dir / ".local" / "share" / LINUX_APP_DATA_NAME)

    candidates.extend([
        home_dir / APP_FOLDER_NAME,
        base_dir / "library",
        Path(tempfile.gettempdir()) / APP_FOLDER_NAME,
    ])
    for cand in candidates:
        if _dir_is_writable(cand):
            return cand.resolve()
    return candidates[0]


def _resolve_settings_file(app_data_dir: Path, portable: bool) -> Path:
    """Returns settings.json path, shared with Windows AppData on WSL so .exe and --web stay synced."""
    if portable or os.environ.get(ENV_DATA_DIR, "").strip():
        return app_data_dir / "settings.json"
    if _is_wsl():
        win_home = detect_wsl_windows_home()
        if win_home is not None:
            win_app_data = win_home / "AppData" / "Local" / APP_FOLDER_NAME
            if _dir_is_writable(win_app_data):
                return win_app_data / "settings.json"
    return app_data_dir / "settings.json"


def _resolve_os_default_library_dir(base_dir: Path, app_data_dir: Path, portable: bool) -> Path:
    """Returns the OS-standard default audio library directory (independent of install location)."""
    if portable:
        cand = base_dir / "library" / "inbox"
        if _dir_is_writable(cand):
            return cand.resolve()

    candidates = [
        _resolve_os_music_dir() / APP_FOLDER_NAME,
        Path.home() / APP_FOLDER_NAME / "inbox",
        app_data_dir / "inbox",
        Path(tempfile.gettempdir()) / APP_FOLDER_NAME / "inbox",
    ]
    for cand in candidates:
        if _dir_is_writable(cand):
            return cand.resolve()
    return candidates[0]


def _determine_app_dirs() -> tuple[Path, Path]:
    """Returns (RESOURCE_DIR, BASE_DIR) supporting normal execution and PyInstaller frozen bundles."""
    here = REPO_ROOT
    if getattr(sys, "frozen", False):
        resource_dir = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent)).resolve()
        exe_dir = Path(sys.executable).resolve().parent
        return resource_dir, exe_dir
    return here, here


def _initialize_storage_paths() -> tuple[Path, Path, Path, Path, Path, Path, str]:
    """Initializes RESOURCE_DIR, BASE_DIR, APP_DATA_DIR, SETTINGS_FILE, DEFAULT_LIBRARY_DIR, INBOX_DIR."""
    resource_dir, base_dir = _determine_app_dirs()
    portable = _is_portable_mode()
    app_data_dir = _resolve_app_data_dir(base_dir, portable=portable)
    settings_file = _resolve_settings_file(app_data_dir, portable=portable)
    default_library_dir = _resolve_os_default_library_dir(base_dir, app_data_dir, portable=portable)
    return resource_dir, base_dir, app_data_dir, settings_file, default_library_dir, default_library_dir, "default"


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
    # Deliberately excludes APP_DATA_DIR so private files (cookies.txt, github_token.txt,
    # browser profiles) cannot be browsed or streamed through library endpoints.
    candidates = [
        INBOX_DIR.resolve(),
        DEFAULT_LIBRARY_DIR.resolve(),
        BASE_DIR.resolve(),
        RESOURCE_DIR.resolve(),
        Path.home().resolve(),
    ]
    roots: list[Path] = []
    seen: set[Path] = set()
    for cand in candidates:
        if cand not in seen:
            seen.add(cand)
            roots.append(cand)
    if os.name == "nt":
        for letter in string.ascii_uppercase:
            drive_root = Path(f"{letter}:\\")
            try:
                if drive_root.exists():
                    dr = drive_root.resolve()
                    if dr not in seen:
                        seen.add(dr)
                        roots.append(dr)
            except OSError:
                pass
    else:
        for extra in ("/tmp", "/media", "/mnt", "/run/media", "/Volumes"):
            ep = Path(extra)
            try:
                if ep.exists():
                    er = ep.resolve()
                    if er not in seen:
                        seen.add(er)
                        roots.append(er)
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


def _format_storable_library_path(resolved: Path) -> str:
    """Formats a library path for settings.json so WSL and Windows share Windows drive paths cleanly."""
    p_str = str(resolved)
    if os.name != "nt" and _is_wsl():
        mnt_match = re.match(r"^/mnt/([a-zA-Z])(?:/(.*))?$", p_str)
        if mnt_match:
            drive = mnt_match.group(1).upper()
            rest = (mnt_match.group(2) or "").replace("/", "\\")
            return f"{drive}:\\{rest}" if rest else f"{drive}:\\"
    return p_str


def resolve_safe_dir(raw_path: str | None, default_dir: Path | None = None, must_exist: bool = True) -> Path:
    """Validates and resolves a directory path within allowed local roots (WSL + Windows /mnt/*)."""
    effective_default = default_dir if default_dir is not None else INBOX_DIR
    if not raw_path or not isinstance(raw_path, str) or not raw_path.strip():
        return effective_default.resolve()
    if "\x00" in raw_path:
        raise ValueError("Invalid path.")
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


def load_library_settings(settings_file: Path | None = None) -> dict:
    """Loads persisted JSON settings safely, failing closed to an empty dict on error."""
    target = settings_file if settings_file is not None else SETTINGS_FILE
    with _settings_lock:
        try:
            if not target.is_file():
                return {}
            raw = target.read_text(encoding="utf-8")
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}


def save_library_settings(data: dict, settings_file: Path | None = None) -> None:
    """Atomically writes settings.json with restricted permissions where supported."""
    target = settings_file if settings_file is not None else SETTINGS_FILE
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    with _settings_lock:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".settings.", suffix=".tmp", dir=str(target.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            if os.name != "nt":
                try:
                    os.chmod(tmp_name, 0o600)
                except OSError:
                    pass
            os.replace(tmp_name, target)
        except Exception:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except OSError:
                pass
            raise


def _validate_configured_library_path(raw_path: str, *, allow_create: bool = False) -> Path:
    """Strictly validates a candidate default library directory (no symlinks, dot-folders, or system dirs)."""
    if not isinstance(raw_path, str) or not raw_path.strip() or "\x00" in raw_path:
        raise ValueError("Jalur folder koleksi tidak valid.")
    cleaned = normalize_windows_or_wsl_path(raw_path)
    raw_p = Path(cleaned).expanduser()
    if not raw_p.is_absolute():
        raw_p = BASE_DIR / raw_p
    if raw_p.is_symlink():
        raise ValueError("Folder koleksi default tidak boleh berupa symbolic link.")

    resolved = resolve_safe_dir(cleaned, DEFAULT_LIBRARY_DIR, must_exist=not allow_create)

    allowed_roots = get_allowed_roots()
    matched_root = next((r for r in allowed_roots if resolved.is_relative_to(r)), None)
    if matched_root is not None:
        rel_parts = resolved.relative_to(matched_root).parts
        if any(part.startswith(".") or part.startswith("$") or part in SKIP_SYSTEM_DIRS for part in rel_parts):
            raise ValueError("Tidak diizinkan menetapkan folder sistem atau tersembunyi sebagai koleksi default.")

    app_data_res = APP_DATA_DIR.resolve()
    default_res = DEFAULT_LIBRARY_DIR.resolve()
    if resolved == app_data_res or (resolved.is_relative_to(app_data_res) and resolved != default_res):
        raise ValueError("Tidak diizinkan menetapkan folder data internal aplikasi sebagai koleksi audio.")

    if allow_create:
        if not _dir_is_writable(resolved):
            raise ValueError(f"Folder tidak dapat dibuat atau ditulis: {resolved}")
    else:
        if not resolved.is_dir():
            raise ValueError(f"Folder tidak ditemukan: {resolved}")
        if not _dir_is_writable(resolved):
            raise ValueError(f"Folder tidak memiliki izin tulis: {resolved}")

    return resolved


def resolve_effective_library_dir(
    *,
    default_dir: Path | None = None,
    settings_file: Path | None = None,
) -> tuple[Path, str]:
    """Resolves active library dir by priority: SWARARIA_LIBRARY_DIR env > settings.json > OS default."""
    os_default = (default_dir if default_dir is not None else DEFAULT_LIBRARY_DIR).resolve()

    env_val = os.environ.get(ENV_LIBRARY_DIR, "").strip()
    if env_val:
        try:
            resolved = _validate_configured_library_path(env_val, allow_create=True)
            return resolved, "env"
        except (ValueError, OSError):
            pass

    cfg = load_library_settings(settings_file=settings_file)
    saved_dir = cfg.get("library_dir")
    if isinstance(saved_dir, str) and saved_dir.strip():
        try:
            resolved = _validate_configured_library_path(saved_dir, allow_create=False)
            if resolved == os_default:
                return resolved, "default"
            return resolved, "custom"
        except (ValueError, OSError):
            pass

    return os_default, "default"


def set_configured_library_dir(
    raw_path: str | None,
    *,
    reset: bool = False,
    settings_file: Path | None = None,
) -> tuple[Path, str]:
    """Validates and persists the user's preferred default library folder in settings.json."""
    global INBOX_DIR, _INBOX_SOURCE

    if os.environ.get(ENV_LIBRARY_DIR, "").strip():
        raise ValueError(
            f"Folder koleksi default sedang dikunci oleh variabel lingkungan {ENV_LIBRARY_DIR}."
        )

    os_default = DEFAULT_LIBRARY_DIR.resolve()
    cfg = load_library_settings(settings_file=settings_file)

    if reset or raw_path is None or not str(raw_path).strip():
        cfg.pop("library_dir", None)
        save_library_settings(cfg, settings_file=settings_file)
        INBOX_DIR = os_default
        _INBOX_SOURCE = "default"
        return os_default, "default"

    resolved = _validate_configured_library_path(str(raw_path), allow_create=False)
    if resolved == os_default:
        cfg.pop("library_dir", None)
        source = "default"
    else:
        cfg["library_dir"] = _format_storable_library_path(resolved)
        source = "custom"

    save_library_settings(cfg, settings_file=settings_file)
    INBOX_DIR = resolved
    _INBOX_SOURCE = source
    return resolved, source


def get_library_settings_snapshot(
    active_inbox: Path | None = None,
    settings_file: Path | None = None,
) -> dict:
    """Returns a truthful JSON-serializable snapshot of active and OS-default library settings."""
    effective = (active_inbox if active_inbox is not None else INBOX_DIR).resolve()
    os_default = DEFAULT_LIBRARY_DIR.resolve()
    env_override = bool(os.environ.get(ENV_LIBRARY_DIR, "").strip())
    if env_override:
        source = "env"
    elif effective != os_default:
        source = "custom"
    else:
        source = "default"
    return {
        "library_dir": str(effective),
        "os_default_dir": str(os_default),
        "source": source,
        "is_custom": source == "custom",
        "env_override": env_override,
    }


def get_legacy_inbox_dir() -> Path | None:
    """Returns the legacy <repo|exe>/library/inbox folder if it exists and differs from active defaults."""
    legacy = (BASE_DIR / "library" / "inbox").resolve()
    try:
        if not legacy.is_dir():
            return None
        if legacy == INBOX_DIR.resolve() or legacy == DEFAULT_LIBRARY_DIR.resolve():
            return None
        return legacy
    except OSError:
        return None


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
    default_inbox: Path | None = None,
) -> tuple[Path, str]:
    """Strictly validates and resolves a local audio file within source_dir for playback streaming."""
    if not rel_path or not isinstance(rel_path, str) or not rel_path.strip():
        raise ValueError("Jalur file audio tidak boleh kosong.")

    effective_default = default_inbox if default_inbox is not None else INBOX_DIR
    source_dir = resolve_safe_dir(raw_source_dir, effective_default, must_exist=True)
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


RESOURCE_DIR, BASE_DIR, APP_DATA_DIR, SETTINGS_FILE, DEFAULT_LIBRARY_DIR, INBOX_DIR, _INBOX_SOURCE = (
    _initialize_storage_paths()
)
INBOX_DIR, _INBOX_SOURCE = resolve_effective_library_dir(
    default_dir=DEFAULT_LIBRARY_DIR,
    settings_file=SETTINGS_FILE,
)
