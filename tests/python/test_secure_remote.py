"""Native remote security/transport regressions; no external networking."""
import sys
from pathlib import Path


def test_pairing_requires_one_time_invite_and_local_approval():
    from swararia.remote.secure_remote import SecureRemote, RemoteError
    from swararia.server.web_ui import RemotePlayerHub
    now = [100.0]
    remote = SecureRemote(RemotePlayerHub(), clock=lambda: now[0])
    assert remote.admin_status()['enabled'] is False
    # Core activation is separate from the listener for deterministic tests.
    remote.activate()
    invite = remote.open_invite()
    pair = remote.pair(invite, 'Telepon operator')
    assert remote.session(pair['token'])['status'] == 'pending'
    try:
        remote.pair(invite, 'replay')
        assert False, 'invite must be single-use'
    except RemoteError as exc:
        assert exc.code == 'invalid_invite'
    remote.approve(pair['device_id'], 'view_only')
    assert remote.session(pair['token'])['role'] == 'view_only'
    remote.revoke(pair['device_id'])
    try:
        remote.session(pair['token'])
        assert False, 'revoked identity must fail closed'
    except RemoteError as exc:
        assert exc.code == 'revoked'
    invite = remote.open_invite()
    now[0] += 91
    try:
        remote.pair(invite, 'expired')
        assert False
    except RemoteError as exc:
        assert exc.code == 'invalid_invite'
    remote.deactivate()
    assert remote.admin_status()['enabled'] is False


def test_commands_deduplicate_expire_and_require_observed_ack():
    from swararia.remote.secure_remote import SecureRemote, RemoteError
    from swararia.server.web_ui import RemotePlayerHub
    import time
    now = [time.time()]
    hub = RemotePlayerHub()
    remote = SecureRemote(hub, clock=lambda: now[0])
    remote.activate()
    pair = remote.pair(remote.open_invite(), 'Operator')
    token = pair['token']
    remote.approve(pair['device_id'], 'control')
    try:
        remote.command(token, 'play', 'request-0001')
        assert False, 'offline master must reject before queueing'
    except RemoteError as exc:
        assert exc.code == 'master_unavailable'
    remote.master_poll({'track': 'test', 'is_playing': False}, [], 'master-instance-one')
    command = remote.command(token, 'play', 'request-0001')
    assert command['status'] == 'accepted'
    assert remote.command(token, 'play', 'request-0001') == command
    try:
        remote.command(token, 'stop', 'request-0001')
        assert False, 'key reuse for different action must conflict'
    except RemoteError as exc:
        assert exc.code == 'idempotency_conflict'
    queue, *_ = hub.update_master_state({'track': 'test'})
    delivered = remote.master_exchange(queue, [])
    assert len(delivered) == 1
    assert remote.command_status(token, command['command_id'])['status'] == 'pending'
    remote.master_exchange([], [{'command_id': command['command_id'], 'status': 'applied', 'observed': {'is_playing': False}}])
    assert remote.command_status(token, command['command_id'])['status'] == 'pending'
    remote.master_exchange([], [{'command_id': command['command_id'], 'status': 'applied', 'observed': {'is_playing': True}}])
    assert remote.command_status(token, command['command_id'])['status'] == 'applied'
    expired = remote.command(token, 'stop', 'request-0002')
    now[0] += remote.COMMAND_TTL + 1
    queue, *_ = hub.update_master_state({'track': 'test'})
    assert remote.master_exchange(queue, []) == []
    assert remote.command_status(token, expired['command_id'])['status'] == 'expired'
    now[0] = time.time()
    hub.update_master_state({'track': 'test'})
    pending = remote.command(token, 'pause', 'request-0003')
    queue, *_ = hub.update_master_state({'track': 'test'})
    remote.master_exchange(queue, [])
    now[0] += remote.COMMAND_TTL + 1
    assert remote.command_status(token, pending['command_id'])['status'] == 'unknown'
    remote.approve(pair['device_id'], 'view_only')
    try:
        remote.command(token, 'stop', 'request-0004')
        assert False
    except RemoteError as exc:
        assert exc.code == 'view_only'
    remote.revoke(pair['device_id'])
    assert hub.master_state == {'track': 'test'}, 'revocation must not stop PC audio'


