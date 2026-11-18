#!/usr/bin/env python3
"""SWARARIA AUDIO LAB — Automated Standalone Desktop App Builder.

Packages the entire application (Python runtime, Web UI, yt-dlp, pywebview,
and local FFmpeg/ffprobe binaries) into a standalone distributable directory
(`dist/SwarariaAudioLab`) that end-users can run without installing Python or FFmpeg.

Usage:
    python build_desktop.py
    python build_desktop.py --install-deps
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

PACKAGING_DIR = Path(__file__).resolve().parent
BASE_DIR = PACKAGING_DIR.parent  # repository root
VENDOR_BIN_DIR = BASE_DIR / ".vendor" / "bin"
DIST_DIR = BASE_DIR / "dist" / "SwarariaAudioLab"

FFMPEG_SHARED_ASSET = "ffmpeg-n9.0-latest-win64-gpl-shared-9.0.zip"
FFMPEG_SHARED_ARCHIVE_URL = (
    f"https://github.com/FFmpeg-Builds/FFmpeg-Builds/releases/download/latest/{FFMPEG_SHARED_ASSET}"
)
FFMPEG_CHECKSUMS_URL = (
    "https://github.com/FFmpeg-Builds/FFmpeg-Builds/releases/download/latest/checksums.sha256"
)
_STATIC_FFMPEG_WARN_BYTES = 80 * 1024 * 1024
_FFMPEG_SHARED_LIB_PREFIXES = ("avcodec", "avdevice", "avfilter", "avformat", "avutil", "postproc", "swresample", "swscale")


def _is_companion_shared_lib(path: Path) -> bool:
    """Returns True for shared FFmpeg runtime libraries next to ffmpeg/ffprobe."""
    if not path.is_file():
        return False
    lower = path.name.lower()
    if os.name == "nt":
        return lower.endswith(".dll")
    if sys.platform == "darwin":
        return lower.endswith(".dylib") and any(lower.startswith(f"lib{p}") for p in _FFMPEG_SHARED_LIB_PREFIXES)
    return ".so" in lower and any(lower.startswith(f"lib{p}") for p in _FFMPEG_SHARED_LIB_PREFIXES)


def _copy_companion_shared_libs(src_bin: Path, target_dir: Path, staged: list[Path]) -> None:
    """Copies sibling FFmpeg shared libraries when staging from a shared FFmpeg install."""
    for candidate in src_bin.parent.iterdir():
        if not _is_companion_shared_lib(candidate):
            continue
        dest = target_dir / candidate.name
        if not dest.exists():
            try:
                shutil.copy2(candidate, dest)
                staged.append(dest)
                print(f"  [OK] Membundel shared lib {candidate.name} -> {dest}")
            except OSError as exc:
                print(f"  [WARN] Gagal menyalin {candidate.name}: {exc}")


def parse_sha256_checksum(checksum_text: str, asset_name: str) -> str | None:
    """Extracts the expected SHA-256 hex digest for asset_name from a sha256sum manifest."""
    for line in checksum_text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == asset_name:
            digest = parts[0].lower()
            if len(digest) == 64 and all(ch in "0123456789abcdef" for ch in digest):
                return digest
    return None


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().lower()


def extract_shared_ffmpeg_runtime(archive_path: Path, dest_dir: Path) -> list[Path]:
    """Extracts Windows ffmpeg.exe, ffprobe.exe, and runtime DLLs safely into dest_dir."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="swararia_ffmpeg_") as tmp:
        tmp_dir = Path(tmp)
        extracted: list[Path] = []
        with zipfile.ZipFile(archive_path) as zf:
            for member in zf.infolist():
                if member.is_dir():
                    continue
                rel = Path(member.filename)
                if rel.is_absolute() or ".." in rel.parts:
                    raise ValueError(f"Path tidak aman di arsip FFmpeg: {member.filename}")
                if "bin" not in rel.parts:
                    continue
                name = rel.name
                lower = name.lower()
                if lower != "ffmpeg.exe" and lower != "ffprobe.exe" and not lower.endswith(".dll"):
                    continue
                out_path = (tmp_dir / name).resolve()
                if out_path.parent != tmp_dir.resolve():
                    raise ValueError(f"Path ekstraksi di luar direktori sementara: {member.filename}")
                with zf.open(member) as src, out_path.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                extracted.append(out_path)

        names_lower = {p.name.lower() for p in extracted}
        if "ffmpeg.exe" not in names_lower or "ffprobe.exe" not in names_lower:
            raise RuntimeError("Arsip FFmpeg tidak berisi bin/ffmpeg.exe dan bin/ffprobe.exe.")

        # Remove old monolithic Windows FFmpeg binaries / stale DLLs cleanly while preserving POSIX tools.
        for old in list(dest_dir.iterdir()):
            if old.is_file() and (old.name.lower() in ("ffmpeg.exe", "ffprobe.exe", "ffplay.exe") or old.suffix.lower() == ".dll"):
                old.unlink()

        installed: list[Path] = []
        for src in sorted(extracted):
            target = dest_dir / src.name
            shutil.copy2(src, target)
            installed.append(target)
        return installed


