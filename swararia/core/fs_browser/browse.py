"""Directory browsing, audio file iteration, Windows shortcuts and file-manager integration."""
from __future__ import annotations

import os
import re
import shutil
import string
import subprocess
from pathlib import Path

from swararia.core import audio_manager
from swararia.core.fs_browser import safe_paths


def iter_audio_files_safe(root_dir: Path, allowed_exts: set[str] | None = None) -> list[Path]:
    """Safely walks root_dir (including Windows /mnt/* drives), skipping inaccessible NTFS junctions."""
    exts = allowed_exts if allowed_exts is not None else audio_manager.EXTENSIONS
    results: list[Path] = []
    if not root_dir.is_dir():
        return results
    for dirpath, dirnames, filenames in os.walk(root_dir, onerror=lambda _: None):
        dirnames[:] = sorted(
            d for d in dirnames
            if not d.startswith(".") and not d.startswith("$") and d not in safe_paths.SKIP_SYSTEM_DIRS
        )
        for fname in sorted(filenames):
            if fname.startswith("."):
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext in exts:
                results.append(Path(dirpath) / fname)
    return results


def get_windows_shortcuts(default_inbox: Path = safe_paths.INBOX_DIR) -> list[dict]:
    """Builds quick-access shortcuts for Native Windows, macOS, Linux, and WSL environments."""
    home_dir = Path.home().resolve()
    shortcuts = [
        {"label": "📥 Inbox Proyek", "path": str(default_inbox.resolve())},
        {"label": "🏠 Home User" if os.name == "nt" else "🏠 WSL/Linux Home", "path": str(home_dir)},
    ]

    for sub_label, sub_name in (
        ("🎵 Music", "Music"),
        ("⬇️ Downloads", "Downloads"),
        ("🖥️ Desktop", "Desktop"),
    ):
        sub_p = home_dir / sub_name
        try:
            if sub_p.is_dir():
                shortcuts.append({"label": sub_label, "path": str(sub_p.resolve())})
        except OSError:
            pass

    if os.name == "nt":
        for letter in string.ascii_uppercase:
            drive_p = Path(f"{letter}:\\")
            try:
                if drive_p.exists():
                    shortcuts.append({
                        "label": f"💽 Drive {letter}:",
                        "path": str(drive_p.resolve()),
                    })
            except OSError:
                pass
        return shortcuts

    mnt_root = Path("/mnt")
    if mnt_root.is_dir():
        win_users_dir = mnt_root / "c" / "Users"
        if win_users_dir.is_dir():
            skip_users = {
                "public",
                "default",
                "default user",
                "all users",
                "wsiaccount",
                "wdagutilityaccount",
                "administrator",
                "guest",
            }
            candidate_users: list[tuple[float, Path]] = []
            try:
                for udir in sorted(win_users_dir.iterdir()):
                    name_low = udir.name.lower()
                    if (
                        udir.name.startswith(".")
                        or name_low in skip_users
                        or name_low.startswith("defaultuser")
                        or name_low.startswith("temp")
                    ):
                        continue
                    try:
                        if not udir.is_dir():
                            continue
                        latest_mtime = udir.stat().st_mtime
                        for probe_sub in ("Downloads", "Desktop", "Music", "NTUSER.DAT"):
                            sp = udir / probe_sub
                            try:
                                if sp.exists():
                                    latest_mtime = max(latest_mtime, sp.stat().st_mtime)
                            except OSError:
                                pass
                        candidate_users.append((latest_mtime, udir))
                    except OSError:
                        continue
            except OSError:
                pass

            if candidate_users:
                candidate_users.sort(key=lambda item: item[0], reverse=True)
                primary_udir = candidate_users[0][1]
                shortcuts.append({"label": f"🪟 Win User ({primary_udir.name})", "path": str(primary_udir)})
                for sub_label, sub_name in (
                    ("🎵 Win Music", "Music"),
                    ("⬇️ Win Downloads", "Downloads"),
                    ("🖥️ Win Desktop", "Desktop"),
                ):
                    sub_p = primary_udir / sub_name
                    try:
                        if sub_p.is_dir():
                            shortcuts.append({"label": sub_label, "path": str(sub_p)})
                    except OSError:
                        pass

        try:
            for d in sorted(mnt_root.iterdir()):
                if len(d.name) == 1 and d.name.isalpha():
                    try:
                        if d.is_dir():
                            shortcuts.append({
                                "label": f"💽 Drive {d.name.upper()}: (/mnt/{d.name.lower()})",
                                "path": str(d),
                            })
                    except OSError:
                        pass
        except OSError:
            pass

    for m_root in ("/media", "/run/media", "/Volumes"):
        mp = Path(m_root)
        if mp.is_dir():
            shortcuts.append({"label": f"💾 {m_root}", "path": str(mp.resolve())})
    return shortcuts


