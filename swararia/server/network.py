"""LAN/WSL/Windows networking: interface discovery, port bridges and firewall helpers."""
from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path


_cached_lan_ips: list[str] = []
_cached_interfaces: list[dict] = []
_cached_lan_ips_ts: float = 0.0
_lan_ips_lock = threading.Lock()
_is_wsl_cached: bool | None = None


def is_wsl_environment() -> bool:
    """Returns True if running inside Windows Subsystem for Linux (WSL/WSL2)."""
    global _is_wsl_cached
    if _is_wsl_cached is not None:
        return _is_wsl_cached
    if os.name == "nt":
        _is_wsl_cached = False
        return False
    try:
        proc_ver = Path("/proc/version").read_text(encoding="utf-8", errors="ignore").lower()
        if "microsoft" in proc_ver or "wsl" in proc_ver:
            _is_wsl_cached = True
            return True
    except Exception:
        pass
    _is_wsl_cached = bool(os.environ.get("WSL_DISTRO_NAME"))
    return _is_wsl_cached


def _is_valid_lan_ipv4(candidate: str) -> bool:
    c = candidate.strip()
    if not c or ":" in c:
        return False
    if c.startswith("127.") or c.startswith("169.254.") or c == "0.0.0.0":
        return False
    parts = c.split(".")
    return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


def _is_rfc1918_ipv4(candidate: str) -> bool:
    try:
        ip = ipaddress.IPv4Address(candidate.strip())
    except (ValueError, ipaddress.AddressValueError):
        return False
    return any(ip in ipaddress.ip_network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def _find_windows_sys32_bin(exe_name: str) -> str | None:
    """Locates a Windows System32 executable from WSL or native Windows."""
    found = shutil.which(exe_name)
    if found:
        return found
    for prefix in ("/mnt/c/Windows/System32", "/mnt/c/WINDOWS/system32", "/mnt/c/Windows/Sysnative"):
        cand = f"{prefix}/{exe_name}"
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def _parse_windows_ipconfig() -> list[dict]:
    """Runs ipconfig.exe (from Windows or WSL2) and extracts real Wi-Fi / LAN adapters."""
    ipconfig_bin = _find_windows_sys32_bin("ipconfig.exe")
    if not ipconfig_bin:
        return []
    try:
        raw = subprocess.check_output(
            [ipconfig_bin],
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=2.0,
        )
    except Exception:
        return []

    adapters: list[dict] = []
    curr_name = "Windows Network"
    curr_ips: list[str] = []
    curr_gw = ""

    def _flush_adapter() -> None:
        nonlocal curr_ips, curr_gw
        if not curr_ips:
            return
        low = curr_name.lower()
        is_virtual = any(
            k in low
            for k in ("wsl", "hyper-v", "vethernet", "docker", "vmware", "virtualbox", "loopback", "bluetooth")
        )
        is_wifi = any(
            k in low
            for k in ("wi-fi", "wireless", "wlan", "local area connection*", "hotspot")
        )
        short_name = re.sub(
            r"^(Wireless LAN adapter|Ethernet adapter)\s+",
            "",
            curr_name,
            flags=re.IGNORECASE,
        ).strip() or curr_name
        for ip in curr_ips:
            if not _is_valid_lan_ipv4(ip):
                continue
            if is_virtual:
                kind = "virtual"
                label = f"Virtual ({short_name})"
                reachable = False
                priority = 90
            elif is_wifi:
                kind = "wifi"
                label = f"Wi-Fi ({short_name})"
                reachable = True
                priority = 1 if curr_gw else 2
            else:
                kind = "lan"
                label = f"LAN ({short_name})"
                reachable = True
                priority = 3 if curr_gw else 4
            adapters.append({
                "ip": ip,
                "adapter": curr_name,
                "label": label,
                "kind": kind,
                "has_gateway": bool(curr_gw),
                "reachable_from_hp": reachable,
                "priority": priority,
            })
        curr_ips = []
        curr_gw = ""

    for raw_line in raw.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        if not line[0].isspace() and line.endswith(":"):
            _flush_adapter()
            curr_name = line[:-1].strip()
            continue
        ip_match = re.search(r"IPv4[^:]*:\s*(\d{1,3}(?:\.\d{1,3}){3})", line, re.IGNORECASE)
        if ip_match:
            curr_ips.append(ip_match.group(1))
            continue
        gw_match = re.search(r"Default Gateway[^:]*:\s*(\d{1,3}(?:\.\d{1,3}){3})", line, re.IGNORECASE)
        if gw_match:
            curr_gw = gw_match.group(1)

    _flush_adapter()
    adapters.sort(key=lambda item: item["priority"])
    return adapters


def _get_linux_internal_ips() -> list[str]:
    ips: list[str] = []

    def _add(c: str) -> None:
        c = c.strip()
        if _is_valid_lan_ipv4(c) and c not in ips:
            ips.append(c)

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.2)
            s.connect(("10.255.255.255", 1))
            _add(s.getsockname()[0])
    except Exception:
        pass

    try:
        _, _, host_ips = socket.gethostbyname_ex(socket.gethostname())
        for ip in host_ips:
            _add(ip)
    except Exception:
        pass

    if sys.platform.startswith("linux"):
        try:
            out = subprocess.check_output(["hostname", "-I"], text=True, timeout=1.0)
            for token in out.split():
                _add(token)
        except Exception:
            pass
    return ips