def _download_https(url: str, dest_path: Path) -> None:
    if not url.startswith("https://"):
        raise ValueError(f"Hanya URL HTTPS yang diizinkan: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "SwarariaAudioLab-Builder/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp, dest_path.open("wb") as out:
        shutil.copyfileobj(resp, out)


def fetch_shared_ffmpeg_windows(dest_dir: Path = VENDOR_BIN_DIR, verify_checksum: bool = True) -> list[Path]:
    """Downloads FFmpeg-Builds win64-gpl-shared runtime (.exe + .dll) into dest_dir."""
    print(f"  Mengunduh FFmpeg Shared Windows ({FFMPEG_SHARED_ASSET})...")
    with tempfile.TemporaryDirectory(prefix="swararia_dl_") as tmp:
        tmp_dir = Path(tmp)
        archive_path = tmp_dir / FFMPEG_SHARED_ASSET
        _download_https(FFMPEG_SHARED_ARCHIVE_URL, archive_path)

        if verify_checksum:
            checksum_path = tmp_dir / "checksums.sha256"
            _download_https(FFMPEG_CHECKSUMS_URL, checksum_path)
            expected = parse_sha256_checksum(checksum_path.read_text(encoding="utf-8"), FFMPEG_SHARED_ASSET)
            if not expected:
                raise RuntimeError(f"Checksum SHA-256 untuk {FFMPEG_SHARED_ASSET} tidak ditemukan.")
            actual = _sha256_file(archive_path)
            if actual != expected:
                raise RuntimeError(f"Verifikasi SHA-256 gagal ({actual} != {expected}).")
            print(f"  [OK] SHA-256 terverifikasi: {actual[:16]}...")

        installed = extract_shared_ffmpeg_runtime(archive_path, dest_dir)
        total_mb = sum(p.stat().st_size for p in installed) / (1024 * 1024)
        print(f"  [OK] Terpasang {len(installed)} file runtime FFmpeg Shared di {dest_dir} ({total_mb:.1f} MiB)")
        return installed


def stage_ffmpeg_binaries() -> list[Path]:
    """Stages ffmpeg & ffprobe (plus shared libs if applicable) into .vendor/bin/."""
    staged: list[Path] = []
    VENDOR_BIN_DIR.mkdir(parents=True, exist_ok=True)
    exe_suffix = ".exe" if os.name == "nt" else ""

    for tool in ("ffmpeg", "ffprobe"):
        target = VENDOR_BIN_DIR / f"{tool}{exe_suffix}"
        if target.is_file():
            staged.append(target)
            if os.name == "nt" and target.stat().st_size > _STATIC_FFMPEG_WARN_BYTES:
                mb = target.stat().st_size / (1024 * 1024)
                print(
                    f"  [WARN] {target.name} berukuran {mb:.1f} MiB (build statis penuh). "
                    "Jalankan dengan '--fetch-ffmpeg' untuk memangkas ukuran paket ~240 MB."
                )
            continue
        sys_bin = shutil.which(tool)
        if sys_bin:
            src = Path(sys_bin).resolve()
            try:
                shutil.copy2(src, target)
                staged.append(target)
                print(f"  [OK] Membundel {tool} dari {src} -> {target}")
                _copy_companion_shared_libs(src, VENDOR_BIN_DIR, staged)
            except OSError as exc:
                print(f"  [WARN] Gagal menyalin {tool}: {exc}")
        else:
            print(f"  [WARN] '{tool}' tidak ditemukan di PATH. Bundle tidak akan menyertakan {tool}.")
    return staged


def ensure_build_dependencies(auto_install: bool = False) -> bool:
    """Checks if PyInstaller is installed; optionally installs pyinstaller & pywebview."""
    try:
        import PyInstaller  # noqa: F401
        return True
    except ImportError:
        if not auto_install:
            print("[INFO] PyInstaller belum terpasang di environment ini.")
            print("       Menginstall pyinstaller & pywebview secara otomatis...")
        cmd = [sys.executable, "-m", "pip", "install", "pyinstaller>=6.0", "pywebview>=5.0"]
        res = subprocess.run(cmd)
        return res.returncode == 0


def find_inno_setup_compiler() -> Path | None:
    """Mencari ISCC.exe (Inno Setup Command Line Compiler) di PATH atau folder standar Windows."""
    # 1. Cek di PATH
    for name in ("iscc", "ISCC", "iscc.exe", "ISCC.exe"):
        found = shutil.which(name)
        if found:
            return Path(found).resolve()

    # 2. Cek lokasi instalasi standar di Windows
    roots = [
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("LocalAppData", ""),
    ]
    subpaths = [
        Path("Inno Setup 6") / "ISCC.exe",
        Path("Inno Setup 5") / "ISCC.exe",
        Path("Programs") / "Inno Setup 6" / "ISCC.exe",
        Path("Programs") / "Inno Setup 5" / "ISCC.exe",
    ]
    for r in roots:
        if not r:
            continue
        for sub in subpaths:
            cand = Path(r) / sub
            if cand.is_file():
                return cand.resolve()
    return None


def compile_inno_setup_installer() -> bool:
    """Mengompilasi installer.iss menjadi SwarariaAudioLab_Setup.exe menggunakan Inno Setup."""
    iss_file = PACKAGING_DIR / "installer.iss"
    if not iss_file.is_file():
        print(f"[ERROR] File konfigurasi installer '{iss_file}' tidak ditemukan.")
        return False

    iscc_path = find_inno_setup_compiler()
    if not iscc_path:
        print("\n[PERINGATAN] Inno Setup Compiler (ISCC.exe) belum terpasang.")
        print("             Untuk membuat installer ala wizard Windows secara otomatis:")
        print("             1. Install Inno Setup 6 via PowerShell / CMD:")
        print("                winget install JRSoftware.InnoSetup")
        print("             2. Atau download manual dari: https://jrsoftware.org/isdl.php")
        print("             3. Jalankan kembali: python build_desktop.py --installer")
        return False

    print(f"\n[5/5] Mengompilasi Setup Wizard Windows via Inno Setup ({iscc_path})...")
    res = subprocess.run([str(iscc_path), str(iss_file)], cwd=str(BASE_DIR))
    if res.returncode == 0:
        setup_exe = BASE_DIR / "dist" / "SwarariaAudioLab_Setup.exe"
        print("\n" + "=" * 68)
        print("  🎉 INSTALLER WIZARD BERHASIL DIBUAT!")
        print("=" * 68)
        print(f"  File Installer : {setup_exe}")
        print("  Tinggal bagikan file 'SwarariaAudioLab_Setup.exe' ke pengguna Windows.")
        print("  Pengguna tinggal double click -> Next -> Next -> Install -> Selesai!")
        print("=" * 68)
        return True
    else:
        print(f"[ERROR] Inno Setup kompilasi gagal dengan exit code {res.returncode}.")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Build standalone Swararia Audio Lab desktop app.")
    parser.add_argument(
        "--install-deps",
        action="store_true",
        help="Otomatis install pyinstaller dan pywebview sebelum build.",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Bersihkan folder build/ dan dist/ sebelum memulai.",
    )
    parser.add_argument(
        "--installer",
        action="store_true",
        help="Otomatis jalankan Inno Setup setelah build untuk membuat file SwarariaAudioLab_Setup.exe.",
    )
    parser.add_argument(
        "--fetch-ffmpeg",
        action="store_true",
        help="Unduh FFmpeg Windows GPL-Shared terbaru ke .vendor/bin/ untuk memangkas ukuran paket.",
    )
    parser.add_argument(
        "--fetch-ffmpeg-only",
        action="store_true",
        help="Hanya unduh/perbarui FFmpeg Shared ke .vendor/bin/ lalu keluar tanpa menjalankan PyInstaller.",
    )
    args = parser.parse_args()

    print("=" * 68)
    print("  SWARARIA AUDIO LAB — STANDALONE DESKTOP APP BUILDER")
    print("=" * 68)

    if args.clean:
        for d in (BASE_DIR / "build", BASE_DIR / "dist"):
            if d.is_dir():
                print(f"  Membersihkan {d}...")
                shutil.rmtree(d, ignore_errors=True)

    print("\n[1/4] Menyiapkan binary FFmpeg & ffprobe portable...")
    if args.fetch_ffmpeg or args.fetch_ffmpeg_only:
        fetch_shared_ffmpeg_windows(VENDOR_BIN_DIR)
        if args.fetch_ffmpeg_only:
            return 0
    else:
        stage_ffmpeg_binaries()

    print("\n[2/4] Memeriksa PyInstaller & dependensi build...")
    if not ensure_build_dependencies(auto_install=args.install_deps):
        print("[ERROR] Gagal menyiapkan PyInstaller. Jalankan: pip install pyinstaller pywebview")
        return 1

    print("\n[3/4] Menjalankan PyInstaller (SwarariaAudioLab.spec)...")
    spec_file = PACKAGING_DIR / "SwarariaAudioLab.spec"
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", str(spec_file)]
    res = subprocess.run(cmd, cwd=str(BASE_DIR))
    if res.returncode != 0:
        print(f"[ERROR] Proses build PyInstaller gagal (exit code {res.returncode}).")
        return res.returncode

    print("\n[4/4] Menyiapkan struktur folder library/inbox di paket distribusi...")
    if DIST_DIR.is_dir():
        inbox = DIST_DIR / "library" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        gitkeep = inbox / ".gitkeep"
        if not gitkeep.exists():
            gitkeep.write_text("", encoding="utf-8")

    exe_name = "SwarariaAudioLab.exe" if os.name == "nt" else "SwarariaAudioLab"
    exe_path = DIST_DIR / exe_name

    print("\n" + "=" * 68)
    print("  ✅ BUILD DESKTOP APP SELESAI!")
    print("=" * 68)
    print(f"  Folder Distribusi : {DIST_DIR}")
    print(f"  File Executable   : {exe_path}")
    if args.installer:
        compile_inno_setup_installer()
    else:
        print("\n  Cara membagikan ke orang awam:")
        print("  1. Kompres folder 'dist/SwarariaAudioLab' menjadi file .zip (Portable)")
        print("     ATAU")
        print("  2. Jalankan dengan opsi '--installer':")
        print("     python build_desktop.py --installer")
        print("     (Atau buka 'installer.iss' dengan Inno Setup Compiler di Windows)")
        print("     untuk membuat 1 file installer 'SwarariaAudioLab_Setup.exe'.")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())

