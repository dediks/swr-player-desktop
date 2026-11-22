"""Browser (HP) remote player hub: clients, roles, PIN and command queue."""
from __future__ import annotations

import hmac
import re
import secrets
import threading
import time


ALLOWED_REMOTE_ACTIONS = {
    "play_pause",
    "play",
    "pause",
    "stop",
    "prev",
    "next",
    "fade",
    "seek_ratio",
    "nudge",
    "cue_zero",
    "set_play_mode",
    "set_volume",
    "toggle_mute",
    "set_preamp",
    "set_eq",
    "reset_eq",
    "set_routing",
    "set_autotrim",
    "reset_peak",
    "trigger_pad",
    "restart_pad",
    "clear_pad",
    "assign_pad",
    "load_track",
    "select_playlist",
}

COALESCING_REMOTE_ACTIONS = {
    "set_volume",
    "set_preamp",
    "set_eq",
    "seek_ratio",
    "set_autotrim",
    "set_routing",
    "set_play_mode",
}


def _parse_client_device_label(user_agent: str, client_ip: str) -> str:
    """Extracts a safe, human-readable device & browser label from User-Agent."""
    ua = (user_agent or "").strip()
    ua_low = ua.lower()

    if "iphone" in ua_low:
        os_name = "iPhone (iOS)"
    elif "ipad" in ua_low:
        os_name = "iPad (iPadOS)"
    elif "android" in ua_low:
        model_match = re.search(
            r"android\s+[\d.]+;\s*([a-zA-Z0-9 _\-]{2,24}?)(?:\s+build/|\))",
            ua,
            flags=re.IGNORECASE,
        )
        model_str = model_match.group(1).strip() if model_match else ""
        if model_str and model_str.upper() != "K":
            os_name = f"Android ({model_str})"
        else:
            os_name = "Android"
    elif "macintosh" in ua_low or "mac os" in ua_low:
        os_name = "Mac"
    elif "windows" in ua_low:
        os_name = "Windows"
    elif "linux" in ua_low:
        os_name = "Linux"
    else:
        os_name = "Mobile"

    if "edg/" in ua_low or "edga/" in ua_low or "edgios/" in ua_low:
        browser = "Edge"
    elif "opr/" in ua_low or "opera" in ua_low:
        browser = "Opera"
    elif "samsungbrowser" in ua_low:
        browser = "Samsung"
    elif "crios/" in ua_low or ("chrome/" in ua_low and "chromium" not in ua_low):
        browser = "Chrome"
    elif "fxios/" in ua_low or "firefox/" in ua_low:
        browser = "Firefox"
    elif "safari/" in ua_low and "chrome/" not in ua_low:
        browser = "Safari"
    else:
        browser = "Browser"

    if client_ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1") and os_name in (
        "Windows",
        "Linux",
        "Mac",
        "Mobile",
    ):
        return f"Local Sim • {browser}"
    return f"{os_name} • {browser}"


