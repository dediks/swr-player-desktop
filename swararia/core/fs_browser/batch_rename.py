"""Filename cleaning, auto-numbering and batch rename planning/execution."""
from __future__ import annotations

import os
import re
from pathlib import Path

from swararia.core import audio_manager
from swararia.core.fs_browser import entry_ops, rename_history, safe_paths


JUNK_TAG_WORDS = (
    r"(?:official\s*(?:music\s*)?(?:video|audio|lyric\s*video|lyrics|visualizer|mv)|"
    r"music\s*video|lyric\s*video|lyrics?|lirik(?:\s*lagu)?|video\s*klip|"
    r"visualizer|audio\s*only|full\s*album|karaoke(?:\s*version)?|"
    r"hd|hq|4k(?:\s*uhd)?|1080p|720p|mv|ost)"
)
# Unambiguous junk words that are safe to strip even without brackets (only at the end of the title).
JUNK_TRAILING_WORDS = (
    r"(?:official\s*(?:music\s*)?(?:video|audio|lyric\s*video|lyrics|visualizer|mv)|"
    r"music\s*video|lyric\s*video|lyrics?|lirik(?:\s*lagu)?|video\s*klip|"
    r"visualizer|audio\s*only|full\s*album)"
)
# Bracketed tags: any junk word. Ambiguous words (hd, mv, ost, ...) are only removed inside brackets.
JUNK_TAG_PATTERN = re.compile(
    rf"[\(\[\{{]\s*{JUNK_TAG_WORDS}\s*[\)\]\}}]",
    flags=re.IGNORECASE,
)
# Dash-separated trailing tag, e.g. "Title - Official Video". Anchored to the end so "Song - OST Remix" stays intact.
JUNK_TRAILING_PATTERN = re.compile(
    rf"(?:[｜|\-–—]+\s*){JUNK_TRAILING_WORDS}\s*$",
    flags=re.IGNORECASE,
)
YT_ID_SUFFIX_PATTERN = re.compile(r"\s*\[([A-Za-z0-9_-]{11})\]$")
# Leading track number such as "01 - ", "01. ", "01)". Not stripped when followed by another digit
# (e.g. "1-800-273-8255") and never for 4-digit numbers like "1999 - Prince".
LEADING_TRACK_NUM_PATTERN = re.compile(r"^\s*\d{1,3}\s*(?:[-._)]+|\s+-)\s*(?!\d)")


def clean_and_number_filename(
    old_name: str,
    index: int | None = None,
    pad_width: int = 2,
    numbering_mode: str = "dash",
    strip_yt_id: bool = True,
    strip_junk_tags: bool = True,
    clean_spaces: bool = True,
) -> str:
    """Computes a cleaned and optionally numbered filename while preserving extension."""
    p = Path(old_name)
    ext = p.suffix
    stem = p.stem

    # Capture trailing [11-char YouTube ID] before cleaning so we can either strip or preserve it at the end
    yt_id_match = YT_ID_SUFFIX_PATTERN.search(stem)
    yt_id_suffix = ""
    if yt_id_match:
        stem = stem[:yt_id_match.start()]
        if not strip_yt_id:
            yt_id_suffix = f" [{yt_id_match.group(1)}]"

    if strip_junk_tags:
        stem = JUNK_TAG_PATTERN.sub(" ", stem)
        stem = JUNK_TRAILING_PATTERN.sub("", stem.rstrip())

    if numbering_mode in ("dash", "dot", "strip"):
        stem = LEADING_TRACK_NUM_PATTERN.sub("", stem)

    if clean_spaces:
        # Normalize fullwidth pipes/slashes/underscores often inserted by yt-dlp
        stem = re.sub(r"[｜|]{2,}", " - ", stem)
        stem = re.sub(r"[｜|]", " - ", stem)
        stem = stem.replace("_", " ")
        stem = re.sub(r"\s*-\s*-\s*", " - ", stem)
        stem = re.sub(r"\s{2,}", " ", stem)
        stem = stem.strip(" -_|.")

    stem = stem.strip()
    if not stem:
        stem = p.stem.strip() or "Track"

    prefix = ""
    if index is not None and numbering_mode in ("dash", "dot"):
        num_str = str(index).zfill(max(2, pad_width))
        prefix = f"{num_str} - " if numbering_mode == "dash" else f"{num_str}. "

    proposed = f"{prefix}{stem}{yt_id_suffix}{ext}"
    return entry_ops.sanitize_entry_name(proposed)


