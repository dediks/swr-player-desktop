"""YouTube playlist inspection and preview stream resolution (cached)."""
from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from swararia.core.audio_manager import constants, toolchain, yt_cookies, yt_search, ytdlp_engine


_PLAYLIST_CACHE: dict[str, tuple[dict, float]] = {}


def extract_playlist_canonical_and_browse_ids(url_or_id: str) -> tuple[str, str, str]:
    """Returns (canonical_id, browse_id, canonical_url) for a playlist or album."""
    clean = (url_or_id or "").strip()
    if not clean:
        return "", "", ""
    parsed = urllib.parse.urlparse(clean)
    qs = urllib.parse.parse_qs(parsed.query)
    raw_id = qs.get("list", [None])[0]
    if not raw_id:
        path_parts = [p for p in parsed.path.split("/") if p]
        if path_parts and any(path_parts[-1].startswith(prefix) for prefix in ("MPREb_", "VL", "PL", "OLAK5uy_")):
            raw_id = path_parts[-1]
        else:
            raw_id = clean

    if raw_id.startswith("VL"):
        canon_id = raw_id[2:]
        browse_id = raw_id
    elif raw_id.startswith("MPREb_"):
        canon_id = raw_id
        browse_id = raw_id
    else:
        canon_id = raw_id
        browse_id = f"VL{raw_id}"

    if canon_id.startswith("MPREb_"):
        canon_url = f"https://music.youtube.com/browse/{canon_id}"
    else:
        canon_url = f"https://music.youtube.com/playlist?list={canon_id}"

    return canon_id, browse_id, canon_url


def _inspect_playlist_innertube(
    browse_id: str,
    limit: int = 100,
    cookies_file: Path | None = None,
) -> dict | None:
    """Fetches tracklist from YouTube Music Innertube browse API."""
    api_url = "https://music.youtube.com/youtubei/v1/browse"
    payload_data = {
        "context": {
            "client": {
                "clientName": "WEB_REMIX",
                "clientVersion": "1.20240401.01.00",
                "hl": "id",
                "gl": "ID",
            }
        },
        "browseId": browse_id,
    }
    req_body = json.dumps(payload_data).encode("utf-8")
    req = urllib.request.Request(
        api_url,
        data=req_body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Origin": "https://music.youtube.com",
            "Referer": "https://music.youtube.com/",
        },
    )
    cookie_header = yt_cookies._load_cookie_header(cookies_file)
    if cookie_header:
        req.add_header("Cookie", cookie_header)

    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            if resp.status != 200:
                return None
            resp_data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None

    title = ""
    artist = ""
    thumb_url = ""
    second_subtitle = ""

    header = resp_data.get("header", {})
    hdr_data = (
        header.get("musicDetailHeaderRenderer")
        or header.get("musicResponsiveHeaderRenderer")
        or header.get("musicEditablePlaylistDetailHeaderRenderer")
        or {}
    )

    if hdr_data:
        t_runs = hdr_data.get("title", {}).get("runs", [])
        if t_runs:
            title = "".join(r.get("text", "") for r in t_runs).strip()
        s_runs = hdr_data.get("subtitle", {}).get("runs", [])
        if s_runs:
            artist = "".join(r.get("text", "") for r in s_runs if (r.get("text") or "").strip() != "•").strip()
        sec_runs = hdr_data.get("secondSubtitle", {}).get("runs", [])
        if sec_runs:
            second_subtitle = "".join(r.get("text", "") for r in sec_runs).strip()
        thumbs = (
            hdr_data.get("thumbnail", {})
            .get("croppedSquareThumbnailRenderer", {})
            .get("thumbnail", {})
            .get("thumbnails", [])
            or hdr_data.get("thumbnail", {})
            .get("musicThumbnailRenderer", {})
            .get("thumbnail", {})
            .get("thumbnails", [])
        )
        if thumbs:
            thumb_url = thumbs[-1].get("url", "")
            if thumb_url.startswith("//"):
                thumb_url = "https:" + thumb_url

    tracks: list[dict] = []
    seen_ids: set[str] = set()

    def walk(obj: any):
        if len(tracks) >= limit:
            return
        if isinstance(obj, dict):
            if "musicResponsiveListItemRenderer" in obj:
                parsed = yt_search._parse_music_responsive_item(obj["musicResponsiveListItemRenderer"])
                if parsed and parsed.get("id") and parsed["id"] not in seen_ids:
                    seen_ids.add(parsed["id"])
                    parsed["index"] = len(tracks) + 1
                    if not parsed.get("url") or "playlist" in parsed["url"]:
                        parsed["url"] = f"https://music.youtube.com/watch?v={parsed['id']}"
                    parsed["type"] = "song"
                    tracks.append(parsed)
                    if len(tracks) >= limit:
                        return
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for it in obj:
                walk(it)

    walk(resp_data.get("contents", resp_data))
    if not tracks and not title:
        return None

    if not thumb_url and tracks and tracks[0].get("thumbnail"):
        thumb_url = tracks[0]["thumbnail"]

    return {
        "title": title or "Playlist",
        "artist": artist,
        "thumbnail": thumb_url,
        "track_count": len(tracks),
        "subtitle": second_subtitle,
        "tracks": tracks,
    }


