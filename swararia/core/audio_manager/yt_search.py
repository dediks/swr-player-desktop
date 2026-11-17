"""YouTube Music search (InnerTube API with yt-dlp fallback) and result parsing."""
from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from swararia.core.audio_manager import yt_cookies, ytdlp_engine


def _parse_music_responsive_item(renderer: dict) -> dict | None:
    """Parses a musicResponsiveListItemRenderer from YouTube Music Innertube response."""
    if not isinstance(renderer, dict):
        return None

    video_id = renderer.get("playlistItemData", {}).get("videoId")
    nav_endpoint = renderer.get("navigationEndpoint") or {}
    watch_endpoint = nav_endpoint.get("watchEndpoint") or {}
    browse_endpoint = nav_endpoint.get("browseEndpoint") or {}

    if not video_id:
        video_id = watch_endpoint.get("videoId")

    playlist_id = watch_endpoint.get("playlistId") or browse_endpoint.get("browseId")
    if playlist_id and str(playlist_id).startswith("VL"):
        playlist_id = str(playlist_id)[2:]

    flex_columns = renderer.get("flexColumns", [])
    title = ""
    artists: list[str] = []
    album = ""
    duration = ""
    track_count = ""

    # Column 0: Title
    if flex_columns:
        col0_runs = (
            flex_columns[0]
            .get("musicResponsiveListItemFlexColumnRenderer", {})
            .get("text", {})
            .get("runs", [])
        )
        if col0_runs:
            title = "".join(r.get("text", "") for r in col0_runs).strip()
            if not video_id:
                t_nav = col0_runs[0].get("navigationEndpoint") or {}
                video_id = t_nav.get("watchEndpoint", {}).get("videoId")
                if not playlist_id:
                    playlist_id = t_nav.get("watchEndpoint", {}).get("playlistId") or t_nav.get("browseEndpoint", {}).get("browseId")

    media_kind = ""
    # Column 1: Subtitle / Meta
    if len(flex_columns) > 1:
        col1_runs = (
            flex_columns[1]
            .get("musicResponsiveListItemFlexColumnRenderer", {})
            .get("text", {})
            .get("runs", [])
        )
        if col1_runs:
            first_raw = (col1_runs[0].get("text") or "").strip().lower()
            if "video" in first_raw:
                media_kind = "video"
            elif first_raw in ("lagu", "song", "singel", "single"):
                media_kind = "song"
            elif first_raw == "album":
                media_kind = "album"
            elif first_raw in ("daftar putar", "playlist"):
                media_kind = "playlist"

        for r in col1_runs:
            txt = (r.get("text") or "").strip()
            if not txt or txt == "•":
                continue
            nav = r.get("navigationEndpoint") or {}
            browse_config = (
                nav.get("browseEndpoint", {})
                .get("browseEndpointContextSupportedConfigs", {})
                .get("browseEndpointContextMusicConfig", {})
            )
            page_type = browse_config.get("pageType")
            if page_type == "MUSIC_PAGE_TYPE_ARTIST":
                artists.append(txt)
            elif page_type == "MUSIC_PAGE_TYPE_ALBUM":
                album = txt
            elif ":" in txt and any(ch.isdigit() for ch in txt):
                duration = txt
            elif any(tag in txt.lower() for tag in ("lagu", "track", "song")) and any(ch.isdigit() for ch in txt):
                track_count = txt
            elif not artists and not any(tag in txt.lower() for tag in ("song", "lagu", "video", "track", "singel")):
                artists.append(txt)

    fixed_columns = renderer.get("fixedColumns", [])
    if fixed_columns and not duration:
        fixed_runs = (
            fixed_columns[0]
            .get("musicResponsiveListItemFixedColumnRenderer", {})
            .get("text", {})
            .get("runs", [])
        )
        if fixed_runs:
            duration = fixed_runs[0].get("text", "").strip()

    thumb_url = ""
    thumbnails = (
        renderer.get("thumbnail", {})
        .get("musicThumbnailRenderer", {})
        .get("thumbnail", {})
        .get("thumbnails", [])
    )
    if thumbnails:
        thumb_url = thumbnails[-1].get("url", "")
        if thumb_url.startswith("//"):
            thumb_url = "https:" + thumb_url

    if video_id:
        v_type = media_kind if media_kind in ("song", "video") else "song"
        return {
            "id": video_id,
            "title": title or "Unknown Title",
            "artist": ", ".join(artists) if artists else "",
            "album": album,
            "duration": duration,
            "track_count": track_count,
            "thumbnail": thumb_url,
            "url": f"https://music.youtube.com/watch?v={video_id}",
            "type": v_type,
            "audio_tier": "hq" if (v_type == "song" or album) else "video",
        }
    elif playlist_id:
        p_type = media_kind if media_kind in ("playlist", "album") else "playlist"
        return {
            "id": playlist_id,
            "title": title or "Unknown Playlist",
            "artist": ", ".join(artists) if artists else "",
            "album": album,
            "duration": duration,
            "track_count": track_count,
            "thumbnail": thumb_url,
            "url": f"https://music.youtube.com/playlist?list={playlist_id}",
            "type": p_type,
            "audio_tier": "hq",
        }
    return None


