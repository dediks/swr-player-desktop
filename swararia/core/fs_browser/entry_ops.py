"""Safe rename and delete of files and folders (with companion-file handling)."""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from swararia.core import audio_manager
from swararia.core.fs_browser import safe_paths


FORBIDDEN_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_entry_name(name: str) -> str:
    """Sanitizes a proposed filename or folder name."""
    clean = name.strip()
    if not clean:
        raise ValueError("Name must not be empty.")
    if clean in (".", "..") or clean.startswith("."):
        raise ValueError("Name must not start with a dot (.) or be a hidden name.")
    if FORBIDDEN_NAME_CHARS.search(clean):
        raise ValueError('Name contains invalid characters (< > : " / \\ | ? * or control characters).')
    if len(clean) > 255:
        raise ValueError("Name is too long (maximum 255 characters).")
    return clean


def _is_same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def rename_fs_entry(
    source_dir: Path,
    rel_path: str,
    new_name: str,
    is_folder: bool | None = None,
) -> tuple[bool, str, Path, Path]:
    """Safely renames an audio file or a subfolder within source_dir.

    Returns:
        tuple[bool, message, old_resolved_path, new_resolved_path]
    """
    if not rel_path or not isinstance(rel_path, str):
        return False, "Invalid source file/folder path.", Path(), Path()

    clean_new_name = sanitize_entry_name(new_name)

    base = source_dir.resolve()
    target_path = (base / rel_path.strip().lstrip("/\\")).resolve()
    if not target_path.is_relative_to(base):
        return False, "Path access denied (outside the source directory).", Path(), Path()
    if not target_path.exists():
        return False, f"File or folder not found: {rel_path}", Path(), Path()

    target_is_dir = target_path.is_dir() if is_folder is None else bool(is_folder)
    parent_dir = target_path.parent

    if target_is_dir:
        new_path = (parent_dir / clean_new_name).resolve()
        if not new_path.is_relative_to(base):
            return False, "Path access denied while resolving the destination folder.", Path(), Path()
        if new_path.name == target_path.name:
            return True, "Folder name is unchanged.", target_path, new_path
        if new_path.exists() and not _is_same_file(new_path, target_path):
            return False, f"A folder named '{clean_new_name}' already exists.", Path(), Path()
    else:
        orig_ext = target_path.suffix.lower()
        if not clean_new_name.lower().endswith(orig_ext):
            clean_new_name = f"{clean_new_name}{orig_ext}"

        new_path = (parent_dir / clean_new_name).resolve()
        if not new_path.is_relative_to(base):
            return False, "Path access denied while resolving the destination file.", Path(), Path()
        if new_path.name == target_path.name:
            return True, "File name is unchanged.", target_path, new_path
        if new_path.exists() and not _is_same_file(new_path, target_path):
            return False, f"A file named '{clean_new_name}' already exists.", Path(), Path()

    try:
        if new_path.exists():
            # Case-only rename on a case-insensitive filesystem: go through a temp name.
            tmp_p = target_path.with_name(f".swararia_case_tmp_{os.getpid()}{target_path.suffix}")
            target_path.rename(tmp_p)
            try:
                tmp_p.rename(new_path)
            except OSError:
                tmp_p.rename(target_path)
                raise
        else:
            target_path.rename(new_path)
        msg = f"Renamed successfully to '{new_path.name}'"
        return True, msg, target_path, new_path
    except OSError as exc:
        return False, f"Rename failed on the file system: {exc}", Path(), Path()


SAFE_COMPANION_EXTS = {
    ".part", ".ytdl", ".jpg", ".jpeg", ".png", ".webp",
    ".json", ".txt", ".csv", ".lrc", ".m3u", ".m3u8",
}


