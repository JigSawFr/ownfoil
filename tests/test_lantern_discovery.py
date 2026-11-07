"""Finding the server and reaching it from a browser: DNS-SD, CORS, and a static copy of the library."""
import json
import os

import pytest

from db import Apps, Files, Libraries, Titles, db
from lantern_server import export, hashing, library as lib, service, tls
from settings import get_settings

from app import create_app

SECURE = {'base_url': 'https://shop.example.org', 'environ_overrides': {'REMOTE_ADDR': '203.0.113.9'}}


def settings(**lantern):
    value = dict(get_settings())
    value['lantern'] = {**value['lantern'], 'enabled': True, 'https_port': 0, **lantern}
    value['shop'] = {**value['shop'], 'public': True}
    return value


@pytest.fixture
def shop(tmp_path, monkeypatch):
    app = create_app(f"sqlite:///{tmp_path/'test.db'}")
    ctx = app.app_context()
    ctx.push()
    db.create_all()
    service.init_app(app)
    service._state['key'] = None
    yield app, monkeypatch, tmp_path
    service.stop_dnssd()
    db.session.remove()
    ctx.pop()


def test_a_page_of_an_allowed_origin_may_read_the_server(shop):
    app, monkeypatch, _ = shop
    monkeypatch.setattr(service, 'get_settings', lambda: settings(cors_origins=['https://app.example.org']))
    client = app.test_client()
    allowed = client.get('/.well-known/lantern', headers={'Origin': 'https://app.example.org'}, **SECURE)
    assert allowed.headers['Access-Control-Allow-Origin'] == 'https://app.example.org'
    assert 'Access-Control-Allow-Credentials' not in allowed.headers
    stranger = client.get('/.well-known/lantern', headers={'Origin': 'https://evil.example.org'}, **SECURE)
    assert 'Access-Control-Allow-Origin' not in stranger.headers
    preflight = client.options('/api/lantern/v1/apps', headers={
        'Origin': 'https://app.example.org', 'Access-Control-Request-Method': 'GET',
        'Access-Control-Request-Headers': 'authorization'}, **SECURE)
    assert preflight.status_code == 204


def test_no_origin_is_allowed_unless_one_is_named(shop):
    app, monkeypatch, _ = shop
    monkeypatch.setattr(service, 'get_settings', lambda: settings())
    answer = app.test_client().get('/.well-known/lantern', headers={'Origin': 'https://app.example.org'}, **SECURE)
    assert 'Access-Control-Allow-Origin' not in answer.headers


def test_the_tls_port_is_announced_on_the_network_and_the_announcement_stops(shop):
    app, monkeypatch, _ = shop
    monkeypatch.setattr(service, 'get_settings', lambda: settings(https_port=8466))
    told = []

    class Handle:
        def close(self):
            told.append('closed')

    import lantern.server.discovery as discovery
    monkeypatch.setattr(discovery, 'advertise_dnssd', lambda lantern, port, address, **kw: (told.append((lantern.name, port, address)), Handle())[1])
    import utils
    monkeypatch.setattr(utils, 'get_lan_ip', lambda: '192.0.2.10')
    monkeypatch.setattr(tls, '_server', type('Listening', (), {'port': 8466})())
    service.reconcile_dnssd(app)
    assert told == [('Ownfoil', 8466, '192.0.2.10')]
    service.reconcile_dnssd(app)  # nothing changed, nothing announced twice
    assert len(told) == 1
    monkeypatch.setattr(service, 'get_settings', lambda: settings(https_port=8466, dnssd=False))
    service.reconcile_dnssd(app)
    assert told[-1] == 'closed'


def test_nothing_is_announced_without_a_tls_port(shop):
    app, monkeypatch, _ = shop
    monkeypatch.setattr(service, 'get_settings', lambda: settings())
    monkeypatch.setattr(tls, '_server', None)
    service.reconcile_dnssd(app)
    assert service._zeroconf['handle'] is None


def test_the_library_is_exported_as_a_static_library(shop):
    app, monkeypatch, tmp_path = shop
    games = tmp_path / 'games'
    games.mkdir()
    library = Libraries(path=str(games))
    title = Titles(title_id='0100AB1200010000')
    db.session.add_all([library, title])
    db.session.commit()
    monkeypatch.setattr(hashing, 'enabled', lambda: True)
    monkeypatch.setattr(lib, '_name', lambda a, f: 'Lumen Drift')
    path = games / 'base.nsp'
    path.write_bytes(b'game bytes')
    f = Files(filepath=str(path), library_id=library.id, folder=str(games), filename='base.nsp', extension='nsp',
              size=10, mtime=os.path.getmtime(path))
    row = Apps(title_id=title.id, app_id='0100AB1200010000', app_version='0', app_type='BASE', owned=True)
    db.session.add_all([f, row])
    f.apps.append(row)
    db.session.commit()
    assert hashing.hash_file(f)
    db.session.commit()
    out = tmp_path / 'export'
    summary = export.run(str(out), app)
    document = json.loads((out / 'lantern.json').read_text())
    from lantern_server.identity import lantern_uid
    assert document['uid'] == lantern_uid() and document['static']
    assert (out / 'v1').is_dir() and summary
    assert any(p.read_bytes() == b'game bytes' for p in out.rglob('*') if p.is_file())
