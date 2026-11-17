"""Multitrack/video splitter endpoints."""
from __future__ import annotations

from swararia.core import splitter_engine
from swararia.server import state


class SplitterPostMixin:
    """Multitrack/video splitter endpoints."""


    def _handle_post_splitter_inspect(self, payload: dict) -> None:
        raw_src = payload.get("source_dir")
        raw_rel = payload.get("target_rel")
        probe_detail = bool(payload.get("probe_detail", False))
        try:
            src_dir = state.resolve_safe_dir(raw_src, state.INBOX_DIR, must_exist=True) if raw_src else state.INBOX_DIR.resolve()
            dir_data = splitter_engine.inspect_splitter_directory(src_dir)
            dir_data["default_inbox"] = str(state.INBOX_DIR.resolve())

            if raw_rel and isinstance(raw_rel, str) and raw_rel.strip():
                target_file = splitter_engine.validate_safe_file_path(raw_rel.strip(), base_dir=src_dir)
                dir_data["selected_probe"] = splitter_engine.probe_media_detailed(target_file)
            elif probe_detail and dir_data.get("files"):
                # Probe up to 15 direct files for quick channel/duration badges
                probed_map: dict[str, dict] = {}
                for item in dir_data["files"][:15]:
                    try:
                        fp = splitter_engine.validate_safe_file_path(item["rel_path"], base_dir=src_dir)
                        probed_map[item["rel_path"]] = splitter_engine.probe_media_detailed(fp)
                    except Exception:
                        pass
                dir_data["probed_files"] = probed_map

            self.send_json(200, dir_data)
        except Exception as exc:
            self.send_json(400, {"error": str(exc)})

    def _handle_post_splitter_youtube_info(self, payload: dict) -> None:
        url = (payload or {}).get("url") or (payload or {}).get("youtube_url")
        if not url or not splitter_engine.is_youtube_url(url):
            self.send_json(400, {"error": "URL YouTube tidak valid. Pastikan link youtube.com atau youtu.be."})
            return
        try:
            info = splitter_engine.fetch_youtube_video_info(str(url).strip())
            self.send_json(200, info)
        except Exception as exc:
            self.send_json(400, {"error": str(exc)})

    def _handle_post_splitter_detect(self, payload: dict) -> None:
        raw_src = payload.get("source_dir")
        raw_target_rel = payload.get("target_rel")
        target_mode = str(payload.get("target_mode", "auto")).strip().lower()
        detect_mode = str(payload.get("detect_mode", "auto")).strip().lower()
        noise_db = float(payload.get("noise_db", -32.0))
        min_silence_s = float(payload.get("min_silence_s", 2.5))
        min_song_s = float(payload.get("min_song_s", 60.0))
        pad_before_s = float(payload.get("pad_before_s", 0.5))
        pad_after_s = float(payload.get("pad_after_s", 1.0))
        trim_silence_edges = bool(payload.get("trim_silence_edges", True))
        reference_stem = payload.get("reference_stem")
        channel_mode = str(payload.get("channel_mode", "auto")).strip().lower()
        manual_timestamps = payload.get("manual_timestamps")

        try:
            yt_url = payload.get("youtube_url") or (raw_target_rel if splitter_engine.is_youtube_url(raw_target_rel) else None)
            if yt_url:
                target_path = str(yt_url).strip()
            else:
                src_dir = state.resolve_safe_dir(raw_src, state.INBOX_DIR, must_exist=True) if raw_src else state.INBOX_DIR.resolve()
                if target_mode == "folder_stems" or not raw_target_rel:
                    target_path = src_dir
                else:
                    target_path = splitter_engine.validate_safe_file_path(str(raw_target_rel), base_dir=src_dir)

            result = splitter_engine.detect_song_segments(
                target_path=target_path,
                mode=detect_mode,
                noise_db=noise_db,
                min_silence_s=min_silence_s,
                min_song_s=min_song_s,
                pad_before_s=pad_before_s,
                pad_after_s=pad_after_s,
                trim_silence_edges=trim_silence_edges,
                reference_stem=str(reference_stem) if reference_stem else None,
                channel_mode=channel_mode,
                manual_timestamps=str(manual_timestamps) if manual_timestamps else None,
            )
            self.send_json(200, result)
        except Exception as exc:
            self.send_json(400, {"error": str(exc)})