def test_tls_pinned_pair_state_roles_and_strict_protocol():
    from swararia.remote.secure_remote import SecureRemote, RemoteError
    from swararia.server.web_ui import RemotePlayerHub
    import hashlib
    import http.client
    import json
    import socket
    import ssl
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    remote = SecureRemote(RemotePlayerHub())
    for host in ('0.0.0.0', '8.8.8.8', '169.254.1.1', 'localhost', '::1'):
        try:
            remote.start(host, 0)
            assert False, 'must require explicit private IPv4'
        except RemoteError as exc:
            assert exc.code == 'invalid_bind'
    remote.start('127.0.0.1', 0)
    port = remote.listener.server_address[1]
    cert = x509.load_pem_x509_certificate(remote.certificate.encode())
    assert str(cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.IPAddress)[0]) == '127.0.0.1'
    assert remote.admin_status()['fingerprint_sha256'] == hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    def request(method, path, body=None, token=None, headers=None, context=None):
        c = http.client.HTTPSConnection('127.0.0.1', port, timeout=4, context=context or trusted)
        h = {'Content-Type': 'application/json'}
        if token:
            h['Authorization'] = 'Bearer ' + token
        h.update(headers or {})
        c.request(method, path, json.dumps(body) if body is not None else None, h)
        r = c.getresponse()
        data = json.loads(r.read())
        c.close()
        return r.status, data
    trusted = ssl.create_default_context(cadata=remote.certificate)
    try:
        try:
            request('GET', '/v1/capabilities', context=ssl.create_default_context())
            assert False, 'normal clients must reject self-signed certificates'
        except ssl.SSLCertVerificationError:
            pass
        assert request('GET', '/v1/capabilities')[0] == 401
        assert request('GET', '/v1/state', token='bogus')[0] == 401
        invite = remote.open_invite()
        status, pair = request('POST', '/v1/pair', {'label': 'HP'}, invite)
        assert status == 202
        token = pair['token']
        assert request('GET', '/v1/session', token=token)[1]['status'] == 'pending'
        assert request('GET', '/v1/state', token=token)[0] == 403
        remote.approve(pair['device_id'], 'view_only')
        legacy = ['play', 'pause', 'stop']
        # Released Android 0.6.1 rejects unknown actions; default stays v1/3.
        for headers in [None, {'X-Swararia-Capabilities': 'unsupported'}, {'X-Swararia-Capabilities': 'extended-v1,other'}, {'X-Swararia-Capabilities': 'EXTENDED-V1'}]:
            status, capabilities = request('GET', '/v1/capabilities', token=token, headers=headers)
            assert status == 200 and capabilities['protocol'] == 'v1'
            assert capabilities['commands'] == legacy
        extended = {'X-Swararia-Capabilities': 'extended-v1'}
        assert request('GET', '/v1/capabilities', token=token, headers=extended)[1]['commands'] == ['play', 'pause', 'stop', 'fade_out', 'prev', 'next', 'select_track', 'select_group']
        assert request('GET', '/v1/capabilities?extended-v1', token=token, headers=extended)[0] == 404
        assert request('GET', '/v1/state', token=token)[0] == 200
        assert request('POST', '/v1/commands', {'action': 'play', 'idempotency_key': 'request-0001'}, token)[1]['code'] == 'view_only'
        for path in ('/api/library', '/v1/state?token=secret', '/v1/state/', '//v1/state', '/v1/%73tate'):
            assert request('GET', path, token=token)[0] == 404
        assert request('POST', '/v1/pair', [], invite)[0] == 400
        assert request('POST', '/v1/pair', {}, invite, {'Content-Type': 'text/plain'})[0] == 415
        assert request('POST', '/v1/pair', {}, invite, {'Content-Length': '-1'})[0] == 400
        assert request('POST', '/v1/pair', {}, invite, {'Content-Length': '9999999'})[0] == 413
        assert request('POST', '/v1/pair', {}, invite, {'Transfer-Encoding': 'chunked'})[0] == 400
        # A client that never starts TLS must not block the accepting thread.
        slow = socket.create_connection(('127.0.0.1', port), timeout=2)
        try:
            assert request('GET', '/v1/session', token=token)[0] == 200
        finally:
            slow.close()
        remote.revoke(pair['device_id'])
        assert request('GET', '/v1/state', token=token)[1]['code'] == 'revoked'
        # No silent fallback if another process owns the explicit port.
        other = SecureRemote(RemotePlayerHub())
        try:
            other.start('127.0.0.1', port)
            assert False
        except RemoteError as exc:
            assert exc.code == 'listener_unavailable'
        assert not other.enabled
    finally:
        remote.stop()
    assert remote.listener is None
    assert not remote.enabled


