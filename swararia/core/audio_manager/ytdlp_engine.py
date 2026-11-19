"""yt-dlp command building, execution, archive sync and engine auto-recovery."""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from swararia.core.audio_manager import constants, toolchain, yt_cookies


_LAST_YTDLP_AUTO_HEAL_LOCK = threading.Lock()
_LAST_YTDLP_AUTO_HEAL_TS: float = 0.0


def should_attempt_engine_recovery(error_text: str | None) -> bool:
    """Returns True for YouTube cipher/n-challenge/SABR/PO-token/403 extraction failures."""
    if not isinstance(error_text, str):
        return False
    normalized = error_text.strip().lower()
    if not normalized:
        return False
    if any(marker in normalized for marker in constants._PERMANENT_YOUTUBE_FAILURE_MARKERS):
        return False
    return any(marker in normalized for marker in constants._RECOVERABLE_YOUTUBE_ENGINE_MARKERS)


def can_auto_update_ytdlp(last_attempt_ts: float, now_ts: float) -> bool:
    """Limits automatic yt-dlp upgrades to at most once per YTDLP_AUTO_HEAL_COOLDOWN_S."""
    return last_attempt_ts <= 0.0 or (now_ts - last_attempt_ts) >= constants.YTDLP_AUTO_HEAL_COOLDOWN_S


def try_claim_ytdlp_auto_heal_slot(now_ts: float | None = None) -> bool:
    """Atomically reserves the auto-heal update slot if cooldown has elapsed."""
    global _LAST_YTDLP_AUTO_HEAL_TS
    current = time.monotonic() if now_ts is None else now_ts
    with _LAST_YTDLP_AUTO_HEAL_LOCK:
        if not can_auto_update_ytdlp(_LAST_YTDLP_AUTO_HEAL_TS, current):
            return False
        _LAST_YTDLP_AUTO_HEAL_TS = current
        return True


def update_ytdlp_preferring_nightly(
    env: dict[str, str] | None = None,
    on_log: Callable[[str], None] | None = None,
) -> bool:
    """Upgrades yt-dlp favoring Nightly, falling back to Stable, then pip when unfrozen."""
    log_fn = on_log or (lambda msg: print(msg, flush=True))
    base_cmd = list(get_ytdlp_base_cmd())
    candidates: list[tuple[str, list[str]]] = [
        ("nightly", [*base_cmd, "--update-to", "nightly"]),
        ("stable", [*base_cmd, "--update-to", "stable"]),
    ]
    if not getattr(sys, "frozen", False):
        candidates.append(
            ("pip-pre", [sys.executable, "-m", "pip", "install", "-U", "--pre", "yt-dlp[default]", "yt-dlp-ejs"])
        )

    for label, cmd in candidates:
        try:
            proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
            if proc.returncode == 0:
                log_fn(f"[ENGINE AUTO-HEAL] Pembaruan yt-dlp ({label}) berhasil.")
                return True
        except Exception:
            continue
    log_fn("[ENGINE AUTO-HEAL] Pembaruan yt-dlp gagal atau tidak tersedia; melanjutkan dengan klien cadangan.")
    return False


def classify_format_tier(format_id: str) -> dict:
    """Classifies a yt-dlp format ID into structured bitrate and YouTube Premium quality metadata."""
    clean_id = (format_id or "").strip()
    is_drc = "-drc" in clean_id
    audio_id = clean_id.split("+")[-1].strip() if "+" in clean_id else clean_id
    base_id = audio_id[:-4] if audio_id.endswith("-drc") else audio_id
    meta = constants.YOUTUBE_STREAM_TIERS_META.get(base_id)
    if meta and not is_drc:
        return dict(meta)
    if is_drc:
        return {
            "format_id": clean_id,
            "tier": "drc",
            "label": "Dynamic Range Compressed (DRC)",
            "short_label": "DRC",
            "codec": (meta or {}).get("codec", ""),
            "bitrate_kbps": (meta or {}).get("bitrate_kbps"),
            "sample_rate_hz": (meta or {}).get("sample_rate_hz"),
            "is_premium": False,
            "is_highest_quality": False,
            "is_drc": True,
        }
    return {
        "format_id": clean_id,
        "tier": "other",
        "label": "Audio stream terpilih",
        "short_label": clean_id or "AUDIO",
        "codec": "",
        "bitrate_kbps": None,
        "sample_rate_hz": None,
        "is_premium": False,
        "is_highest_quality": False,
        "is_drc": False,
    }


