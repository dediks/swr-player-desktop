"""Bundled/vendored binary discovery (ffmpeg, yt-dlp, JS runtime) and availability checks."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from swararia.config import REPO_ROOT


def _setup_bundled_environment() -> Path:
    """Prepends bundled/vendored binary and module directories to PATH and sys.path."""
    candidate_roots = [REPO_ROOT]
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidate_roots.insert(0, Path(meipass).resolve())
        exe_parent = Path(sys.executable).resolve().parent
        if exe_parent not in candidate_roots:
            candidate_roots.append(exe_parent)

    primary_vendor = candidate_roots[0] / ".vendor"
    for root in candidate_roots:
        vendor = root / ".vendor"
        if vendor.is_dir():
            primary_vendor = vendor
            if str(vendor) not in sys.path:
                sys.path.insert(0, str(vendor))
            os.environ["PYTHONPATH"] = str(vendor) + (
                os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""
            )
        for bin_dir in (root / ".vendor" / "bin", root / "bin", root):
            if bin_dir.is_dir():
                has_ff = (bin_dir / "ffmpeg").is_file() or (bin_dir / "ffmpeg.exe").is_file()
                has_yt = (bin_dir / "yt-dlp").is_file() or (bin_dir / "yt-dlp.exe").is_file()
                if has_ff or has_yt or bin_dir.name == "bin":
                    current_path = os.environ.get("PATH", "")
                    if str(bin_dir) not in current_path.split(os.pathsep):
                        os.environ["PATH"] = str(bin_dir) + (os.pathsep + current_path if current_path else "")
    return primary_vendor


_VENDOR_DIR = _setup_bundled_environment()


def _prepend_bin_to_env(env: dict[str, str], extra_bins: list[str], binary_path: str) -> None:
    bin_dir = str(Path(binary_path).parent)
    if bin_dir not in extra_bins:
        extra_bins.insert(0, bin_dir)
    if extra_bins:
        env["PATH"] = os.pathsep.join(extra_bins) + os.pathsep + env.get("PATH", "")


def find_js_runtime() -> tuple[str | None, dict[str, str]]:
    """Finds Node.js, Bun, Deno, or QuickJS runtime and returns (runtime_spec, env)."""
    env = os.environ.copy()
    extra_bins: list[str] = []
    local_bin = Path.home() / ".local" / "bin"
    if local_bin.is_dir():
        extra_bins.append(str(local_bin))

    node_bin = shutil.which("node")
    if not node_bin:
        nvm_nodes = sorted(Path.home().glob(".nvm/versions/node/*/bin/node"), reverse=True)
        if nvm_nodes:
            node_bin = str(nvm_nodes[0])
    if node_bin:
        _prepend_bin_to_env(env, extra_bins, node_bin)
        return f"node:{node_bin}", env

    bun_bin = shutil.which("bun")
    if not bun_bin:
        cand_bun = Path.home() / ".bun" / "bin" / "bun"
        if cand_bun.is_file():
            bun_bin = str(cand_bun)
    if bun_bin:
        _prepend_bin_to_env(env, extra_bins, bun_bin)
        return f"bun:{bun_bin}", env

    deno_bin = shutil.which("deno")
    if not deno_bin:
        cand_deno = Path.home() / ".deno" / "bin" / "deno"
        if cand_deno.is_file():
            deno_bin = str(cand_deno)
    if deno_bin:
        _prepend_bin_to_env(env, extra_bins, deno_bin)
        return f"deno:{deno_bin}", env

    qjs_bin = shutil.which("qjs")
    if qjs_bin:
        _prepend_bin_to_env(env, extra_bins, qjs_bin)
        return f"quickjs:{qjs_bin}", env

    if extra_bins:
        env["PATH"] = os.pathsep.join(extra_bins) + os.pathsep + env.get("PATH", "")
    return None, env


def is_ytdlp_available() -> bool:
    """Checks if yt-dlp is available either as a binary in PATH or as an importable Python module."""
    if shutil.which("yt-dlp") is not None:
        return True
    try:
        import yt_dlp  # noqa: F401
        return True
    except ImportError:
        return (_VENDOR_DIR / "yt_dlp").is_dir()


def is_mutagen_available() -> bool:
    """Checks if mutagen library is available for embedding album cover art into Opus/MP3."""
    try:
        import mutagen  # noqa: F401
        return True
    except ImportError:
        return (_VENDOR_DIR / "mutagen").is_dir()


def require(program: str) -> None:
    if program == "yt-dlp":
        if not is_ytdlp_available():
            raise RuntimeError("'yt-dlp' tidak ditemukan. Silakan install yt-dlp terlebih dahulu.")
        return
    if shutil.which(program) is None:
        raise RuntimeError(f"'{program}' tidak ditemukan di PATH. Silakan install terlebih dahulu.")
