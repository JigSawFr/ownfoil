"""Cheats: sets written by clients, kept in the database, and Atmosphere's own folder, read as it is."""
import os

import pytest

from db import LanternCheat, User, db
from lantern_server import accounts as acc, cheats, library as lib, service
from settings import get_settings

from app import create_app

SECURE = {'base_url': 'https://shop.example.org', 'environ_overrides': {'REMOTE_ADDR': '203.0.113.9'}}
TITLE = '0100AB1200010000'
BUILD = 'AABBCCDD11223344'
TEXT = '[Infinite fuel]\n04000000 00123456 00000001\n'


@pytest.fixture
def shop(tmp_path, monkeypatch):
    app = create_app(f"sqlite:///{tmp_path/'test.db'}")
    ctx = app.app_context()
    ctx.push()
    db.create_all()
    monkeypatch.setattr(cheats, 'FOLDER', str(tmp_path / 'cheats'))
    monkeypatch.setitem(cheats._seen, 'at', 0.0)
    yield app
    db.session.remove()
    ctx.pop()


def folder_file(tmp_path, title=TITLE, build=BUILD, text=TEXT):
    directory = tmp_path / 'cheats' / title
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f'{build}.txt').write_bytes(text.encode())


def test_the_folder_is_read_as_atmospheres_layout_has_it(shop, tmp_path):
    folder_file(tmp_path)
    folder_file(tmp_path, title='not-a-title')
    folder_file(tmp_path, build='toggles')  # not a build id
    [found] = cheats.folder_sets(str(tmp_path / 'cheats'))
    cheat, data = found
    assert (cheat.title_id, cheat.build_id, cheat.source, cheat.size) == (TITLE, BUILD, 'ownfoil', len(TEXT))
    assert data.decode() == TEXT and cheat.id.startswith('cs_')


def test_a_set_written_by_a_client_is_kept_listed_replaced_and_deleted(shop):
    library = lib.OwnfoilLibrary()
    heard = []
    library.subscribe(heard.append)
    made = library.write_cheat(platform='switch', title_id=TITLE, build_id=BUILD, source='Sam', note='fuel',
                               text=TEXT)
    assert [c.id for c in library.cheats()] == [made.id] and library.cheat_file(made.id).data.decode() == TEXT
    again = library.write_cheat(platform='switch', title_id=TITLE, build_id=BUILD, source='Sam', note='fuel',
                                text=TEXT + 'x', replacing=made.id)
    assert again.id == made.id and again.size == len(TEXT) + 1 and LanternCheat.query.count() == 1
    assert library.delete_cheat(made.id) is True and library.delete_cheat(made.id) is False
    assert library.cheats() == [] and [c.family for c in heard] == ['cheats'] * 3


def test_the_files_of_a_folder_are_listed_with_the_sets_written(shop, tmp_path):
    folder_file(tmp_path)
    library = lib.OwnfoilLibrary()
    library.write_cheat(platform='switch', title_id=TITLE, build_id='FFFF', source='Sam', note=None, text=TEXT)
    assert sorted(c.build_id for c in library.cheats()) == [BUILD, 'FFFF']
    [from_folder] = [c for c in library.cheats() if c.build_id == BUILD]
    assert library.cheat_file(from_folder.id).sha256 == from_folder.sha256
    assert library.cheat_file('cs_nope') is None


def test_the_library_offers_cheats(shop):
    assert 'cheats' in lib.OwnfoilLibrary().families


def _settings():
    settings = dict(get_settings())
    settings['lantern'] = {**settings['lantern'], 'enabled': True, 'https_port': 0}
    return settings


def test_a_client_with_the_scope_writes_a_set_through_the_routes(shop, monkeypatch):
    monkeypatch.setattr(service, 'get_settings', _settings)
    service.init_app(shop)
    service._state['key'] = None
    admin = User(user='root', password='x', admin_access=True, shop_access=True, backup_access=True)
    reader = User(user='ada', password='x', admin_access=False, shop_access=True, backup_access=False)
    db.session.add_all([admin, reader])
    db.session.commit()

    def auth(user):
        name = acc.ensure_account(user).name
        return {'Authorization': 'Bearer ' + acc.OwnfoilAccounts().issue(name, 'console', acc.allowed_scopes(user))[0],
                'Lantern-Platform': 'switch'}

    client = shop.test_client()
    body = {'schema': 1, 'platform': 'switch', 'title_id': TITLE, 'build_id': BUILD, 'source': 'root', 'text': TEXT}
    refused = client.post('/api/lantern/v1/cheats', json=body, headers=auth(reader), **SECURE)
    assert refused.status_code == 403
    made = client.post('/api/lantern/v1/cheats', json=body, headers=auth(admin), **SECURE)
    assert made.status_code == 201, made.data
    listed = client.get(f'/api/lantern/v1/cheats?title_id={TITLE}', headers=auth(reader), **SECURE).get_json()
    assert [c['build_id'] for c in listed['cheats']] == [BUILD]