def _parse_music_card_shelf(renderer: dict) -> dict | None:
    """Parses a musicCardShelfRenderer (hero result card) from YouTube Music."""
    if not isinstance(renderer, dict):
        return None
    title_runs = renderer.get("title", {}).get("runs", [])
    title = "".join(r.get("text", "") for r in title_runs).strip()
    subtitle_runs = renderer.get("subtitle", {}).get("runs", [])
    meta_parts = [r.get("text", "").strip() for r in subtitle_runs if r.get("text", "").strip() not in ("•", "")]

    nav = renderer.get("onTap") or (title_runs[0].get("navigationEndpoint") if title_runs else {}) or {}
    watch = nav.get("watchEndpoint") or {}
    browse = nav.get("browseEndpoint") or {}
    video_id = watch.get("videoId")
    playlist_id = watch.get("playlistId") or browse.get("browseId")
    if playlist_id and str(playlist_id).startswith("VL"):
        playlist_id = str(playlist_id)[2:]

    artists = []
    duration = ""
    track_count = ""
    card_kind = "song"
    for part in meta_parts:
        if ":" in part and any(ch.isdigit() for ch in part):
            duration = part
        elif any(tag in part.lower() for tag in ("song", "track", "lagu")) and any(ch.isdigit() for ch in part):
            track_count = part
        elif "video" in part.lower():
            card_kind = "video"
        elif not any(tag in part.lower() for tag in ("song", "lagu", "artis", "artist", "video", "album")):
            artists.append(part)

    thumb_url = ""
    thumbnails = renderer.get("thumbnail", {}).get("musicThumbnailRenderer", {}).get("thumbnail", {}).get("thumbnails", [])
    if thumbnails:
        thumb_url = thumbnails[-1].get("url", "")
        if thumb_url.startswith("//"):
            thumb_url = "https:" + thumb_url

    if video_id:
        return {
            "id": video_id,
            "title": title or "Unknown Title",
            "artist": ", ".join(artists) if artists else "",
            "album": "",
            "duration": duration,
            "track_count": track_count,
            "thumbnail": thumb_url,
            "url": f"https://music.youtube.com/watch?v={video_id}",
            "type": card_kind,
            "audio_tier": "hq" if card_kind == "song" else "video",
        }
    elif playlist_id:
        return {
            "id": playlist_id,
            "title": title or "Unknown Playlist",
            "artist": ", ".join(artists) if artists else "",
            "album": "",
            "duration": duration,
            "track_count": track_count,
            "thumbnail": thumb_url,
            "url": f"https://music.youtube.com/playlist?list={playlist_id}",
            "type": "playlist",
            "audio_tier": "hq",
        }
    return None


def _search_youtube_music_innertube(
    query: str,
    search_type: str = "songs",
    limit: int = 15,
    cookies_file: Path | None = None,
) -> list[dict]:
    """Queries YouTube Music Innertube API (WEB_REMIX) directly."""
    params_map = {
        "songs": "EgWKAQIIAWoKEAoQAxAEEAkQBQ==",
        "playlists": "EgeKAQQoAEABagoQChADEAQQCRAF",
        "albums": "EgWKAQIYAWoKEAoQAxAEEAkQBQ==",
        "videos": "EgWKAQIQAWoKEAoQAxAEEAkQBQ==",
    }
    api_url = "https://music.youtube.com/youtubei/v1/search"
    payload_data: dict[str, any] = {
        "context": {
            "client": {
                "clientName": "WEB_REMIX",
                "clientVersion": "1.20240401.01.00",
                "hl": "id",
                "gl": "ID",
            }
        },
        "query": query,
    }
    if search_type in params_map:
        payload_data["params"] = params_map[search_type]

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

    with urllib.request.urlopen(req, timeout=6) as resp:
        if resp.status != 200:
            return []
        resp_data = json.loads(resp.read().decode("utf-8"))

    items: list[dict] = []

    def walk(obj: any):
        if len(items) >= limit:
            return
        if isinstance(obj, dict):
            if "musicResponsiveListItemRenderer" in obj:
                parsed = _parse_music_responsive_item(obj["musicResponsiveListItemRenderer"])
                if parsed and parsed.get("id"):
                    if not any(it["id"] == parsed["id"] for it in items):
                        items.append(parsed)
                        if len(items) >= limit:
                            return
            elif "musicCardShelfRenderer" in obj:
                parsed = _parse_music_card_shelf(obj["musicCardShelfRenderer"])
                if parsed and parsed.get("id"):
                    if not any(it["id"] == parsed["id"] for it in items):
                        items.append(parsed)
                        if len(items) >= limit:
                            return
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for it in obj:
                walk(it)

    walk(resp_data)
    return items


