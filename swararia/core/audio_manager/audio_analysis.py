"""ffprobe metadata, EBU R128 loudness, spectral quality and per-file QC audit."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

from swararia.core.audio_manager import constants


def get_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def sha256_file(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def classify_audio_quality_tier(
    codec: str = "",
    bitrate_kbps: float | str | None = None,
    ext: str = "",
    spectral_status: str = "",
    source_format_id: str = "",
) -> dict:
    """Classifies a local audio file's quality tier and YouTube Premium highest-quality status."""
    codec_low = (codec or "").strip().lower()
    ext_low = (ext or "").strip().lower()
    spec_upper = (spectral_status or "").strip().upper()
    src_fmt = (source_format_id or "").strip()
    br_val = get_float(bitrate_kbps)
    br_round = round(br_val) if br_val and br_val > 0 else None

    is_lossless = (
        codec_low in constants.LOSSLESS
        or codec_low.startswith("pcm_")
        or ext_low in (".flac", ".wav", ".alac", ".aif", ".aiff")
    )
    if is_lossless:
        return {
            "quality_tier": "lossless",
            "quality_label": "LOSSLESS",
            "bitrate_kbps": br_round,
            "is_youtube_premium": False,
            "is_highest_quality": True,
            "quality_verified_source": False,
            "source_format_id": src_fmt,
        }

    if spec_upper == "FAKE_HQ":
        return {
            "quality_tier": "fake_hq",
            "quality_label": "FAKE HQ",
            "bitrate_kbps": br_round,
            "is_youtube_premium": False,
            "is_highest_quality": False,
            "quality_verified_source": False,
            "source_format_id": src_fmt,
        }

    is_native_yt_container = ext_low in (".opus", ".webm", ".ogg", ".m4a", ".aac") or codec_low in ("opus", "aac")
    src_meta = constants.YOUTUBE_STREAM_TIERS_META.get(src_fmt) if (src_fmt and is_native_yt_container) else None
    if src_meta:
        is_prem = bool(src_meta.get("is_premium"))
        is_max = bool(src_meta.get("is_highest_quality"))
        return {
            "quality_tier": str(src_meta.get("tier") or "other"),
            "quality_label": str(src_meta.get("short_label") or ""),
            "bitrate_kbps": br_round or src_meta.get("bitrate_kbps"),
            "is_youtube_premium": is_prem,
            "is_highest_quality": is_max,
            "quality_verified_source": True,
            "source_format_id": src_fmt,
        }

    br_num = float(br_val or 0.0)
    is_opus = codec_low == "opus" or ext_low in (".opus", ".webm")
    is_aac = codec_low == "aac" or ext_low in (".m4a", ".aac")
    if (is_opus and br_num >= 195.0) or (is_aac and br_num >= 220.0):
        return {
            "quality_tier": "yt_premium_max",
            "quality_label": "YT PREMIUM MAX",
            "bitrate_kbps": br_round,
            "is_youtube_premium": True,
            "is_highest_quality": True,
            "quality_verified_source": False,
            "source_format_id": src_fmt,
        }

    if br_num >= 192.0:
        return {
            "quality_tier": "hq",
            "quality_label": f"HQ {br_round}k",
            "bitrate_kbps": br_round,
            "is_youtube_premium": False,
            "is_highest_quality": False,
            "quality_verified_source": False,
            "source_format_id": src_fmt,
        }

    if br_num >= 135.0:
        return {
            "quality_tier": "standard",
            "quality_label": f"{'PUB' if is_opus else 'STD'} {br_round}k",
            "bitrate_kbps": br_round,
            "is_youtube_premium": False,
            "is_highest_quality": False,
            "quality_verified_source": False,
            "source_format_id": src_fmt,
        }

    if br_num > 0.0:
        return {
            "quality_tier": "low",
            "quality_label": f"LOW {br_round}k",
            "bitrate_kbps": br_round,
            "is_youtube_premium": False,
            "is_highest_quality": False,
            "quality_verified_source": False,
            "source_format_id": src_fmt,
        }

    return {
        "quality_tier": "unknown",
        "quality_label": "",
        "bitrate_kbps": None,
        "is_youtube_premium": False,
        "is_highest_quality": False,
        "quality_verified_source": False,
        "source_format_id": src_fmt,
    }