def _natural_key(text: str) -> list:
    """Natural sort key: 'Track 2' < 'Track 10', case-insensitive."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", text)]


# Numbering modes sent by the UI (2digit_dash, ...) mapped to (internal mode, minimum zero-pad width).
_NUMBERING_ALIASES: dict[str, tuple[str, int]] = {
    "2digit_dash": ("dash", 2),
    "3digit_dash": ("dash", 3),
    "2digit_dot": ("dot", 2),
    "none": ("keep", 2),
}


def _normalize_numbering_mode(raw: object) -> tuple[str, int]:
    mode = str(raw or "dash").strip().lower()
    if mode in _NUMBERING_ALIASES:
        return _NUMBERING_ALIASES[mode]
    if mode in ("dash", "dot", "strip", "keep"):
        return mode, 2
    return "dash", 2


def batch_rename_entries(
    source_dir: Path,
    rel_paths: list[str],
    options: dict | None = None,
    preview_only: bool = False,
    expected_plan: dict[str, str] | None = None,
) -> tuple[bool, str, list[dict]]:
    """Previews or executes batch track numbering & tag cleaning per folder within source_dir.

    When expected_plan ({rel_path: new_rel_path}) is given on apply, the freshly computed plan must
    match it exactly, otherwise nothing is renamed.
    """
    base = source_dir.resolve()
    allowed_roots = safe_paths.get_allowed_roots()
    if not any(base.is_relative_to(r) for r in allowed_roots):
        return False, "Source directory access denied.", []

    if not isinstance(rel_paths, list) or not rel_paths:
        return False, "No audio files selected for renaming.", []

    opts = options or {}
    numbering_mode, min_pad = _normalize_numbering_mode(opts.get("numbering_mode", "dash"))
    try:
        start_number = max(0, int(opts.get("start_number", 1)))
    except (TypeError, ValueError):
        start_number = 1
    sort_by = str(opts.get("sort_by", "selection")).strip().lower()
    if sort_by not in ("selection", "name", "mtime"):
        sort_by = "selection"
    strip_yt_id = bool(opts.get("strip_yt_id", True))
    strip_junk_tags = bool(opts.get("strip_junk_tags", True))
    clean_spaces = bool(opts.get("clean_spaces", True))

    # Validate and group files by parent directory while preserving selection order
    grouped_by_parent: dict[Path, list[Path]] = {}
    seen_paths: set[Path] = set()
    for raw_rel in rel_paths:
        if not isinstance(raw_rel, str) or not raw_rel.strip():
            continue
        clean_rel = raw_rel.strip().lstrip("/\\")
        if not clean_rel or clean_rel in (".", ".."):
            continue
        target_p = (base / clean_rel).resolve()
        if not target_p.is_relative_to(base) or target_p == base:
            return False, "Path access denied (outside the source directory).", []
        if not target_p.is_file() or target_p.suffix.lower() not in audio_manager.EXTENSIONS:
            continue
        if target_p in seen_paths:
            continue
        seen_paths.add(target_p)
        grouped_by_parent.setdefault(target_p.parent, []).append(target_p)

    if not seen_paths:
        return False, "No valid audio files found.", []

    plan: list[dict] = []
    for parent_dir, folder_files in grouped_by_parent.items():
        if sort_by == "name":
            folder_files = sorted(folder_files, key=lambda p: _natural_key(p.name))
        elif sort_by == "mtime":
            def _mtime(p: Path) -> float:
                try:
                    return p.stat().st_mtime
                except OSError:
                    return 0.0
            folder_files = sorted(folder_files, key=lambda p: (_mtime(p), _natural_key(p.name)))
        max_num = start_number + len(folder_files) - 1
        pad_width = max(min_pad, 3 if max_num >= 100 else 2)
        reserved_names: set[str] = set()
        # Pre-populate existing unselected files in the same folder so we never collide with them
        try:
            for existing_f in parent_dir.iterdir():
                if existing_f.is_file() and existing_f not in seen_paths:
                    reserved_names.add(existing_f.name.lower())
        except OSError:
            pass

        for offset, old_p in enumerate(folder_files):
            seq_idx = start_number + offset
            proposed_name = clean_and_number_filename(
                old_p.name,
                index=seq_idx,
                pad_width=pad_width,
                numbering_mode=numbering_mode,
                strip_yt_id=strip_yt_id,
                strip_junk_tags=strip_junk_tags,
                clean_spaces=clean_spaces,
            )

            # Avoid duplicate target filenames within the same folder
            candidate_name = proposed_name
            if candidate_name.lower() != old_p.name.lower() and candidate_name.lower() in reserved_names:
                c_path = Path(proposed_name)
                dup_idx = 2
                while f"{c_path.stem} ({dup_idx}){c_path.suffix}".lower() in reserved_names:
                    dup_idx += 1
                candidate_name = f"{c_path.stem} ({dup_idx}){c_path.suffix}"

            reserved_names.add(candidate_name.lower())
            new_p = (parent_dir / candidate_name).resolve()
            folder_rel = str(parent_dir.relative_to(base)) if parent_dir != base else ""
            plan.append({
                "rel_path": str(old_p.relative_to(base)),
                "new_rel_path": str(new_p.relative_to(base)),
                "folder": folder_rel,
                "old_name": old_p.name,
                "new_name": candidate_name,
                "changed": old_p.name != candidate_name,
                "old_path": str(old_p),
                "new_path": str(new_p),
            })

    changed_items = [item for item in plan if item["changed"]]
    if preview_only:
        msg = f"Preview ready: {len(changed_items)} of {len(plan)} files will be renamed."
        return True, msg, plan

    # Guard against drift between what the user previewed and what would be applied now.
    if expected_plan is not None:
        current = {item["rel_path"]: item["new_rel_path"] for item in plan}
        if current != expected_plan:
            return False, "The folder contents or settings changed since the preview. Reload the preview and try again.", plan

    if not changed_items:
        return True, "All file names already match the settings (no changes).", plan

    # Two-phase rename to prevent any collision when swapping numbers (e.g. 02 -> 01 and 01 -> 02)
    staged: list[tuple[Path, Path, Path, dict]] = []
    errors: list[str] = []
    for item in changed_items:
        item["applied"] = False

    # Phase 1 is all-or-nothing: if any file cannot be staged, roll back every staged file.
    stage_failed = False
    for idx, item in enumerate(changed_items):
        old_p = Path(item["old_path"])
        new_p = Path(item["new_path"])
        tmp_p = old_p.with_name(f".swararia_ren_tmp_{os.getpid()}_{idx}{old_p.suffix}")
        try:
            old_p.rename(tmp_p)
            staged.append((old_p, tmp_p, new_p, item))
        except OSError as exc:
            errors.append(f"{old_p.name}: {exc}")
            stage_failed = True
            break

    if stage_failed:
        for old_p, tmp_p, _new_p, _item in reversed(staged):
            try:
                tmp_p.rename(old_p)
            except OSError as exc:
                errors.append(f"Rollback failed for {old_p.name} (kept as {tmp_p.name}): {exc}")
        return False, f"Rename failed, all changes were rolled back: {'; '.join(errors[:3])}", plan

    renamed_count = 0
    for old_p, tmp_p, new_p, item in staged:
        try:
            if new_p.exists():
                raise FileExistsError(f"Destination already exists: {new_p.name}")
            tmp_p.rename(new_p)
            item["applied"] = True
            renamed_count += 1
        except OSError as exc:
            errors.append(f"{old_p.name} -> {new_p.name}: {exc}")
            restore_p = old_p
            if restore_p.exists():
                restore_p = old_p.with_name(f"{old_p.stem} (restored){old_p.suffix}")
                n = 2
                while restore_p.exists():
                    restore_p = old_p.with_name(f"{old_p.stem} (restored {n}){old_p.suffix}")
                    n += 1
            try:
                tmp_p.rename(restore_p)
            except OSError as exc2:
                errors.append(f"Rollback failed for {old_p.name} (kept as {tmp_p.name}): {exc2}")

    if renamed_count == 0 and errors:
        return False, f"Failed to rename files: {'; '.join(errors[:3])}", plan

    msg = f"Cleaned up and numbered {renamed_count} audio file(s) successfully."
    if errors:
        msg += f" ({len(errors)} failed)"
    rename_history.record_rename_history([
        (Path(item["old_path"]), Path(item["new_path"])) for item in changed_items if item.get("applied")
    ])
    return True, msg, plan