def describe_format_tier(format_id: str) -> str:
    clean_id = format_id.strip()
    if clean_id in constants.STREAM_TIERS:
        return f"[STREAM TIER] Format {clean_id}: {constants.STREAM_TIERS[clean_id]}"
    if "-drc" in clean_id:
        return f"[STREAM TIER] Format {clean_id}: ⚠️ WARNING — Dynamic Range Compressed (DRC / Stable Volume) stream"
    return f"[STREAM TIER] Format {clean_id}: Audio stream terpilih"


def sync_download_archive(
    inbox_dir: Path,
    archive_file: Path,
    audio_format: str = "native",
    keep_opus: bool = False,
) -> int:
    """Prunes downloaded_ids.txt entries whose target file(s) do not exist in inbox_dir.

    This ensures that:
    1) If the user deleted a folder/song from inbox_dir, re-downloading works immediately.
    2) If the user previously downloaded .webm (native) and now downloads MP3 (or vice versa),
       yt-dlp is not blocked by the old archive entry.
    """
    if not archive_file.is_file():
        return 0

    mp3_ids: set[str] = set()
    opus_ids: set[str] = set()
    aac_ids: set[str] = set()
    any_ids: set[str] = set()

    if inbox_dir.is_dir():
        for p in inbox_dir.rglob("*"):
            if p.is_file() and p.suffix.lower() in constants.EXTENSIONS and not p.name.startswith("."):
                m = re.search(r"\[([A-Za-z0-9_-]{11})\]$", p.stem)
                if not m:
                    continue
                vid = m.group(1)
                ext = p.suffix.lower()
                any_ids.add(vid)
                if ext == ".mp3":
                    mp3_ids.add(vid)
                elif ext in (".webm", ".opus", ".ogg"):
                    opus_ids.add(vid)
                elif ext in (".m4a", ".aac"):
                    aac_ids.add(vid)

    if audio_format in ("mp3", "mp3_320k", "mp3_vbr0"):
        valid_ids = (mp3_ids & opus_ids) if keep_opus else mp3_ids
    elif audio_format == "opus":
        valid_ids = opus_ids | aac_ids
    else:
        valid_ids = any_ids

    try:
        lines = archive_file.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return 0

    kept_lines: list[str] = []
    removed_count = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split()
        vid = parts[-1] if parts else ""
        if vid in valid_ids:
            kept_lines.append(stripped)
        else:
            removed_count += 1

    if removed_count > 0:
        try:
            content = ("\n".join(kept_lines) + "\n") if kept_lines else ""
            archive_file.write_text(content, encoding="utf-8")
        except OSError:
            pass

    return removed_count


