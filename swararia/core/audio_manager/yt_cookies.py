"""YouTube cookie parsing and subscription-tier detection (cached)."""
from __future__ import annotations

import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from swararia.core.audio_manager import constants


_COOKIES_TIER_CACHE_LOCK = threading.Lock()
_COOKIES_TIER_CACHE: dict[tuple[str, int, int], tuple[float, dict]] = {}


def _parse_netscape_cookies_for_url(cookies_file: Path | None, target_url: str) -> str:
    """Returns domain-, path-, scheme-, and expiry-matched HTTP Cookie header for target_url."""
    if not cookies_file or not Path(cookies_file).is_file():
        return ""
    parsed = urllib.parse.urlparse(target_url)
    host = (parsed.hostname or "").lower()
    req_path = parsed.path or "/"
    is_https = parsed.scheme.lower() == "https"
    if not host:
        return ""

    now_s = int(time.time())
    pairs: list[str] = []
    try:
        with open(cookies_file, "r", encoding="utf-8", errors="ignore") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                if line.startswith("#HttpOnly_"):
                    line = line[len("#HttpOnly_"):]
                elif line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 7:
                    continue
                domain_raw, _include_sub, path_val, secure_flag, exp_str, name, value = parts[:7]
                cookie_domain = domain_raw.strip().lower()
                cookie_path = path_val.strip() or "/"
                is_secure = secure_flag.strip().upper() == "TRUE"
                name = name.strip()
                if not cookie_domain or not name:
                    continue
                if is_secure and not is_https:
                    continue
                try:
                    exp_ts = int(exp_str.strip() or "0")
                except ValueError:
                    exp_ts = 0
                if exp_ts > 0 and exp_ts < now_s:
                    continue
                if name.startswith("__Host-"):
                    if not (is_https and is_secure and cookie_path == "/" and host == cookie_domain.lstrip(".")):
                        continue
                elif cookie_domain.startswith("."):
                    base_domain = cookie_domain[1:]
                    if host != base_domain and not host.endswith(cookie_domain):
                        continue
                elif host != cookie_domain:
                    continue
                if not req_path.startswith(cookie_path):
                    continue
                pairs.append(f"{name}={value}")
    except Exception:
        return ""
    return "; ".join(pairs)


def _load_cookie_header(cookies_file: Path | None, target_url: str = "https://music.youtube.com/") -> str:
    """Reads Netscape cookie file and formats into domain-matched HTTP Cookie header string."""
    return _parse_netscape_cookies_for_url(cookies_file, target_url)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Prevents urllib from following redirects automatically and leaking Cookie headers off-origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_url_no_redirect(req: urllib.request.Request, timeout: float = 6.0):
    """Uses a non-redirecting opener unless urlopen is patched by tests."""
    if getattr(urllib.request.urlopen, "__module__", "") != "urllib.request":
        return urllib.request.urlopen(req, timeout=timeout)
    opener = urllib.request.build_opener(_NoRedirectHandler())
    return opener.open(req, timeout=timeout)


