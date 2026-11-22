"""Shared paths, constants and singleton managers for the local server."""
from __future__ import annotations

from swararia import config
from swararia.core import fs_browser
from swararia.core.task_engine import AudioMetadataCache, TaskQueueManager


APP_VERSION = "1.0.0"
GITHUB_REPO = "dediks/swr-player-desktop"
BASE_DIR = fs_browser.BASE_DIR
RESOURCE_DIR = fs_browser.RESOURCE_DIR
APP_DATA_DIR = fs_browser.APP_DATA_DIR
SETTINGS_FILE = fs_browser.SETTINGS_FILE
DEFAULT_LIBRARY_DIR = fs_browser.DEFAULT_LIBRARY_DIR
STATIC_DIR = config.STATIC_DIR
INBOX_DIR = fs_browser.INBOX_DIR
ARCHIVE_FILE = APP_DATA_DIR / "downloaded_ids.txt"
COOKIES_FILE = APP_DATA_DIR / "cookies.txt"
GITHUB_TOKEN_FILE = APP_DATA_DIR / "github_token.txt"

# Instantiate shared state managers
metadata_cache = AudioMetadataCache()
task_manager = TaskQueueManager(
    metadata_cache=metadata_cache,
    inbox_dir=INBOX_DIR,
    archive_file=ARCHIVE_FILE,
    cookies_file=COOKIES_FILE,
)


def get_library_settings() -> dict:
    """Returns a snapshot of the active and OS-default library directory settings."""
    return fs_browser.get_library_settings_snapshot(
        active_inbox=INBOX_DIR,
        settings_file=SETTINGS_FILE,
    )


def update_default_library_dir(
    raw_path: str | None,
    *,
    reset: bool = False,
    server=None,
) -> dict:
    """Validates, persists, and propagates a new default library folder across live state."""
    global INBOX_DIR
    new_dir, _source = fs_browser.set_configured_library_dir(
        raw_path,
        reset=reset,
        settings_file=SETTINGS_FILE,
    )
    INBOX_DIR = new_dir
    task_manager.inbox_dir = new_dir
    metadata_cache.invalidate_dir_cache()
    if server is not None and getattr(server, "secure_remote", None) is not None:
        server.secure_remote.default_inbox = new_dir
    return get_library_settings()

# Backward-compatible module-level aliases
SKIP_SYSTEM_DIRS = fs_browser.SKIP_SYSTEM_DIRS
get_allowed_roots = fs_browser.get_allowed_roots
normalize_windows_or_wsl_path = fs_browser.normalize_windows_or_wsl_path
resolve_safe_dir = fs_browser.resolve_safe_dir
iter_audio_files_safe = fs_browser.iter_audio_files_safe
get_windows_shortcuts = fs_browser.get_windows_shortcuts
validate_url = fs_browser.validate_url

queue_lock = task_manager.lock
cache_lock = metadata_cache.lock
task_queue = task_manager.queue
task_history = task_manager.history
cached_qc_results = metadata_cache.qc_results
cached_meta_results = metadata_cache.meta_results
probe_files_fast = metadata_cache.probe_files_fast
run_worker = task_manager.run_worker_loop
