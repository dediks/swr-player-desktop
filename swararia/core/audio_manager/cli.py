"""Command-line subcommands and argument parsing."""
from __future__ import annotations

import argparse
import csv
import re
import shutil
import sys
from pathlib import Path

from swararia.core.audio_manager import audio_analysis, constants, conversion, toolchain, yt_cookies, yt_search, ytdlp_engine


def scan(args) -> int:
    toolchain.require("ffprobe")
    toolchain.require("ffmpeg")
    folder = Path(args.folder).expanduser().resolve()
    if not folder.is_dir():
        raise RuntimeError(f"Folder tidak ada: {folder}")
    files = sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in constants.EXTENSIONS)
    if not files:
        print("Tidak ada audio di folder tersebut.")
        return 0
    destination = Path(args.out).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    hashes = {}
    with destination.open("w", newline="", encoding="utf-8-sig") as output:
        writer = csv.DictWriter(output, fieldnames=constants.FIELDNAMES)
        writer.writeheader()
        for index, path in enumerate(files, 1):
            print(f"[{index}/{len(files)}] QC {path.name}", flush=True)
            row = audio_analysis.audit_one(path)
            try:
                digest = audio_analysis.sha256_file(path)
                row["sha256"] = digest
                if digest in hashes:
                    row["status"] = "REVIEW"
                    row["notes"] = (str(row["notes"]) + "; " if row["notes"] else "") + f"EXACT_DUPLICATE: {hashes[digest]}"
                else:
                    hashes[digest] = str(path)
            except OSError as exc:
                row["status"] = "ERROR"
                row["notes"] = (str(row["notes"]) + "; " if row["notes"] else "") + str(exc)
            writer.writerow(row)
    print(f"Selesai. Laporan: {destination.resolve()}")
    print("MEASURED hanya berarti pemindaian selesai tanpa flag, BUKAN bukti master berkualitas tinggi.")
    return 0


def download(args) -> int:
    if not args.authorized:
        raise RuntimeError("Hanya untuk sumber yang kamu miliki / diizinkan. Tambahkan --authorized jika sudah pasti.")
    toolchain.require("yt-dlp")
    folder = Path(args.folder).expanduser().resolve()
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / "downloaded_ids.txt"
    if args.playlist:
        template = str(folder / "%(playlist_title,playlist|Playlist)s" / "%(title).150B [%(id)s].%(ext)s")
    else:
        template = str(folder / "%(title).150B [%(id)s].%(ext)s")

    selected_format = getattr(args, "format", "opus")
    keep_opus = bool(getattr(args, "keep_opus", False))
    if selected_format in ("premium", "premium_aac"):  # legacy alias
        selected_format = "opus"

    removed_stale = ytdlp_engine.sync_download_archive(folder, archive, selected_format, keep_opus)
    if removed_stale > 0:
        print(f"[ARCHIVE SYNC] Membersihkan {removed_stale} ID riwayat lama yang file targetnya belum ada di {folder.name}.", flush=True)

    js_runtime, run_env = toolchain.find_js_runtime()
    cookies_file = folder.parent / "cookies.txt"
    if not cookies_file.is_file():
        cookies_file = Path("library/cookies.txt").resolve()

    browser = getattr(args, "cookies_from_browser", None)
    premium_active = False
    if cookies_file.is_file():
        print(f"Menggunakan file sesi YouTube: {cookies_file}", flush=True)
        tier_info = yt_cookies.check_youtube_cookies_tier(cookies_file)
        print(f"[COOKIES TIER] {tier_info['label']} — {tier_info['note']}", flush=True)
        premium_active = tier_info.get("status") in ("premium", "inconclusive")
    elif browser:
        premium_active = True

    cmd, target_desc = ytdlp_engine.build_ytdlp_command(
        url=args.url,
        output_template=template,
        archive_file=archive,
        audio_format=selected_format,
        keep_opus=keep_opus,
        is_playlist=bool(args.playlist),
        cookies_file=cookies_file if cookies_file.is_file() else None,
        cookie_browser=browser,
        js_runtime=js_runtime,
        premium_active=premium_active,
    )
    print(target_desc, flush=True)

    before_files = set(p for p in folder.rglob("*") if p.is_file())
    run_res = ytdlp_engine.run_ytdlp_process(cmd, env=run_env)
    returncode = run_res["returncode"]
    if returncode != 0 and (browser or cookies_file.is_file()):
        print("[AUTH FALLBACK] Percobaan dengan cookies gagal. Mencoba ulang mode publik...", flush=True)
        fallback_cmd, _ = ytdlp_engine.build_ytdlp_command(
            url=args.url,
            output_template=template,
            archive_file=archive,
            audio_format=selected_format,
            keep_opus=keep_opus,
            is_playlist=bool(args.playlist),
            cookies_file=None,
            cookie_browser=None,
            js_runtime=js_runtime,
            premium_active=False,
        )
        run_res = ytdlp_engine.run_ytdlp_process(fallback_cmd, env=run_env)
        returncode = run_res["returncode"]

    if returncode != 0 and run_res.get("engine_recovery_needed"):
        print(
            "[ENGINE AUTO-HEAL] Terdeteksi perubahan enkripsi/anti-bot YouTube. Memperbarui yt-dlp & mengulang dengan klien cadangan...",
            flush=True,
        )
        if ytdlp_engine.try_claim_ytdlp_auto_heal_slot():
            ytdlp_engine.update_ytdlp_preferring_nightly(env=run_env)
        recovery_cmd, recovery_desc = ytdlp_engine.build_ytdlp_command(
            url=args.url,
            output_template=template,
            archive_file=archive,
            audio_format=selected_format,
            keep_opus=keep_opus,
            is_playlist=bool(args.playlist),
            cookies_file=None,
            cookie_browser=None,
            js_runtime=js_runtime,
            premium_active=False,
            fallback_clients=True,
        )
        print(recovery_desc, flush=True)
        run_res = ytdlp_engine.run_ytdlp_process(recovery_cmd, env=run_env)
        returncode = run_res["returncode"]

    if returncode != 0:
        return returncode

    if shutil.which("ffprobe"):
        new_files = sorted(
            p for p in folder.rglob("*")
            if p.is_file() and p not in before_files and p.suffix.lower() in constants.EXTENSIONS
        )
        for nf in new_files:
            try:
                info = audio_analysis.metadata(nf)
                sr = f"{info['sample_rate_hz']} Hz" if info.get("sample_rate_hz") else "?"
                br = f"{info['bitrate_kbps']} kbps" if info.get("bitrate_kbps") else "VBR"
                print(
                    f"[STREAM VERIFIED] {nf.name} | Codec: {info['codec']} | Sample Rate: {sr} | Bitrate: {br}",
                    flush=True,
                )
            except Exception:
                pass

    if args.scan:
        return scan(argparse.Namespace(folder=str(folder), out=str(folder / "quality_report.csv")))
    return 0


