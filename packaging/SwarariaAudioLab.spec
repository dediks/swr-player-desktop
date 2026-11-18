# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller Specification for SWARARIA AUDIO LAB Desktop App."""
import os
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None
project_dir = Path(SPECPATH).resolve().parent  # spec lives in packaging/

# 1. Collect static web UI assets
datas = [
    (str(project_dir / "swararia" / "web" / "static"), "swararia/web/static"),
]

binaries = []
hiddenimports = [
    "swararia.server.app",
    "swararia.remote.secure_remote",
    "swararia.remote.secure_remote_tls",
    "swararia.core.audio_manager",
    "swararia.core.youtube_login",
    "swararia.core.task_engine",
    "swararia.core.fs_browser",
    "swararia.core.splitter_engine",
]
hiddenimports += collect_submodules("swararia")

def _append_collected(ret: tuple[list, list, list]) -> None:
    """Appends collect_all output while skipping __pycache__ and build-time PyInstaller hooks."""
    global datas, binaries, hiddenimports
    d_list, b_list, h_list = ret
    datas += [d for d in d_list if "__pycache__" not in Path(d[0]).parts and "__pyinstaller" not in d[0]]
    binaries += [b for b in b_list if "__pyinstaller" not in b[0]]
    hiddenimports += [m for m in h_list if ".__pyinstaller" not in m]


# Session TLS runtime, including cryptography's native Rust extension. Fail the
# package preparation if absent; never ship an insecure HTTP fallback.
_append_collected(collect_all("cryptography"))


def _iter_package_submodules(pkg_dir: Path) -> list[str]:
    """Lists dotted module names inside a vendored Python package without subprocess import."""
    mods: list[str] = []
    pkg_name = pkg_dir.name
    for py_file in sorted(pkg_dir.rglob("*.py")):
        if "__pycache__" in py_file.parts or "__pyinstaller" in py_file.parts:
            continue
        parts = list(py_file.relative_to(pkg_dir).parts)
        if parts[-1] == "__init__.py":
            parts = parts[:-1]
        else:
            parts[-1] = py_file.stem
        if not all(part.isidentifier() for part in parts):
            continue
        mods.append(".".join([pkg_name, *parts]))
    return mods


# 2. Bundle vendored yt_dlp bytecode once in PYZ (skip duplicate .py / __pycache__)
vendor_dir = project_dir / ".vendor"
vendor_ytdlp = vendor_dir / "yt_dlp"
if vendor_ytdlp.is_dir():
    hiddenimports += _iter_package_submodules(vendor_ytdlp)
    for item in sorted(vendor_ytdlp.rglob("*")):
        if (
            item.is_file()
            and "__pycache__" not in item.parts
            and "__pyinstaller" not in item.parts
            and item.suffix.lower() not in (".py", ".pyc", ".pyo")
        ):
            rel_parent = item.relative_to(vendor_ytdlp).parent
            dest_dir = os.path.join("yt_dlp", str(rel_parent)) if str(rel_parent) != "." else "yt_dlp"
            datas.append((str(item), dest_dir))
else:
    try:
        _append_collected(collect_all("yt_dlp"))
    except Exception:
        pass

try:
    _append_collected(collect_all("yt_dlp_ejs"))
except Exception:
    pass

# 3. Collect pywebview if installed (excluding its build-time PyInstaller hook)
try:
    _append_collected(collect_all("webview"))
except Exception:
    pass


def _should_bundle_vendor_bin(path: Path) -> bool:
    """Includes runtime FFmpeg executables/shared libs matching the target platform."""
    name = path.name.lower()
    suffix = path.suffix.lower()
    if name in ("ffplay", "ffplay.exe", "yt-dlp", "yt-dlp.exe"):
        return False
    if suffix in (".pdb", ".lib", ".a", ".h", ".exp", ".def"):
        return False
    if sys.platform == "win32":
        return suffix in (".exe", ".dll")
    return suffix not in (".exe", ".dll")


# 4. Bundle portable ffmpeg / ffprobe runtime files as datas (not binaries) so
# PyInstaller's bindepend scanner does not duplicate shared FFmpeg DLLs into root.
for bin_folder in (project_dir / ".vendor" / "bin", project_dir / "bin"):
    if bin_folder.is_dir():
        for item in sorted(bin_folder.iterdir()):
            if item.is_file() and _should_bundle_vendor_bin(item):
                datas.append((str(item), os.path.join(".vendor", "bin")))

a = Analysis(
    [str(project_dir / "packaging" / "launcher.py")],
    pathex=[str(project_dir), str(project_dir / ".vendor")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "test", "PyInstaller", "webview.__pyinstaller", "yt_dlp.__pyinstaller"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SwarariaAudioLab",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # Windowed mode (no black terminal popup on Windows/macOS)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="SwarariaAudioLab",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="SwarariaAudioLab.app",
        icon=None,
        bundle_identifier="com.swararia.audiolab",
    )