def run_ytdlp_process(
    cmd: list[str],
    env: dict[str, str] | None = None,
    existing_by_vid: dict[str, str] | None = None,
    on_log: Callable[[str], None] | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> dict:
    """Executes yt-dlp subprocess and parses tier info, skipped files, cookie errors, and progress."""
    skipped_files: list[str] = []
    last_error_line = ""
    cookie_err_detected = False
    engine_signal_detected = False
    playlist_item = 1
    playlist_total = 1
    selected_format_id = ""
    stream_tier: dict = {}
    video_formats: dict[str, dict] = {}
    vid_map = existing_by_vid or {}
    log_fn = on_log or (lambda msg: print(msg, flush=True))

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True,
        env=env,
    )
    if proc.stdout:
        for line in proc.stdout:
            clean_line = line.strip()
            if not clean_line:
                continue
            log_fn(clean_line)
            lower_line = clean_line.lower()

            if any(
                k in lower_line
                for k in (
                    "could not copy",
                    "cookie database",
                    "database is locked",
                    "failed to extract cookies",
                    "no longer valid",
                    "sign in to confirm",
                )
            ):
                cookie_err_detected = True

            if any(m in lower_line for m in constants._RECOVERABLE_YOUTUBE_ENGINE_MARKERS):
                engine_signal_detected = True

            if "ERROR:" in clean_line:
                last_error_line = clean_line.split("ERROR:", 1)[-1].strip()

            arch_match = re.search(
                r"\[download\]\s+([A-Za-z0-9_-]{11}):\s+has already been recorded in the archive",
                clean_line,
            )
            if arch_match:
                vid = arch_match.group(1)
                fname = vid_map.get(vid, f"Video ID [{vid}]")
                if fname not in skipped_files:
                    skipped_files.append(fname)
                log_fn(f"[SKIPPED] File sudah ada di Library: {fname}")

            disk_match = re.search(r"\[download\]\s+(.+?)\s+has already been downloaded", clean_line)
            if disk_match:
                fname = Path(disk_match.group(1)).name
                if fname not in skipped_files:
                    skipped_files.append(fname)
                log_fn(f"[SKIPPED] File sudah ada di Library: {fname}")

            fmt_match = re.search(
                r"(?:([A-Za-z0-9_-]{11}):\s+)?Downloading \d+ format\(s\):\s*([^\s,]+)",
                clean_line,
            )
            if fmt_match:
                fmt_vid = fmt_match.group(1) or ""
                fmt_id = fmt_match.group(2).strip()
                tier_info = classify_format_tier(fmt_id)
                selected_format_id = fmt_id
                stream_tier = tier_info
                if fmt_vid:
                    video_formats[fmt_vid] = tier_info
                log_fn(describe_format_tier(fmt_id))

            if on_progress is not None:
                item_match = re.search(r"\[download\]\s+Downloading item\s+(\d+)\s+of\s+(\d+)", clean_line)
                if item_match:
                    playlist_item = int(item_match.group(1))
                    playlist_total = max(1, int(item_match.group(2)))

                prog_match = re.search(r"\[download\]\s+([\d\.]+)%", clean_line)
                if prog_match:
                    try:
                        song_pct = float(prog_match.group(1))
                        if playlist_total > 1:
                            overall_pct = ((playlist_item - 1) + (song_pct / 100.0)) / playlist_total * 90.0
                        else:
                            overall_pct = song_pct * 0.90
                        on_progress(min(90.0, max(0.0, round(overall_pct, 1))))
                    except ValueError:
                        pass

    returncode = proc.wait()
    last_lower = last_error_line.lower()
    has_permanent = any(m in last_lower for m in constants._PERMANENT_YOUTUBE_FAILURE_MARKERS)
    engine_recovery_needed = bool(
        returncode != 0
        and (
            should_attempt_engine_recovery(last_error_line)
            or (engine_signal_detected and not has_permanent)
        )
    )
    return {
        "returncode": returncode,
        "skipped_files": skipped_files,
        "last_error_line": last_error_line,
        "cookie_err_detected": cookie_err_detected,
        "engine_recovery_needed": engine_recovery_needed,
        "selected_format_id": selected_format_id,
        "stream_tier": stream_tier,
        "video_formats": video_formats,
        "is_youtube_premium": bool(stream_tier.get("is_premium")),
        "is_highest_quality": bool(stream_tier.get("is_highest_quality")),
        "expected_bitrate_kbps": stream_tier.get("bitrate_kbps"),
    }


def _run_ytdlp_with_tier_log(cmd: list[str], env: dict[str, str] | None = None) -> int:
    return run_ytdlp_process(cmd, env=env)["returncode"]


def get_ytdlp_base_cmd() -> list[str]:
    """Returns the base command to run yt-dlp, preferring system binary or bundled/vendored package."""
    which_ytdlp = shutil.which("yt-dlp")
    # If a standalone system yt-dlp exists outside .vendor, use it directly
    if which_ytdlp and not str(Path(which_ytdlp).resolve()).startswith(str(toolchain._VENDOR_DIR.resolve())):
        return [which_ytdlp]
    try:
        import yt_dlp  # noqa: F401
        return [sys.executable, "-m", "yt_dlp"]
    except ImportError:
        pass
    if (toolchain._VENDOR_DIR / "yt_dlp").is_dir():
        return [sys.executable, "-m", "yt_dlp"]
    return [which_ytdlp] if which_ytdlp else ["yt-dlp"]


