"""What Ownfoil lists to Lantern, and when: the library projected from the database, and its routes."""
import datetime
import os
import types

import pytest

from db import Apps, Files, Libraries, Titles, app_files, db
from lantern_server import hashing, library as lib
from lantern_server import service

from app import create_app

TITLE = '0100AB1200010000'
UPDATE = '0100AB1200010800'
DLC = '0100AB1200011001'
DATA = b'game bytes' * 100


@pytest.fixture
def env(tmp_path, monkeypatch):
    app = create_app(f"sqlite:///{tmp_path/'test.db'}")
    games = tmp_path / 'games'
    games.mkdir()
    ctx = app.app_context()
    ctx.push()
    db.create_all()
    library = Libraries(path=str(games))
    title = Titles(title_id=TITLE)
    db.session.add_all([library, title])
    db.session.commit()
    monkeypatch.setattr(lib, '_name', lambda app_, file_: 'Lumen Drift')

    def seed(name, apps, data=DATA, hashed=True, extension='nsp'):
        path = games / name
        path.write_bytes(data)
        f = Files(filepath=str(path), library_id=library.id, folder=str(games), filename=name,
                  extension=extension, size=len(data), mtime=os.path.getmtime(path),
                  added_at=datetime.datetime(2026, 11, 7, 12, 0, 0))
        db.session.add(f)
        for app_id, version, kind, display in apps:
            row = Apps.query.filter_by(app_id=app_id, app_version=version).first()
            if row is None:
                row = Apps(title_id=title.id, app_id=app_id, app_version=version, app_type=kind,
                           owned=True, display_version=display)
                db.session.add(row)
            f.apps.append(row)
        db.session.commit()
        if hashed:
            monkeypatch.setattr(hashing, 'enabled', lambda: True)
            assert hashing.hash_file(f)
            db.session.commit()
        return f

    yield types.SimpleNamespace(app=app, seed=seed, tmp=tmp_path, monkeypatch=monkeypatch)
    ctx.pop()


def listed(env):
    return lib.OwnfoilLibrary().apps()


def test_a_hashed_file_is_listed_as_an_app(env):
    f = env.seed('Lumen Drift [0100AB1200010000][v0].nsp', [(TITLE, '0', 'BASE', '1.0.0')])
    [app] = listed(env)
    assert (app.platform, app.title_id, app.app_id, app.type) == ('switch', TITLE, TITLE, 'base')
    assert (app.version_code, app.version_name, app.format, app.size) == (0, '1.0.0', 'nsp', len(DATA))
    assert app.sha256 == f.sha256 and app.id == 'a_' + f.sha256[:12] and app.name == 'Lumen Drift'
    assert app.added == '2026-11-07T12:00:00Z'
    assert app.file == f'/api/lantern/v1/files/f_{f.download_token}'


def test_a_file_without_a_current_hash_is_not_listed(env):
    env.seed('a.nsp', [(TITLE, '0', 'BASE', None)], hashed=False)
    assert listed(env) == []
    f = env.seed('b.nsp', [(TITLE, '1', 'BASE', None)], data=b'other')
    f.size += 1  # replaced since it was hashed
    db.session.commit()
    assert [a.name for a in listed(env)] == []


def test_updates_and_dlc_are_their_own_apps_with_their_versions(env):
    env.seed('base.nsp', [(TITLE, '0', 'BASE', '1.0.0')], data=b'base')
    env.seed('update.nsz', [(UPDATE, '65536', 'UPDATE', '1.1.0')], data=b'update', extension='nsz')
    env.seed('dlc.nsp', [(DLC, '0', 'DLC', None)], data=b'dlc')
    apps = {a.app_id: a for a in listed(env)}
    assert apps[UPDATE].type == 'update' and apps[UPDATE].version_code == 65536 and apps[UPDATE].format == 'nsz'
    assert apps[DLC].type == 'dlc'
    assert apps[UPDATE].title_id == TITLE