def metadata(path: Path) -> dict:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(proc.stdout)
    track = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if track is None:
        raise ValueError("Tidak ada stream audio")
    fmt = data.get("format", {})
    bitrate = get_float(track.get("bit_rate") or fmt.get("bit_rate"))
    duration_s = get_float(track.get("duration") or fmt.get("duration"))
    # Containers like WebM/Opus often omit stream/format bit_rate; derive from file size.
    if (not bitrate or bitrate <= 0) and duration_s and duration_s >= 1.0:
        try:
            size_bytes = path.stat().st_size
            if size_bytes > 0:
                bitrate = (size_bytes * 8.0) / duration_s
        except OSError:
            pass
    return {
        "codec": track.get("codec_name", "unknown"),
        "sample_rate_hz": track.get("sample_rate", ""),
        "channels": track.get("channels", ""),
        "duration_s": duration_s,
        "bitrate_kbps": round(bitrate / 1000, 1) if bitrate else "",
    }


def loudness(path: Path):
    # Does not modify or normalize the source. FFmpeg fully decodes and scans it.
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-xerror", "-i", str(path),
         "-map", "0:a:0", "-af", "ebur128=peak=true", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError("Decode / analisis FFmpeg gagal: " + proc.stderr[-350:].replace("\n", " "))
    if "Summary:" not in proc.stderr:
        raise RuntimeError("FFmpeg tidak menghasilkan ringkasan EBU R128")
    summary = proc.stderr.rsplit("Summary:", 1)[-1]

    def grab(pattern):
        matched = re.search(pattern, summary, flags=re.MULTILINE | re.IGNORECASE)
        return get_float(matched.group(1)) if matched else None

    integrated = grab(r"^\s*I:\s*(-?[\d.]+)\s*LUFS")
    lra = grab(r"^\s*LRA:\s*(-?[\d.]+)\s*LU\b")
    true_peak = grab(r"True peak:\s*\n\s*Peak:\s*(-?[\d.]+)\s*dBFS")
    return integrated, lra, true_peak


