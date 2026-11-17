"""Audio Library Manager: authorized yt-dlp acquisition + local QC audit.

Requires: Python 3.10+, FFmpeg (ffmpeg/ffprobe on PATH), yt-dlp for download.
No DRM bypass, authentication bypass, or streaming-service extraction.
"""
from __future__ import annotations

from swararia.core.audio_manager.constants import (  # noqa: F401
    EXTENSIONS,
    LOSSLESS,
    FIELDNAMES,
    ALLOWED_BITRATES,
    ALLOWED_LUFS_TARGETS,
    TAG_FOLDER_PATTERN,
    YOUTUBE_AUDIO_SELECTOR,
    YOUTUBE_OPUS_SELECTOR,
    STREAM_TIERS,
    YOUTUBE_PREMIUM_EXTRACTOR_ARGS,
    YOUTUBE_FALLBACK_EXTRACTOR_ARGS,
    YTDLP_AUTO_HEAL_COOLDOWN_S,
    _PERMANENT_YOUTUBE_FAILURE_MARKERS,
    _RECOVERABLE_YOUTUBE_ENGINE_MARKERS,
    _DESKTOP_CHROME_UA,
    _COOKIES_TIER_TTL_S,
)
from swararia.core.audio_manager.toolchain import (  # noqa: F401
    _setup_bundled_environment,
    _VENDOR_DIR,
    _prepend_bin_to_env,
    find_js_runtime,
    is_ytdlp_available,
    is_mutagen_available,
    require,
)
from swararia.core.audio_manager.ytdlp_engine import (  # noqa: F401
    _LAST_YTDLP_AUTO_HEAL_LOCK,
    _LAST_YTDLP_AUTO_HEAL_TS,
    should_attempt_engine_recovery,
    can_auto_update_ytdlp,
    try_claim_ytdlp_auto_heal_slot,
    update_ytdlp_preferring_nightly,
    describe_format_tier,
    sync_download_archive,
    run_ytdlp_process,
    _run_ytdlp_with_tier_log,
    get_ytdlp_base_cmd,
    build_ytdlp_command,
)
from swararia.core.audio_manager.audio_analysis import (  # noqa: F401
    get_float,
    sha256_file,
    metadata,
    loudness,
    analyze_spectral_quality,
    audit_one,
)
from swararia.core.audio_manager.yt_cookies import (  # noqa: F401
    _COOKIES_TIER_CACHE_LOCK,
    _COOKIES_TIER_CACHE,
    _parse_netscape_cookies_for_url,
    _load_cookie_header,
    _NoRedirectHandler,
    _open_url_no_redirect,
    _fetch_youtube_html_with_cookies,
    _cookie_cache_key,
    get_cached_youtube_cookies_tier,
    check_youtube_cookies_tier,
)
from swararia.core.audio_manager.yt_search import (  # noqa: F401
    _parse_music_responsive_item,
    _parse_music_card_shelf,
    _search_youtube_music_innertube,
    _search_youtube_ytdlp,
    search_youtube_music,
)
from swararia.core.audio_manager.yt_playlist import (  # noqa: F401
    _PLAYLIST_CACHE,
    extract_playlist_canonical_and_browse_ids,
    _inspect_playlist_innertube,
    _inspect_playlist_ytdlp,
    inspect_playlist,
    _STREAM_INFO_CACHE,
    get_preview_stream_info,
)
from swararia.core.audio_manager.conversion import (  # noqa: F401
    _has_mp3_xing_header,
    _select_resample_codec_args,
    _resolve_resample_output_path,
    remux_to_opus_one,
    resample_one,
    normalize_one,
)
from swararia.core.audio_manager.cli import (  # noqa: F401
    scan,
    download,
    search_cli,
    resample,
    split_cli,
    main,
)