def browse_directory(
    raw_path: str | None,
    create_subfolder: str | None = None,
    default_inbox: Path = safe_paths.INBOX_DIR,
) -> dict:
    """Resolves a directory, optionally creates a sanitized subfolder, and lists subdirectories."""
    curr_dir = safe_paths.resolve_safe_dir(raw_path, default_inbox, must_exist=True)
    if isinstance(create_subfolder, str) and create_subfolder.strip():
        clean_sub = re.sub(r'[<>:"/\\|?*]', "", create_subfolder.strip())
        if clean_sub and not clean_sub.startswith("."):
            new_dir = (curr_dir / clean_sub).resolve()
            if new_dir.is_relative_to(curr_dir):
                new_dir.mkdir(parents=True, exist_ok=True)
                curr_dir = new_dir

    allowed_roots = safe_paths.get_allowed_roots()
    parent_path = None
    if curr_dir.parent != curr_dir:
        if any(curr_dir.parent.is_relative_to(r) for r in allowed_roots):
            parent_path = str(curr_dir.parent)

    subdirs = []
    try:
        entries = sorted(curr_dir.iterdir(), key=lambda x: x.name.lower())
    except OSError:
        entries = []
    for d in entries:
        if d.name.startswith(".") or d.name.startswith("$") or d.name in safe_paths.SKIP_SYSTEM_DIRS:
            continue
        try:
            if not d.is_dir():
                continue
            media_exts = audio_manager.EXTENSIONS | {".mp4", ".mkv", ".mov", ".ts", ".m4v", ".avi", ".rf64", ".w64"}
            audio_cnt = sum(
                1 for f in d.iterdir()
                if not f.name.startswith(".") and f.suffix.lower() in media_exts and f.is_file()
            )
        except OSError:
            continue
        subdirs.append({"name": d.name, "path": str(d), "audio_count": audio_cnt})

    return {
        "current": str(curr_dir),
        "parent": parent_path,
        "dirs": subdirs,
        "subdirs": subdirs,
        "shortcuts": get_windows_shortcuts(default_inbox),
    }


def open_in_file_manager(target_path: Path) -> tuple[bool, str]:
    """Opens a directory or highlights a file in Windows File Explorer (or host file manager)."""
    if not target_path.exists():
        return False, f"Path not found on this system: {target_path}"

    win_path = safe_paths.to_windows_path(target_path)

    # Native Windows fast path
    if os.name == "nt" and target_path.is_dir() and hasattr(os, "startfile"):
        try:
            os.startfile(str(target_path))  # type: ignore[attr-defined]
            return True, f"Membuka di Windows Explorer: {win_path}"
        except Exception:
            pass

    # 1. Search for explorer.exe or Windows launch binaries in PATH and standard locations
    explorer_candidates = [
        shutil.which("explorer.exe"),
        "/mnt/c/Windows/explorer.exe",
        "/mnt/c/WINDOWS/explorer.exe",
    ]
    explorer_bin = next((b for b in explorer_candidates if b and os.path.isfile(b) and os.access(b, os.X_OK)), None)

    if explorer_bin:
        try:
            if target_path.is_dir():
                subprocess.Popen(
                    [explorer_bin, "."],
                    cwd=str(target_path),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                subprocess.Popen(
                    [explorer_bin, f"/select,{win_path}"],
                    cwd=str(target_path.parent),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            return True, f"Membuka di Windows Explorer: {win_path}"
        except Exception as exc:
            return False, f"Gagal menjalankan explorer.exe: {exc}"

    # 2. Try PowerShell Start-Process
    ps_candidates = [
        shutil.which("powershell.exe"),
        "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
        "/mnt/c/WINDOWS/System32/WindowsPowerShell/v1.0/powershell.exe",
    ]
    ps_bin = next((b for b in ps_candidates if b and os.path.isfile(b) and os.access(b, os.X_OK)), None)
    if ps_bin:
        try:
            cmd = f"Start-Process explorer.exe -ArgumentList '{win_path}'"
            subprocess.Popen([ps_bin, "-NoProfile", "-Command", cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True, f"Membuka via PowerShell: {win_path}"
        except Exception as exc:
            pass

    # 3. Try xdg-open for Linux desktops
    xdg_bin = shutil.which("xdg-open")
    if xdg_bin:
        try:
            subprocess.Popen([xdg_bin, str(target_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True, f"Membuka via xdg-open: {target_path}"
        except Exception as exc:
            return False, f"Gagal menjalankan xdg-open: {exc}"

    # 4. Try macOS open
    mac_open = shutil.which("open")
    if mac_open:
        try:
            subprocess.Popen([mac_open, str(target_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True, f"Membuka via open: {target_path}"
        except Exception as exc:
            return False, f"Gagal menjalankan open: {exc}"

    return False, "Could not find explorer.exe or a file manager on the host system."