class RemotePlayerHub:
    """Thread-safe synchronization hub between Master FOH Deck (Laptop/PC) and HP Wireless Remotes."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.enabled: bool = True
        self.pin: str = f"{secrets.randbelow(9000) + 1000:04d}"
        self.master_state: dict = {
            "track": None,
            "is_playing": False,
            "transport_state": "STOPPED",
            "current_time": 0.0,
            "duration": 0.0,
            "play_mode": "single_stop",
            "volume": 100,
            "is_muted": False,
            "preamp_db": 0.0,
            "eq_low_db": 0.0,
            "eq_mid_db": 0.0,
            "eq_high_db": 0.0,
            "routing_mode": "stereo",
            "auto_trim": False,
            "peak_db": -100.0,
            "rta_bands": [],
            "vu_l": 0.0,
            "vu_r": 0.0,
            "cue_pads": [None] * 8,
            "source_dir": "",
            "active_playlist": "__ALL__",
        }
        self.master_updated_at: float = 0.0
        self.command_queue: list[dict] = []
        self.next_cmd_id: int = 1
        self.remote_clients: dict[str, dict] = {}
        self.blocked_clients: dict[str, dict] = {}
        self.client_roles: dict[str, str] = {}
        self.upload_enabled: bool = False
        self.upload_dest_dir: str = ""
        self.upload_dest_label: str = "library/inbox"
        self.library_revision: int = 0
        self._upload_rate_buckets: dict[str, list[float]] = {}

    def _prune_clients_unlocked(self, now: float) -> None:
        stale = [
            cid
            for cid, info in self.remote_clients.items()
            if (now - float(info.get("last_seen", 0.0) if isinstance(info, dict) else info)) > 12.0
        ]
        for cid in stale:
            self.remote_clients.pop(cid, None)

    def _touch_client_unlocked(
        self,
        client_id: str,
        client_ip: str,
        client_tag: str,
        user_agent: str,
        now: float,
        inc_cmd: bool = False,
    ) -> dict:
        existing = self.remote_clients.get(client_id)
        if not isinstance(existing, dict):
            existing = {
                "client_id": client_id,
                "ip": client_ip or client_id.split(":", 1)[0],
                "tag": client_tag or "mobile",
                "label": _parse_client_device_label(user_agent, client_ip),
                "first_seen": now,
                "last_seen": now,
                "commands_sent": 0,
            }
            self.remote_clients[client_id] = existing
        else:
            existing["last_seen"] = now
            if user_agent and existing.get("label", "").startswith("HP Remote"):
                existing["label"] = _parse_client_device_label(user_agent, client_ip)
        if inc_cmd:
            existing["commands_sent"] = int(existing.get("commands_sent", 0)) + 1
        return existing

    def _get_clients_lists_unlocked(self, now: float) -> tuple[list[dict], list[dict]]:
        self._prune_clients_unlocked(now)
        active_list: list[dict] = []
        for cid, raw_info in self.remote_clients.items():
            if isinstance(raw_info, dict):
                ip = str(raw_info.get("ip") or cid.split(":", 1)[0])
                tag = str(raw_info.get("tag") or "")
                label = str(raw_info.get("label") or "HP Remote")
                first_seen = float(raw_info.get("first_seen") or now)
                last_seen = float(raw_info.get("last_seen") or now)
                cmds = int(raw_info.get("commands_sent") or 0)
            else:
                ip = cid.split(":", 1)[0]
                tag = cid.split(":", 1)[1] if ":" in cid else "mobile"
                label = "HP Remote"
                first_seen = float(raw_info)
                last_seen = float(raw_info)
                cmds = 0
            active_list.append({
                "client_id": cid,
                "ip": ip,
                "tag": tag,
                "label": label,
                "role": self.client_roles.get(cid, "control"),
                "last_seen_ago": round(max(0.0, now - last_seen), 1),
                "connected_sec": int(max(0.0, now - first_seen)),
                "commands_sent": cmds,
            })
        active_list.sort(key=lambda x: x["connected_sec"], reverse=True)

        blocked_list: list[dict] = []
        for cid, binfo in self.blocked_clients.items():
            blocked_list.append({
                "client_id": cid,
                "ip": str(binfo.get("ip") or cid.split(":", 1)[0]),
                "label": str(binfo.get("label") or "HP Remote"),
                "blocked_ago": int(max(0.0, now - float(binfo.get("blocked_at") or now))),
            })
        return active_list, blocked_list

    def get_clients_lists(self) -> tuple[list[dict], list[dict]]:
        now = time.time()
        with self.lock:
            return self._get_clients_lists_unlocked(now)

    def is_client_blocked(self, client_id: str) -> bool:
        with self.lock:
            return client_id in self.blocked_clients

    def get_client_role(self, client_id: str) -> str:
        with self.lock:
            return self.client_roles.get(client_id, "control")

    def kick_client(self, client_id: str) -> bool:
        clean_id = str(client_id or "").strip()
        if not clean_id:
            return False
        now = time.time()
        with self.lock:
            info = self.remote_clients.pop(clean_id, None)
            ip = clean_id.split(":", 1)[0]
            label = "HP Remote"
            if isinstance(info, dict):
                ip = str(info.get("ip") or ip)
                label = str(info.get("label") or label)
            self.blocked_clients[clean_id] = {
                "client_id": clean_id,
                "ip": ip,
                "label": label,
                "blocked_at": now,
            }
            self.command_queue = [c for c in self.command_queue if c.get("client_id") != clean_id]
            return True

    def kick_all_clients(self) -> int:
        now = time.time()
        with self.lock:
            count = len(self.remote_clients)
            for cid, info in list(self.remote_clients.items()):
                ip = cid.split(":", 1)[0]
                label = "HP Remote"
                if isinstance(info, dict):
                    ip = str(info.get("ip") or ip)
                    label = str(info.get("label") or label)
                self.blocked_clients[cid] = {
                    "client_id": cid,
                    "ip": ip,
                    "label": label,
                    "blocked_at": now,
                }
            self.remote_clients.clear()
            self.command_queue = [c for c in self.command_queue if c.get("native_id")]
            return count

    def unblock_client(self, client_id: str) -> bool:
        clean_id = str(client_id or "").strip()
        if not clean_id:
            return False
        with self.lock:
            return self.blocked_clients.pop(clean_id, None) is not None

    def set_client_role(self, client_id: str, role: str) -> str:
        clean_id = str(client_id or "").strip()
        new_role = "view_only" if str(role or "").strip().lower() == "view_only" else "control"
        if clean_id:
            with self.lock:
                self.client_roles[clean_id] = new_role
        return new_role

    def verify_pin(self, candidate: str | None) -> bool:
        clean = str(candidate or "").strip()
        if not clean:
            return False
        with self.lock:
            return hmac.compare_digest(clean, self.pin)

    def regenerate_pin(self) -> str:
        with self.lock:
            self.pin = f"{secrets.randbelow(9000) + 1000:04d}"
            self.remote_clients.clear()
            self.blocked_clients.clear()
            self.command_queue = [c for c in self.command_queue if c.get("native_id")]
            return self.pin

    def set_enabled(self, flag: bool) -> bool:
        with self.lock:
            self.enabled = bool(flag)
            if not self.enabled:
                self.command_queue = [c for c in self.command_queue if c.get("native_id")]
                self.remote_clients.clear()
            return self.enabled

    def get_active_remotes_count(self) -> int:
        now = time.time()
        with self.lock:
            self._prune_clients_unlocked(now)
            return len(self.remote_clients)

    def update_master_state(self, state_payload: dict | None, include_native: bool = True) -> tuple[list[dict], int, str, bool]:
        now = time.time()
        with self.lock:
            if isinstance(state_payload, dict) and state_payload:
                self.master_state = state_payload
                self.master_updated_at = now
            self._prune_clients_unlocked(now)
            pending = [c for c in self.command_queue if include_native or not c.get("native_id")]
            self.command_queue = [c for c in self.command_queue if not include_native and c.get("native_id")]
            return pending, len(self.remote_clients), self.pin, self.enabled

    def set_upload_config(
        self,
        enabled: bool | None = None,
        dest_dir: str | None = None,
        dest_label: str | None = None,
    ) -> dict:
        with self.lock:
            if isinstance(enabled, bool):
                self.upload_enabled = enabled
            if dest_dir is not None:
                self.upload_dest_dir = str(dest_dir).strip()
            if dest_label is not None:
                self.upload_dest_label = str(dest_label).strip() or "library/inbox"
            return {
                "upload_enabled": bool(self.upload_enabled),
                "upload_dest_dir": self.upload_dest_dir,
                "upload_dest_label": self.upload_dest_label,
            }

    def get_upload_config(self) -> dict:
        with self.lock:
            return {
                "upload_enabled": bool(self.upload_enabled),
                "upload_dest_dir": self.upload_dest_dir,
                "upload_dest_label": self.upload_dest_label,
            }

    def bump_library_revision(self) -> int:
        with self.lock:
            self.library_revision += 1
            return self.library_revision

    def get_library_revision(self) -> int:
        with self.lock:
            return self.library_revision

    def check_upload_rate_limit(self, client_id: str, capacity: float = 30.0, refill_per_sec: float = 1.0) -> bool:
        now = time.monotonic()
        with self.lock:
            if len(self._upload_rate_buckets) > 128:
                self._upload_rate_buckets.clear()
            bucket = self._upload_rate_buckets.get(client_id)
            if bucket is None:
                bucket = [capacity, now]
                self._upload_rate_buckets[client_id] = bucket
            elapsed = max(0.0, now - bucket[1])
            bucket[0] = min(capacity, bucket[0] + elapsed * refill_per_sec)
            bucket[1] = now
            if bucket[0] < 1.0:
                return False
            bucket[0] -= 1.0
            return True

    def get_remote_state(
        self,
        client_id: str,
        client_ip: str = "",
        client_tag: str = "",
        user_agent: str = "",
    ) -> dict:
        now = time.time()
        with self.lock:
            self._touch_client_unlocked(client_id, client_ip, client_tag, user_agent, now, inc_cmd=False)
            self._prune_clients_unlocked(now)
            return {
                "ok": True,
                "enabled": self.enabled,
                "upload_enabled": bool(self.upload_enabled),
                "upload_dest_label": self.upload_dest_label,
                "role": self.client_roles.get(client_id, "control"),
                "master_online": (now - self.master_updated_at) < 5.0,
                "master_updated_at": self.master_updated_at,
                "active_remotes_count": len(self.remote_clients),
                "state": self.master_state,
            }

    def enqueue_command(
        self,
        action: str,
        params: dict | None,
        client_id: str,
        client_ip: str = "",
        client_tag: str = "",
        user_agent: str = "",
    ) -> dict:
        now = time.time()
        safe_params = params if isinstance(params, dict) else {}
        with self.lock:
            self._touch_client_unlocked(client_id, client_ip, client_tag, user_agent, now, inc_cmd=True)
            self._prune_clients_unlocked(now)

            if action in COALESCING_REMOTE_ACTIONS:
                for existing in reversed(self.command_queue):
                    if existing["action"] == action:
                        if action == "set_eq":
                            existing["params"].update(safe_params)
                        else:
                            existing["params"] = safe_params
                        existing["created_at"] = now
                        existing["client_id"] = client_id
                        return existing

            cmd = {
                "id": self.next_cmd_id,
                "action": action,
                "params": safe_params,
                "client_id": client_id,
                "created_at": now,
            }
            self.next_cmd_id += 1
            self.command_queue.append(cmd)
            if len(self.command_queue) > 50:
                self.command_queue = self.command_queue[-50:]
            return cmd


remote_hub = RemotePlayerHub()