def _search_youtube_ytdlp(
    query: str,
    search_type: str = "songs",
    limit: int = 15,
    cookies_file: Path | None = None,
) -> list[dict]:
    """Fallback search using yt-dlp flat-playlist metadata dump."""
    cmd = list(ytdlp_engine.get_ytdlp_base_cmd())
    cmd.extend([
        "--dump-json",
        "--flat-playlist",
        "--no-warnings",
        "--ignore-errors",
        "--socket-timeout", "10",
        "--playlist-end", str(max(1, min(limit, 50))),
    ])
    if cookies_file and Path(cookies_file).is_file():
        cmd.extend(["--cookies", str(cookies_file)])

    clean_q = query.strip()
    encoded_q = urllib.parse.quote_plus(clean_q)
    if search_type == "songs":
        target_url = f"https://music.youtube.com/search?q={encoded_q}#songs"
    elif search_type in ("playlists", "playlist"):
        target_url = f"https://music.youtube.com/search?q={encoded_q}#community playlists"
    elif search_type in ("albums", "album"):
        target_url = f"https://music.youtube.com/search?q={encoded_q}#albums"
    else:
        target_url = f"ytsearch{limit}:{clean_q}"

    cmd.append(target_url)

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
    results = []
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
        title = entry.get("title") or "Unknown Title"
        artist = entry.get("uploader") or entry.get("channel") or ""
        dur_s = entry.get("duration")
        dur_str = ""
        if isinstance(dur_s, (int, float)) and dur_s > 0:
            secs = max(0, int(round(dur_s)))
            mins, rem = divmod(secs, 60)
            hrs, mins = divmod(mins, 60)
            dur_str = f"{hrs:02d}:{mins:02d}:{rem:02d}" if hrs > 0 else f"{mins:02d}:{rem:02d}"

        thumb = entry.get("thumbnail") or ""
        if not thumb and entry.get("thumbnails"):
            thumb = entry["thumbnails"][-1].get("url", "")

        is_pl = entry.get("_type") == "playlist" or search_type in ("playlists", "playlist")
        is_video = "video" in title.lower() or search_type in ("videos", "video")
        if is_pl:
            url = f"https://music.youtube.com/playlist?list={v_id}"
            item_type = "playlist"
            audio_tier = "hq"
        elif is_video:
            url = f"https://music.youtube.com/watch?v={v_id}"
            item_type = "video"
            audio_tier = "video"
        else:
            url = f"https://music.youtube.com/watch?v={v_id}"
            item_type = "song"
            audio_tier = "hq"

        track_count = ""
        n_tracks = entry.get("playlist_count") or entry.get("n_entries")
        if isinstance(n_tracks, (int, float)) and n_tracks > 0:
            track_count = f"{int(n_tracks)} lagu"

        results.append({
            "id": v_id,
            "title": title,
            "artist": artist,
            "album": "",
            "duration": dur_str,
            "track_count": track_count,
            "thumbnail": thumb,
            "url": url,
            "type": item_type,
            "audio_tier": audio_tier,
        })
        if len(results) >= limit:
            break
    return results


def search_youtube_music(
    query: str,
    search_type: str = "songs",
    limit: int = 15,
    cookies_file: Path | None = None,
) -> list[dict]:
    """Searches YouTube Music for songs, playlists, or albums.

    Uses Innertube API directly for low latency, with automatic fallback to yt-dlp flat-playlist.
    """
    clean_q = (query or "").strip()
    if not clean_q:
        return []

    norm_type = (search_type or "songs").strip().lower()
    if norm_type not in ("songs", "playlists", "albums", "all", "videos"):
        norm_type = "songs"

    clean_limit = max(1, min(int(limit), 50))

    # 1. Primary: Direct Innertube API
    try:
        results = _search_youtube_music_innertube(
            query=clean_q,
            search_type=norm_type,
            limit=clean_limit,
            cookies_file=cookies_file,
        )
        if results:
            return results
    except Exception:
        pass

    # 2. Fallback: yt-dlp flat-playlist metadata
    try:
        return _search_youtube_ytdlp(
            query=clean_q,
            search_type=norm_type,
            limit=clean_limit,
            cookies_file=cookies_file,
        )
    except Exception:
        return []