def search_cli(args) -> int:
    """Executes a YouTube Music search from CLI and prints formatted results."""
    cookies_file = Path("library/cookies.txt").resolve() if Path("library/cookies.txt").is_file() else None
    results = yt_search.search_youtube_music(
        query=args.query,
        search_type=getattr(args, "type", "songs"),
        limit=getattr(args, "limit", 10),
        cookies_file=cookies_file,
    )
    if not results:
        print(f"Tidak ada hasil untuk '{args.query}'.")
        return 0

    print(f"\nHasil pencarian YouTube Music ({len(results)} item):\n")
    for idx, item in enumerate(results, 1):
        dur = f"[{item['duration']}]" if item.get("duration") else ""
        art = f" - {item['artist']}" if item.get("artist") else ""
        print(f"{idx:2d}. {item['title']}{art} {dur} ({item['type'].upper()})")
        print(f"    URL: {item['url']}")
    print()
    return 0


def resample(args) -> int:
    toolchain.require("ffmpeg")
    toolchain.require("ffprobe")
    target = Path(args.target).expanduser().resolve()
    if not target.exists():
        raise RuntimeError(f"Path tidak ditemukan: {target}")

    out_dir = Path(args.out_dir).expanduser().resolve() if getattr(args, "out_dir", None) else None
    target_bitrate = int(getattr(args, "bitrate", 320))
    remux_opus = bool(getattr(args, "remux_opus", False))

    if target.is_file():
        files = [target]
    else:
        if remux_opus:
            allowed_exts = {".webm", ".ogg"}
        elif args.only_mp3:
            allowed_exts = {".mp3"}
        else:
            allowed_exts = constants.EXTENSIONS
        files = sorted(
            p for p in target.rglob("*")
            if p.is_file()
            and p.suffix.lower() in allowed_exts
            and not p.name.startswith(".")
            and not re.search(constants.TAG_FOLDER_PATTERN, p.parent.name)
        )

    if not files:
        print("Tidak ada file audio yang cocok untuk dikonversi.")
        return 0

    converted = 0
    skipped = 0
    errors = 0
    for idx, path in enumerate(files, 1):
        try:
            if remux_opus:
                res = conversion.remux_to_opus_one(
                    path,
                    overwrite=args.overwrite,
                    out_dir=out_dir,
                )
            else:
                res = conversion.resample_one(
                    path,
                    target_rate=args.rate,
                    target_bitrate=target_bitrate,
                    overwrite=args.overwrite,
                    out_dir=out_dir,
                )
            if res["status"] == "CONVERTED":
                converted += 1
                print(f"[{idx}/{len(files)}] CONVERTED: {res['message']}", flush=True)
            else:
                skipped += 1
                print(f"[{idx}/{len(files)}] SKIPPED: {path.name} ({res['message']})", flush=True)
        except Exception as exc:
            errors += 1
            print(f"[{idx}/{len(files)}] ERROR {path.name}: {exc}", file=sys.stderr, flush=True)

    print(f"Selesai. Dikonversi: {converted} | Dilewati: {skipped} | Error: {errors}")
    if args.scan and target.is_dir():
        return scan(argparse.Namespace(folder=str(target), out=str(target / "quality_report.csv")))
    return 0 if errors == 0 else 1


