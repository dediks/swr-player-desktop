"""Resampling, lossless remux and loudness normalization of audio files."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from swararia.core.audio_manager import audio_analysis, constants, toolchain


def _has_mp3_xing_header(path: Path) -> bool:
    """Checks if an MP3 file contains a Xing/Info VBR header in the first 8 KB."""
    try:
        with path.open("rb") as fh:
            head_bytes = fh.read(8192)
            return b"Xing" in head_bytes or b"Info" in head_bytes
    except OSError:
        return False


def _select_resample_codec_args(
    ext: str,
    codec: str,
    target_rate: int,
    target_bitrate: int,
) -> tuple[str, list[str]]:
    """Determines the target file extension and FFmpeg audio encoder arguments."""
    mp3_codec_args = [
        "-c:a", "libmp3lame",
        "-b:a", f"{target_bitrate}k",
        "-ac", "2",
        "-write_xing", "0",
        "-id3v2_version", "3",
    ]
    if ext in (".webm", ".opus", ".ogg") or codec == "opus":
        if target_rate == 44100 or target_bitrate != 320:
            return ".mp3", mp3_codec_args
        return (".opus" if ext != ".webm" else ".webm"), ["-c:a", "libopus", "-b:a", "256k", "-ac", "2"]
    if ext == ".mp3" or codec == "mp3":
        return ".mp3", mp3_codec_args
    if ext in (".m4a", ".aac"):
        if target_bitrate != 320:
            return ".mp3", mp3_codec_args
        return ext, ["-c:a", "aac", "-b:a", "256k", "-ac", "2"]
    if ext == ".flac" or codec == "flac":
        if target_bitrate != 320:
            return ".mp3", mp3_codec_args
        return ".flac", ["-c:a", "flac", "-ac", "2"]
    if ext in (".wav", ".aif", ".aiff"):
        if target_bitrate != 320:
            return ".mp3", mp3_codec_args
        pcm_codec = codec if codec.startswith("pcm_") else "pcm_s16le"
        return ext, ["-c:a", pcm_codec, "-ac", "2"]
    return ".mp3", mp3_codec_args


def _resolve_resample_output_path(
    path: Path,
    target_ext: str,
    tag_label: str,
    overwrite: bool,
    out_dir: Path | None,
) -> Path:
    """Resolves the destination file path for resample_one while preserving folder organization."""
    if overwrite:
        return path.with_suffix(target_ext)
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir / f"{path.stem}{target_ext}"
    if path.parent.name and path.parent.name != "inbox" and path.parent.parent.name == "inbox":
        base_folder = re.sub(constants.TAG_FOLDER_PATTERN, "", path.parent.name)
        dest_dir = path.parent.parent / f"{base_folder} [{tag_label}]"
        dest_dir.mkdir(parents=True, exist_ok=True)
        return dest_dir / f"{path.stem}{target_ext}"
    if path.parent.name == "inbox":
        dest_dir = path.parent / f"Converted [{tag_label}]"
        dest_dir.mkdir(parents=True, exist_ok=True)
        return dest_dir / f"{path.stem}{target_ext}"
    clean_stem = re.sub(constants.TAG_FOLDER_PATTERN, "", path.stem)
    return path.with_name(f"{clean_stem} [{tag_label}]{target_ext}")


def remux_to_opus_one(
    path: Path,
    overwrite: bool = False,
    out_dir: Path | None = None,
) -> dict:
    """Losslessly remuxes an Opus stream (e.g. from .webm or .ogg) into a standard .opus container (-c:a copy).

    Does NOT re-encode audio data (0% quality degradation, bit-exact stream copy).
    If the input file is already .opus or does not contain an Opus audio stream, it is safely skipped.
    """
    toolchain.require("ffmpeg")
    toolchain.require("ffprobe")

    ext = path.suffix.lower()
    info = audio_analysis.metadata(path)
    codec = info.get("codec", "")
    current_rate = int(info.get("sample_rate_hz") or 48000)
    current_br = float(info.get("bitrate_kbps") or 0)

    if ext == ".opus":
        return {
            "status": "SKIPPED",
            "file": str(path),
            "old_rate": current_rate,
            "new_rate": current_rate,
            "old_bitrate": round(current_br) if current_br else "",
            "new_bitrate": round(current_br) if current_br else "",
            "message": "Sudah berformat .opus (dilewati)",
        }

    if codec != "opus":
        return {
            "status": "SKIPPED",
            "file": str(path),
            "old_rate": current_rate,
            "new_rate": current_rate,
            "old_bitrate": round(current_br) if current_br else "",
            "new_bitrate": round(current_br) if current_br else "",
            "message": f"Bukan stream Opus (codec: {codec or ext}), dilewati agar tidak terjadi re-encode",
        }

    out_path = _resolve_resample_output_path(path, ".opus", "Opus", overwrite, out_dir)

    if not overwrite and out_path != path and out_path.is_file():
        return {
            "status": "SKIPPED",
            "file": str(out_path),
            "old_rate": current_rate,
            "new_rate": current_rate,
            "old_bitrate": round(current_br) if current_br else "",
            "new_bitrate": round(current_br) if current_br else "",
            "message": f"Hasil .opus sudah ada ({out_path.parent.name}/{out_path.name})",
        }

    tmp_path = out_path.with_name(f".{out_path.stem}.remux_tmp.opus")
    try:
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-nostats", "-xerror",
            "-i", str(path),
            "-map", "0:a:0",
            "-map_metadata", "0",
            "-c:a", "copy",
            str(tmp_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError("FFmpeg lossless remux gagal: " + proc.stderr[-300:].replace("\n", " "))

        new_info = audio_analysis.metadata(tmp_path)
        tmp_path.replace(out_path)
        if overwrite and out_path != path and path.exists():
            path.unlink()

        dest_display = f"{out_path.parent.name}/{out_path.name}" if out_path.parent != path.parent else out_path.name
        new_br = float(new_info.get("bitrate_kbps") or current_br)
        return {
            "status": "CONVERTED",
            "file": str(out_path),
            "old_file": str(path),
            "overwritten": overwrite,
            "old_rate": current_rate,
            "new_rate": int(new_info.get("sample_rate_hz") or current_rate),
            "old_bitrate": round(current_br) if current_br else "",
            "new_bitrate": round(new_br) if new_br else "",
            "codec": "opus",
            "message": f"Lossless Remux {ext} -> .opus ({current_rate} Hz, -c:a copy) -> {dest_display}",
        }
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def resample_one(
    path: Path,
    target_rate: int = 44100,
    target_bitrate: int = 320,
    overwrite: bool = False,
    out_dir: Path | None = None,
    normalize_lufs: float | None = None,
) -> dict:
    """Resamples an existing audio file to target_rate (44100/48000 Hz) and target_bitrate (320/192/128/96 kbps).

    Optionally applies EBU R128 loudness normalization (e.g. -14.0 or -16.0 LUFS with -1.5 dBTP True Peak limit).
    By default (overwrite=False), never overwrites the original file:
    - If inside a playlist folder (e.g. 'Top songs/Lagu.mp3'), writes to
      'Top songs [44.1kHz]/Lagu.mp3' or 'Top songs [44.1kHz 192k]/Lagu.mp3'.
    - Otherwise writes to 'Converted [...]/Lagu.mp3' or 'Lagu [...].mp3'.
    Uses FFmpeg SoXR 28-bit high-precision resampler (with standard fallback).
    """
    toolchain.require("ffmpeg")
    toolchain.require("ffprobe")
    if target_rate not in (44100, 48000):
        raise ValueError("Target sample rate hanya mendukung 44100 atau 48000 Hz.")
    if target_bitrate not in constants.ALLOWED_BITRATES:
        raise ValueError(f"Target bitrate hanya mendukung {constants.ALLOWED_BITRATES} kbps.")
    if normalize_lufs is not None:
        normalize_lufs = float(normalize_lufs)
        if normalize_lufs not in constants.ALLOWED_LUFS_TARGETS:
            raise ValueError(f"Target normalisasi hanya mendukung {constants.ALLOWED_LUFS_TARGETS} LUFS.")

    info = audio_analysis.metadata(path)
    current_rate = int(info.get("sample_rate_hz") or 0)
    current_br = float(info.get("bitrate_kbps") or 0)
    current_ch = int(info.get("channels") or 0)
    codec = info.get("codec", "")
    ext = path.suffix.lower()
    rate_label = "44.1kHz" if target_rate == 44100 else "48kHz"
    tag_label = rate_label if target_bitrate == 320 else f"{rate_label} {target_bitrate}k"
    if normalize_lufs is not None:
        tag_label = f"{tag_label} {int(normalize_lufs)}LUFS"

    rate_matches = (current_rate == target_rate)
    has_xing = _has_mp3_xing_header(path) if ext == ".mp3" else False
    br_matches = (ext == ".mp3" and abs(current_br - target_bitrate) <= 8 and current_ch == 2 and not has_xing)

    if rate_matches and br_matches and normalize_lufs is None:
        return {
            "status": "SKIPPED",
            "file": str(path),
            "old_rate": current_rate,
            "new_rate": current_rate,
            "old_bitrate": round(current_br) if current_br else "",
            "new_bitrate": target_bitrate,
            "message": f"Sudah {target_rate} Hz • {target_bitrate} kbps (dilewati)",
        }

    target_ext, codec_args = _select_resample_codec_args(ext, codec, target_rate, target_bitrate)
    out_path = _resolve_resample_output_path(path, target_ext, tag_label, overwrite, out_dir)

    # Skip if non-overwrite target file already exists with target sample rate and bitrate
    if not overwrite and out_path != path and out_path.is_file():
        try:
            existing_info = audio_analysis.metadata(out_path)
            ex_rate = int(existing_info.get("sample_rate_hz") or 0)
            ex_br = float(existing_info.get("bitrate_kbps") or 0)
            if ex_rate == target_rate and abs(ex_br - target_bitrate) <= 8:
                return {
                    "status": "SKIPPED",
                    "file": str(out_path),
                    "old_rate": current_rate,
                    "new_rate": target_rate,
                    "old_bitrate": round(current_br) if current_br else "",
                    "new_bitrate": target_bitrate,
                    "message": f"Hasil konversi sudah ada ({out_path.parent.name}/{out_path.name})",
                }
        except Exception:
            pass

    tmp_path = out_path.with_name(f".{out_path.stem}.resample_tmp{out_path.suffix}")
    try:
        base_cmd = [
            "ffmpeg", "-y", "-hide_banner", "-nostats", "-xerror",
            "-i", str(path),
            "-map", "0:a:0",
            "-map_metadata", "0",
        ]
        ln_prefix = f"loudnorm=I={normalize_lufs}:TP=-1.5:LRA=11," if normalize_lufs is not None else ""
        cmd_soxr = base_cmd + [
            "-af", f"{ln_prefix}aresample=resampler=soxr:precision=28:osr={target_rate}",
            "-ar", str(target_rate),
            *codec_args,
            str(tmp_path),
        ]
        proc = subprocess.run(cmd_soxr, capture_output=True, text=True)
        if proc.returncode != 0:
            fallback_af = ["-af", f"loudnorm=I={normalize_lufs}:TP=-1.5:LRA=11"] if normalize_lufs is not None else []
            cmd_std = base_cmd + [
                *fallback_af,
                "-ar", str(target_rate),
                *codec_args,
                str(tmp_path),
            ]
            proc = subprocess.run(cmd_std, capture_output=True, text=True)
            if proc.returncode != 0:
                raise RuntimeError("FFmpeg resample gagal: " + proc.stderr[-300:].replace("\n", " "))

        new_info = audio_analysis.metadata(tmp_path)
        tmp_path.replace(out_path)
        if overwrite and out_path != path and path.exists():
            path.unlink()

        dest_display = f"{out_path.parent.name}/{out_path.name}" if out_path.parent != path.parent else out_path.name
        old_br_label = f"{round(current_br)}k" if current_br else codec
        norm_suffix = f" • EBU R128 {int(normalize_lufs)} LUFS" if normalize_lufs is not None else ""
        return {
            "status": "CONVERTED",
            "file": str(out_path),
            "old_file": str(path),
            "overwritten": overwrite,
            "old_rate": current_rate,
            "new_rate": int(new_info.get("sample_rate_hz") or target_rate),
            "old_bitrate": round(current_br) if current_br else "",
            "new_bitrate": target_bitrate,
            "codec": new_info.get("codec", ""),
            "normalized_lufs": normalize_lufs,
            "message": f"{current_rate} Hz ({old_br_label}) -> {target_rate} Hz • {target_bitrate} kbps{norm_suffix} ({dest_display})",
        }
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def normalize_one(
    path: Path,
    target_lufs: float = -14.0,
    overwrite: bool = False,
    out_dir: Path | None = None,
) -> dict:
    """Normalizes audio loudness to target_lufs (-14.0 or -16.0 LUFS) with -1.5 dBTP True Peak limiter (EBU R128).

    Preserves the track's existing sample rate (44.1 kHz or 48 kHz) and container/format family.
    """
    toolchain.require("ffmpeg")
    toolchain.require("ffprobe")
    target_lufs = float(target_lufs)
    if target_lufs not in constants.ALLOWED_LUFS_TARGETS:
        raise ValueError(f"Target normalisasi hanya mendukung {constants.ALLOWED_LUFS_TARGETS} LUFS.")

    info = audio_analysis.metadata(path)
    current_rate = int(info.get("sample_rate_hz") or 48000)
    out_rate = 44100 if current_rate == 44100 else 48000
    current_br = float(info.get("bitrate_kbps") or 0)
    codec = info.get("codec", "")
    ext = path.suffix.lower()

    if ext in (".webm", ".opus", ".ogg") or codec == "opus":
        target_ext = ".opus"
        codec_args = ["-c:a", "libopus", "-b:a", "256k", "-ac", "2"]
    elif ext in (".m4a", ".aac"):
        target_ext = ext
        codec_args = ["-c:a", "aac", "-b:a", "256k", "-ac", "2"]
    elif ext == ".flac" or codec == "flac":
        target_ext = ".flac"
        codec_args = ["-c:a", "flac", "-ac", "2"]
    elif ext in (".wav", ".aif", ".aiff"):
        target_ext = ext
        pcm_codec = codec if codec.startswith("pcm_") else "pcm_s16le"
        codec_args = ["-c:a", pcm_codec, "-ac", "2"]
    else:
        target_ext = ".mp3"
        codec_args = [
            "-c:a", "libmp3lame",
            "-b:a", "320k",
            "-ac", "2",
            "-write_xing", "0",
            "-id3v2_version", "3",
        ]

    tag_label = f"{int(target_lufs)}LUFS"
    out_path = _resolve_resample_output_path(path, target_ext, tag_label, overwrite, out_dir)

    if not overwrite and out_path != path and out_path.is_file():
        return {
            "status": "SKIPPED",
            "file": str(out_path),
            "old_rate": current_rate,
            "new_rate": out_rate,
            "old_bitrate": round(current_br) if current_br else "",
            "new_bitrate": round(current_br) if current_br else "",
            "message": f"Hasil normalisasi {int(target_lufs)} LUFS sudah ada ({out_path.parent.name}/{out_path.name})",
        }

    tmp_path = out_path.with_name(f".{out_path.stem}.norm_tmp{out_path.suffix}")
    try:
        base_cmd = [
            "ffmpeg", "-y", "-hide_banner", "-nostats", "-xerror",
            "-i", str(path),
            "-map", "0:a:0",
            "-map_metadata", "0",
        ]
        cmd_soxr = base_cmd + [
            "-af", f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11,aresample=resampler=soxr:precision=28:osr={out_rate}",
            "-ar", str(out_rate),
            *codec_args,
            str(tmp_path),
        ]
        proc = subprocess.run(cmd_soxr, capture_output=True, text=True)
        if proc.returncode != 0:
            cmd_std = base_cmd + [
                "-af", f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11",
                "-ar", str(out_rate),
                *codec_args,
                str(tmp_path),
            ]
            proc = subprocess.run(cmd_std, capture_output=True, text=True)
            if proc.returncode != 0:
                raise RuntimeError("FFmpeg loudnorm gagal: " + proc.stderr[-300:].replace("\n", " "))

        new_info = audio_analysis.metadata(tmp_path)
        tmp_path.replace(out_path)
        if overwrite and out_path != path and path.exists():
            path.unlink()

        dest_display = f"{out_path.parent.name}/{out_path.name}" if out_path.parent != path.parent else out_path.name
        new_br = float(new_info.get("bitrate_kbps") or current_br)
        return {
            "status": "CONVERTED",
            "file": str(out_path),
            "old_file": str(path),
            "overwritten": overwrite,
            "old_rate": current_rate,
            "new_rate": int(new_info.get("sample_rate_hz") or out_rate),
            "old_bitrate": round(current_br) if current_br else "",
            "new_bitrate": round(new_br) if new_br else "",
            "codec": new_info.get("codec", ""),
            "normalized_lufs": target_lufs,
            "message": f"EBU R128 {int(target_lufs)} LUFS (TP -1.5 dBTP) • {out_rate} Hz -> {dest_display}",
        }
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
