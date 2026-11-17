"""Android native-remote admin endpoints."""
from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlparse

from swararia.remote.secure_remote import RemoteError
from swararia.server import network


class NativeAdminMixin:
    """Android native-remote admin endpoints."""


    def _build_native_admin_status(self, force_refresh: bool = False) -> dict:
        remote = self.server.secure_remote
        status = remote.admin_status()
        http_port = int(self.server.server_address[1])
        is_wsl = network.is_wsl_environment()
        lan_ips = [ip for ip in network.get_local_lan_ips(force_refresh=force_refresh) if network._is_rfc1918_ipv4(ip)]
        linux_ips = network._get_linux_internal_ips() if is_wsl else []
        status['lan_ips'] = lan_ips
        if lan_ips:
            status['default_bind_ip'] = lan_ips[0]
        status['default_port'] = network._get_native_remote_default_port(http_port)
        status['is_wsl'] = is_wsl
        status['is_wsl_nat'] = bool(is_wsl and (not lan_ips or lan_ips[0] not in linux_ips))
        status['is_windows'] = (os.name == 'nt')
        if is_wsl:
            status['foh_bridge_port'] = network._get_wsl_remote_bridge_port(http_port)
        return status

    def _resolve_native_bridge_target(self, payload: dict | None = None) -> tuple[int, int | None, str, bool, int | None]:
        http_port = int(self.server.server_address[1])
        remote = self.server.secure_remote
        with remote.lock:
            enabled = bool(remote.enabled)
            conn_url = remote.connection_info.get('url', '') if enabled else ''
            bind_port = remote.bind_info.get('port') if enabled else None
        if enabled and conn_url:
            parsed = urlparse(conn_url)
            return http_port, parsed.port, (parsed.hostname or ''), True, bind_port
        req_ip = ''
        req_port = None
        if isinstance(payload, dict):
            cand_ip = payload.get('bind_ip')
            if isinstance(cand_ip, str) and network._is_rfc1918_ipv4(cand_ip):
                req_ip = cand_ip.strip()
            cand_port = payload.get('port')
            if type(cand_port) is int and 1 <= cand_port <= 65535:
                req_port = cand_port
        return http_port, req_port, req_ip, False, None

    def native_admin(self, payload):
        remote = self.server.secure_remote
        try:
            op = payload.get('action')
            fields = {'start': {'action', 'bind_ip', 'port'}, 'stop': {'action'},
                      'invite_open': {'action'}, 'invite_close': {'action'},
                      'approve': {'action', 'device_id', 'role'},
                      'revoke': {'action', 'device_id'}, 'deny': {'action', 'device_id'},
                      'set_media_transfer': {'action', 'enabled'}}
            if not isinstance(op, str) or op not in fields or set(payload) - fields[op]:
                raise RemoteError('invalid_admin_action', 400)
            if op == 'start':
                bind_ip = payload.get('bind_ip')
                http_port = int(self.server.server_address[1])
                port = payload.get('port', network._get_native_remote_default_port(http_port))
                from swararia.remote.secure_remote_tls import validate_bind
                validate_bind(bind_ip, port)
                is_wsl = network.is_wsl_environment()
                if port in network._reserved_browser_ports(http_port):
                    raise RemoteError('wsl_port_reserved' if is_wsl else 'port_reserved', 409)
                linux_ips = set(network._get_linux_internal_ips()) if is_wsl else set()
                if is_wsl and not ipaddress.IPv4Address(bind_ip).is_loopback and bind_ip not in linux_ips:
                    reachable_ips = {ip for ip in network.get_local_lan_ips(force_refresh=False) if network._is_rfc1918_ipv4(ip)}
                    if bind_ip not in reachable_ips:
                        reachable_ips = {ip for ip in network.get_local_lan_ips(force_refresh=True) if network._is_rfc1918_ipv4(ip)}
                    if bind_ip in reachable_ips:
                        internal_port = network._get_wsl_native_internal_port(port, http_port)
                        remote.start('127.0.0.1', internal_port, advertise_ip=bind_ip, advertise_port=port)
                    else:
                        remote.start(bind_ip, port)
                else:
                    remote.start(bind_ip, port)
            elif op == 'stop':
                remote.stop()
            elif op == 'invite_open':
                remote.open_invite()
            elif op == 'invite_close':
                remote.close_invite()
            elif op == 'approve':
                remote.approve(payload.get('device_id'), payload.get('role'))
            elif op in ('deny', 'revoke'):
                remote.revoke(payload.get('device_id'), deny=op == 'deny')
            elif op == 'set_media_transfer':
                remote.set_media_transfer(payload.get('enabled'))
            self.send_json(200, self._build_native_admin_status(force_refresh=False))
        except RemoteError as exc:
            self.send_json(exc.status, {'code': exc.code})
        except (ValueError, TypeError):
            self.send_json(400, {'code': 'invalid_request'})