def analyze_spectral_quality(
    path: Path,
    codec: str = "",
    bitrate_kbps: float = 0.0,
    sample_rate_hz: int = 48000,
    duration_s: float = 0.0,
) -> dict:
    """Fast multi-band spectral high-frequency cutoff analyzer to detect fake/upsampled audio.

    Measures energy in 3 steep Butterworth filter bands over a 15s active window:
    - Mid band (400 - 6000 Hz)
    - High band (14.5 - 16.8 kHz)
    - Ultra-High Air band (> 17.2 kHz)
    """
    if sample_rate_hz and int(sample_rate_hz) < 32000:
        return {
            "spectral_status": "LOW_BW",
            "spectral_cutoff_khz": round(int(sample_rate_hz) / 2000, 1),
            "spectral_label": f"<{round(int(sample_rate_hz) / 2000)} kHz (Low SR)",
        }

    fc = (
        "[0:a:0]asplit=3[m][h][u];"
        "[m]highpass=f=400,lowpass=f=6000,astats=measure_perchannel=none[m_out];"
        "[h]highpass=f=14500,highpass=f=14500,highpass=f=14500,lowpass=f=16800,astats=measure_perchannel=none[h_out];"
        "[u]highpass=f=17200,highpass=f=17200,highpass=f=17200,highpass=f=17200,astats=measure_perchannel=none[u_out]"
    )

    seek_sec = 0.0
    if duration_s and duration_s > 35.0:
        seek_sec = min(40.0, max(10.0, round(duration_s * 0.25, 1)))
    elif duration_s and duration_s > 12.0:
        seek_sec = round(duration_s * 0.2, 1)

    cmd = ["ffmpeg", "-hide_banner", "-nostats"]
    if seek_sec > 0:
        cmd.extend(["-ss", str(seek_sec)])
    cmd.extend([
        "-t", "15",
        "-i", str(path),
        "-filter_complex", fc,
        "-map", "[m_out]", "-f", "null", "-",
        "-map", "[h_out]", "-f", "null", "-",
        "-map", "[u_out]", "-f", "null", "-",
    ])

    proc = subprocess.run(cmd, capture_output=True, text=True)
    blocks: dict[int, dict[str, float]] = {}
    for line in proc.stderr.splitlines():
        m = re.search(r"Parsed_astats_(\d+)", line)
        if not m:
            continue
        idx = int(m.group(1))
        b = blocks.setdefault(idx, {})
        if "RMS level dB:" in line:
            val = get_float(line.split("RMS level dB:")[-1].strip())
            if val is not None:
                b["rms"] = val
        elif "Peak level dB:" in line:
            val = get_float(line.split("Peak level dB:")[-1].strip())
            if val is not None:
                b["peak"] = val

    ordered = [blocks[k] for k in sorted(blocks)]
    if len(ordered) < 3:
        return {
            "spectral_status": "UNKNOWN",
            "spectral_cutoff_khz": 0,
            "spectral_label": "Spectrum N/A",
        }

    mid_rms = ordered[0].get("rms", -100.0)
    mid_peak = ordered[0].get("peak", -100.0)
    high_rms = ordered[1].get("rms", -120.0)
    high_peak = ordered[1].get("peak", -120.0)
    ultra_rms = ordered[2].get("rms", -120.0)
    ultra_peak = ordered[2].get("peak", -120.0)

    ultra_rel_rms = ultra_rms - mid_rms
    ultra_rel_peak = ultra_peak - mid_peak
    high_rel_rms = high_rms - mid_rms
    high_rel_peak = high_peak - mid_peak

    if ultra_rms > -86.0 and (ultra_rel_rms > -58.0 or ultra_rel_peak > -54.0):
        cutoff_khz = 20
    elif high_rms > -82.0 and (high_rel_rms > -54.0 or high_rel_peak > -50.0):
        cutoff_khz = 16
    else:
        cutoff_khz = 12

    codec_low = (codec or "").lower()
    br = float(bitrate_kbps or 0.0)
    claims_hq = (codec_low in constants.LOSSLESS) or (codec_low == "mp3" and br >= 245.0) or (codec_low == "aac" and br >= 220.0)

    if cutoff_khz >= 20:
        return {
            "spectral_status": "AUTHENTIC",
            "spectral_cutoff_khz": 20,
            "spectral_label": "20 kHz Full-Range",
        }
    if cutoff_khz == 16:
        if claims_hq:
            return {
                "spectral_status": "FAKE_HQ",
                "spectral_cutoff_khz": 16,
                "spectral_label": "⚠️ 16 kHz (Upsampled 128k)",
            }
        return {
            "spectral_status": "STANDARD",
            "spectral_cutoff_khz": 16,
            "spectral_label": "16 kHz Standard",
        }

    if claims_hq or br >= 190.0:
        return {
            "spectral_status": "FAKE_HQ",
            "spectral_cutoff_khz": 12,
            "spectral_label": "⚠️ <14 kHz (Fake HQ)",
        }
    return {
        "spectral_status": "LOW_BW",
        "spectral_cutoff_khz": 12,
        "spectral_label": "<14 kHz (Low-BW)",
    }


def audit_one(path: Path) -> dict:
    row = {key: "" for key in constants.FIELDNAMES}
    row["file"] = str(path)
    try:
        details = metadata(path)
        row.update(details)
        row["source_class"] = "lossless codec" if details["codec"] in constants.LOSSLESS else "lossy / other codec"
        integrated, lra, true_peak = loudness(path)
        row["integrated_lufs"] = integrated if integrated is not None else ""
        row["lra_lu"] = lra if lra is not None else ""
        row["true_peak_dbtp"] = true_peak if true_peak is not None else ""
        spec = analyze_spectral_quality(
            path,
            codec=str(details.get("codec") or ""),
            bitrate_kbps=float(details.get("bitrate_kbps") or 0.0),
            sample_rate_hz=int(details.get("sample_rate_hz") or 48000),
            duration_s=float(details.get("duration_s") or 0.0),
        )
        row.update(spec)
        flags = []
        if true_peak is None:
            flags.append("TRUE_PEAK_UNAVAILABLE")
        elif true_peak > 0:
            flags.append("TRUE_PEAK_ABOVE_0")
        elif true_peak > -1:
            flags.append("LOW_TRUE_PEAK_HEADROOM")
        if spec.get("spectral_status") == "FAKE_HQ":
            flags.append("FAKE_HQ_UPSAMPLED")
        if details["channels"] != 2:
            flags.append("CHECK_CHANNEL_LAYOUT")
        if integrated is None:
            flags.append("LUFS_UNAVAILABLE")
        row["status"] = "REVIEW" if flags else "MEASURED"
        row["notes"] = "; ".join(flags)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        row["status"] = "ERROR"
        row["notes"] = str(exc)[:350]
    return row