def _inspect_playlist_ytdlp(
    canonical_url: str,
    limit: int = 100,
    cookies_file: Path | None = None,
) -> dict | None:
    """Fallback tracklist extraction using in-process yt_dlp or CLI."""
    # 1. Try yt_dlp Python module
    try:
        import yt_dlp
        ydl_opts = {
            "extract_flat": True,
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "socket_timeout": 15,
            "playlist_end": max(1, min(limit, 300)),
        }
        if cookies_file and Path(cookies_file).is_file():
            ydl_opts["cookiefile"] = str(cookies_file)

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(canonical_url, download=False)
            if info:
                title = info.get("title") or "Playlist"
                artist = info.get("uploader") or info.get("channel") or ""
                thumb = info.get("thumbnail") or ""
                if not thumb and info.get("thumbnails"):
                    thumb = info["thumbnails"][-1].get("url", "")

                entries = info.get("entries") or []
                tracks: list[dict] = []
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    v_id = entry.get("id")
                    if not v_id:
                        continue
                    t_title = entry.get("title") or "Unknown Title"
                    t_artist = entry.get("uploader") or entry.get("channel") or ""
                    dur_s = entry.get("duration")
                    dur_str = ""
                    if isinstance(dur_s, (int, float)) and dur_s > 0:
                        secs = max(0, int(round(dur_s)))
                        mins, rem = divmod(secs, 60)
                        hrs, mins = divmod(mins, 60)
                        dur_str = f"{hrs:02d}:{mins:02d}:{rem:02d}" if hrs > 0 else f"{mins:02d}:{rem:02d}"

                    t_thumb = entry.get("thumbnail") or ""
                    if not t_thumb and entry.get("thumbnails"):
                        t_thumb = entry["thumbnails"][-1].get("url", "")

                    tracks.append({
                        "id": v_id,
                        "index": len(tracks) + 1,
                        "title": t_title,
                        "artist": t_artist,
                        "album": "",
                        "duration": dur_str,
                        "thumbnail": t_thumb,
                        "url": f"https://music.youtube.com/watch?v={v_id}",
                        "type": "song",
                        "audio_tier": "hq",
                    })
                    if len(tracks) >= limit:
                        break

                return {
                    "title": title,
                    "artist": artist,
                    "thumbnail": thumb,
                    "track_count": len(tracks),
                    "tracks": tracks,
                }
    except Exception:
        pass

    # 2. Try CLI subprocess
    try:
        cmd = list(ytdlp_engine.get_ytdlp_base_cmd())
        cmd.extend([
            "--dump-json",
            "--flat-playlist",
            "--no-warnings",
            "--ignore-errors",
            "--socket-timeout", "15",
            "--playlist-end", str(max(1, min(limit, 300))),
        ])
        if cookies_file and Path(cookies_file).is_file():
            cmd.extend(["--cookies", str(cookies_file)])
        cmd.append(canonical_url)

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        tracks = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            v_id = entry.get("id")
            if not v_id:
                continue
            t_title = entry.get("title") or "Unknown Title"
            t_artist = entry.get("uploader") or entry.get("channel") or ""
            dur_s = entry.get("duration")
            dur_str = ""
            if isinstance(dur_s, (int, float)) and dur_s > 0:
                secs = max(0, int(round(dur_s)))
                mins, rem = divmod(secs, 60)
                hrs, mins = divmod(mins, 60)
                dur_str = f"{hrs:02d}:{mins:02d}:{rem:02d}" if hrs > 0 else f"{mins:02d}:{rem:02d}"

            t_thumb = entry.get("thumbnail") or ""
            if not t_thumb and entry.get("thumbnails"):
                t_thumb = entry["thumbnails"][-1].get("url", "")

            tracks.append({
                "id": v_id,
                "index": len(tracks) + 1,
                "title": t_title,
                "artist": t_artist,
                "album": "",
                "duration": dur_str,
                "thumbnail": t_thumb,
                "url": f"https://music.youtube.com/watch?v={v_id}",
                "type": "song",
                "audio_tier": "hq",
            })
            if len(tracks) >= limit:
                break

        if tracks:
            return {
                "title": "Playlist",
                "artist": "",
                "thumbnail": tracks[0]["thumbnail"] if tracks else "",
                "track_count": len(tracks),
                "tracks": tracks,
            }
    except Exception:
        pass

    return None