def split_cli(args) -> int:
    """CLI handler for automatic or timestamp-based splitting of Multitrack WAVs, Concert Videos, or YouTube URLs."""
    from swararia.core import splitter_engine

    if splitter_engine.is_youtube_url(args.target):
        target = str(args.target).strip()
    else:
        target = Path(args.target).expanduser().resolve()
        if not target.exists():
            raise RuntimeError(f"Path target tidak ditemukan: {target}")

    out_dir = Path(args.out_dir).expanduser().resolve() if getattr(args, "out_dir", None) else None
    manual_text = None
    if getattr(args, "timestamps_file", None):
        ts_path = Path(args.timestamps_file).expanduser().resolve()
        if not ts_path.is_file():
            raise RuntimeError(f"File daftar timestamp tidak ditemukan: {ts_path}")
        manual_text = ts_path.read_text(encoding="utf-8", errors="ignore")

    print(f"[SPLITTER] Menganalisis target: {target} ...", flush=True)
    det = splitter_engine.detect_song_segments(
        target_path=target,
        mode="manual" if manual_text else args.detect_mode,
        noise_db=float(args.noise_db),
        min_silence_s=float(args.min_silence),
        min_song_s=float(args.min_song),
        pad_before_s=float(args.pad_before),
        pad_after_s=float(args.pad_after),
        trim_silence_edges=not bool(args.continuous),
        reference_stem=args.reference_stem,
        manual_timestamps=manual_text,
    )
    segments = det.get("segments", [])
    print(f"[SPLITTER] {det.get('detection_note')}", flush=True)
    for seg in segments:
        st_lbl = splitter_engine.format_timestamp(seg["start_s"])
        et_lbl = splitter_engine.format_timestamp(seg["end_s"])
        print(f"  #{seg['index']:02d}  {st_lbl} -> {et_lbl}  ({seg['duration_s']:6.1f}s)  {seg['title']}", flush=True)

    if getattr(args, "dry_run", False):
        print("[DRY-RUN] Preview titik potong selesai (tidak ada file yang dipotong).", flush=True)
        return 0

    res = splitter_engine.execute_split_job(
        target_path=target,
        segments=segments,
        out_dir=out_dir,
        video_cut_mode=args.video_mode,
        extract_audio_fmt=args.extract_audio,
        multichannel_mode=args.multichannel_mode,
        stem_org_mode=args.stem_org,
    )
    print(
        f"[SELESAI] Status: {res['status']} | Lagu: {res['songs_count']} | "
        f"File dibuat: {res['created_files']} | Error: {res['errors']} | Output: {res['out_dir']}",
        flush=True,
    )
    return 0 if res["errors"] == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Authorized download + local audio quality audit")
    sub = parser.add_subparsers(dest="action", required=True)
    d = sub.add_parser("download", help="Download sumber yang memang diizinkan")
    d.add_argument("url")
    d.add_argument("--authorized", action="store_true", help="Saya memiliki izin sumber ini")
    d.add_argument("--format", choices=["opus", "native", "mp3", "mp3_320k", "mp3_vbr0", "premium"], default="opus",
                   help="'opus' (default): Best Quality Opus (.opus) 48 kHz dengan lossless remux & embedded metadata. 'native': stream mentah asli YouTube (.webm/.m4a). 'mp3': konversi MP3 320 kbps.")
    d.add_argument("--keep-opus", action="store_true",
                   help="Saat download --format mp3, tetap simpan juga file asli Opus (.webm) (default: tidak aktif)")
    d.add_argument("--playlist", action="store_true", help="Proses seluruh playlist (otomatis disimpan ke folder nama playlist)")
    d.add_argument("--cookies-from-browser", default=None,
                   help="Baca sesi login browser (contoh: chrome, firefox, brave) untuk akun YouTube Premium")
    d.add_argument("--folder", default="library/inbox")
    d.add_argument("--scan", action="store_true", help="Audit otomatis setelah download")
    s = sub.add_parser("scan", help="Audit file audio lokal tanpa download")
    s.add_argument("folder")
    s.add_argument("--out", default="quality_report.csv")
    r = sub.add_parser("resample", help="Konversi sample rate & bitrate lagu yang sudah ada")
    r.add_argument("target", help="Path ke file audio atau folder koleksi")
    r.add_argument("--rate", type=int, choices=[44100, 48000], default=44100,
                   help="Target sample rate dalam Hz (default: 44100 untuk 44.1 kHz)")
    r.add_argument("--bitrate", type=int, choices=[320, 192, 128, 96], default=320,
                   help="Target bitrate MP3 dalam kbps: 320, 192, 128, atau 96 (default: 320)")
    r.add_argument("--only-mp3", action="store_true",
                   help="Hanya konversi file .mp3 (biarkan file .webm/.opus tetap utuh)")
    r.add_argument("--remux-opus", action="store_true",
                   help="Lossless remux file .webm/.ogg (Opus) menjadi wadah .opus (-c:a copy) tanpa re-encode")
    r.add_argument("--overwrite", action="store_true",
                   help="Timpa file asli secara langsung (default: TIDAK menimpa file asli)")
    r.add_argument("--out-dir", default=None,
                   help="Folder tujuan khusus untuk menyimpan hasil konversi")
    r.add_argument("--scan", action="store_true", help="Audit ulang folder setelah konversi selesai")
    sp = sub.add_parser("split", help="Potong otomatis file Multitrack WAV atau Video Konser panjang per lagu")
    sp.add_argument("target", help="Path ke folder Multi-Stem WAV, file Multichannel WAV, atau Video Konser")
    sp.add_argument("--detect-mode", choices=["auto", "silence", "adaptive", "chapters"], default="auto",
                    help="Metode deteksi batas lagu otomatis (default: auto)")
    sp.add_argument("--noise-db", type=float, default=-32.0,
                    help="Ambang desibel jeda antar lagu dalam dB (default: -32.0)")
    sp.add_argument("--min-silence", type=float, default=2.5,
                    help="Durasi hening/jeda minimum antar lagu dalam detik (default: 2.5)")
    sp.add_argument("--min-song", type=float, default=60.0,
                    help="Durasi minimum sebuah lagu dalam detik (default: 60.0)")
    sp.add_argument("--pad-before", type=float, default=0.5,
                    help="Lead-in (padding sebelum lagu mulai) dalam detik (default: 0.5)")
    sp.add_argument("--pad-after", type=float, default=1.0,
                    help="Lead-out (padding setelah lagu selesai) dalam detik (default: 1.0)")
    sp.add_argument("--continuous", action="store_true",
                    help="Potong menyambung tanpa membuang bagian jeda hening di tengah")
    sp.add_argument("--reference-stem", default=None,
                    help="Nama file stem referensi saat memotong folder Multi-Stem WAV")
    sp.add_argument("--timestamps-file", default=None,
                    help="File teks berisi daftar timestamp manual per lagu (opsional)")
    sp.add_argument("--video-mode", choices=["copy", "reencode"], default="copy",
                    help="Mode pemotongan video konser: 'copy' (lossless cepat) atau 'reencode' (presisi frame)")
    sp.add_argument("--extract-audio", choices=["none", "wav", "mp3_320k", "flac", "opus"], default="none",
                    help="Sekaligus ekstrak audio per lagu saat memotong video konser")
    sp.add_argument("--multichannel-mode", choices=["keep_multichannel", "explode_mono_stems"], default="keep_multichannel",
                    help="Untuk file WAV multichannel (X-Live): simpan multichannel per lagu atau pecah ke mono WAV per channel")
    sp.add_argument("--stem-org", choices=["folder_per_song", "prefix_per_song"], default="folder_per_song",
                    help="Organisasi output untuk folder Multi-Stem WAV")
    sp.add_argument("--out-dir", default=None,
                    help="Folder tujuan output hasil pemotongan")
    sp.add_argument("--dry-run", action="store_true",
                    help="Hanya tampilkan preview titik potong lagu tanpa mengeksekusi pemotongan")

    # Subcommand: search
    sp = sub.add_parser("search", help="Cari lagu/playlist di YouTube Music tanpa browser")
    sp.add_argument("query", help="Kata kunci pencarian (judul lagu, artis, atau playlist)")
    sp.add_argument("--type", choices=["songs", "playlists", "albums", "all"], default="songs",
                    help="Tipe pencarian (default: songs)")
    sp.add_argument("--limit", type=int, default=10,
                    help="Jumlah hasil pencarian maksimum (default: 10)")

    args = parser.parse_args()
    try:
        if args.action == "search":
            return search_cli(args)
        if args.action == "download":
            return download(args)
        if args.action == "resample":
            return resample(args)
        if args.action == "split":
            return split_cli(args)
        return scan(args)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
