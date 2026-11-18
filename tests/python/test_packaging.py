"""Packaging and desktop bundle size-optimization tests."""
from __future__ import annotations

import importlib.util
import sys
import types
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = ROOT / "packaging" / "SwarariaAudioLab.spec"
BUILDER_PATH = ROOT / "packaging" / "build_desktop.py"


def _load_builder():
    spec = importlib.util.spec_from_file_location("test_build_desktop", BUILDER_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def _eval_pyinstaller_spec(project_dir: Path, target_platform: str) -> dict:
    """Executes packaging/SwarariaAudioLab.spec with stubbed PyInstaller globals."""
    captured: dict = {}

    class _FakeAnalysis:
        def __init__(self, scripts, **kwargs):
            captured["scripts"] = scripts
            captured["analysis"] = kwargs
            self.scripts = scripts
            self.binaries = list(kwargs.get("binaries", []))
            self.zipfiles = []
            self.datas = list(kwargs.get("datas", []))
            self.pure = []
            self.zipped_data = []

    class _FakeStage:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    fake_hooks = types.ModuleType("PyInstaller.utils.hooks")
    fake_hooks.collect_all = lambda name: (
        [(f"/env/{name}/res.dat", name), (f"/env/{name}/__pyinstaller/hook.py", f"{name}/__pyinstaller")],
        [(f"/env/{name}/native.dll", name), (f"/env/{name}/__pyinstaller/native.dll", name)],
        [name, f"{name}.__pyinstaller", f"{name}.__pyinstaller.hook"],
    )
    fake_hooks.collect_submodules = lambda name: [name, f"{name}.sub"]

    fake_pyinstaller = types.ModuleType("PyInstaller")
    fake_utils = types.ModuleType("PyInstaller.utils")
    spec_dir = project_dir / "packaging"
    spec_code = SPEC_PATH.read_text(encoding="utf-8")

    ns = {
        "__file__": str(spec_dir / "SwarariaAudioLab.spec"),
        "SPECPATH": str(spec_dir),
        "Analysis": _FakeAnalysis,
        "PYZ": _FakeStage,
        "EXE": _FakeStage,
        "COLLECT": _FakeStage,
        "BUNDLE": _FakeStage,
    }
    orig_platform = sys.platform
    try:
        sys.platform = target_platform
        with patch.dict(
            sys.modules,
            {
                "PyInstaller": fake_pyinstaller,
                "PyInstaller.utils": fake_utils,
                "PyInstaller.utils.hooks": fake_hooks,
            },
        ):
            exec(compile(spec_code, str(SPEC_PATH), "exec"), ns)
    finally:
        sys.platform = orig_platform
    return captured


def test_spec_deduplicates_ytdlp_and_filters_vendor_binaries_by_platform(tmp_path: Path):
    pkg_dir = tmp_path / "packaging"
    pkg_dir.mkdir(parents=True)
    (tmp_path / "swararia" / "web" / "static").mkdir(parents=True)

    # Mock vendored yt_dlp with .py source, multi-version .pyc caches, build-time hook, and one .js asset
    yt_vendor = tmp_path / ".vendor" / "yt_dlp"
    (yt_vendor / "__pycache__").mkdir(parents=True)
    (yt_vendor / "__pyinstaller").mkdir(parents=True)
    (yt_vendor / "extractor" / "youtube" / "__pycache__").mkdir(parents=True)
    (yt_vendor / "__init__.py").write_text("", encoding="utf-8")
    (yt_vendor / "__pyinstaller" / "__init__.py").write_text("", encoding="utf-8")
    (yt_vendor / "__pyinstaller" / "hook-yt_dlp.py").write_text("# hook", encoding="utf-8")
    (yt_vendor / "__pyinstaller" / "hook.dat").write_text("hook data", encoding="utf-8")
    (yt_vendor / "extractor" / "__init__.py").write_text("", encoding="utf-8")
    (yt_vendor / "extractor" / "youtube" / "__init__.py").write_text("", encoding="utf-8")
    (yt_vendor / "__pycache__" / "__init__.cpython-311.pyc").write_bytes(b"pyc")
    (yt_vendor / "extractor" / "youtube" / "__pycache__" / "__init__.cpython-312.pyc").write_bytes(b"pyc")
    (yt_vendor / "extractor" / "youtube" / "solver.js").write_text("// js", encoding="utf-8")

    # Mock mixed OS binaries in .vendor/bin
    vbin = tmp_path / ".vendor" / "bin"
    vbin.mkdir(parents=True)
    for name in ("ffmpeg.exe", "ffprobe.exe", "avcodec-63.dll", "ffplay.exe", "ffmpeg.pdb", "ffmpeg", "ffprobe", "yt-dlp"):
        (vbin / name).write_bytes(b"bin")

    win_res = _eval_pyinstaller_spec(tmp_path, "win32")["analysis"]
    win_data_srcs = {Path(src).name for src, _ in win_res["datas"]}
    win_data_pairs = [(Path(src).name, dest) for src, dest in win_res["datas"]]
    assert "solver.js" in win_data_srcs
    assert "hook.dat" not in win_data_srcs
    assert "__init__.py" not in win_data_srcs
    assert "__init__.cpython-311.pyc" not in win_data_srcs
    assert "__init__.cpython-312.pyc" not in win_data_srcs
    assert ("solver.js", "yt_dlp/extractor/youtube") in win_data_pairs or (
        "solver.js",
        "yt_dlp\\extractor\\youtube",
    ) in win_data_pairs

    assert {"ffmpeg.exe", "ffprobe.exe", "avcodec-63.dll"}.issubset(win_data_srcs)
    for unwanted in ("ffplay.exe", "ffmpeg.pdb", "ffmpeg", "ffprobe", "yt-dlp"):
        assert unwanted not in win_data_srcs

    # External FFmpeg DLLs must not go through PyInstaller binaries scanning (prevents root duplication)
    win_bin_names = {Path(src).name for src, _ in win_res["binaries"]}
    assert "avcodec-63.dll" not in win_bin_names
    assert "ffmpeg.exe" not in win_bin_names

    # Build-time PyInstaller hooks must be excluded
    assert "PyInstaller" in win_res["excludes"]
    assert "webview.__pyinstaller" in win_res["excludes"]
    assert "yt_dlp.__pyinstaller" in win_res["excludes"]
    assert all("pyinstaller" not in m.lower() for m in win_res["hiddenimports"])
    assert "yt_dlp.extractor.youtube" in win_res["hiddenimports"]

    # Linux build skips .exe and .dll from .vendor/bin
    linux_res = _eval_pyinstaller_spec(tmp_path, "linux")["analysis"]
    linux_data_srcs = {Path(src).name for src, _ in linux_res["datas"]}
    assert {"ffmpeg", "ffprobe"}.issubset(linux_data_srcs)
    for win_only in ("ffmpeg.exe", "ffprobe.exe", "avcodec-63.dll"):
        assert win_only not in linux_data_srcs


def test_extract_shared_ffmpeg_runtime_filters_files_and_preserves_posix(tmp_path: Path):
    builder = _load_builder()
    archive = tmp_path / "ffmpeg-shared.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("ffmpeg-test/bin/ffmpeg.exe", b"new-ffmpeg")
        zf.writestr("ffmpeg-test/bin/ffprobe.exe", b"new-ffprobe")
        zf.writestr("ffmpeg-test/bin/avcodec-63.dll", b"avcodec")
        zf.writestr("ffmpeg-test/bin/swresample-7.dll", b"swresample")
        zf.writestr("ffmpeg-test/bin/ffplay.exe", b"drop-me")
        zf.writestr("ffmpeg-test/lib/avcodec.lib", b"drop-lib")
        zf.writestr("ffmpeg-test/include/libavcodec/avcodec.h", b"drop-header")

    dest = tmp_path / ".vendor" / "bin"
    dest.mkdir(parents=True)
    (dest / "ffmpeg.exe").write_bytes(b"old-208mb-static")
    (dest / "ffprobe.exe").write_bytes(b"old-208mb-static")
    (dest / "ffplay.exe").write_bytes(b"stale-ffplay")
    (dest / "old-avcodec-60.dll").write_bytes(b"stale-dll")
    (dest / "ffmpeg").write_bytes(b"posix-ffmpeg")

    installed = builder.extract_shared_ffmpeg_runtime(archive, dest)
    names = {p.name for p in installed}
    assert names == {"ffmpeg.exe", "ffprobe.exe", "avcodec-63.dll", "swresample-7.dll"}
    assert (dest / "ffmpeg.exe").read_bytes() == b"new-ffmpeg"
    assert not (dest / "ffplay.exe").exists()
    assert not (dest / "old-avcodec-60.dll").exists()
    assert (dest / "ffmpeg").read_bytes() == b"posix-ffmpeg"


def test_extract_shared_ffmpeg_runtime_blocks_zip_slip(tmp_path: Path):
    builder = _load_builder()
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("ffmpeg-test/bin/../../evil.dll", b"bad")

    dest = tmp_path / "out"
    try:
        builder.extract_shared_ffmpeg_runtime(archive, dest)
    except ValueError as exc:
        assert "Path tidak aman" in str(exc)
    else:
        raise AssertionError("Expected ValueError on zip-slip path")


def test_parse_sha256_checksum_matches_exact_asset():
    builder = _load_builder()
    digest = "a" * 64
    manifest = f"{'b' * 64}  other.zip\n{digest} *ffmpeg-n9.0-latest-win64-gpl-shared-9.0.zip\n"
    assert builder.parse_sha256_checksum(manifest, "ffmpeg-n9.0-latest-win64-gpl-shared-9.0.zip") == digest
    assert builder.parse_sha256_checksum(manifest, "missing.zip") is None