def delete_fs_entries(
    source_dir: Path,
    rel_paths: list[str] | None = None,
    rel_path: str | None = None,
    is_folder: bool | None = None,
) -> tuple[bool, str, list[Path], list[Path]]:
    """Safely deletes one or more audio files or a playlist subfolder within source_dir.

    Returns:
        tuple[bool, message, deleted_file_paths, deleted_folder_paths]
    """
    base = source_dir.resolve()
    allowed_roots = safe_paths.get_allowed_roots()
    if not any(base.is_relative_to(r) for r in allowed_roots):
        return False, "Source directory access denied.", [], []

    raw_targets: list[str] = []
    if isinstance(rel_paths, list):
        for rp in rel_paths:
            if isinstance(rp, str) and rp.strip():
                raw_targets.append(rp.strip())
    if isinstance(rel_path, str) and rel_path.strip():
        raw_targets.append(rel_path.strip())

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique_targets: list[str] = []
    for t in raw_targets:
        if t not in seen:
            seen.add(t)
            unique_targets.append(t)

    if not unique_targets:
        return False, "No files or folders selected for deletion.", [], []

    validated_paths: list[Path] = []
    for raw_rel in unique_targets:
        clean_rel = raw_rel.strip().lstrip("/\\")
        if not clean_rel or clean_rel in (".", ".."):
            return False, "Invalid file/folder path.", [], []
        target_path = (base / clean_rel).resolve()
        if not target_path.is_relative_to(base) or target_path == base:
            return False, "Path access denied (outside the source directory).", [], []
        rel_parts = target_path.relative_to(base).parts
        if any(p.startswith(".") or p.startswith("$") or p in safe_paths.SKIP_SYSTEM_DIRS for p in rel_parts):
            return False, "Deleting system or hidden files/folders is not allowed.", [], []
        if target_path.is_symlink():
            return False, "Deleting symbolic links is not allowed.", [], []
        validated_paths.append(target_path)

    deleted_files: list[Path] = []
    deleted_folders: list[Path] = []
    errors: list[str] = []
    parent_dirs_to_check: set[Path] = set()

    for target_path in validated_paths:
        if not target_path.exists():
            errors.append(f"Not found: {target_path.name}")
            continue

        if target_path.is_dir():
            if is_folder is False:
                errors.append(f"'{target_path.name}' is a folder, not an audio file.")
                continue
            # Ensure folder only contains audio or safe companion files before rmtree
            unsafe_found = False
            folder_audio_files: list[Path] = []
            try:
                for p in target_path.rglob("*"):
                    if p.is_symlink():
                        unsafe_found = True
                        break
                    if p.is_file():
                        ext_low = p.suffix.lower()
                        if ext_low in audio_manager.EXTENSIONS:
                            folder_audio_files.append(p)
                        elif ext_low not in SAFE_COMPANION_EXTS and not p.name.startswith("."):
                            unsafe_found = True
                            break
            except OSError as exc:
                errors.append(f"Failed to inspect folder '{target_path.name}': {exc}")
                continue

            if unsafe_found:
                errors.append(
                    f"Folder '{target_path.name}' contains other non-audio files, so it was not deleted automatically for safety."
                )
                continue

            try:
                shutil.rmtree(target_path)
                deleted_folders.append(target_path)
                deleted_files.extend(folder_audio_files)
            except OSError as exc:
                errors.append(f"Failed to delete folder '{target_path.name}': {exc}")
        else:
            if target_path.suffix.lower() not in audio_manager.EXTENSIONS:
                errors.append(f"File '{target_path.name}' is not a supported audio format.")
                continue
            parent_dir = target_path.parent
            try:
                target_path.unlink()
                deleted_files.append(target_path)
                if parent_dir != base and parent_dir.is_relative_to(base):
                    parent_dirs_to_check.add(parent_dir)
            except OSError as exc:
                errors.append(f"Failed to delete '{target_path.name}': {exc}")

    # Clean up any playlist subfolders that became completely empty after file deletion
    for pdir in sorted(parent_dirs_to_check, key=lambda p: len(p.parts), reverse=True):
        try:
            if pdir.is_dir() and not any(pdir.iterdir()):
                pdir.rmdir()
                deleted_folders.append(pdir)
        except OSError:
            pass

    if not deleted_files and not deleted_folders:
        err_detail = "; ".join(errors[:3]) if errors else "File or folder not found."
        return False, f"Delete failed: {err_detail}", [], []

    if is_folder and len(deleted_folders) == 1 and len(unique_targets) == 1:
        folder_name = deleted_folders[0].name
        msg = f"Folder '{folder_name}' ({len(deleted_files)} songs) deleted successfully."
    elif len(deleted_files) == 1 and not errors:
        msg = f"File '{deleted_files[0].name}' deleted successfully."
    else:
        msg = f"Deleted {len(deleted_files)} audio file(s)"
        if deleted_folders and not is_folder:
            msg += f" ({len(deleted_folders)} empty folder(s) also cleaned up)"
        if errors:
            msg += f" • {len(errors)} failed"
        msg += "."

    return True, msg, deleted_files, deleted_folders