def test_a_package_with_several_contents_is_its_main_app_and_includes_the_rest(env):
    env.seed('all.nsp', [(DLC, '0', 'DLC', None), (UPDATE, '65536', 'UPDATE', '1.1.0'), (TITLE, '0', 'BASE', '1.0.0')])
    [app] = listed(env)
    assert app.app_id == TITLE and app.type == 'base'
    assert [(i.app_id, i.version_code) for i in app.includes] == [(UPDATE, 65536), (DLC, 0)]


def test_the_same_bytes_under_two_names_are_one_app(env):
    env.seed('one.nsp', [(TITLE, '0', 'BASE', None)])
    env.seed('copy.nsp', [(TITLE, '0', 'BASE', None)])
    assert len(listed(env)) == 1


def test_apps_that_are_missing_or_unknown_are_not_listed(env):
    env.seed('odd.nsp', [('not-an-id', '0', 'BASE', None)])
    env.seed('other.nsp', [(TITLE, '0', 'MYSTERY', None)], data=b'x')
    assert listed(env) == []


def test_the_projection_follows_the_database(env):
    library = lib.OwnfoilLibrary()
    assert library.apps() == []
    env.seed('base.nsp', [(TITLE, '0', 'BASE', None)])
    assert len(library.apps()) == 1
    Files.query.delete()
    db.session.commit()
    assert library.apps() == []


def test_a_file_is_served_only_while_it_is_the_one_that_was_hashed(env):
    f = env.seed('base.nsp', [(TITLE, '0', 'BASE', None)])
    library = lib.OwnfoilLibrary()
    [app] = library.apps()
    ref = library.file(app.file.rsplit('/', 1)[1])
    assert ref.path == f.filepath and ref.sha256 == f.sha256 and ref.size == len(DATA)
    assert library.file('f_nope') is None
    with open(f.filepath, 'ab') as out:
        out.write(b'more')
    assert library.file(app.file.rsplit('/', 1)[1]) is None


def test_the_library_offers_only_what_it_has(env):
    library = lib.OwnfoilLibrary()
    assert library.families == {'library'}
    assert list(library.titles(['en'])) == [] and list(library.homebrew(['en'])) == []
    assert library.journal() is None and library.cheats() == []


# The routes.


@pytest.fixture
def served(env):
    service.init_app(env.app)
    service._state['key'] = None
    env.monkeypatch.setattr(service, 'get_settings', lambda: _on(True))
    return env.app.test_client()


def _on(enabled):
    from settings import get_settings
    settings = dict(get_settings())
    settings['lantern'] = {**settings['lantern'], 'enabled': enabled, 'https_port': 0}
    settings['shop'] = {**settings['shop'], 'public': True}
    return settings


def test_the_handshake_is_served_over_tls(env, served):
    env.seed('base.nsp', [(TITLE, '0', 'BASE', '1.0.0')])
    response = served.get('/.well-known/lantern', base_url='https://shop.example.org', environ_overrides={'REMOTE_ADDR': '203.0.113.9'})
    assert response.status_code == 200
    document = response.get_json()
    assert document['software']['name'] == 'Ownfoil' and document['features']['library'] == 1


def test_the_library_lists_its_apps_over_tls(env, served):
    env.seed('base.nsp', [(TITLE, '0', 'BASE', '1.0.0')])
    response = served.get('/api/lantern/v1/apps?platform=switch', base_url='https://shop.example.org',
                          headers={'Lantern-Platform': 'switch'}, environ_overrides={'REMOTE_ADDR': '203.0.113.9'})
    assert response.status_code == 200, response.data
    assert [a['app_id'] for a in response.get_json()['items']] == [TITLE]


def test_plain_http_from_another_machine_is_not_served(env, served):
    response = served.get('/.well-known/lantern', environ_overrides={'REMOTE_ADDR': '203.0.113.9'})
    assert response.status_code == 404


def test_loopback_may_use_plain_http(env, served):
    response = served.get('/.well-known/lantern', environ_overrides={'REMOTE_ADDR': '127.0.0.1'})
    assert response.status_code == 200


def test_nothing_is_served_while_lantern_is_off(env, served):
    env.monkeypatch.setattr(service, 'get_settings', lambda: _on(False))
    response = served.get('/.well-known/lantern', base_url='https://shop.example.org')
    assert response.status_code == 404
