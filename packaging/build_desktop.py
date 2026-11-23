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
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import Mapping
import urllib.request
import zipfile
from pathlib import Path

PACKAGING_DIR = Path(__file__).resolve().parent
BASE_DIR = PACKAGING_DIR.parent  # repository root
VENDOR_BIN_DIR = BASE_DIR / ".vendor" / "bin"
DIST_DIR = BASE_DIR / "dist" / "SwarariaAudioLab"

DEFAULT_SIGN_TIMESTAMP_URL = "http://timestamp.digicert.com"
_SIGN_ENV_KEYS = (
    "SWARARIA_SIGN_THUMBPRINT",
    "SWARARIA_SIGN_PFX",
    "SWARARIA_SIGN_PFX_PASSWORD",
    "SWARARIA_SIGN_TIMESTAMP_URL",
    "SWARARIA_SIGN_COMMAND",
    "SWARARIA_SIGNTOOL_PATH",
    "SWARARIA_SIGN_MACHINE_STORE",
)

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


class SigningConfig:
    """Validated Authenticode signing configuration."""

    def __init__(
        self,
        *,
        mode: str,
        thumbprint: str | None = None,
        pfx_path: Path | None = None,
        pfx_password: str | None = None,
        timestamp_url: str = DEFAULT_SIGN_TIMESTAMP_URL,
        command_template: str | None = None,
        signtool_path: Path | None = None,
        machine_store: bool = False,
    ) -> None:
        self.mode = mode
        self.thumbprint = thumbprint
        self.pfx_path = pfx_path
        self.pfx_password = pfx_password
        self.timestamp_url = timestamp_url
        self.command_template = command_template
        self.signtool_path = signtool_path
        self.machine_store = machine_store

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SigningConfig) and self.__dict__ == other.__dict__

    def __repr__(self) -> str:
        return (
            f"SigningConfig(mode={self.mode!r}, thumbprint={self.thumbprint!r}, "
            f"pfx_path={self.pfx_path!r}, has_password={bool(self.pfx_password)}, "
            f"timestamp_url={self.timestamp_url!r}, machine_store={self.machine_store!r})"
        )


def _parse_version_tuple(version_name: str) -> tuple[int, ...]:
    """Parses dotted numeric SDK folder names for descending version sorting."""
    parts = []
    for token in version_name.split("."):
        if not token.isdigit():
            return ()
        parts.append(int(token))
    return tuple(parts)


def find_signtool(
    explicit_path: str | Path | None = None,
    *,
    program_roots: tuple[str, ...] | None = None,
) -> Path | None:
    """Locates Windows signtool.exe from an explicit path, PATH, or Windows Kits."""
    if explicit_path:
        candidate = Path(explicit_path).expanduser()
        return candidate.resolve() if candidate.is_file() else None

    for name in ("signtool", "signtool.exe"):
        found = shutil.which(name)
        if found:
            return Path(found).resolve()

    if program_roots is None:
        program_roots = (
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            os.environ.get("ProgramFiles", r"C:\Program Files"),
        )

    for root_str in program_roots:
        if not root_str:
            continue
        root = Path(root_str)
        kits10_bin = root / "Windows Kits" / "10" / "bin"
        if kits10_bin.is_dir():
            sdk_dirs = sorted(
                (p for p in kits10_bin.iterdir() if p.is_dir() and _parse_version_tuple(p.name)),
                key=lambda p: _parse_version_tuple(p.name),
                reverse=True,
            )
            for sdk_dir in sdk_dirs:
                for arch in ("x64", "arm64", "x86"):
                    cand = sdk_dir / arch / "signtool.exe"
                    if cand.is_file():
                        return cand.resolve()

        for rel in (
            Path("Windows Kits") / "10" / "App Certification Kit" / "signtool.exe",
            Path("Windows Kits") / "8.1" / "bin" / "x64" / "signtool.exe",
            Path("Windows Kits") / "8.1" / "bin" / "x86" / "signtool.exe",
        ):
            cand = root / rel
            if cand.is_file():
                return cand.resolve()

    return None


