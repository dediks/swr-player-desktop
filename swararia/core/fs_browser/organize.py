"""Tag-based organization of audio files into artist/album folders."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from swararia.core import audio_manager
from swararia.core.fs_browser import entry_ops, rename_history, safe_paths


# ---------------------------------------------------------------------------
# Organize songs into Artist/Album folders based on embedded tags
# ---------------------------------------------------------------------------
ORGANIZE_TEMPLATES = ("artist_album", "artist")
UNKNOWN_ARTIST = "Unknown Artist"
UNKNOWN_ALBUM = "Singles"


def read_audio_tags(path: Path) -> dict[str, str]:
    """Reads lowercase tag keys (artist, album, title, ...) via ffprobe. Returns {} on failure."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format_tags", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=20, check=True,
        )
        raw = json.loads(proc.stdout or "{}").get("format", {}).get("tags", {}) or {}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    return {str(k).lower(): str(v).strip() for k, v in raw.items() if isinstance(v, (str, int, float))}


def _safe_folder_component(value: str, fallback: str) -> str:
    """Turns a tag value into a safe single folder name (never raises)."""
    clean = entry_ops.FORBIDDEN_NAME_CHARS.sub("_", value or "").strip().strip(".").strip()
    clean = re.sub(r"\s{2,}", " ", clean)[:100].strip()
    return clean or fallback


def organize_into_folders(
    source_dir: Path,
    rel_paths: list[str],
    template: str = "artist_album",
    preview_only: bool = False,
    expected_plan: dict[str, str] | None = None,
    tag_reader=None,
) -> tuple[bool, str, list[dict]]:
    """Moves audio files into Artist/Album (or Artist) folders under source_dir based on their tags.

    Files are never overwritten: a destination that already exists (or is claimed twice) is
    reported with a `reason` and skipped. Applied moves are recorded for undo.
    """
    base = source_dir.resolve()
    if not any(base.is_relative_to(r) for r in safe_paths.get_allowed_roots()):
        return False, "Source directory access denied.", []
    if template not in ORGANIZE_TEMPLATES:
        template = "artist_album"
    if not isinstance(rel_paths, list) or not rel_paths:
        return False, "No audio files selected.", []
    read_tags = tag_reader or read_audio_tags

    plan: list[dict] = []
    claimed: set[Path] = set()
    seen: set[Path] = set()
    for raw_rel in rel_paths:
        if not isinstance(raw_rel, str) or not raw_rel.strip():
            continue
        src = (base / raw_rel.strip().lstrip("/\\")).resolve()
        if not src.is_relative_to(base) or src == base:
            return False, "Path access denied (outside the source directory).", []
        if src in seen or not src.is_file() or src.suffix.lower() not in audio_manager.EXTENSIONS:
            continue
        seen.add(src)

        tags = read_tags(src)
        artist = _safe_folder_component(tags.get("album_artist") or tags.get("artist") or "", UNKNOWN_ARTIST)
        parts = [artist]
        if template == "artist_album":
            parts.append(_safe_folder_component(tags.get("album") or "", UNKNOWN_ALBUM))
        dest = base.joinpath(*parts, src.name).resolve()

        reason = ""
        if not dest.is_relative_to(base):
            reason = "Destination is outside the source directory"
        elif dest == src:
            reason = ""
        elif dest.exists() or dest in claimed:
            reason = "A file with the same name already exists in the destination folder"
        else:
            claimed.add(dest)
        changed = dest != src and not reason
        plan.append({
            "rel_path": str(src.relative_to(base)),
            "new_rel_path": str(dest.relative_to(base)) if dest.is_relative_to(base) else str(src.relative_to(base)),
            "folder": str(Path(*parts)),
            "old_name": src.name,
            "new_name": src.name,
            "changed": changed,
            "reason": reason,
            "old_path": str(src),
            "new_path": str(dest),
        })

    if not plan:
        return False, "No valid audio files found.", []

    # Items that cannot be moved keep their current location in the expected-plan comparison
    for item in plan:
        if item["reason"]:
            item["new_rel_path"] = item["rel_path"]

    movable = [i for i in plan if i["changed"]]
    if preview_only:
        skipped = sum(1 for i in plan if i["reason"])
        msg = f"Preview ready: {len(movable)} of {len(plan)} files will be moved into folders."
        if skipped:
            msg += f" {skipped} skipped (name conflict)."
        return True, msg, plan

    if expected_plan is not None:
        current = {i["rel_path"]: i["new_rel_path"] for i in plan}
        if current != expected_plan:
            return False, "The folder contents or tags changed since the preview. Reload the preview and try again.", plan
    if not movable:
        return True, "All files are already in the right folders (no changes).", plan

    errors: list[str] = []
    moved: list[tuple[Path, Path]] = []
    for item in movable:
        src, dest = Path(item["old_path"]), Path(item["new_path"])
        item["applied"] = False
        try:
            if dest.exists():
                raise FileExistsError(f"Destination already exists: {dest.name}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dest)
            item["applied"] = True
            moved.append((src, dest))
        except OSError as exc:
            errors.append(f"{src.name}: {exc}")

    rename_history.record_rename_history(moved)
    if not moved:
        return False, f"Failed to move files: {'; '.join(errors[:3])}", plan
    msg = f"Moved {len(moved)} file(s) into folders based on tags."
    if errors:
        msg += f" ({len(errors)} failed)"
    return True, msg, plan