def get_network_interfaces_detail(force_refresh: bool = False) -> list[dict]:
    """Returns detailed network interface list prioritizing real Wi-Fi/Hotspot IPs over WSL2 virtual IPs."""
    global _cached_lan_ips, _cached_interfaces, _cached_lan_ips_ts
    now = time.time()
    with _lan_ips_lock:
        if not force_refresh and _cached_interfaces and (now - _cached_lan_ips_ts) < 10.0:
            return list(_cached_interfaces)

    is_wsl = is_wsl_environment()
    interfaces: list[dict] = []
    seen_ips: set[str] = set()

    if is_wsl or os.name == "nt":
        for item in _parse_windows_ipconfig():
            ip = item["ip"]
            if ip not in seen_ips:
                seen_ips.add(ip)
                interfaces.append(item)

    linux_ips = _get_linux_internal_ips()
    for ip in linux_ips:
        if ip in seen_ips:
            continue
        seen_ips.add(ip)
        if is_wsl:
            interfaces.append({
                "ip": ip,
                "adapter": "WSL2 eth0",
                "label": "WSL2 Internal IP",
                "kind": "wsl_internal",
                "has_gateway": True,
                "reachable_from_hp": False,
                "priority": 95,
            })
        else:
            interfaces.append({
                "ip": ip,
                "adapter": "Local Network",
                "label": "Wi-Fi / LAN",
                "kind": "wifi",
                "has_gateway": True,
                "reachable_from_hp": True,
                "priority": 1,
            })

    interfaces.sort(key=lambda x: x["priority"])
    # Prefer reachable Wi-Fi/LAN IPs for the main list; fall back to all only if none found
    reachable_ifaces = [x for x in interfaces if x["reachable_from_hp"]]
    visible_interfaces = reachable_ifaces if reachable_ifaces else interfaces
    primary_ips = [x["ip"] for x in visible_interfaces]

    with _lan_ips_lock:
        _cached_interfaces = list(visible_interfaces)
        _cached_lan_ips = list(primary_ips)
        _cached_lan_ips_ts = now
    return list(visible_interfaces)


def get_local_lan_ips(force_refresh: bool = False) -> list[str]:
    """Detects local Wi-Fi / LAN IPv4 addresses for FOH Mobile Remote connectivity."""
    get_network_interfaces_detail(force_refresh=force_refresh)
    with _lan_ips_lock:
        return list(_cached_lan_ips)


def _verify_windows_http_reachable(ip: str, port: int) -> bool:
    """Tests from the Windows host side (via curl.exe) whether http://ip:port/api/system returns HTTP 200."""
    if not ip:
        return False
    curl_bin = _find_windows_sys32_bin("curl.exe")
    if not curl_bin:
        return False
    try:
        out = subprocess.check_output(
            [
                curl_bin,
                "-s",
                "-o",
                "NUL",
                "-w",
                "%{http_code}",
                "--max-time",
                "1.5",
                f"http://{ip}:{port}/api/system",
            ],
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=2.5,
        ).strip()
        return out == "200"
    except Exception:
        return False


def _get_wsl_remote_bridge_port(server_port: int) -> int:
    """Uses server_port + 1 (8766) on Windows host for WSL2 portproxy so port 8765 (wslrelay) is never hijacked."""
    return (server_port + 1) if is_wsl_environment() else server_port


def _cleanup_stale_wsl_listen_port(port: int) -> None:
    """If an old netsh portproxy rule is listening on `port` (8765), removes it before server bind so wslrelay works."""
    if not is_wsl_environment():
        return
    netsh_bin = _find_windows_sys32_bin("netsh.exe")
    ps_bin = _find_windows_sys32_bin("powershell.exe")
    if not netsh_bin or not ps_bin:
        return
    stale_listen_ips: list[str] = []
    try:
        out = subprocess.check_output(
            [netsh_bin, "interface", "portproxy", "show", "v4tov4"],
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=2.0,
        )
        for line in out.splitlines():
            m = re.match(
                r"^\s*(\d{1,3}(?:\.\d{1,3}){3}|\*)\s+(\d+)\s+(\d{1,3}(?:\.\d{1,3}){3}|\*)\s+(\d+)",
                line,
            )
            if m and int(m.group(2)) == port:
                stale_listen_ips.append(m.group(1))
    except Exception:
        return

    if not stale_listen_ips:
        return

    print("=" * 65)
    print(f"  [WSL2 Auto-Heal] Membersihkan aturan portproxy lama di port {port}")
    print("  Silakan klik 'Yes' pada jendela konfirmasi Windows yang muncul...")
    print("=" * 65)
    del_cmds = [
        f"netsh interface portproxy delete v4tov4 listenport={port} listenaddress={lip} | Out-Null;"
        for lip in stale_listen_ips
    ]
    inner_script = " ".join(del_cmds)
    inner_b64 = base64.b64encode(inner_script.encode("utf-16le")).decode("ascii")
    outer_cmd = (
        f"Start-Process powershell.exe -Verb RunAs -WindowStyle Hidden "
        f"-Wait -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-EncodedCommand','{inner_b64}'"
    )
    outer_b64 = base64.b64encode(outer_cmd.encode("utf-16le")).decode("ascii")
    try:
        subprocess.run(
            [ps_bin, "-NoProfile", "-NonInteractive", "-EncodedCommand", outer_b64],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=30.0,
            check=False,
        )
    except Exception:
        pass


