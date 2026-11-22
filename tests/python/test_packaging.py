"""Packaging and desktop bundle size-optimization tests."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import types
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = ROOT / "packaging" / "SwarariaAudioLab.spec"
BUILDER_PATH = ROOT / "packaging" / "build_desktop.py"
ISS_PATH = ROOT / "packaging" / "installer.iss"


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


def test_installer_iss_guards_signtool_with_preprocessor():
    iss_text = ISS_PATH.read_text(encoding="utf-8")
    assert "#ifdef SignToolName" in iss_text
    assert "SignTool={#SignToolName}" in iss_text
    assert "SignedUninstaller=yes" in iss_text
    assert "#endif" in iss_text


def test_find_signtool_prefers_newest_windows_10_x64_sdk(tmp_path: Path):
    builder = _load_builder()
    kit_root = tmp_path / "pf86"
    old_x64 = kit_root / "Windows Kits" / "10" / "bin" / "10.0.19041.0" / "x64" / "signtool.exe"
    new_x86 = kit_root / "Windows Kits" / "10" / "bin" / "10.0.26100.0" / "x86" / "signtool.exe"
    new_x64 = kit_root / "Windows Kits" / "10" / "bin" / "10.0.26100.0" / "x64" / "signtool.exe"
    for candidate in (old_x64, new_x86, new_x64):
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_bytes(b"mz")

    with patch.object(builder.shutil, "which", return_value=None):
        found = builder.find_signtool(program_roots=(str(kit_root),))
    assert found == new_x64.resolve()

    explicit = tmp_path / "custom-signtool.exe"
    explicit.write_bytes(b"mz")
    assert builder.find_signtool(explicit_path=explicit) == explicit.resolve()
    assert builder.find_signtool(explicit_path=tmp_path / "missing.exe") is None


def test_resolve_signing_config_modes_and_validation(tmp_path: Path):
    builder = _load_builder()
    signtool = tmp_path / "signtool.exe"
    signtool.write_bytes(b"mz")
    pfx = tmp_path / "cert.pfx"
    pfx.write_bytes(b"pfx")

    assert builder.resolve_signing_config(environ={}) is None

    thumb_cfg = builder.resolve_signing_config(
        thumbprint="aa:bb cc:dd" + "00" * 16,
        signtool_path=str(signtool),
        machine_store=True,
        environ={},
    )
    assert thumb_cfg is not None
    assert thumb_cfg.mode == "thumbprint"
    assert thumb_cfg.thumbprint == "AABBCCDD" + "00" * 16
    assert thumb_cfg.machine_store is True
    assert thumb_cfg.signtool_path == signtool.resolve()

    pfx_cfg = builder.resolve_signing_config(
        environ={
            "SWARARIA_SIGN_PFX": str(pfx),
            "SWARARIA_SIGN_PFX_PASSWORD": "super-secret-password",
            "SWARARIA_SIGNTOOL_PATH": str(signtool),
            "SWARARIA_SIGN_TIMESTAMP_URL": "https://timestamp.example.test",
        }
    )
    assert pfx_cfg is not None
    assert pfx_cfg.mode == "pfx"
    assert pfx_cfg.pfx_path == pfx.resolve()
    assert pfx_cfg.pfx_password == "super-secret-password"
    assert pfx_cfg.timestamp_url == "https://timestamp.example.test"

    cmd_cfg = builder.resolve_signing_config(sign_command='azuresigntool sign "{file}"', environ={})
    assert cmd_cfg is not None
    assert cmd_cfg.mode == "command"

    for bad_kwargs in (
        {"thumbprint": "0" * 40, "pfx": str(pfx), "environ": {}},
        {"thumbprint": "not-hex", "signtool_path": str(signtool), "environ": {}},
        {"thumbprint": "0" * 40, "timestamp_url": "ftp://bad.example", "signtool_path": str(signtool), "environ": {}},
    ):
        try:
            builder.resolve_signing_config(**bad_kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Expected ValueError for {bad_kwargs}")

    try:
        builder.resolve_signing_config(pfx=str(tmp_path / "missing.pfx"), signtool_path=str(signtool), environ={})
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("Expected FileNotFoundError for missing PFX")


def test_build_sign_argv_and_sign_binary_redacts_pfx_password(tmp_path: Path):
    builder = _load_builder()
    signtool = tmp_path / "signtool.exe"
    signtool.write_bytes(b"mz")
    pfx = tmp_path / "cert.pfx"
    pfx.write_bytes(b"pfx")
    target = tmp_path / "SwarariaAudioLab.exe"
    target.write_bytes(b"exe")

    pfx_cfg = builder.SigningConfig(
        mode="pfx",
        pfx_path=pfx,
        pfx_password="top-secret-password",
        signtool_path=signtool,
    )
    argv = builder.build_sign_argv(target, pfx_cfg)
    assert argv == [
        str(signtool),
        "sign",
        "/fd",
        "SHA256",
        "/tr",
        builder.DEFAULT_SIGN_TIMESTAMP_URL,
        "/td",
        "SHA256",
        "/f",
        str(pfx),
        "/p",
        "top-secret-password",
        str(target),
    ]

    cmd_cfg = builder.SigningConfig(mode="command", command_template='my-signer --in "{file}"')
    assert builder.build_sign_argv(target, cmd_cfg) == ["my-signer", "--in", str(target)]

    fake_fail = types.SimpleNamespace(
        returncode=1,
        stdout="",
        stderr="SignTool Error: failed with /p top-secret-password on cert.pfx",
    )
    buf = io.StringIO()
    with patch.object(builder.subprocess, "run", return_value=fake_fail):
        with contextlib.redirect_stdout(buf):
            ok = builder.sign_binary(target, pfx_cfg)
    assert ok is False
    output = buf.getvalue()
    assert "top-secret-password" not in output
    assert "***" in output


def test_compile_inno_setup_installer_signing_and_sha256(tmp_path: Path):
    builder = _load_builder()
    pkg_dir = tmp_path / "packaging"
    dist_dir = tmp_path / "dist"
    pkg_dir.mkdir(parents=True)
    dist_dir.mkdir(parents=True)
    (pkg_dir / "installer.iss").write_text("[Setup]\n", encoding="utf-8")
    setup_exe = dist_dir / "SwarariaAudioLab_Setup.exe"
    setup_exe.write_bytes(b"setup-payload")
    iscc = tmp_path / "ISCC.exe"
    iscc.write_bytes(b"iscc")
    signtool = tmp_path / "signtool.exe"
    signtool.write_bytes(b"mz")
    pfx = tmp_path / "cert.pfx"
    pfx.write_bytes(b"pfx")

    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return types.SimpleNamespace(returncode=0)

    with (
        patch.object(builder, "PACKAGING_DIR", pkg_dir),
        patch.object(builder, "BASE_DIR", tmp_path),
        patch.object(builder, "find_inno_setup_compiler", return_value=iscc),
        patch.object(builder.subprocess, "run", side_effect=_fake_run),
    ):
        assert builder.compile_inno_setup_installer() is True
        cfg = builder.SigningConfig(
            mode="pfx",
            pfx_path=pfx,
            pfx_password="hidden-pfx-password",
            signtool_path=signtool,
        )
        assert builder.compile_inno_setup_installer(signing_config=cfg) is True

    unsigned_cmd, unsigned_kw = calls[0]
    assert unsigned_cmd == [str(iscc), str(pkg_dir / "installer.iss")]
    assert unsigned_kw.get("env") is None

    signed_cmd, signed_kw = calls[1]
    assert "/DSignToolName=swararia" in signed_cmd
    signtool_flags = [arg for arg in signed_cmd if arg.startswith("/Sswararia=")]
    assert len(signtool_flags) == 1
    assert "--sign-target $q$f$q" in signtool_flags[0]
    assert "hidden-pfx-password" not in " ".join(signed_cmd)
    assert signed_kw["env"]["SWARARIA_SIGN_PFX"] == str(pfx)
    assert signed_kw["env"]["SWARARIA_SIGN_PFX_PASSWORD"] == "hidden-pfx-password"

    sha_manifest = dist_dir / "SwarariaAudioLab_Setup.exe.sha256"
    assert sha_manifest.is_file()
    digest = builder.parse_sha256_checksum(sha_manifest.read_text(encoding="utf-8"), "SwarariaAudioLab_Setup.exe")
    assert digest == builder._sha256_file(setup_exe)


def test_main_sign_target_invokes_sign_binary_from_env(tmp_path: Path):
    builder = _load_builder()
    target = tmp_path / "uninst.exe"
    target.write_bytes(b"uninst")
    seen = []

    def _fake_run(argv, **kwargs):
        seen.append(argv)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    with (
        patch.dict(builder.os.environ, {"SWARARIA_SIGN_COMMAND": 'custom-signer "{file}"'}, clear=False),
        patch.object(builder.sys, "argv", ["build_desktop.py", "--sign-target", str(target)]),
        patch.object(builder.subprocess, "run", side_effect=_fake_run),
    ):
        assert builder.main() == 0

    assert seen == [["custom-signer", str(target.resolve())]]