def inspect_playlist(
    url_or_id: str,
    limit: int = 100,
    cookies_file: Path | None = None,
) -> dict | None:
    """Inspects playlist or album metadata and tracklist."""
    canon_id, browse_id, canon_url = extract_playlist_canonical_and_browse_ids(url_or_id)
    if not canon_id:
        return None

    cache_key = f"{canon_id}:{limit}"
    now = time.time()
    if cache_key in _PLAYLIST_CACHE:
        cached_data, expire = _PLAYLIST_CACHE[cache_key]
        if now < expire:
            return cached_data

    # 1. Primary: Innertube
    data = _inspect_playlist_innertube(browse_id, limit=limit, cookies_file=cookies_file)
    if not data or not data.get("tracks"):
        # 2. Fallback: yt-dlp
        data = _inspect_playlist_ytdlp(canon_url, limit=limit, cookies_file=cookies_file)

    if data:
        data["id"] = canon_id
        data["url"] = canon_url
        data["type"] = "album" if (canon_id.startswith("MPREb_") or canon_id.startswith("OLAK5uy_")) else "playlist"
        _PLAYLIST_CACHE[cache_key] = (data, now + 600)
        return data

    return None


_STREAM_INFO_CACHE: dict[str, tuple[str, dict, float]] = {}


def get_preview_stream_info(url: str, cookies_file: Path | None = None) -> tuple[str, dict] | None:
    """Extracts direct audio stream URL and HTTP headers using yt-dlp with caching."""
    clean_url = (url or "").strip()
    if not clean_url:
        return None

    now = time.time()
    if clean_url in _STREAM_INFO_CACHE:
        s_url, s_headers, expire = _STREAM_INFO_CACHE[clean_url]
        if now < expire:
            return s_url, s_headers

    # Try in-process yt_dlp first
    cookies_path = Path(cookies_file) if cookies_file and Path(cookies_file).is_file() else None
    use_premium_preview = False
    if cookies_path:
        tier_info = yt_cookies.get_cached_youtube_cookies_tier(cookies_path)
        use_premium_preview = bool(tier_info and tier_info.get("tier") == "premium")

    try:
        import yt_dlp
        ydl_opts = {
            "format": "ba[format_id!*=-drc]/ba",
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "socket_timeout": 10,
            "noplaylist": True,
        }
        if cookies_path:
            ydl_opts["cookiefile"] = str(cookies_path)
        if use_premium_preview:
            ydl_opts["extractor_args"] = {
                "youtube": {
                    "player_client": ["web_music", "web_creator", "tv_samsung", "web"],
                    "formats": ["missing_pot"],
                }
            }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(clean_url, download=False)
            if info:
                if "entries" in info and info["entries"]:
                    info = info["entries"][0]
                stream_url = info.get("url")
                http_headers = info.get("http_headers") or {}

                # If no direct url, pick the best audio format from formats list
                if not stream_url and "formats" in info:
                    formats = info["formats"]
                    audio_fmts = [
                        f for f in formats
                        if f.get("url") and f.get("acodec") != "none" and f.get("vcodec") == "none"
                    ]
                    if not audio_fmts:
                        audio_fmts = [f for f in formats if f.get("url") and f.get("acodec") != "none"]

                    def _rank_audio(f):
                        fid = str(f.get("format_id") or "")
                        score = 0
                        if fid == "774":
                            score += 1200
                        elif fid in ("251", "141"):
                            score += 1000
                        elif fid == "140":
                            score += 500
                        elif f.get("vcodec") == "none":
                            score += 200
                        score += int(f.get("abr") or f.get("tbr") or 0)
                        return score

                    audio_fmts.sort(key=_rank_audio, reverse=True)
                    if audio_fmts:
                        best = audio_fmts[0]
                        stream_url = best.get("url")
                        http_headers = best.get("http_headers") or http_headers

                if stream_url:
                    _STREAM_INFO_CACHE[clean_url] = (stream_url, http_headers, now + 3600)
                    return stream_url, http_headers
    except Exception:
        pass

    # Fallback to get_ytdlp_base_cmd() -g
    try:
        cmd = list(ytdlp_engine.get_ytdlp_base_cmd())
        cmd.extend([
            "-g",
            "-f", "ba[format_id!*=-drc]/ba",
            "--no-warnings",
            "--socket-timeout", "20",
            "--no-playlist",
        ])
        js_spec, _ = toolchain.find_js_runtime()
        if js_spec:
            cmd.extend(["--js-runtimes", js_spec])
        if cookies_path:
            cmd.extend(["--cookies", str(cookies_path)])
        if use_premium_preview:
            cmd.extend(["--extractor-args", constants.YOUTUBE_PREMIUM_EXTRACTOR_ARGS])
        cmd.append(clean_url)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        if proc.returncode == 0:
            lines = [line.strip() for line in proc.stdout.splitlines() if line.strip().startswith("http")]
            if lines:
                _STREAM_INFO_CACHE[clean_url] = (lines[0], {}, now + 3600)
                return lines[0], {}
    except Exception:
        pass

    return None