_wsl_foh_ports_lock = threading.Lock()
_active_wsl_foh_ports: dict[int, int] = {}


def _get_wsl_foh_internal_port(server_port: int) -> int:
    """Returns the internal WSL2 loopback port for the dedicated bridged FOH listener."""
    with _wsl_foh_ports_lock:
        active = _active_wsl_foh_ports.get(int(server_port))
    if active:
        return active
    bridge_port = _get_wsl_remote_bridge_port(server_port)
    reserved = {int(server_port), int(bridge_port)}
    try:
        android_pub = _get_native_remote_default_port(int(server_port))
        reserved.add(android_pub)
        reserved.add(_get_wsl_native_internal_port(android_pub, int(server_port), _include_foh_internal=False))
    except Exception:
        reserved.update({8767, 8768})
    for delta in range(1, 32):
        candidate = bridge_port + delta
        if 1024 <= candidate <= 65535 and candidate not in reserved:
            return candidate
    for delta in range(1, 32):
        candidate = int(server_port) - delta
        if 1024 <= candidate <= 65535 and candidate not in reserved:
            return candidate
    return 8769


def _foh_bridge_script_lines(
    port: int,
    bridge_port: int,
    internal_port: int,
    wifi_ip: str,
    quiet: bool = False,
) -> list[str]:
    suffix = " | Out-Null;" if quiet else ""
    rule_name = f"Swararia FOH Remote {bridge_port}"
    parts: list[str] = [
        f"netsh interface portproxy delete v4tov4 listenport={port} listenaddress=0.0.0.0{suffix}",
    ]
    if wifi_ip:
        parts.append(f"netsh interface portproxy delete v4tov4 listenport={port} listenaddress={wifi_ip}{suffix}")
    parts.append(f'netsh advfirewall firewall delete rule name="Swararia FOH Remote {port}"{suffix}')
    parts.extend(_native_bridge_script_lines(True, bridge_port, internal_port, wifi_ip, rule_name, quiet=quiet))
    return parts


def get_wsl_bridge_status(port: int) -> dict:
    """Checks if Windows portproxy is forwarding Wi-Fi IP:8766 -> 127.0.0.1:8769 (bridged WSL2 FOH listener)."""
    if not is_wsl_environment():
        return {"is_wsl": False, "bridge_active": True, "bridge_port": port}

    bridge_port = _get_wsl_remote_bridge_port(port)
    internal_port = _get_wsl_foh_internal_port(port)
    wsl_ips = _get_linux_internal_ips()
    wsl_ip = wsl_ips[0] if wsl_ips else ""
    lan_ips = get_local_lan_ips(force_refresh=False)
    wifi_ip = lan_ips[0] if lan_ips else ""
    rule_name = f"Swararia FOH Remote {bridge_port}"

    observed_rules = _list_windows_portproxy_rules()
    rules = observed_rules or []
    valid_addrs = {"0.0.0.0", "*"}
    if wifi_ip:
        valid_addrs.add(wifi_ip)
    has_primary_rule = any(
        lp == bridge_port and la in valid_addrs and (ca, cp) == ("127.0.0.1", internal_port)
        for (la, lp, ca, cp) in rules
    )
    has_fallback_rule = bool(wsl_ip) and any(
        lp == bridge_port and la in valid_addrs and (ca, cp) in {(wsl_ip, port), (wsl_ip, internal_port)}
        for (la, lp, ca, cp) in rules
    )
    firewall_rules = _inspect_windows_firewall_rules((rule_name, f"Swararia FOH Remote {port}"))
    firewall_active = firewall_rules.get(rule_name, False) if firewall_rules is not None else None
    firewall_present = bool(firewall_rules) if firewall_rules is not None else None

    if wifi_ip and _find_windows_sys32_bin("curl.exe"):
        has_rule = has_primary_rule or has_fallback_rule
        bridge_active = has_rule and firewall_active is True and _verify_windows_http_reachable(wifi_ip, bridge_port)
    else:
        has_rule = has_primary_rule
        bridge_active = has_rule and firewall_active is True

    ps_parts = _foh_bridge_script_lines(port, bridge_port, internal_port, wifi_ip, quiet=False)
    return {
        "is_wsl": True,
        "wsl_ip": wsl_ip,
        "wifi_ip": wifi_ip,
        "bridge_port": bridge_port,
        "internal_port": internal_port,
        "bridge_active": bridge_active,
        "has_portproxy": (any(lp in {port, bridge_port, internal_port} for _, lp, _, _ in rules)
                          if observed_rules is not None else None),
        "firewall_active": firewall_active,
        "firewall_present": firewall_present,
        "powershell_admin_cmd": "; ".join(ps_parts),
    }


