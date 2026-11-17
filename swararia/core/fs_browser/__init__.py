"""Filesystem Browser, Path Boundary Validation & URL Allowlist Helpers."""
from __future__ import annotations

from swararia.core.fs_browser.safe_paths import (  # noqa: F401
    _determine_app_dirs,
    RESOURCE_DIR,
    BASE_DIR,
    INBOX_DIR,
    SKIP_SYSTEM_DIRS,
    ALLOWED_YOUTUBE_DOMAINS,
    get_allowed_roots,
    normalize_windows_or_wsl_path,
    resolve_safe_dir,
    validate_url,
    to_windows_path,
    AUDIO_MIME_TYPES,
    resolve_safe_audio_file,
)
from swararia.core.fs_browser.browse import (  # noqa: F401
    iter_audio_files_safe,
    get_windows_shortcuts,
    browse_directory,
    open_in_file_manager,
)
from swararia.core.fs_browser.entry_ops import (  # noqa: F401
    FORBIDDEN_NAME_CHARS,
    sanitize_entry_name,
    _is_same_file,
    rename_fs_entry,
    SAFE_COMPANION_EXTS,
    delete_fs_entries,
)
from swararia.core.fs_browser.rename_history import (  # noqa: F401
    RENAME_HISTORY_FILE,
    RENAME_HISTORY_LIMIT,
    _history_lock,
    _load_rename_history,
    _save_rename_history,
    record_rename_history,
    peek_rename_history,
    undo_last_batch_rename,
)
from swararia.core.fs_browser.batch_rename import (  # noqa: F401
    JUNK_TAG_WORDS,
    JUNK_TRAILING_WORDS,
    JUNK_TAG_PATTERN,
    JUNK_TRAILING_PATTERN,
    YT_ID_SUFFIX_PATTERN,
    LEADING_TRACK_NUM_PATTERN,
    clean_and_number_filename,
    _natural_key,
    _NUMBERING_ALIASES,
    _normalize_numbering_mode,
    batch_rename_entries,
)
from swararia.core.fs_browser.organize import (  # noqa: F401
    ORGANIZE_TEMPLATES,
    UNKNOWN_ARTIST,
    UNKNOWN_ALBUM,
    read_audio_tags,
    _safe_folder_component,
    organize_into_folders,
)