def _normalize_thumbprint(raw: str) -> str:
    """Normalizes and validates a 40-char SHA-1 certificate thumbprint."""
    cleaned = raw.strip().lstrip("\ufeff\u200e\u200f").replace(" ", "").replace(":", "")
    if len(cleaned) != 40 or not all(ch in "0123456789abcdefABCDEF" for ch in cleaned):
        raise ValueError("Thumbprint sertifikat harus berupa 40 karakter heksadesimal (SHA-1).")
    return cleaned.upper()


def _parse_bool_env(raw: str | None) -> bool:
    return (raw or "").strip().lower() in ("1", "true", "yes", "on")


def resolve_signing_config(
    *,
    thumbprint: str | None = None,
    pfx: str | None = None,
    timestamp_url: str | None = None,
    sign_command: str | None = None,
    signtool_path: str | None = None,
    machine_store: bool = False,
    environ: Mapping[str, str] | None = None,
) -> SigningConfig | None:
    """Resolves optional Authenticode signing settings from CLI flags or env vars."""
    env = os.environ if environ is None else environ
    eff_thumb = (thumbprint if thumbprint is not None else env.get("SWARARIA_SIGN_THUMBPRINT", "")).strip()
    eff_pfx = (pfx if pfx is not None else env.get("SWARARIA_SIGN_PFX", "")).strip()
    eff_cmd = (sign_command if sign_command is not None else env.get("SWARARIA_SIGN_COMMAND", "")).strip()

    configured_modes = [name for name, val in (("thumbprint", eff_thumb), ("pfx", eff_pfx), ("command", eff_cmd)) if val]
    if not configured_modes:
        return None
    if len(configured_modes) > 1:
        raise ValueError(
            "Pilih hanya satu metode signing: --sign-thumbprint, --sign-pfx, atau --sign-command."
        )

    eff_ts = (timestamp_url if timestamp_url is not None else env.get("SWARARIA_SIGN_TIMESTAMP_URL", "")).strip()
    if not eff_ts:
        eff_ts = DEFAULT_SIGN_TIMESTAMP_URL
    if not (eff_ts.startswith("https://") or eff_ts.startswith("http://")) or any(ch.isspace() for ch in eff_ts):
        raise ValueError(f"URL timestamp signing tidak valid: {eff_ts}")

    eff_signtool = (signtool_path if signtool_path is not None else env.get("SWARARIA_SIGNTOOL_PATH", "")).strip() or None
    eff_machine = bool(machine_store or _parse_bool_env(env.get("SWARARIA_SIGN_MACHINE_STORE")))

    if eff_cmd:
        return SigningConfig(mode="command", command_template=eff_cmd, timestamp_url=eff_ts)

    resolved_signtool = find_signtool(eff_signtool)
    if resolved_signtool is None:
        raise FileNotFoundError(
            "signtool.exe tidak ditemukan. Pasang Windows SDK atau tentukan jalur lewat --sign-tool-path."
        )

    if eff_thumb:
        return SigningConfig(
            mode="thumbprint",
            thumbprint=_normalize_thumbprint(eff_thumb),
            timestamp_url=eff_ts,
            signtool_path=resolved_signtool,
            machine_store=eff_machine,
        )

    pfx_file = Path(eff_pfx).expanduser()
    if not pfx_file.is_file():
        raise FileNotFoundError(f"File sertifikat PFX tidak ditemukan: {pfx_file}")

    pfx_password = env.get("SWARARIA_SIGN_PFX_PASSWORD") or None
    return SigningConfig(
        mode="pfx",
        pfx_path=pfx_file.resolve(),
        pfx_password=pfx_password,
        timestamp_url=eff_ts,
        signtool_path=resolved_signtool,
    )


def _split_command_template(template: str) -> list[str]:
    """Splits a custom signing command template into argv tokens without shell expansion."""
    use_posix = os.name != "nt"
    tokens = shlex.split(template, posix=use_posix)
    if not use_posix:
        cleaned = []
        for tok in tokens:
            if len(tok) >= 2 and tok[0] == tok[-1] == '"':
                cleaned.append(tok[1:-1])
            else:
                cleaned.append(tok)
        tokens = cleaned
    if not tokens:
        raise ValueError("Template perintah signing kosong.")
    return tokens