def enable_wsl_port_bridge(port: int) -> tuple[bool, str, dict]:
    """Launches an elevated Windows PowerShell UAC prompt to bridge Wi-Fi port 8766 -> 127.0.0.1:8769 (wslrelay)."""
    status = get_wsl_bridge_status(port)
    if not status.get("is_wsl"):
        return True, "Bukan lingkungan WSL2 (tidak memerlukan bridge).", status
    bridge_port = _get_wsl_remote_bridge_port(port)
    internal_port = int(status.get("internal_port") or _get_wsl_foh_internal_port(port))
    wsl_ip = status.get("wsl_ip") or ""

    lan_ips = get_local_lan_ips(force_refresh=True)
    wifi_ip = lan_ips[0] if lan_ips else ""
    listen_addr = wifi_ip if wifi_ip else "0.0.0.0"

    if not _find_windows_sys32_bin("powershell.exe"):
        return False, "powershell.exe tidak ditemukan dari WSL2.", status

    inner_lines = _foh_bridge_script_lines(port, bridge_port, internal_port, wifi_ip, quiet=True)
    if wifi_ip and wsl_ip:
        inner_lines.extend([
            f"$code = (& curl.exe -s -o NUL -w '%{{http_code}}' --max-time 2 'http://{wifi_ip}:{bridge_port}/api/system');",
            "if ($code -ne '200') {",
            f"  try {{ New-NetFirewallHyperVRule -Name 'SwarariaFOH{port}' -DisplayName 'Swararia FOH Remote {port}' -Direction Inbound -VMCreatorId '{{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}}' -Protocol TCP -LocalPorts {port} -Action Allow -ErrorAction SilentlyContinue | Out-Null; }} catch {{}}",
            f"  netsh interface portproxy delete v4tov4 listenport={bridge_port} listenaddress=0.0.0.0 | Out-Null;",
            f"  netsh interface portproxy add v4tov4 listenport={bridge_port} listenaddress=0.0.0.0 connectport={port} connectaddress={wsl_ip} | Out-Null;",
            "}",
        ])

    ok_exec, exec_err = _run_elevated_windows_powershell(inner_lines, timeout=35.0)
    if not ok_exec:
        return False, f"Gagal menjalankan PowerShell UAC: {exec_err}", get_wsl_bridge_status(port)

    updated = get_wsl_bridge_status(port)
    if updated.get("bridge_active"):
        return (
            True,
            f"Jembatan Port {bridge_port} ({listen_addr}:{bridge_port} -> 127.0.0.1:{internal_port}) berhasil diaktifkan & terverifikasi!",
            updated,
        )
    return (
        False,
        "Jembatan belum merespons HTTP 200 (Pastikan Anda klik 'Yes' pada jendela konfirmasi Admin Windows).",
        updated,
    )


def disable_wsl_port_bridge(port: int) -> tuple[bool, str, dict]:
    """Removes the Windows portproxy rule and firewall rules for the specified FOH bridge port."""
    status = get_wsl_bridge_status(port)
    if not status.get("is_wsl"):
        return True, "Bukan lingkungan WSL2.", status

    bridge_port = _get_wsl_remote_bridge_port(port)
    internal_port = int(status.get("internal_port") or _get_wsl_foh_internal_port(port))
    lan_ips = get_local_lan_ips(force_refresh=True)
    wifi_ip = lan_ips[0] if lan_ips else ""
    if not _find_windows_sys32_bin("powershell.exe"):
        return False, "powershell.exe tidak ditemukan.", status

    inner_lines = [
        f"netsh interface portproxy delete v4tov4 listenport={port} listenaddress=0.0.0.0 | Out-Null;",
        f"netsh interface portproxy delete v4tov4 listenport={bridge_port} listenaddress=0.0.0.0 | Out-Null;",
        f"netsh interface portproxy delete v4tov4 listenport={internal_port} listenaddress=0.0.0.0 | Out-Null;",
    ]
    if wifi_ip:
        inner_lines.extend([
            f"netsh interface portproxy delete v4tov4 listenport={port} listenaddress={wifi_ip} | Out-Null;",
            f"netsh interface portproxy delete v4tov4 listenport={bridge_port} listenaddress={wifi_ip} | Out-Null;",
            f"netsh interface portproxy delete v4tov4 listenport={internal_port} listenaddress={wifi_ip} | Out-Null;",
        ])
    inner_lines.extend([
        f"netsh advfirewall firewall delete rule name=\"Swararia FOH Remote {port}\" | Out-Null;",
        f"netsh advfirewall firewall delete rule name=\"Swararia FOH Remote {bridge_port}\" | Out-Null;",
        "try {",
        f"  Remove-NetFirewallHyperVRule -Name 'SwarariaFOH{port}' -ErrorAction SilentlyContinue;",
        "  Set-NetFirewallHyperVVMSetting -Name '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -DefaultInboundAction Block -ErrorAction SilentlyContinue | Out-Null;",
        "} catch {}",
    ])

    ok_exec, exec_err = _run_elevated_windows_powershell(inner_lines, timeout=30.0)
    if not ok_exec:
        return False, f"Gagal menutup jembatan port: {exec_err}", get_wsl_bridge_status(port)

    updated = get_wsl_bridge_status(port)
    if updated.get("has_portproxy") is not False or updated.get("firewall_present") is not False:
        return False, "Penghapusan belum terverifikasi; aturan Windows masih aktif atau status tidak tersedia.", updated
    return True, f"Aturan jembatan Port {bridge_port} & Firewall telah dihapus.", updated