def test_tls_advertised_lan_san_and_posix_immediate_restart():
    import os
    import socket
    import ssl
    from cryptography import x509
    from swararia.remote.secure_remote import SecureRemote
    from swararia.server.web_ui import RemotePlayerHub
    remote = SecureRemote(RemotePlayerHub())
    remote.start('127.0.0.1', 0, advertise_ip='192.168.18.47', advertise_port=8767)
    try:
        bind_port = remote.listener.server_address[1]
        status = remote.admin_status()
        assert status['url'] == 'https://192.168.18.47:8767'
        assert status['bind_url'] == f'https://127.0.0.1:{bind_port}'
        cert = x509.load_pem_x509_certificate(remote.certificate.encode('ascii'))
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        san_ips = {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
        assert san_ips == {'127.0.0.1', '192.168.18.47'}
        ctx = ssl.create_default_context(cadata=remote.certificate)
        with socket.create_connection(('127.0.0.1', bind_port), timeout=2) as raw:
            with ctx.wrap_socket(raw, server_hostname='192.168.18.47') as tls_sock:
                assert tls_sock.version() is not None
        try:
            with socket.create_connection(('127.0.0.1', bind_port), timeout=2) as raw:
                ctx.wrap_socket(raw, server_hostname='10.0.0.99')
            assert False, 'unadvertised IP must fail SAN verification'
        except ssl.SSLCertVerificationError:
            pass
    finally:
        remote.stop()
    if os.name == 'posix':
        # Rebinding immediately on the same port after a real TLS client connection must work on POSIX.
        remote2 = SecureRemote(RemotePlayerHub())
        remote2.start('127.0.0.1', bind_port, advertise_ip='192.168.18.47', advertise_port=8767)
        remote2.stop()


def test_fade_out_command_requires_fading_state_and_rejects_paused():
    from swararia.server.web_ui import RemotePlayerHub
    from swararia.remote.secure_remote import SecureRemote
    remote = SecureRemote(RemotePlayerHub())
    remote.activate()
    pair = remote.pair(remote.open_invite(), 'Operator')
    remote.approve(pair['device_id'], 'control')
    remote.master_poll({'is_playing': True, 'transport_state': 'PLAYING'}, [], 'master-instance-one')
    accepted = remote.command(pair['token'], 'fade_out', 'fade-req-0001')
    assert accepted['status'] == 'accepted'
    delivered = remote.master_poll({'is_playing': True, 'transport_state': 'PLAYING'}, [], 'master-instance-one')
    assert [c['action'] for c in delivered] == ['fade_out']
    # Spoofed ack reporting paused instead of FADING must not verify fade_out.
    remote.master_poll({'is_playing': False}, [
        {'command_id': accepted['command_id'], 'status': 'applied', 'observed': {'is_playing': False, 'transport_state': 'PAUSED'}}
    ], 'master-instance-one')
    assert remote.command_status(pair['token'], accepted['command_id'])['status'] == 'pending'
    remote.master_poll({'is_playing': True, 'transport_state': 'FADING'}, [
        {'command_id': accepted['command_id'], 'status': 'applied', 'observed': {'is_playing': True, 'transport_state': 'FADING'}}
    ], 'master-instance-one')
    assert remote.command_status(pair['token'], accepted['command_id'])['status'] == 'applied'
    rejected = remote.command(pair['token'], 'fade_out', 'fade-req-0002')
    remote.master_poll({'is_playing': False}, [], 'master-instance-one')
    remote.master_poll({'is_playing': False}, [
        {'command_id': rejected['command_id'], 'status': 'rejected'}
    ], 'master-instance-one')
    assert remote.command_status(pair['token'], rejected['command_id'])['status'] == 'rejected'


def test_tracks_catalog_sanitization_and_select_track_commands():
    import json
    from swararia.server.web_ui import RemotePlayerHub
    from swararia.remote.secure_remote import SecureRemote, RemoteError
    now = [1000.0]
    remote = SecureRemote(RemotePlayerHub(), clock=lambda: now[0])
    remote.activate()
    invite = remote.open_invite()
    pair = remote.pair(invite, 'Selector')
    remote.approve(pair['device_id'], 'control')
    remote.master_poll(
        {'is_playing': False, 'native_track_id': 'trk_one'},
        [],
        'master-track-instance',
        catalog={
            'playlist': r'C:\private\Resepsi',
            'items': [
                {'id': 'trk_one', 'title': r'D:\Music\01 - Opening.wav', 'rel_path': 'secret/path'},
                {'id': 'trk_two', 'title': 'https://evil.test/steal.mp3'},
                {'id': 'bad id!', 'title': 'Ignored'},
                {'id': 'trk_three', 'title': '02 - Masuk.\x00mp3'},
            ],
        },
    )
    catalog = remote.tracks(pair['token'])
    assert catalog['master_online'] is True
    assert catalog['playlist'] == 'Resepsi'
    assert catalog['current_track_id'] == 'trk_one'
    assert catalog['tracks'] == [
        {'id': 'trk_one', 'title': '01 - Opening.wav'},
        {'id': 'trk_three', 'title': '02 - Masuk.mp3'},
    ]
    # Even view_only can inspect tracks, and /v1/state still keeps only its 5 public fields.
    assert set(remote.state(pair['token'])['state']) == {'track_title', 'is_playing', 'transport_state', 'current_time', 'duration'}
    # Unknown track or wrong auto_play fails closed.
    for args in [('trk_missing', False), ('bad id', False), ('trk_three', 'yes')]:
        try:
            remote.command(pair['token'], 'select_track', f'bad-{args[0]}', track_id=args[0], auto_play=args[1])
            assert False
        except RemoteError:
            pass
    accepted = remote.command(pair['token'], 'select_track', 'sel-req-0001', track_id='trk_three', auto_play=True)
    # Reusing same idempotency key for a different track conflicts.
    try:
        remote.command(pair['token'], 'select_track', 'sel-req-0001', track_id='trk_one', auto_play=True)
        assert False
    except RemoteError as exc:
        assert exc.code == 'idempotency_conflict'
    delivered = remote.master_poll({'is_playing': False}, [], 'master-track-instance')
    assert delivered[0]['params'] == {'track_id': 'trk_three', 'auto_play': True}
    # Spoofing auto_play=True as stopped must not verify as applied.
    remote.master_poll({'is_playing': False}, [
        {'command_id': accepted['command_id'], 'status': 'applied', 'observed': {'is_playing': False, 'track_id': 'trk_three'}}
    ], 'master-track-instance')
    assert remote.command_status(pair['token'], accepted['command_id'])['status'] == 'pending'
    remote.master_poll({'is_playing': True, 'native_track_id': 'trk_three'}, [
        {'command_id': accepted['command_id'], 'status': 'applied', 'observed': {'is_playing': True, 'track_id': 'trk_three'}}
    ], 'master-track-instance')
    assert remote.command_status(pair['token'], accepted['command_id'])['status'] == 'applied'
    assert remote.tracks(pair['token'])['current_track_id'] == 'trk_three'
    # Offline master clears served tracks cleanly.
    now[0] += 3.0
    offline_tracks = remote.tracks(pair['token'])
    assert offline_tracks['master_online'] is False and offline_tracks['tracks'] == [] and offline_tracks['groups'] == []


def test_tracks_catalog_groups_and_select_group_command():
    from swararia.server.web_ui import RemotePlayerHub
    from swararia.remote.secure_remote import SecureRemote, RemoteError
    remote = SecureRemote(RemotePlayerHub())
    remote.activate()
    pair = remote.pair(remote.open_invite(), 'GroupRemote')
    remote.approve(pair['device_id'], 'control')
    remote.master_poll(
        {'is_playing': False},
        [],
        'master-group-instance',
        catalog={
            'playlist': 'Semua Lagu',
            'active_group_id': 'g_all',
            'total_tracks': 2,
            'groups': [
                {'id': 'g_all', 'name': 'Semua Lagu', 'count': 2},
                {'id': 'g_opening', 'name': r'D:\Music\Opening', 'count': 2},
                {'id': 'g_bad_url', 'name': 'https://evil.test/steal', 'count': 1},
            ],
            'items': [
                {'id': 'trk_a', 'title': 'Fanfare.wav', 'group_id': 'g_opening'},
                {'id': 'trk_b', 'title': 'Anthem.wav', 'group_id': 'g_missing'},
            ],
        },
    )
    catalog = remote.tracks(pair['token'])
    assert catalog['active_group_id'] == 'g_all'
    assert catalog['total_tracks'] == 2
    assert catalog['groups'] == [
        {'id': 'g_all', 'name': 'Semua Lagu', 'count': 2},
        {'id': 'g_opening', 'name': 'Opening', 'count': 2},
    ]
    assert catalog['tracks'] == [
        {'id': 'trk_a', 'title': 'Fanfare.wav', 'group_id': 'g_opening'},
        {'id': 'trk_b', 'title': 'Anthem.wav'},
    ]
    try:
        remote.command(pair['token'], 'select_group', 'grp-missing-1', group_id='g_missing')
        assert False
    except RemoteError as exc:
        assert exc.code == 'unknown_group'
    accepted = remote.command(pair['token'], 'select_group', 'grp-req-0001', group_id='g_opening')
    delivered = remote.master_poll({'is_playing': False}, [], 'master-group-instance')
    assert delivered[0]['params'] == {'group_id': 'g_opening'}
    remote.master_poll({'is_playing': False}, [
        {'command_id': accepted['command_id'], 'status': 'applied', 'observed': {'active_group_id': 'g_opening'}}
    ], 'master-group-instance')
    assert remote.command_status(pair['token'], accepted['command_id'])['status'] == 'applied'
    assert remote.tracks(pair['token'])['active_group_id'] == 'g_opening'


def test_media_transfer_disabled_by_default_and_streams_verified_audio(tmp_path):
    import hashlib
    import http.client
    import json
    import ssl
    import urllib.parse
    from swararia.core import fs_browser
    from swararia.server.web_ui import RemotePlayerHub
    from swararia.remote.secure_remote import SecureRemote

    inbox = tmp_path / 'inbox'
    inbox.mkdir()
    song_bytes = (b'ID3\x04\x00test-audio-payload-' * 128)
    song_sha = hashlib.sha256(song_bytes).hexdigest()
    (inbox / 'Lagu Uji [abc].mp3').write_bytes(song_bytes)
    (tmp_path / 'outside.mp3').write_bytes(b'secret-outside')

    remote = SecureRemote(
        RemotePlayerHub(),
        media_resolver=fs_browser.resolve_safe_audio_file,
        default_inbox=inbox,
    )
    remote.start('127.0.0.1', 0)
    try:
        invite1 = remote.open_invite()
        ctrl = remote.pair(invite1, 'ControlPhone')
        remote.approve(ctrl['device_id'], 'control')
        invite2 = remote.open_invite()
        viewer = remote.pair(invite2, 'ViewOnlyPhone')
        remote.approve(viewer['device_id'], 'view_only')

        remote.master_poll(
            {'is_playing': False},
            [],
            'master-media-instance',
            catalog={
                'source_dir': str(inbox),
                'playlist': 'Semua Lagu',
                'total_tracks': 2,
                'items': [
                    {'id': 'trk_ok', 'title': 'Lagu Uji', 'rel_path': 'Lagu Uji [abc].mp3'},
                    {'id': 'trk_evil', 'title': 'Outside', 'rel_path': '../outside.mp3'},
                ],
            },
        )

        # Catalog JSON never leaks source_dir or rel_path
        tracks_payload = remote.tracks(ctrl['token'])
        assert tracks_payload['tracks'] == [
            {'id': 'trk_ok', 'title': 'Lagu Uji'},
            {'id': 'trk_evil', 'title': 'Outside'},
        ]

        host = remote.bind_info['ip']
        port = remote.listener.server_address[1]
        ctx = ssl.create_default_context(cadata=remote.certificate)

        def https_req(method, path, token, headers=None):
            conn = http.client.HTTPSConnection(host, port, context=ctx, timeout=5)
            req_headers = {'Authorization': f'Bearer {token}', **(headers or {})}
            conn.request(method, path, headers=req_headers)
            resp = conn.getresponse()
            body = resp.read()
            hdrs = {k.lower(): v for k, v in resp.getheaders()}
            conn.close()
            return resp.status, hdrs, body

        # Default disabled: capabilities.media=False and endpoint 403
        st, _, body = https_req('GET', '/v1/capabilities', ctrl['token'], {'X-Swararia-Capabilities': 'extended-v1'})
        assert st == 200 and json.loads(body)['media'] is False
        st, _, body = https_req('GET', '/v1/tracks/trk_ok/media', ctrl['token'])
        assert st == 403 and json.loads(body)['code'] == 'media_disabled'

        # Enable media transfer: view_only still denied, control allowed
        remote.set_media_transfer(True)
        assert remote.admin_status()['media_transfer_enabled'] is True

        st, _, body = https_req('GET', '/v1/capabilities', viewer['token'], {'X-Swararia-Capabilities': 'extended-v1'})
        assert st == 200 and json.loads(body)['media'] is False
        st, _, body = https_req('GET', '/v1/tracks/trk_ok/media', viewer['token'])
        assert st == 403 and json.loads(body)['code'] == 'view_only'

        st, _, body = https_req('GET', '/v1/capabilities', ctrl['token'], {'X-Swararia-Capabilities': 'extended-v1'})
        caps = json.loads(body)
        assert st == 200 and caps['media'] is True and caps['media_max_bytes'] == remote.MAX_MEDIA_BYTES

        # HEAD returns metadata & hash with empty body
        st, hdrs, body = https_req('HEAD', '/v1/tracks/trk_ok/media', ctrl['token'])
        assert st == 200 and body == b''
        assert hdrs['x-swararia-sha256'] == song_sha
        assert int(hdrs['content-length']) == len(song_bytes)
        assert hdrs['x-content-type-options'] == 'nosniff'
        assert hdrs['cache-control'] == 'no-store'
        assert urllib.parse.unquote(hdrs['x-swararia-filename']) == 'Lagu Uji [abc].mp3'
        assert hdrs['content-disposition'].startswith('attachment;')

        # GET streams exact file bytes matching SHA-256
        st, hdrs, body = https_req('GET', '/v1/tracks/trk_ok/media', ctrl['token'])
        assert st == 200 and body == song_bytes and hashlib.sha256(body).hexdigest() == song_sha

        # Path traversal fails closed inside fs_browser.resolve_safe_audio_file
        st, _, body = https_req('GET', '/v1/tracks/trk_evil/media', ctrl['token'])
        assert st == 403 and json.loads(body)['code'] == 'media_forbidden'

        # HEAD on JSON endpoint remains 405
        st, _, _ = https_req('HEAD', '/v1/state', ctrl['token'])
        assert st == 405
    finally:
        remote.stop()
        assert remote.admin_status()['media_transfer_enabled'] is False