def build_sign_argv(target_path: Path, config: SigningConfig) -> list[str]:
    """Builds the subprocess argument list for signing target_path."""
    target_str = str(target_path)
    if config.mode == "command":
        if not config.command_template:
            raise ValueError("Template perintah signing tidak tersedia.")
        tokens = _split_command_template(config.command_template)
        has_placeholder = any("{file}" in tok for tok in tokens)
        argv = [tok.replace("{file}", target_str) for tok in tokens]
        if not has_placeholder:
            argv.append(target_str)
        return argv

    if not config.signtool_path:
        raise ValueError("Lokasi signtool.exe belum tersedia.")

    argv = [
        str(config.signtool_path),
        "sign",
        "/fd",
        "SHA256",
        "/tr",
        config.timestamp_url,
        "/td",
        "SHA256",
    ]
    if config.mode == "thumbprint":
        if config.machine_store:
            argv.append("/sm")
        argv.extend(["/sha1", config.thumbprint or "", target_str])
        return argv

    if config.mode == "pfx":
        argv.extend(["/f", str(config.pfx_path)])
        if config.pfx_password:
            argv.extend(["/p", config.pfx_password])
        argv.append(target_str)
        return argv

    raise ValueError(f"Mode signing tidak dikenal: {config.mode}")


def _redact_secret(text: str, secret: str | None) -> str:
    if secret:
        return text.replace(secret, "***")
    return text


def sign_binary(target_path: Path, config: SigningConfig) -> bool:
    """Applies an Authenticode digital signature to target_path."""
    if not target_path.is_file():
        print(f"[ERROR] File target untuk ditandatangani tidak ditemukan: {target_path}")
        return False

    argv = build_sign_argv(target_path, config)
    res = subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if res.returncode != 0:
        details = _redact_secret((res.stderr or res.stdout or "").strip(), config.pfx_password)
        print(f"[ERROR] Penandatanganan kode gagal untuk {target_path.name} (exit code {res.returncode}).")
        if details:
            print(f"        {details}")
        return False

    print(f"  [OK] Signed (SHA-256): {target_path.name}")
    return True