def build_ytdlp_command(
    url: str,
    output_template: str,
    archive_file: Path,
    audio_format: str = "opus",
    keep_opus: bool = False,
    is_playlist: bool = False,
    cookies_file: Path | None = None,
    cookie_browser: str | None = None,
    js_runtime: str | None = None,
    premium_active: bool | None = None,
    fallback_clients: bool = False,
) -> tuple[list[str], str]:
    """Builds the yt-dlp command list and a human-readable target description."""
    norm_format = "opus" if audio_format in ("opus", "premium", "premium_aac") else audio_format
    if norm_format == "mp3":
        norm_format = "mp3_320k"

    cmd = list(get_ytdlp_base_cmd())
    thumb_args = ["--embed-thumbnail", "--convert-thumbnails", "jpg"] if toolchain.is_mutagen_available() else []

    if norm_format == "opus":
        # Prioritize 774 (Premium Opus 256k) then 251 (Public High Opus 160k),
        # extract to .opus via lossless stream copy (-c:a copy), and embed metadata
        cmd.extend([
            "-f", f"{constants.YOUTUBE_OPUS_SELECTOR}/b",
            "--extract-audio",
            "--audio-format", "opus",
            "--embed-metadata",
            *thumb_args,
        ])
    elif norm_format == "native":
        # Audio-only stream, kept as-is. Prioritize Premium 256k (141 AAC / 774 Opus),
        # then Public 160k 48kHz Opus (251), 128k AAC (140), and non-DRC streams.
        cmd.extend([
            "-f", constants.YOUTUBE_AUDIO_SELECTOR,
            "--embed-metadata",
            *thumb_args,
        ])
    elif keep_opus:
        cmd.extend([
            "-f", f"{constants.YOUTUBE_OPUS_SELECTOR}/b",
            "--embed-metadata",
            *thumb_args,
        ])
    else:
        cmd.extend([
            "-f", f"{constants.YOUTUBE_AUDIO_SELECTOR}/b",
            "--embed-metadata",
            *thumb_args,
        ])

    # 1. Performance: Multi-threaded fragments (3x-5x faster download) & network timeout
    cmd.extend([
        "--concurrent-fragments", "4",
        "--socket-timeout", "30",
        "--retries", "5",
        "--fragment-retries", "5",
    ])

    if js_runtime:
        cmd.extend(["--js-runtimes", js_runtime])
    cmd.extend(["--remote-components", "ejs:github"])

    cmd.extend([
        "--download-archive", str(archive_file),
        "-o", output_template,
        "--yes-playlist" if is_playlist else "--no-playlist",
    ])

    auth_desc = "Public"
    effective_premium = False
    if cookies_file and cookies_file.is_file():
        cmd.extend(["--cookies", str(cookies_file)])
        if premium_active is None:
            cached_tier = yt_cookies.get_cached_youtube_cookies_tier(cookies_file)
            effective_premium = bool(cached_tier and cached_tier.get("tier") == "premium")
            tier_label = (cached_tier or {}).get("label")
            auth_desc = f"cookies.txt ({tier_label})" if tier_label else "cookies.txt"
        elif premium_active:
            effective_premium = True
            auth_desc = "cookies.txt (YT Music Premium)"
        else:
            auth_desc = "cookies.txt (Standar)"
    elif cookie_browser:
        cmd.extend(["--cookies-from-browser", cookie_browser])
        effective_premium = premium_active is not False
        auth_desc = f"{cookie_browser} (Premium)" if effective_premium else cookie_browser

    if effective_premium:
        cmd.extend(["--extractor-args", constants.YOUTUBE_PREMIUM_EXTRACTOR_ARGS])
    elif fallback_clients:
        cmd.extend(["--extractor-args", constants.YOUTUBE_FALLBACK_EXTRACTOR_ARGS])

    opus_note = " + Simpan File Asli Opus (.webm)" if keep_opus else ""
    tier_tag = "~256 kbps ★ YT Premium Highest Quality" if effective_premium else "~160 kbps Publik"
    if norm_format == "opus":
        desc = f"Target: Opus Best Quality (.opus) — {tier_tag}, Lossless Remux 48 kHz (Auth: {auth_desc}, Anti-DRC: Active, Multi-thread: 4x)"
    elif norm_format == "mp3_320k":
        cmd.extend([
            "--extract-audio",
            "--audio-format", "mp3",
            "--audio-quality", "320K",
            "--embed-metadata",
            *thumb_args,
        ])
        if keep_opus:
            cmd.append("--keep-video")
        desc = f"Target: MP3 320 kbps CBR{opus_note} (Sumber: {tier_tag}, KONVERSI lossy, Auth: {auth_desc}, Multi-thread: 4x)"
    elif norm_format == "mp3_vbr0":
        cmd.extend([
            "--extract-audio",
            "--audio-format", "mp3",
            "--audio-quality", "0",
            "--embed-metadata",
            *thumb_args,
        ])
        if keep_opus:
            cmd.append("--keep-video")
        desc = f"Target: MP3 VBR 0 (~245 kbps){opus_note} (Sumber: {tier_tag}, KONVERSI lossy, Auth: {auth_desc}, Multi-thread: 4x)"
    else:
        # Native: no postprocessing at all (no extract/convert/remux/metadata embed).
        native_tier = "256 kbps AAC 141 / Opus 774 ★ YT Premium Highest Quality" if effective_premium else "~160 kbps Opus 251 / 128 kbps AAC 140"
        desc = f"Target: Native Raw Stream ({native_tier}) - stream asli YouTube tanpa konversi (Auth: {auth_desc}, Anti-DRC: Active, Multi-thread: 4x)"

    clean_url = url
    if "list=VL" in clean_url:
        clean_url = re.sub(r"([?&]list=)VL([a-zA-Z0-9_\-]+)", r"\1\2", clean_url)
    cmd.append(clean_url)
    return cmd, desc
