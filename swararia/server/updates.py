"""GitHub release update checking and installation."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from swararia.server import network, state


def _parse_semver(ver_str: str) -> tuple[int, ...]:
    """Extracts numeric semver components from version strings like 'v1.0.2' or '1.0.0'."""
    clean = re.sub(r"^[vV]\.?", "", str(ver_str).strip())
    parts = []
    for token in clean.split("."):
        m = re.match(r"^(\d+)", token)
        if m:
            parts.append(int(m.group(1)))
        else:
            break
    return tuple(parts) or (0,)


def check_for_updates() -> dict:
    """Queries GitHub Releases API for dediks/swr-player-desktop and checks for new versions."""
    api_url = f"https://api.github.com/repos/{state.GITHUB_REPO}/releases/latest"
    headers = {
        "User-Agent": f"SwarariaAudioLab/{state.APP_VERSION}",
        "Accept": "application/vnd.github.v3+json",
    }

    # Optional GitHub token for private repo access
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token and state.GITHUB_TOKEN_FILE.is_file():
        try:
            token = state.GITHUB_TOKEN_FILE.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = Request(api_url, headers=headers)
    try:
        with urlopen(req, timeout=5.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            tag = str(data.get("tag_name", "")).strip()
            latest_tuple = _parse_semver(tag)
            current_tuple = _parse_semver(state.APP_VERSION)
            has_update = latest_tuple > current_tuple

            download_url = None
            asset_size = 0
            asset_name = ""
            for asset in data.get("assets", []):
                name = str(asset.get("name", ""))
                if name.lower().endswith(".exe"):
                    download_url = asset.get("browser_download_url")
                    asset_size = asset.get("size", 0)
                    asset_name = name
                    break

            return {
                "ok": True,
                "has_update": has_update,
                "current_version": state.APP_VERSION,
                "latest_version": tag,
                "release_name": data.get("name") or tag,
                "release_notes": data.get("body") or "",
                "html_url": data.get("html_url") or f"https://github.com/{state.GITHUB_REPO}/releases",
                "download_url": download_url,
                "asset_size": asset_size,
                "asset_name": asset_name,
                "published_at": data.get("published_at", ""),
            }
    except HTTPError as err:
        if err.code == 404:
            return {
                "ok": True,
                "has_update": False,
                "current_version": state.APP_VERSION,
                "latest_version": state.APP_VERSION,
                "message": "Belum ada rilis publik di GitHub Repository ini (atau repo privat butuh token).",
                "html_url": f"https://github.com/{state.GITHUB_REPO}",
            }
        elif err.code == 403:
            return {
                "ok": False,
                "has_update": False,
                "current_version": state.APP_VERSION,
                "error": "Akses GitHub API mencapai batas (rate limit) atau repo privat membutuhkan token.",
                "html_url": f"https://github.com/{state.GITHUB_REPO}",
            }
        return {
            "ok": False,
            "has_update": False,
            "current_version": state.APP_VERSION,
            "error": f"GitHub API HTTP {err.code}: {err.reason}",
            "html_url": f"https://github.com/{state.GITHUB_REPO}",
        }
    except URLError as err:
        return {
            "ok": False,
            "has_update": False,
            "current_version": state.APP_VERSION,
            "error": f"Gagal terhubung ke GitHub: {err.reason}",
            "html_url": f"https://github.com/{state.GITHUB_REPO}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "has_update": False,
            "current_version": state.APP_VERSION,
            "error": f"Kesalahan saat memeriksa pembaruan: {exc}",
            "html_url": f"https://github.com/{state.GITHUB_REPO}",
        }


def install_update_package(download_url: str) -> tuple[bool, str]:
    """Downloads the release asset installer and executes it for seamless update."""
    if not isinstance(download_url, str) or not download_url.startswith(f"https://github.com/{state.GITHUB_REPO}/releases/download/"):
        return False, "URL unduhan tidak sah atau di luar domain rilis resmi GitHub."

    temp_dir = Path(tempfile.gettempdir()).resolve()
    target_installer = temp_dir / "SwarariaAudioLab_Update_Setup.exe"

    headers = {"User-Agent": f"SwarariaAudioLab/{state.APP_VERSION}"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token and state.GITHUB_TOKEN_FILE.is_file():
        try:
            token = state.GITHUB_TOKEN_FILE.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = Request(download_url, headers=headers)
    try:
        with urlopen(req, timeout=120.0) as resp, open(target_installer, "wb") as out_f:
            shutil.copyfileobj(resp, out_f)
    except Exception as exc:
        return False, f"Gagal mengunduh installer pembaruan: {exc}"

    if not target_installer.is_file() or target_installer.stat().st_size < 1024 * 1024:
        return False, "File unduhan installer tidak lengkap atau rusak."

    try:
        if os.name == "nt":
            subprocess.Popen([str(target_installer), "/CLOSEAPPLICATIONS"])
        elif network.is_wsl_environment():
            pass

        def _delayed_shutdown():
            time.sleep(1.5)
            os._exit(0)

        threading.Thread(target=_delayed_shutdown, daemon=True).start()
        return True, "Installer pembaruan berhasil diunduh dan diluncurkan. Aplikasi akan ditutup untuk memperbarui."
    except Exception as exc:
        return False, f"Gagal menjalankan installer pembaruan: {exc}"
