"""Lantern over TLS: the certificate, the listener, and what counts as a secure request."""
import json
import os
import socket
import ssl
import stat
import sys
import urllib.request

import pytest
from lantern.spki import spki_sha256

from lantern_server import service, tls
from settings import get_settings

from app import create_app


def settings_with(**lantern):
    settings = dict(get_settings())
    settings['lantern'] = {**settings['lantern'], 'enabled': True, **lantern}
    settings['shop'] = {**settings['shop'], 'public': True}
    return settings


@pytest.fixture
def folder(tmp_path):
    return tmp_path / 'lantern-tls'


def test_the_certificate_is_made_once_and_its_key_is_private(folder):
    cert, key = tls.ensure_certificate(str(folder / 'cert.pem'), str(folder / 'key.pem'), names=['shop.example.org'])
    first = open(cert).read()
    assert tls.ensure_certificate(cert, key, names=['other']) == (cert, key) and open(cert).read() == first
    if sys.platform != 'win32':
        assert stat.S_IMODE(os.stat(key).st_mode) == 0o600
    assert len(tls.spki(cert)) == 64 and tls.spki(cert) == spki_sha256(first)


def test_a_certificate_put_there_by_hand_is_used_as_it_is(folder):
    folder.mkdir()
    (folder / 'cert.pem').write_text('not made here')
    (folder / 'key.pem').write_text('nor this')
    tls.ensure_certificate(str(folder / 'cert.pem'), str(folder / 'key.pem'))
    assert (folder / 'cert.pem').read_text() == 'not made here'


def test_the_context_refuses_protocols_older_than_tls_1_2(folder):
    cert, key = tls.ensure_certificate(str(folder / 'cert.pem'), str(folder / 'key.pem'))
    assert tls.context(cert, key).minimum_version >= ssl.TLSVersion.TLSv1_2


@pytest.fixture
def listener(tmp_path, folder, monkeypatch):
    app = create_app(f"sqlite:///{tmp_path/'test.db'}")
    monkeypatch.setattr(service, 'get_settings', lambda: settings_with())
    service.init_app(app)
    service._state['key'] = None
    cert, key = tls.ensure_certificate(str(folder / 'cert.pem'), str(folder / 'key.pem'))
    monkeypatch.setattr(tls, 'CERT_FILE', cert)
    with app.app_context():
        server = tls.TlsServer(app, 0, cert, key, host='127.0.0.1')
        server.start()
        yield server, cert
        server.stop()


def fetch(port, path, cafile=None):
    context = ssl.create_default_context(cafile=cafile)
    context.check_hostname = False
    return urllib.request.urlopen(f'https://127.0.0.1:{port}{path}', context=context, timeout=10)


def test_lantern_is_served_over_tls_to_a_client_that_trusts_the_certificate(listener):
    server, cert = listener
    with fetch(server.port, '/.well-known/lantern', cafile=cert) as response:
        document = json.load(response)
    assert document['software']['name'] == 'Ownfoil' and document['features']['library'] == 1


def test_a_client_that_does_not_trust_the_certificate_is_refused(listener):
    server, _ = listener
    with pytest.raises(OSError):
        fetch(server.port, '/.well-known/lantern')


def test_plain_http_on_the_tls_port_is_not_answered(listener):
    server, _ = listener
    with socket.create_connection(('127.0.0.1', server.port), timeout=5) as raw:
        raw.sendall(b'GET /.well-known/lantern HTTP/1.1\r\nHost: x\r\n\r\n')
        try:
            answer = raw.recv(100)
        except OSError:
            answer = b''
    assert not answer.startswith(b'HTTP/1.1 200')


def test_the_pairing_key_is_the_listeners(listener):
    server, cert = listener
    assert service.build().spki == tls.spki(cert)


# What counts as secure.


class Fake:
    def __init__(self, remote, secure=False, proto=None):
        self.remote_addr, self.is_secure = remote, secure
        self.headers = {'X-Forwarded-Proto': proto} if proto else {}


@pytest.mark.parametrize('remote,secure,proto,proxy,served', [
    ('203.0.113.9', True, None, False, True),
    ('203.0.113.9', False, None, False, False),
    ('127.0.0.1', False, None, False, True),
    ('::1', False, None, False, True),
    ('203.0.113.9', False, 'https', False, False),   # a header nobody vouches for
    ('203.0.113.9', False, 'https', True, True),     # a reverse proxy the settings name
    ('203.0.113.9', False, 'http', True, False),
])
def test_what_counts_as_secure(monkeypatch, remote, secure, proto, proxy, served):
    monkeypatch.setattr(service, 'get_settings', lambda: settings_with(proxy=proxy))
    assert service.served_securely(Fake(remote, secure, proto)) is served


def test_behind_a_proxy_the_app_signs_for_https_and_pins_nothing(monkeypatch):
    monkeypatch.setattr(service, 'get_settings', lambda: settings_with(proxy=True, https_port=0))
    lantern = service.build()
    assert lantern.scheme == 'https' and lantern.spki is None


# The listener follows the settings.


def test_the_listener_starts_moves_and_stops_with_the_settings(tmp_path, folder, monkeypatch):
    app = create_app(f"sqlite:///{tmp_path/'test.db'}")
    monkeypatch.setattr(tls, 'TLS_DIR', str(folder))
    monkeypatch.setattr(tls, 'CERT_FILE', str(folder / 'cert.pem'))
    monkeypatch.setattr(tls, 'KEY_FILE', str(folder / 'key.pem'))
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        free = probe.getsockname()[1]
    state = {'lantern': {'enabled': False}}
    monkeypatch.setattr(tls, 'get_settings', lambda: {**settings_with(https_port=free), 'lantern': {**settings_with(https_port=free)['lantern'], **state['lantern']}})
    try:
        tls.reconcile(app)
        assert tls._server is None  # off
        state['lantern'] = {'enabled': True}
        tls.reconcile(app)
        assert tls._server is not None and tls._server.port == free
        state['lantern'] = {'enabled': False}
        tls.reconcile(app)
        assert tls._server is None
    finally:
        tls.stop()