def build_signing_env(config: SigningConfig, base_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Builds environment variables passed to Inno Setup's child signing subprocess."""
    env = dict(os.environ if base_env is None else base_env)
    for key in _SIGN_ENV_KEYS:
        env.pop(key, None)

    env["SWARARIA_SIGN_TIMESTAMP_URL"] = config.timestamp_url
    if config.signtool_path:
        env["SWARARIA_SIGNTOOL_PATH"] = str(config.signtool_path)

    if config.mode == "thumbprint":
        env["SWARARIA_SIGN_THUMBPRINT"] = config.thumbprint or ""
        if config.machine_store:
            env["SWARARIA_SIGN_MACHINE_STORE"] = "1"
    elif config.mode == "pfx":
        env["SWARARIA_SIGN_PFX"] = str(config.pfx_path)
        if config.pfx_password:
            env["SWARARIA_SIGN_PFX_PASSWORD"] = config.pfx_password
    elif config.mode == "command":
        env["SWARARIA_SIGN_COMMAND"] = config.command_template or ""
    return env


def _escape_inno_dollar(value: str) -> str:
    return value.replace("$", "$$")


def write_sha256_manifest(file_path: Path) -> tuple[str, Path]:
    """Computes SHA-256 digest for file_path and writes a sidecar .sha256 manifest."""
    digest = _sha256_file(file_path)
    manifest_path = Path(f"{file_path}.sha256")
    manifest_path.write_text(f"{digest} *{file_path.name}\n", encoding="utf-8")
    return digest, manifest_path


def compile_inno_setup_installer(signing_config: SigningConfig | None = None) -> bool:
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
    cmd = [str(iscc_path)]
    run_env = None
    if signing_config is not None:
        py_esc = _escape_inno_dollar(sys.executable)
        script_esc = _escape_inno_dollar(str(Path(__file__).resolve()))
        sign_tool_cmd = f"$q{py_esc}$q $q{script_esc}$q --sign-target $q$f$q"
        cmd.extend(["/DSignToolName=swararia", f"/Sswararia={sign_tool_cmd}"])
        run_env = build_signing_env(signing_config)
        print(f"  [INFO] Code signing aktif (mode: {signing_config.mode}) untuk installer & uninstaller.")
    cmd.append(str(iss_file))

    res = subprocess.run(cmd, cwd=str(BASE_DIR), env=run_env)
    if res.returncode == 0:
        setup_exe = BASE_DIR / "dist" / "SwarariaAudioLab_Setup.exe"
        digest_info = None
        if setup_exe.is_file():
            digest_info = write_sha256_manifest(setup_exe)
        print("\n" + "=" * 68)
        print("  [OK] INSTALLER WIZARD BERHASIL DIBUAT!")
        print("=" * 68)
        print(f"  File Installer : {setup_exe}")
        if digest_info is not None:
            digest, manifest_path = digest_info
            print(f"  SHA-256        : {digest}")
            print(f"  File Checksum  : {manifest_path}")
        if signing_config is None:
            print("  Status Signing : Belum di-sign (SmartScreen mungkin menampilkan 'Unknown publisher').")
        else:
            print(f"  Status Signing : Ditandatangani ({signing_config.mode})")
        print("  Tinggal bagikan file 'SwarariaAudioLab_Setup.exe' ke pengguna Windows.")
        print("  Pengguna tinggal double click -> Next -> Next -> Install -> Selesai!")
        print("=" * 68)
        return True
    else:
        print(f"[ERROR] Inno Setup kompilasi gagal dengan exit code {res.returncode}.")
        return False


_SEMVER_CORE_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_RELEASE_TAG_RE = re.compile(
    r"^v(?P<base>(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))"
    r"(?:-(?P<pre>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


def read_project_versions(base_dir: Path = BASE_DIR) -> dict[str, str]:
    """Reads version strings from pyproject.toml, state.py, and installer.iss."""
    pyproject_text = (base_dir / "pyproject.toml").read_text(encoding="utf-8")
    state_text = (base_dir / "swararia" / "server" / "state.py").read_text(encoding="utf-8")
    iss_text = (base_dir / "packaging" / "installer.iss").read_text(encoding="utf-8")

    m_py = re.search(r'(?m)^version\s*=\s*"([^"]+)"', pyproject_text)
    m_state = re.search(r'(?m)^APP_VERSION\s*=\s*"([^"]+)"', state_text)
    m_iss = re.search(r'(?m)^#define\s+MyAppVersion\s+"([^"]+)"', iss_text)
    if not m_py:
        raise ValueError("Versi tidak ditemukan di pyproject.toml.")
    if not m_state:
        raise ValueError("APP_VERSION tidak ditemukan di swararia/server/state.py.")
    if not m_iss:
        raise ValueError("MyAppVersion tidak ditemukan di packaging/installer.iss.")

    return {
        "pyproject.toml": m_py.group(1).strip(),
        "swararia/server/state.py": m_state.group(1).strip(),
        "packaging/installer.iss": m_iss.group(1).strip(),
    }


def parse_release_tag(tag: str) -> dict[str, object]:
    """Validates a release tag such as v1.0.1 or v1.0.1-rc.1 and extracts SemVer parts."""
    clean = str(tag or "").strip()
    m = _RELEASE_TAG_RE.match(clean)
    if not m:
        raise ValueError(
            f"Format tag rilis tidak valid: '{clean}'. Gunakan SemVer seperti v1.0.1 atau v1.0.1-rc.1."
        )
    prerelease = m.group("pre")
    return {
        "tag": clean,
        "base_version": m.group("base"),
        "prerelease": prerelease,
        "is_prerelease": bool(prerelease),
    }


def verify_version_sync(
    base_dir: Path = BASE_DIR,
    expected_tag: str | None = None,
) -> tuple[str, dict[str, object] | None]:
    """Ensures all version files match each other and (optionally) the release tag."""
    versions = read_project_versions(base_dir)
    unique = set(versions.values())
    if len(unique) != 1:
        details = ", ".join(f"{src}={ver}" for src, ver in versions.items())
        raise ValueError(f"Versi proyek tidak sinkron ({details}). Samakan ketiga file sebelum rilis.")

    app_version = next(iter(unique))
    if not _SEMVER_CORE_RE.match(app_version):
        raise ValueError(f"Format versi proyek tidak valid: '{app_version}'. Gunakan format MAJOR.MINOR.PATCH.")

    tag_info: dict[str, object] | None = None
    if expected_tag is not None and str(expected_tag).strip():
        tag_info = parse_release_tag(expected_tag)
        if tag_info["base_version"] != app_version:
            raise ValueError(
                f"Tag rilis '{tag_info['tag']}' (versi dasar {tag_info['base_version']}) "
                f"tidak cocok dengan versi proyek ({app_version})."
            )

    return app_version, tag_info


def verify_dist_bundle(dist_dir: Path = DIST_DIR, *, is_windows: bool | None = None) -> list[str]:
    """Verifies that the built PyInstaller bundle contains the app binary, UI assets, and FFmpeg."""
    win = (os.name == "nt") if is_windows is None else bool(is_windows)
    errors: list[str] = []

    if not dist_dir.is_dir():
        return [f"Folder distribusi tidak ditemukan: {dist_dir}"]

    exe_name = "SwarariaAudioLab.exe" if win else "SwarariaAudioLab"
    exe_path = dist_dir / exe_name
    if not exe_path.is_file() or exe_path.stat().st_size <= 0:
        errors.append(f"Executable utama hilang atau kosong: {exe_path}")

    internal_root = dist_dir / "_internal" if (dist_dir / "_internal").is_dir() else dist_dir
    for rel_asset in (
        Path("swararia") / "web" / "static" / "pages" / "ingest" / "index.html",
        Path("swararia") / "web" / "static" / "pages" / "player" / "index.html",
        Path("swararia") / "web" / "static" / "modules" / "spa-router.js",
    ):
        asset_path = internal_root / rel_asset
        if not asset_path.is_file():
            errors.append(f"Aset Web UI tidak terpaket: {rel_asset.as_posix()}")

    vendor_bin = internal_root / ".vendor" / "bin"
    if not vendor_bin.is_dir():
        errors.append(f"Folder binary FFmpeg tidak terpaket: {vendor_bin}")
    else:
        suffix = ".exe" if win else ""
        for tool in ("ffmpeg", "ffprobe"):
            tool_path = vendor_bin / f"{tool}{suffix}"
            if not tool_path.is_file() or tool_path.stat().st_size <= 0:
                errors.append(f"Binary {tool}{suffix} hilang atau kosong di {vendor_bin}")
        if win:
            dlls = [p for p in vendor_bin.iterdir() if p.is_file() and p.suffix.lower() == ".dll"]
            if not any(p.name.lower().startswith("avcodec") for p in dlls):
                errors.append("Shared library FFmpeg (avcodec*.dll) tidak ditemukan di paket Windows.")

    return errors


def _configure_console_encoding() -> None:
    """Prevents UnicodeEncodeError on Windows cp1252/cp437 redirected console streams."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def main() -> int:
    _configure_console_encoding()
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
    parser.add_argument(
        "--check-version",
        action="store_true",
        help="Verifikasi sinkronisasi versi (pyproject.toml, state.py, installer.iss, dan opsional --release-tag) lalu keluar.",
    )
    parser.add_argument(
        "--release-tag",
        default=None,
        help="Tag rilis SemVer opsional (mis. v1.0.1 atau v1.0.1-rc.1) untuk divalidasi terhadap versi proyek.",
    )
    parser.add_argument(
        "--verify-bundle",
        action="store_true",
        help="Jalankan smoke check pada folder dist/SwarariaAudioLab setelah PyInstaller selesai.",
    )
    parser.add_argument(
        "--sign-thumbprint",
        default=None,
        help="Thumbprint SHA-1 sertifikat di Windows Certificate Store (env: SWARARIA_SIGN_THUMBPRINT).",
    )
    parser.add_argument(
        "--sign-pfx",
        default=None,
        help="Jalur file sertifikat .pfx/.p12; password dibaca dari env SWARARIA_SIGN_PFX_PASSWORD (env: SWARARIA_SIGN_PFX).",
    )
    parser.add_argument(
        "--sign-timestamp-url",
        default=None,
        help=f"URL server RFC 3161 timestamp (default: {DEFAULT_SIGN_TIMESTAMP_URL}; env: SWARARIA_SIGN_TIMESTAMP_URL).",
    )
    parser.add_argument(
        "--sign-command",
        default=None,
        help="Perintah signing kustom berisi placeholder {file} (env: SWARARIA_SIGN_COMMAND).",
    )
    parser.add_argument(
        "--sign-tool-path",
        default=None,
        help="Jalur eksplisit ke signtool.exe (env: SWARARIA_SIGNTOOL_PATH).",
    )
    parser.add_argument(
        "--sign-machine-store",
        action="store_true",
        help="Gunakan LocalMachine certificate store (/sm) bersama --sign-thumbprint.",
    )
    parser.add_argument(
        "--sign-target",
        default=None,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()

    if args.check_version:
        try:
            app_ver, tag_info = verify_version_sync(BASE_DIR, expected_tag=args.release_tag)
        except ValueError as exc:
            print(f"[ERROR] Validasi versi gagal: {exc}")
            return 1
        if tag_info is not None:
            channel = "Pre-release" if tag_info["is_prerelease"] else "Stable"
            print(f"  [OK] Versi sinkron: {app_ver} (tag {tag_info['tag']} - {channel})")
        else:
            print(f"  [OK] Versi proyek sinkron: {app_ver}")
        return 0

    signing_config = None
    if args.sign_target or not args.fetch_ffmpeg_only:
        try:
            signing_config = resolve_signing_config(
                thumbprint=args.sign_thumbprint,
                pfx=args.sign_pfx,
                timestamp_url=args.sign_timestamp_url,
                sign_command=args.sign_command,
                signtool_path=args.sign_tool_path,
                machine_store=args.sign_machine_store,
            )
        except (ValueError, FileNotFoundError) as exc:
            print(f"[ERROR] Konfigurasi penandatanganan tidak valid: {exc}")
            return 1

    if args.sign_target:
        if signing_config is None:
            print("[ERROR] --sign-target dipanggil tanpa konfigurasi penandatanganan aktif.")
            return 1
        return 0 if sign_binary(Path(args.sign_target).resolve(), signing_config) else 1

    if args.release_tag and not args.fetch_ffmpeg_only:
        try:
            app_ver, tag_info = verify_version_sync(BASE_DIR, expected_tag=args.release_tag)
            channel = "Pre-release" if (tag_info and tag_info["is_prerelease"]) else "Stable"
            print(f"  [OK] Versi rilis terverifikasi: {app_ver} ({args.release_tag} - {channel})")
        except ValueError as exc:
            print(f"[ERROR] Validasi versi rilis gagal: {exc}")
            return 1

    print("=" * 68)
    print("  SWARARIA AUDIO LAB - STANDALONE DESKTOP APP BUILDER")
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

    if args.verify_bundle:
        print("\n[4a/5] Memverifikasi kelengkapan paket distribusi...")
        bundle_errors = verify_dist_bundle(DIST_DIR)
        if bundle_errors:
            for err in bundle_errors:
                print(f"  [ERROR] {err}")
            return 1
        print("  [OK] Paket distribusi lengkap (executable, Web UI, dan FFmpeg runtime).")

    exe_name = "SwarariaAudioLab.exe" if os.name == "nt" else "SwarariaAudioLab"
    exe_path = DIST_DIR / exe_name

    if signing_config is not None:
        print("\n[4b/5] Menandatangani binary aplikasi desktop...")
        if not sign_binary(exe_path, signing_config):
            return 1

    print("\n" + "=" * 68)
    print("  [OK] BUILD DESKTOP APP SELESAI!")
    print("=" * 68)
    print(f"  Folder Distribusi : {DIST_DIR}")
    print(f"  File Executable   : {exe_path}")
    if args.installer:
        if not compile_inno_setup_installer(signing_config=signing_config):
            return 1
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