def _reserved_browser_ports(http_port: int) -> set[int]:
    reserved = {http_port}
    if is_wsl_environment():
        reserved.update({_get_wsl_remote_bridge_port(http_port), _get_wsl_foh_internal_port(http_port)})
        with _wsl_foh_ports_lock:
            for primary, internal in _active_wsl_foh_ports.items():
                reserved.update({primary, primary + 1, internal})
    return reserved


def _get_native_remote_default_port(http_port: int) -> int:
    """Returns default Android Remote port, skipping web UI and FOH WSL bridge ports."""
    is_wsl = is_wsl_environment()
    reserved = {http_port}
    if is_wsl:
        reserved.add(_get_wsl_remote_bridge_port(http_port))
        with _wsl_foh_ports_lock:
            for primary, internal in _active_wsl_foh_ports.items():
                reserved.update({primary, primary + 1, internal})
    port = 8767 if is_wsl else 8766
    limit = 65534 if is_wsl else 65535
    while port in reserved and port < limit:
        port += 1
    if port in reserved:
        port = 8766
        while port in reserved and port > 1024:
            port -= 1
    return port


def _get_wsl_native_internal_port(
    public_port: int,
    http_port: int,
    _include_foh_internal: bool = True,
) -> int:
    """Chooses an internal WSL2 loopback port distinct from public_port, web UI, and FOH bridge."""
    reserved = {public_port, http_port, _get_wsl_remote_bridge_port(http_port)}
    if _include_foh_internal:
        reserved.update(_reserved_browser_ports(http_port))
    for delta in range(1, 32):
        for candidate in (public_port + delta, public_port - delta):
            if 1024 <= candidate <= 65535 and candidate not in reserved:
                return candidate
    return 8768


def _list_windows_portproxy_rules() -> list[tuple[str, int, str, int]] | None:
    netsh_bin = _find_windows_sys32_bin("netsh.exe")
    if not netsh_bin:
        return None
    rules: list[tuple[str, int, str, int]] = []
    try:
        out = subprocess.check_output(
            [netsh_bin, "interface", "portproxy", "show", "v4tov4"],
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=2.5,
        )
        for line in out.splitlines():
            m = re.match(
                r"^\s*(\d{1,3}(?:\.\d{1,3}){3}|\*)\s+(\d+)\s+(\d{1,3}(?:\.\d{1,3}){3}|\*)\s+(\d+)",
                line,
            )
            if m:
                rules.append((m.group(1), int(m.group(2)), m.group(3), int(m.group(4))))
    except Exception:
        return None
    return rules


def _inspect_windows_firewall_rules(rule_names: tuple[str, ...]) -> dict[str, bool] | None:
    """Map present app rule names to readiness from one ActiveStore read.

    An empty mapping proves absence; None means unknown, never absent. A key
    remains present even if every rule with that name is disabled or unsuitable.
    """
    ports = {}
    for name in rule_names:
        match = re.fullmatch(r"Swararia (?:Android|FOH) Remote ([0-9]{1,5})", name) if isinstance(name, str) else None
        if not match or not 1 <= int(match[1]) <= 65535:
            return None
        ports[name] = str(int(match[1]))
    if not ports:
        return None
    names_ps = ', '.join(f"'{name}'" for name in ports)
    ps_bin = _find_windows_sys32_bin("powershell.exe")
    if not ps_bin:
        return None
    script = (
        "$ErrorActionPreference = 'Stop'; try { "
        f"$names = @({names_ps}); $rules = @(Get-NetFirewallRule -PolicyStore ActiveStore -ErrorAction Stop | Where-Object {{ $names -contains $_.DisplayName }}); "
        "$rows = @($rules | ForEach-Object { $r = $_; "
        "$p = $r | Get-NetFirewallPortFilter -ErrorAction Stop; "
        "[PSCustomObject]@{ name=[string]$r.DisplayName; enabled=($r.Enabled -eq 'True'); inbound=($r.Direction -eq 'Inbound'); "
        "allow=($r.Action -eq 'Allow'); all_profiles=($r.Profile -eq 'Any' -or [int]$r.Profile -eq 7); "
        "protocol=[string]$p.Protocol; local_port=[string]$p.LocalPort } }); "
        "ConvertTo-Json -InputObject $rows -Compress; exit 0 } catch { exit 1 }"
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    try:
        proc = subprocess.run(
            [ps_bin, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", errors="strict", timeout=2.5, check=False,
        )
        if proc.returncode != 0:
            return None
        rows = json.loads(proc.stdout)
        flags = ('enabled', 'inbound', 'allow', 'all_profiles')
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) or not isinstance(row.get('name'), str) or row['name'] not in ports
            or any(type(row.get(k)) is not bool for k in flags)
            or not isinstance(row.get('protocol'), str) or not isinstance(row.get('local_port'), str)
            for row in rows
        ):
            return None
        present = {}
        for row in rows:
            name = row['name']
            ready = (all(row[k] for k in flags) and row['protocol'].upper() in ('TCP', '6')
                     and row['local_port'] == ports[name])
            present[name] = present.get(name, False) or ready
        return present
    except Exception:
        return None


