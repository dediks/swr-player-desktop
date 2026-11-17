"""Persisted batch-rename history and undo."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from swararia.core.fs_browser import safe_paths


# ---------------------------------------------------------------------------
# Undo log for batch renames
# ---------------------------------------------------------------------------
RENAME_HISTORY_FILE = safe_paths.BASE_DIR / "library" / "rename_history.json"
RENAME_HISTORY_LIMIT = 10
_history_lock = threading.Lock()


def _load_rename_history() -> list[dict]:
    try:
        data = json.loads(RENAME_HISTORY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save_rename_history(history: list[dict]) -> None:
    try:
        RENAME_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        RENAME_HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def record_rename_history(moves: list[tuple[Path, Path]]) -> None:
    """Appends a batch of (old_path, new_path) moves so the batch can be undone later."""
    if not moves:
        return
    with _history_lock:
        history = _load_rename_history()
        history.append({
            "time": time.time(),
            "moves": [[str(o), str(n)] for o, n in moves],
        })
        _save_rename_history(history[-RENAME_HISTORY_LIMIT:])


def peek_rename_history() -> dict | None:
    """Returns summary of the latest undoable batch, or None."""
    with _history_lock:
        history = _load_rename_history()
    if not history:
        return None
    last = history[-1]
    return {"time": last.get("time"), "count": len(last.get("moves", []))}


def undo_last_batch_rename() -> tuple[bool, str, list[tuple[Path, Path]]]:
    """Reverts the most recent batch rename. All-or-nothing.

    Returns (ok, message, [(current_path, restored_path), ...]).
    """
    with _history_lock:
        history = _load_rename_history()
        if not history:
            return False, "No rename history available to undo.", []
        last = history[-1]
        roots = safe_paths.get_allowed_roots()
        pairs: list[tuple[Path, Path]] = []  # (current, restore_to)
        for old_s, new_s in last.get("moves", []):
            cur, restore = Path(new_s), Path(old_s)
            if not any(cur.resolve().is_relative_to(r) and restore.parent.resolve().is_relative_to(r) for r in roots):
                return False, "History points outside the allowed directories.", []
            if not cur.exists():
                return False, f"Cannot undo: '{cur.name}' no longer exists or was moved.", []
            if restore.exists():
                return False, f"Cannot undo: '{restore.name}' already exists again.", []
            pairs.append((cur, restore))

        staged: list[tuple[Path, Path, Path]] = []
        failed: str | None = None
        for idx, (cur, restore) in enumerate(pairs):
            tmp_p = cur.with_name(f".swararia_undo_tmp_{os.getpid()}_{idx}{cur.suffix}")
            try:
                cur.rename(tmp_p)
                staged.append((cur, tmp_p, restore))
            except OSError as exc:
                failed = f"{cur.name}: {exc}"
                break
        if failed is None:
            for i, (cur, tmp_p, restore) in enumerate(staged):
                try:
                    restore.parent.mkdir(parents=True, exist_ok=True)
                    tmp_p.rename(restore)
                except OSError as exc:
                    failed = f"{cur.name}: {exc}"
                    # put back everything not yet restored
                    staged = staged[i:]
                    break
            else:
                staged = []
        if failed is not None:
            for cur, tmp_p, _restore in reversed(staged):
                try:
                    tmp_p.rename(cur)
                except OSError:
                    pass
            return False, f"Undo failed, nothing was changed: {failed}", []

        _save_rename_history(history[:-1])
        # Remove folders left empty by undoing an "organize into folders" batch. Walk upward but never
        # remove the original folder of the file (or any of its ancestors) or an allowed root.
        for cur, restore in pairs:
            parent = cur.parent
            keep = {restore.parent, *restore.parent.parents}
            while parent not in keep and not any(parent.resolve() == r for r in roots):
                try:
                    if not parent.is_dir() or any(parent.iterdir()):
                        break
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
    return True, f"Undid the rename of {len(pairs)} file(s).", pairs
