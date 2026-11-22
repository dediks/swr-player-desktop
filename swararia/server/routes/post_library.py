"""Library endpoints: scan, browse, probe, rename, organize and delete."""
from __future__ import annotations

from pathlib import Path

from swararia.core import audio_manager
from swararia.core import fs_browser
from swararia.server import hub, state


class LibraryPostMixin:
    """Library endpoints: scan, browse, probe, rename, organize, delete and library settings."""

    def _handle_post_library_settings(self, payload: dict) -> None:
        if not self.local_request_allowed(write=True):
            self.send_json(403, {
                "ok": False,
                "code": "local_origin_required",
                "error": "Permintaan tulis lokal tidak diizinkan.",
            })
            return

        allowed_keys = {"library_dir", "reset"}
        if set(payload) - allowed_keys:
            self.send_json(400, {"ok": False, "error": "Parameter pengaturan folder koleksi tidak valid."})
            return

        reset = payload.get("reset", False)
        if not isinstance(reset, bool):
            self.send_json(400, {"ok": False, "error": "Parameter reset harus berupa boolean."})
            return

        raw_dir = payload.get("library_dir")
        if raw_dir is not None and not isinstance(raw_dir, str):
            self.send_json(400, {"ok": False, "error": "Parameter library_dir harus berupa string."})
            return

        try:
            snapshot = state.update_default_library_dir(raw_dir, reset=reset, server=self.server)
            hub.remote_hub.bump_library_revision()
            msg = (
                f"Folder koleksi default dikembalikan ke standar OS: {snapshot['library_dir']}"
                if snapshot["source"] == "default"
                else f"Folder koleksi default berhasil disimpan: {snapshot['library_dir']}"
            )
            self.send_json(200, {"ok": True, **snapshot, "message": msg})
        except ValueError as exc:
            self.send_json(400, {"ok": False, "error": str(exc)})
        except OSError:
            self.send_json(500, {"ok": False, "error": "Gagal menyimpan pengaturan folder koleksi default."})


    def _handle_post_scan(self, payload: dict | None = None) -> None:
        raw_src = (payload or {}).get("source_dir")
        try:
            src_dir = state.resolve_safe_dir(raw_src, state.INBOX_DIR, must_exist=True) if raw_src else state.INBOX_DIR.resolve()
            results = state.metadata_cache.scan_library(src_dir)
            self.send_json(200, {"message": "Audit selesai", "results": results})
        except Exception as exc:
            self.send_json(500, {"error": str(exc)})

    def _handle_post_browse_dirs(self, payload: dict) -> None:
        try:
            result = fs_browser.browse_directory(
                raw_path=payload.get("path"),
                create_subfolder=payload.get("create_subfolder"),
                default_inbox=state.INBOX_DIR,
            )
            self.send_json(200, result)
        except Exception as exc:
            self.send_json(400, {"error": str(exc)})

    def _handle_post_probe(self, payload: dict) -> None:
        folder = payload.get("folder")
        raw_src = payload.get("source_dir")
        try:
            source_resolved = state.resolve_safe_dir(raw_src, state.INBOX_DIR, must_exist=True) if raw_src else state.INBOX_DIR.resolve()
        except Exception as exc:
            self.send_json(400, {"error": str(exc)})
            return

        if not source_resolved.exists():
            self.send_json(200, {"files": [], "playlists": [], "source_dir": str(source_resolved)})
            return

        if folder == "__ROOT__":
            target_files = [
                p for p in source_resolved.iterdir()
                if p.is_file() and p.suffix.lower() in audio_manager.EXTENSIONS and not p.name.startswith(".")
            ]
        elif isinstance(folder, str) and folder.strip():
            pl_dir = (source_resolved / folder.strip()).resolve()
            if not pl_dir.is_relative_to(source_resolved) or not pl_dir.is_dir():
                self.send_json(400, {"error": "Folder playlist tidak ditemukan."})
                return
            target_files = state.iter_audio_files_safe(pl_dir)
        else:
            target_files = state.iter_audio_files_safe(source_resolved)

        include_spectral = bool(payload.get("probe_spectral", True))
        state.metadata_cache.probe_files_fast(target_files, include_spectral=include_spectral)
        files_data, playlists_data = self.get_library_files(source_resolved)
        self.send_json(200, {
            "files": files_data,
            "playlists": playlists_data,
            "source_dir": str(source_resolved),
        })

    def _handle_post_open_folder(self, payload: dict) -> None:
        raw_src = payload.get("source_dir")
        raw_folder = payload.get("folder")
        try:
            base = state.resolve_safe_dir(raw_src, state.INBOX_DIR, must_exist=True) if raw_src else state.INBOX_DIR.resolve()
            if raw_folder and isinstance(raw_folder, str) and raw_folder.strip():
                folder_str = raw_folder.strip()
                if Path(folder_str).is_absolute() or folder_str.startswith("/mnt/"):
                    target = state.resolve_safe_dir(folder_str, must_exist=True)
                else:
                    target = (base / folder_str).resolve()
                    if not target.is_relative_to(base) or not target.exists():
                        self.send_json(400, {"error": "Folder tidak ditemukan atau berada di luar batas direktori."})
                        return
            else:
                target = base

            ok, msg = fs_browser.open_in_file_manager(target)
            if ok:
                self.send_json(200, {"ok": True, "message": msg, "path": str(target)})
            else:
                self.send_json(500, {"ok": False, "error": msg})
        except Exception as exc:
            self.send_json(400, {"error": str(exc)})

    def _handle_post_rename(self, payload: dict) -> None:
        raw_src = payload.get("source_dir")
        rel_path = payload.get("rel_path")
        new_name = payload.get("new_name")
        is_folder = payload.get("is_folder")

        try:
            src_dir = state.resolve_safe_dir(raw_src, state.INBOX_DIR, must_exist=True) if raw_src else state.INBOX_DIR.resolve()
            ok, msg, old_p, new_p = fs_browser.rename_fs_entry(
                source_dir=src_dir,
                rel_path=rel_path or "",
                new_name=new_name or "",
                is_folder=is_folder,
            )
            if not ok:
                self.send_json(400, {"error": msg})
                return

            # Update cache keys
            if new_p.is_dir():
                state.metadata_cache.rename_folder(old_p, new_p)
            else:
                state.metadata_cache.rename_entry(old_p, new_p)

            self.send_json(200, {
                "ok": True,
                "message": msg,
                "old_name": old_p.name,
                "new_name": new_p.name,
                "rel_path": str(new_p.relative_to(src_dir)),
            })
        except Exception as exc:
            self.send_json(400, {"error": str(exc)})

    def _handle_post_organize_folders(self, payload: dict) -> None:
        raw_src = payload.get("source_dir")
        rel_paths = payload.get("rel_paths")
        template = str(payload.get("template") or "artist_album")
        preview_only = bool(payload.get("preview_only", payload.get("preview", False)))
        expected_raw = payload.get("expected_plan")
        expected_plan = (
            {str(k): str(v) for k, v in expected_raw.items()}
            if isinstance(expected_raw, dict) and not preview_only else None
        )
        try:
            src_dir = state.resolve_safe_dir(raw_src, state.INBOX_DIR, must_exist=True) if raw_src else state.INBOX_DIR.resolve()
            ok, msg, plan = fs_browser.organize_into_folders(
                source_dir=src_dir,
                rel_paths=rel_paths if isinstance(rel_paths, list) else [],
                template=template,
                preview_only=preview_only,
                expected_plan=expected_plan,
            )
            if not ok:
                self.send_json(409 if expected_plan is not None and "changed since the preview" in msg else 400, {"error": msg})
                return
            if not preview_only:
                for item in plan:
                    if item.get("applied"):
                        state.metadata_cache.rename_entry(item["old_path"], item["new_path"])
            public_plan = [
                {
                    "rel_path": item["rel_path"],
                    "new_rel_path": item["new_rel_path"],
                    "folder": item["folder"],
                    "old_name": item["old_name"],
                    "new_name": item["new_name"],
                    "changed": item["changed"],
                    "reason": item.get("reason", ""),
                }
                for item in plan
            ]
            self.send_json(200, {
                "ok": True,
                "message": msg,
                "preview_only": preview_only,
                "changed_count": sum(1 for i in plan if i.get("changed")),
                "total_count": len(plan),
                "items": public_plan,
            })
        except Exception as exc:
            self.send_json(400, {"error": str(exc)})

    def _handle_post_batch_rename(self, payload: dict) -> None:
        raw_src = payload.get("source_dir")
        rel_paths = payload.get("rel_paths")
        if isinstance(payload.get("options"), dict):
            options = payload["options"]
        else:
            # Frontend sends options flat at the top level.
            option_keys = ("numbering_mode", "start_number", "strip_yt_id", "strip_junk_tags", "clean_spaces", "sort_by")
            options = {k: payload[k] for k in option_keys if k in payload}
        # Accept both "preview_only" and the frontend's "preview" flag.
        preview_only = bool(payload.get("preview_only", payload.get("preview", False)))
        expected_raw = payload.get("expected_plan")
        expected_plan = (
            {str(k): str(v) for k, v in expected_raw.items()}
            if isinstance(expected_raw, dict) and not preview_only else None
        )

        try:
            src_dir = state.resolve_safe_dir(raw_src, state.INBOX_DIR, must_exist=True) if raw_src else state.INBOX_DIR.resolve()
            ok, msg, plan = fs_browser.batch_rename_entries(
                source_dir=src_dir,
                rel_paths=rel_paths if isinstance(rel_paths, list) else [],
                options=options,
                preview_only=preview_only,
                expected_plan=expected_plan,
            )
            if not ok:
                self.send_json(409 if expected_plan is not None and "changed since the preview" in msg else 400, {"error": msg})
                return

            if not preview_only:
                for item in plan:
                    if item.get("changed") and item.get("applied"):
                        state.metadata_cache.rename_entry(item["old_path"], item["new_path"])

            changed_count = sum(1 for item in plan if item.get("changed"))
            public_plan = [
                {
                    "rel_path": item["rel_path"],
                    "new_rel_path": item["new_rel_path"],
                    "folder": item["folder"],
                    "old_name": item["old_name"],
                    "new_name": item["new_name"],
                    "changed": item["changed"],
                }
                for item in plan
            ]
            self.send_json(200, {
                "ok": True,
                "message": msg,
                "preview_only": preview_only,
                "changed_count": changed_count,
                "total_count": len(plan),
                "plan": public_plan,
                "items": public_plan,
            })
        except Exception as exc:
            self.send_json(400, {"error": str(exc)})

    def _handle_post_delete(self, payload: dict) -> None:
        raw_src = payload.get("source_dir")
        rel_path = payload.get("rel_path")
        rel_paths = payload.get("rel_paths")
        is_folder = payload.get("is_folder")

        try:
            src_dir = state.resolve_safe_dir(raw_src, state.INBOX_DIR, must_exist=True) if raw_src else state.INBOX_DIR.resolve()
            ok, msg, deleted_files, deleted_folders = fs_browser.delete_fs_entries(
                source_dir=src_dir,
                rel_paths=rel_paths if isinstance(rel_paths, list) else None,
                rel_path=rel_path if isinstance(rel_path, str) else None,
                is_folder=is_folder if isinstance(is_folder, bool) else None,
            )
            if not ok:
                self.send_json(400, {"error": msg})
                return

            # Evict deleted files and folders from metadata/QC cache
            for fp in deleted_files:
                state.metadata_cache.remove_entry(fp)
            for dp in deleted_folders:
                state.metadata_cache.remove_folder(dp)

            # Sync download archive if deletions occurred inside default INBOX_DIR
            inbox_resolved = state.INBOX_DIR.resolve()
            if src_dir == inbox_resolved or src_dir.is_relative_to(inbox_resolved):
                audio_manager.sync_download_archive(inbox_resolved, state.ARCHIVE_FILE)

            self.send_json(200, {
                "ok": True,
                "message": msg,
                "deleted_count": len(deleted_files),
                "deleted_paths": [str(p.relative_to(src_dir)) for p in deleted_files if p.is_relative_to(src_dir)],
                "deleted_folders": [str(d.relative_to(src_dir)) for d in deleted_folders if d.is_relative_to(src_dir)],
            })
        except Exception as exc:
            self.send_json(400, {"error": str(exc)})