def _check_windows_firewall_rule(rule_name: str) -> bool | None:
    """Readiness only; False does NOT prove rule absence."""
    rules = _inspect_windows_firewall_rules((rule_name,))
    return rules.get(rule_name, False) if rules is not None else None


def _verify_windows_tls_reachable(ip: str, port: int) -> bool:
    """Tests from Windows host side whether https://ip:port/v1/state responds with HTTP 401 from NativeTLSServer."""
    if not ip or not _is_rfc1918_ipv4(ip):
        return False
    curl_bin = _find_windows_sys32_bin("curl.exe")
    if not curl_bin:
        return False
    try:
        out = subprocess.check_output(
            [
                curl_bin,
                "-sk",
                "-o",
                "NUL",
                "-w",
                "%{http_code}",
                "--max-time",
                "1.5",
                f"https://{ip}:{int(port)}/v1/state",
            ],
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=2.5,
        ).strip()
        return out == "401"
    except Exception:
        return False


def _run_elevated_windows_powershell(inner_lines: list[str], timeout: float = 35.0) -> tuple[bool, str]:
    ps_bin = _find_windows_sys32_bin("powershell.exe")
    if not ps_bin:
        return False, "powershell.exe tidak ditemukan dari lingkungan ini."
    checked_lines = []
    for line in inner_lines:
        if line.lstrip().startswith("netsh "):
            command = line.strip().rstrip("; ")
            checked = command + "; if ($LASTEXITCODE -ne 0) { exit 1 };"
            proxy_delete = re.fullmatch(
                r'netsh interface portproxy delete v4tov4 listenport=([0-9]+) listenaddress=([0-9.]+)(?: \| Out-Null)?', command)
            firewall_delete = re.fullmatch(
                r'netsh advfirewall firewall delete rule name="(Swararia (?:Android|FOH) Remote [0-9]+)"(?: \| Out-Null)?', command)
            # netsh returns failure for an already-absent target too. Skip only
            # after a successful read proves absence; never ignore delete errors.
            if proxy_delete:
                port, address = proxy_delete.groups()
                pattern = rf'^\s*{re.escape(address)}\s+{port}\s+'
                line = (
                    "$existing = @(netsh interface portproxy show v4tov4); if ($LASTEXITCODE -ne 0) { exit 1 }; "
                    f"$existing = @($existing | Where-Object {{ $_ -match '{pattern}' }}); "
                    f"if ($existing.Count -gt 0) {{ {checked} }};"
                )
            elif firewall_delete:
                name = firewall_delete[1]
                line = (
                    "$existing = @(Get-NetFirewallRule -PolicyStore PersistentStore -ErrorAction Stop | "
                    f"Where-Object {{ $_.DisplayName -eq '{name}' }}); "
                    f"if ($existing.Count -gt 0) {{ {checked} }};"
                )
            else:
                line = checked
        checked_lines.append(line)
    inner_script = "$ErrorActionPreference = 'Stop'; try { " + " ".join(checked_lines) + " ; exit 0 } catch { exit 1 }"
    inner_b64 = base64.b64encode(inner_script.encode("utf-16le")).decode("ascii")
    outer_cmd = (
        "$ErrorActionPreference = 'Stop'; try { $child = Start-Process powershell.exe -Verb RunAs -WindowStyle Hidden "
        f"-Wait -PassThru -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-EncodedCommand','{inner_b64}'; "
        "exit $child.ExitCode } catch { exit 1 }"
    )
    outer_b64 = base64.b64encode(outer_cmd.encode("utf-16le")).decode("ascii")
    try:
        proc = subprocess.run(
            [ps_bin, "-NoProfile", "-NonInteractive", "-EncodedCommand", outer_b64],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=timeout,
            check=False,
        )
        return (True, "") if proc.returncode == 0 else (False, "PowerShell tidak berhasil (UAC dibatalkan atau aturan gagal).")
    except Exception:
        return False, "PowerShell tidak selesai atau tidak tersedia."


def _validated_bridge_target(http_port: int, public_port: int | None, wifi_ip: str, native_local_port: int | None) -> tuple[int, int, str]:
    default_port = _get_native_remote_default_port(http_port)
    eff_port = int(public_port) if isinstance(public_port, int) and not isinstance(public_port, bool) and 1 <= public_port <= 65535 else default_port
    eff_ip = wifi_ip.strip() if isinstance(wifi_ip, str) and _is_rfc1918_ipv4(wifi_ip) else ""
    if not eff_ip:
        rfc_ips = [ip for ip in get_local_lan_ips(force_refresh=False) if _is_rfc1918_ipv4(ip)]
        eff_ip = rfc_ips[0] if rfc_ips else ""
    eff_local = int(native_local_port) if isinstance(native_local_port, int) and not isinstance(native_local_port, bool) and 1 <= native_local_port <= 65535 else _get_wsl_native_internal_port(eff_port, http_port)
    return eff_port, eff_local, eff_ip


