"""Static configuration: extensions, bitrates, yt-dlp format selectors and failure markers."""
from __future__ import annotations


EXTENSIONS = {".mp3", ".m4a", ".aac", ".opus", ".ogg", ".webm", ".flac",
              ".wav", ".aif", ".aiff", ".mka", ".wma", ".alac"}
LOSSLESS = {"flac", "alac", "wavpack", "ape", "tta", "pcm_s16le", "pcm_s24le",
            "pcm_s32le", "pcm_f32le", "pcm_f64le", "pcm_s16be", "pcm_s24be"}
FIELDNAMES = ["file", "codec", "source_class", "sample_rate_hz", "channels", "duration_s",
              "bitrate_kbps", "integrated_lufs", "lra_lu", "true_peak_dbtp",
              "spectral_status", "spectral_cutoff_khz", "spectral_label",
              "status", "notes", "sha256"]
ALLOWED_BITRATES = (320, 192, 128, 96)
ALLOWED_LUFS_TARGETS = (-14.0, -16.0)
TAG_FOLDER_PATTERN = r"\s*\[(?:44\.1kHz|48kHz|Opus|-14LUFS|-16LUFS)(?:[^\]]*)\]$"

# Priority order for YouTube & YouTube Music:
# 141 = YouTube Music Premium AAC 256 kbps (44.1 kHz, .m4a - Studio Master)
# 774 = YouTube Music Premium Opus ~256 kbps (48 kHz, .webm)
# 251 = Public High Opus ~160 kbps (48 kHz, .webm, transparent cutoff ~20 kHz)
# 140 = Standard AAC 128 kbps (44.1 kHz, .m4a)
# All selector branches strictly exclude -drc ("Stable Volume" dynamic range compression).
YOUTUBE_AUDIO_SELECTOR = (
    "ba[format_id!*=-drc][format_id=141]/"
    "ba[format_id!*=-drc][format_id=774]/"
    "ba[format_id!*=-drc][format_id=251]/"
    "ba[format_id!*=-drc][format_id=140]/"
    "ba[format_id!*=-drc]/"
    "ba"
)

# Selector prioritizing Opus (48 kHz) streams for zero-reencoding lossless remux:
YOUTUBE_OPUS_SELECTOR = (
    "ba[format_id!*=-drc][format_id=774]/"
    "ba[format_id!*=-drc][format_id=251]/"
    "ba[format_id!*=-drc][acodec=opus]/"
    "ba[format_id!*=-drc][format_id=141]/"
    "ba[format_id!*=-drc][format_id=140]/"
    "ba[format_id!*=-drc]/"
    "ba"
)

YOUTUBE_STREAM_TIERS_META = {
    "141": {
        "format_id": "141",
        "tier": "yt_premium_max",
        "label": "YouTube Music Premium Max Quality",
        "short_label": "YT PREMIUM MAX",
        "codec": "aac",
        "bitrate_kbps": 256,
        "sample_rate_hz": 44100,
        "is_premium": True,
        "is_highest_quality": True,
    },
    "774": {
        "format_id": "774",
        "tier": "yt_premium_max",
        "label": "YouTube Music Premium Max Quality",
        "short_label": "YT PREMIUM MAX",
        "codec": "opus",
        "bitrate_kbps": 256,
        "sample_rate_hz": 48000,
        "is_premium": True,
        "is_highest_quality": True,
    },
    "251": {
        "format_id": "251",
        "tier": "public_high",
        "label": "Public High Fidelity",
        "short_label": "HIGH 160k",
        "codec": "opus",
        "bitrate_kbps": 160,
        "sample_rate_hz": 48000,
        "is_premium": False,
        "is_highest_quality": False,
    },
    "140": {
        "format_id": "140",
        "tier": "standard",
        "label": "Standard Tier",
        "short_label": "STD 128k",
        "codec": "aac",
        "bitrate_kbps": 128,
        "sample_rate_hz": 44100,
        "is_premium": False,
        "is_highest_quality": False,
    },
    "250": {
        "format_id": "250",
        "tier": "low",
        "label": "Low Tier",
        "short_label": "LOW 70k",
        "codec": "opus",
        "bitrate_kbps": 70,
        "sample_rate_hz": 48000,
        "is_premium": False,
        "is_highest_quality": False,
    },
    "249": {
        "format_id": "249",
        "tier": "low",
        "label": "Low Tier",
        "short_label": "LOW 50k",
        "codec": "opus",
        "bitrate_kbps": 50,
        "sample_rate_hz": 48000,
        "is_premium": False,
        "is_highest_quality": False,
    },
    "139": {
        "format_id": "139",
        "tier": "low",
        "label": "Low Tier",
        "short_label": "LOW 48k",
        "codec": "he-aac",
        "bitrate_kbps": 48,
        "sample_rate_hz": 22050,
        "is_premium": False,
        "is_highest_quality": False,
    },
}

STREAM_TIERS = {
    "141": "★ HIGHEST QUALITY • YOUTUBE MUSIC PREMIUM — AAC 256 kbps (44.1 kHz Studio Master, .m4a)",
    "774": "★ HIGHEST QUALITY • YOUTUBE MUSIC PREMIUM — Opus ~256 kbps (48 kHz, .webm)",
    "251": "PUBLIC HIGH FIDELITY — Opus ~160 kbps (48 kHz Transparent, cutoff ~20 kHz, .webm)",
    "140": "STANDARD TIER — AAC 128 kbps (44.1 kHz, cutoff ~16 kHz, .m4a)",
    "250": "LOW TIER — Opus ~70 kbps (48 kHz, .webm)",
    "249": "LOW TIER — Opus ~50 kbps (48 kHz, .webm)",
    "139": "LOW TIER — HE-AAC ~48 kbps (.m4a)",
}

# Explicit client + GVS PO-token bypass flag needed for yt-dlp >= 2026.x when using
# YouTube Music Premium cookies (web_music / web_creator / tv_samsung expose 774 & 141).
YOUTUBE_PREMIUM_EXTRACTOR_ARGS = "youtube:player_client=web_music,web_creator,tv_samsung,web;formats=missing_pot"
YOUTUBE_FALLBACK_EXTRACTOR_ARGS = "youtube:player_client=tv_embedded,web_creator,mweb,android_vr,web;formats=missing_pot"
YTDLP_AUTO_HEAL_COOLDOWN_S = 6.0 * 3600.0

_PERMANENT_YOUTUBE_FAILURE_MARKERS = (
    "private video",
    "video unavailable",
    "has been removed",
    "copyright",
    "not available in your country",
    "premieres in",
    "live event will begin",
)
_RECOVERABLE_YOUTUBE_ENGINE_MARKERS = (
    "http error 403",
    "403: forbidden",
    "signature",
    "nsig",
    "n challenge",
    "js challenge",
    "sabr",
    "po token",
    "missing_pot",
    "requested format is not available",
    "only images are available",
    "unable to extract",
    "player api json",
    "initial data",
    "sign in to confirm",
    "reload the page",
)

_DESKTOP_CHROME_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_COOKIES_TIER_TTL_S = 300.0
