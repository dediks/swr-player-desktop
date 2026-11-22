"""System binary / cookie status and the YouTube browser-login session."""
from __future__ import annotations

import shutil
import sys

from swararia.core import audio_manager
from swararia.core import youtube_login
from swararia.server import state


def check_system_binaries(verify_cookies: bool = False) -> dict:
    """Verifies available system binaries and YouTube cookie tier status."""
    cookies_present = state.COOKIES_FILE.is_file()
    if not cookies_present:
        cookie_info = audio_manager.check_youtube_cookies_tier(None)
    elif verify_cookies:
        cookie_info = audio_manager.check_youtube_cookies_tier(state.COOKIES_FILE, force_refresh=True)
    else:
        cached = audio_manager.get_cached_youtube_cookies_tier(state.COOKIES_FILE)
        cookie_info = cached or {
            "present": True,
            "status": "unchecked",
            "tier": "unknown",
            "label": "cookies.txt (Perlu Cek)",
            "note": "Memeriksa status login & langganan YT Music Premium...",
            "checked_at": 0,
        }

    return {
        "python": sys.version.split()[0],
        "yt_dlp": audio_manager.is_ytdlp_available(),
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "ffprobe": shutil.which("ffprobe") is not None,
        "cookies_present": cookies_present,
        "cookies_status": cookie_info.get("status", "missing"),
        "cookies_tier": cookie_info.get("tier", "unknown"),
        "cookies_label": cookie_info.get("label", ""),
        "cookies_note": cookie_info.get("note", ""),
        "browser_login_available": bool(youtube_login.find_chromium_browser()[0]),
        "browser_login_name": youtube_login.find_chromium_browser()[1] or "",
        "app_version": state.APP_VERSION,
        "github_repo": state.GITHUB_REPO,
    }


login_session = youtube_login.YoutubeBrowserLoginSession(
    state.COOKIES_FILE,
    status_builder=lambda verify=False: check_system_binaries(verify_cookies=verify),
)