def _native_bridge_script_lines(
    needs_portproxy: bool,
    eff_port: int,
    internal_port: int,
    eff_ip: str,
    rule_name: str,
    quiet: bool = False,
) -> list[str]:
    suffix = " | Out-Null;" if quiet else ""
    parts: list[str] = []
    if needs_portproxy:
        if quiet:
            parts.append("Set-Service iphlpsvc -StartupType Automatic -ErrorAction SilentlyContinue;")
        parts.extend([
            "Start-Service iphlpsvc -ErrorAction SilentlyContinue" + (";" if quiet else ""),
            f"netsh interface portproxy delete v4tov4 listenport={eff_port} listenaddress=0.0.0.0{suffix}",
        ])
        if eff_ip:
            parts.append(f"netsh interface portproxy delete v4tov4 listenport={eff_port} listenaddress={eff_ip}{suffix}")
        parts.append(f"netsh interface portproxy delete v4tov4 listenport={internal_port} listenaddress=0.0.0.0{suffix}")
        if eff_ip:
            parts.append(f"netsh interface portproxy delete v4tov4 listenport={internal_port} listenaddress={eff_ip}{suffix}")
        parts.append(
            f"netsh interface portproxy add v4tov4 listenport={eff_port} listenaddress=0.0.0.0 connectport={internal_port} connectaddress=127.0.0.1{suffix}"
        )
    parts.extend([
        f'netsh advfirewall firewall delete rule name="{rule_name}"{suffix}',
        f'netsh advfirewall firewall add rule name="{rule_name}" dir=in action=allow protocol=TCP localport={eff_port} profile=any{suffix}',
    ])
    return parts


def get_native_remote_bridge_status(
    http_port: int,
    public_port: int | None = None,
    wifi_ip: str = "",
    native_enabled: bool = False,
    native_local_port: int | None = None,
) -> dict:
    """Inspects Windows portproxy/firewall readiness for Android Remote in WSL2 or native Windows."""
    is_wsl = is_wsl_environment()
    is_windows = os.name == "nt"
    eff_port, internal_port, eff_ip = _validated_bridge_target(http_port, public_port, wifi_ip, native_local_port)
    foh_bridge_port = _get_wsl_remote_bridge_port(http_port) if is_wsl else http_port
    linux_ips = _get_linux_internal_ips() if is_wsl else []
    wsl_ip = linux_ips[0] if linux_ips else ""

    if not is_wsl and not is_windows:
        return {
            "mode": "native_posix",
            "is_wsl": False,
            "is_windows": False,
            "needs_portproxy": False,
            "needs_firewall": False,
            "bridge_active": True,
            "has_portproxy": False,
            "firewall_active": True,
            "port_conflict": eff_port == http_port,
            "foh_bridge_port": foh_bridge_port,
            "wifi_ip": eff_ip,
            "public_port": eff_port,
            "internal_port": eff_port,
            "tls_reachable": None,
            "powershell_admin_cmd": "",
        }

    needs_portproxy = bool(is_wsl and (not eff_ip or eff_ip not in linux_ips))
    rule_name = f"Swararia Android Remote {eff_port}"
    reserved = _reserved_browser_ports(http_port)
    port_conflict = eff_port in reserved or (needs_portproxy and internal_port in reserved)

    has_portproxy = True
    portproxy_present = False
    if needs_portproxy:
        observed_rules = _list_windows_portproxy_rules()
        portproxy_present = (any(lp == eff_port for _, lp, _, _ in observed_rules)
                             if observed_rules is not None else None)
        valid_targets = {("127.0.0.1", internal_port)}
        if wsl_ip:
            valid_targets.add((wsl_ip, eff_port))
            valid_targets.add((wsl_ip, internal_port))
        has_portproxy = any(
            lp == eff_port and la in ("0.0.0.0", "*", eff_ip) and (ca, cp) in valid_targets
            for (la, lp, ca, cp) in (observed_rules or [])
        )

    firewall_rules = _inspect_windows_firewall_rules((rule_name,))
    firewall_active = firewall_rules.get(rule_name, False) if firewall_rules is not None else None
    firewall_present = bool(firewall_rules) if firewall_rules is not None else None
    tls_reachable = None
    if native_enabled and eff_ip and not port_conflict and _find_windows_sys32_bin("curl.exe"):
        tls_reachable = _verify_windows_tls_reachable(eff_ip, eff_port)

    cmd_parts = _native_bridge_script_lines(
        needs_portproxy, eff_port, internal_port, eff_ip, rule_name, quiet=False
    )

    bridge_active = (
        not port_conflict
        and has_portproxy
        and firewall_active is True
        and (tls_reachable is True if tls_reachable is not None else True)
    )
    return {
        "mode": "wsl2_nat" if needs_portproxy else ("wsl2_mirrored" if is_wsl else "windows_native"),
        "is_wsl": is_wsl,
        "is_windows": is_windows,
        "needs_portproxy": needs_portproxy,
        "needs_firewall": True,
        "bridge_active": bridge_active,
        "has_portproxy": has_portproxy,
        "portproxy_present": portproxy_present,
        "firewall_active": firewall_active,
        "firewall_present": firewall_present,
        "port_conflict": port_conflict,
        "foh_bridge_port": foh_bridge_port,
        "wifi_ip": eff_ip,
        "wsl_ip": wsl_ip,
        "public_port": eff_port,
        "internal_port": internal_port if needs_portproxy else eff_port,
        "tls_reachable": tls_reachable,
        "powershell_admin_cmd": "; ".join(cmd_parts),
    }