def _fetch_youtube_html_with_cookies(start_url: str, cookies_file: Path, max_hops: int = 3) -> tuple[int | None, str, bool]:
    """Fetches bounded HTML (<=4MB) over HTTPS on *.youtube.com without leaking cookies off-origin."""
    current_url = start_url
    for _ in range(max_hops + 1):
        parsed = urllib.parse.urlparse(current_url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme.lower() != "https" or not (host == "youtube.com" or host.endswith(".youtube.com")):
            return None, "", "accounts.google." in host or "servicelogin" in current_url.lower()
        cookie_hdr = _parse_netscape_cookies_for_url(cookies_file, current_url)
        headers = {
            "User-Agent": constants._DESKTOP_CHROME_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
        }
        if cookie_hdr:
            headers["Cookie"] = cookie_hdr
        req = urllib.request.Request(current_url, headers=headers)
        try:
            with _open_url_no_redirect(req, timeout=6.0) as resp:
                final_url = ""
                try:
                    raw_final = resp.geturl()
                    if isinstance(raw_final, str):
                        final_url = raw_final
                except Exception:
                    final_url = ""
                final_low = final_url.lower()
                if final_low and ("accounts.google." in final_low or "servicelogin" in final_low):
                    return 302, "", True
                status = getattr(resp, "status", None)
                if not isinstance(status, int):
                    status = 200
                body = resp.read(4_000_001)[:4_000_000]
                return status, body.decode("utf-8", errors="ignore"), False
        except urllib.error.HTTPError as err:
            if err.code in (301, 302, 303, 307, 308):
                loc = err.headers.get("Location") if err.headers else None
                if not loc:
                    return err.code, "", False
                next_url = urllib.parse.urljoin(current_url, loc)
                next_parsed = urllib.parse.urlparse(next_url)
                next_host = (next_parsed.hostname or "").lower()
                if "accounts.google." in next_host or "servicelogin" in next_url.lower():
                    return err.code, "", True
                if next_parsed.scheme.lower() != "https" or not (next_host == "youtube.com" or next_host.endswith(".youtube.com")):
                    return err.code, "", False
                current_url = next_url
                continue
            return err.code, "", False
        except Exception:
            return None, "", False
    return None, "", False


def _cookie_cache_key(cookies_file: Path) -> tuple[str, int, int] | None:
    try:
        resolved = str(cookies_file.resolve())
        st = cookies_file.stat()
        return (resolved, int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        return None


def get_cached_youtube_cookies_tier(cookies_file: Path | None) -> dict | None:
    """Returns a fresh cached tier verdict for cookies_file without performing network I/O."""
    if not cookies_file or not Path(cookies_file).is_file():
        return None
    key = _cookie_cache_key(Path(cookies_file))
    if not key:
        return None
    now = time.time()
    with _COOKIES_TIER_CACHE_LOCK:
        cached = _COOKIES_TIER_CACHE.get(key)
        if cached and (now - cached[0]) < constants._COOKIES_TIER_TTL_S:
            return dict(cached[1])
    return None


def check_youtube_cookies_tier(cookies_file: Path | None, force_refresh: bool = False) -> dict:
    """Safely verifies whether cookies.txt carries an active YouTube / YT Music Premium session."""
    if not cookies_file or not Path(cookies_file).is_file():
        return {
            "present": False,
            "status": "missing",
            "tier": "unknown",
            "label": "Browser / Tidak Ada File",
            "note": "File library/cookies.txt tidak ditemukan.",
            "www_code": None,
            "music_code": None,
            "checked_at": int(time.time()),
        }

    cookies_path = Path(cookies_file)
    cache_key = _cookie_cache_key(cookies_path)
    if not force_refresh and cache_key:
        cached = get_cached_youtube_cookies_tier(cookies_path)
        if cached is not None:
            return cached

    yt_cookie_hdr = _parse_netscape_cookies_for_url(cookies_path, "https://music.youtube.com/")
    if not yt_cookie_hdr:
        res = {
            "present": True,
            "status": "unauthenticated",
            "tier": "free",
            "label": "Cookies Kosong / Kadaluarsa",
            "note": "Tidak ada cookie aktif untuk *.youtube.com di library/cookies.txt.",
            "www_code": None,
            "music_code": None,
            "checked_at": int(time.time()),
        }
        if cache_key:
            with _COOKIES_TIER_CACHE_LOCK:
                _COOKIES_TIER_CACHE[cache_key] = (time.time(), dict(res))
        return res

    www_code, www_html, www_redir = _fetch_youtube_html_with_cookies("https://www.youtube.com/", cookies_path)
    music_code, music_html, music_redir = _fetch_youtube_html_with_cookies("https://music.youtube.com/", cookies_path)

    def _logged_flag(html: str, redir: bool) -> bool | None:
        if redir or re.search(r'"LOGGED_IN"\s*:\s*false', html):
            return False
        if re.search(r'"LOGGED_IN"\s*:\s*true', html):
            return True
        return None

    www_logged = _logged_flag(www_html, www_redir)
    music_logged = _logged_flag(music_html, music_redir)
    music_is_sub = bool(re.search(r'"IS_SUBSCRIBER"\s*:\s*true', music_html))
    music_not_sub = bool(re.search(r'"IS_SUBSCRIBER"\s*:\s*false', music_html))
    has_yt_premium_logo = "YOUTUBE_PREMIUM_LOGO" in www_html
    has_music_premium_logo = "MUSIC_PREMIUM_LOGO" in music_html or "MUSIC_PREMIUM_LOGO" in www_html
    has_premium_tooltip = bool(
        re.search(r'"topbarLogoRenderer"\s*:\s*\{[^{}]{0,400}"tooltip"\s*:\s*"[^"]*Premium', www_html)
    )
    has_www_std_logo = "YOUTUBE_LOGO" in www_html and not (has_yt_premium_logo or has_premium_tooltip)

    www_premium = (has_yt_premium_logo or has_premium_tooltip) and www_logged is not False
    music_premium = (music_is_sub or has_music_premium_logo) and music_logged is not False

    if www_premium or music_premium:
        label = "YouTube Premium" if (has_yt_premium_logo or has_premium_tooltip) else "YT Music Premium"
        res = {
            "present": True,
            "status": "premium",
            "tier": "premium",
            "label": label,
            "note": "Sesi aktif • stream 256 kbps (Opus 774 / AAC 141) siap dipakai.",
            "www_code": www_code,
            "music_code": music_code,
            "checked_at": int(time.time()),
        }
    elif (www_logged is False or music_logged is False) and www_logged is not True and music_logged is not True:
        res = {
            "present": True,
            "status": "unauthenticated",
            "tier": "free",
            "label": "Cookies Kadaluarsa / Belum Login",
            "note": "Sesi di library/cookies.txt sudah kadaluarsa. Ekspor ulang cookies dari browser yang login.",
            "www_code": www_code,
            "music_code": music_code,
            "checked_at": int(time.time()),
        }
    elif (music_logged is True and music_not_sub) or (www_logged is True and has_www_std_logo):
        res = {
            "present": True,
            "status": "free",
            "tier": "free",
            "label": "Akun Standar (Gratis)",
            "note": "Cookies login valid, namun tidak ada langganan Premium aktif (maks ~160 kbps Opus 251).",
            "www_code": www_code,
            "music_code": music_code,
            "checked_at": int(time.time()),
        }
    else:
        res = {
            "present": True,
            "status": "inconclusive",
            "tier": "unknown",
            "label": "Belum Terverifikasi",
            "note": "Status Premium di library/cookies.txt belum bisa dipastikan dari respons YouTube.",
            "www_code": www_code,
            "music_code": music_code,
            "checked_at": int(time.time()),
        }

    if cache_key:
        with _COOKIES_TIER_CACHE_LOCK:
            _COOKIES_TIER_CACHE[cache_key] = (time.time(), dict(res))
    return res