def enable_native_remote_bridge(
    http_port: int,
    public_port: int | None = None,
    wifi_ip: str = "",
    native_enabled: bool = False,
    native_local_port: int | None = None,
) -> tuple[bool, str, dict]:
    """Prompts Windows UAC (explicit user action only) to enable Android Remote portproxy & firewall."""
    status = get_native_remote_bridge_status(
        http_port,
        public_port=public_port,
        wifi_ip=wifi_ip,
        native_enabled=native_enabled,
        native_local_port=native_local_port,
    )
    if not status.get("is_wsl") and not status.get("is_windows"):
        return True, "Tidak memerlukan bridge Windows.", status
    if status.get("port_conflict"):
        return False, f"Port {status['public_port']} dipakai UI / FOH Remote WSL. Gunakan port {_get_native_remote_default_port(http_port)}.", status

    eff_port = status["public_port"]
    internal_port = status["internal_port"]
    eff_ip = status.get("wifi_ip") or ""
    rule_name = f"Swararia Android Remote {eff_port}"

    inner_lines = _native_bridge_script_lines(
        bool(status.get("needs_portproxy")), eff_port, internal_port, eff_ip, rule_name, quiet=True
    )

    ok_ps, err = _run_elevated_windows_powershell(inner_lines, timeout=35.0)
    if not ok_ps:
        return False, f"Gagal menjalankan PowerShell UAC: {err}", status

    updated = get_native_remote_bridge_status(
        http_port,
        public_port=eff_port,
        wifi_ip=eff_ip,
        native_enabled=native_enabled,
        native_local_port=native_local_port,
    )
    if updated.get("bridge_active"):
        if native_enabled and updated.get("tls_reachable"):
            return True, f"Bridge/Firewall Android Remote {eff_port} aktif & TLS terverifikasi!", updated
        return True, f"Aturan Bridge/Firewall Windows untuk port {eff_port} aktif. Klik 'Hubungkan Android' jika belum aktif.", updated
    return (
        False,
        "Bridge/Firewall belum aktif (Pastikan klik 'Yes' pada jendela konfirmasi Admin Windows dan 'Hubungkan Android' sudah aktif).",
        updated,
    )


def disable_native_remote_bridge(
    http_port: int,
    public_port: int | None = None,
    wifi_ip: str = "",
    native_enabled: bool = False,
    native_local_port: int | None = None,
) -> tuple[bool, str, dict]:
    """Removes the Windows portproxy and firewall rule for Android Remote without disturbing FOH Remote."""
    status = get_native_remote_bridge_status(
        http_port,
        public_port=public_port,
        wifi_ip=wifi_ip,
        native_enabled=native_enabled,
        native_local_port=native_local_port,
    )
    if not status.get("is_wsl") and not status.get("is_windows"):
        return True, "Bukan lingkungan Windows/WSL2.", status
    if status.get("port_conflict"):
        return False, f"Port {status['public_port']} dipakai FOH Remote / UI sehingga tidak dihapus dari panel ini.", status

    eff_port = status["public_port"]
    eff_ip = status.get("wifi_ip") or ""
    rule_name = f"Swararia Android Remote {eff_port}"
    inner_lines = [
        f'netsh advfirewall firewall delete rule name="{rule_name}" | Out-Null;',
    ]
    if status.get("needs_portproxy"):
        inner_lines.insert(0, f"netsh interface portproxy delete v4tov4 listenport={eff_port} listenaddress=0.0.0.0 | Out-Null;")
        if eff_ip:
            inner_lines.insert(1, f"netsh interface portproxy delete v4tov4 listenport={eff_port} listenaddress={eff_ip} | Out-Null;")

    ok_ps, err = _run_elevated_windows_powershell(inner_lines, timeout=30.0)
    updated = get_native_remote_bridge_status(
        http_port,
        public_port=eff_port,
        wifi_ip=eff_ip,
        native_enabled=native_enabled,
        native_local_port=native_local_port,
    )
    if not ok_ps:
        return False, f"Gagal menghapus aturan Windows: {err}", updated
    if (updated.get("firewall_present") is not False or
            (updated.get("needs_portproxy") and updated.get("portproxy_present", updated.get("has_portproxy")) is not False)):
        return False, "Penghapusan belum terverifikasi; aturan Windows masih aktif atau status tidak tersedia.", updated
    return True, f"Aturan Bridge/Firewall Android Remote port {eff_port} telah dihapus.", updated
