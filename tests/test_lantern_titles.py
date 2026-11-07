"""What Ownfoil says of each game to Lantern: the sheet from titledb, and the artwork it keeps locally."""
import base64
import datetime
import os
import types

import pytest

import media as media_lib
import titledb
from db import Apps, Files, Libraries, Media, Titles, db
from lantern_server import hashing, library as lib, titles as sheets
from lantern_server import service
from settings import get_settings

from app import create_app

TITLE = '0100AB1200010000'
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==')
RECORD = {'name': 'Lumen Drift', 'intro': 'A drifting game.', 'description': 'Drift through the dark.',
          'developer': 'Glass Orchard', 'publisher': 'Quiet Press', 'release_date': '20260315',
          'category': ['Racing', 'Indie'], 'number_of_players': 4}


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
    monkeypatch.setattr(media_lib, 'MEDIA_DIR', str(tmp_path / 'media'))
    monkeypatch.setattr(hashing, 'enabled', lambda: True)
    monkeypatch.setattr(lib, '_name', lambda app_, file_: 'Lumen Drift')
    records = {TITLE: dict(RECORD)}
    monkeypatch.setattr(titledb.store, 'get_title_record', lambda title_id: records.get(title_id))

    def game():
        path = games / 'base.nsp'
        path.write_bytes(b'game')
        f = Files(filepath=str(path), library_id=library.id, folder=str(games), filename='base.nsp',
                  extension='nsp', size=4, mtime=os.path.getmtime(path), added_at=datetime.datetime(2026, 1, 1))
        row = Apps(title_id=title.id, app_id=TITLE, app_version='0', app_type='BASE', owned=True)
        db.session.add_all([f, row])
        f.apps.append(row)
        db.session.commit()
        assert hashing.hash_file(f)
        db.session.commit()

    def picture(kind, position=0, data=PNG, name='a1b2c3.jpg'):
        os.makedirs(media_lib.media_dir(kind, media_lib.ORIGINAL), exist_ok=True)
        with open(media_lib.media_path(kind, media_lib.ORIGINAL, name), 'wb') as out:
            out.write(data)
        db.session.add(Media(title_id=TITLE, kind=kind, position=position, source='titledb', filename=name,
                             width=1, height=1))
        db.session.commit()

    yield types.SimpleNamespace(app=app, game=game, picture=picture, records=records, monkeypatch=monkeypatch)
    ctx.pop()


def one(env):
    library = lib.OwnfoilLibrary()
    [title] = library.titles(['en'])
    return library, title


def test_a_game_has_the_sheet_titledb_gives_it(env):
    env.game()
    _, title = one(env)
    assert (title.platform, title.title_id, title.name) == ('switch', TITLE, 'Lumen Drift')
    assert title.summary == 'A drifting game.' and title.description == 'Drift through the dark.'
    assert (title.developer, title.publisher, title.release) == ('Glass Orchard', 'Quiet Press', '2026-03-15')
    assert title.genres == ['racing'] and title.tags == ['Indie'] and title.players.max == 4


def test_a_game_titledb_does_not_know_keeps_the_name_its_files_give(env):
    env.game()
    env.records.clear()
    _, title = one(env)
    assert title.name == 'Lumen Drift' and title.summary is None and title.release is None


def test_a_sheet_lantern_refuses_falls_back_to_the_name(env):
    env.game()
    env.records[TITLE]['developer'] = 'x' * 5000  # more than a sheet may hold
    _, title = one(env)
    assert title.name == 'Lumen Drift' and title.genres is None and title.developer is None


def test_no_title_without_a_listed_file(env):
    assert lib.OwnfoilLibrary().titles(['en']) == []


def test_local_artwork_is_served_by_ownfoil_under_its_hash(env):
    env.game()
    env.picture(media_lib.ICON)
    env.picture(media_lib.SCREENSHOT, 0, name='s0.jpg')
    env.picture(media_lib.SCREENSHOT, 1, name='s1.jpg', data=PNG + b'x')
    library, title = one(env)
    assert title.icon.url.startswith('/api/lantern/v1/media/') and title.icon.url.endswith('.png')
    assert title.icon.sha256 == title.icon.url.rsplit('/', 1)[1].split('.')[0]
    assert len(title.screenshots) == 2 and title.banner is None
    ref = library.media(title.icon.url.rsplit('/', 1)[1])
    assert ref.sha256 == title.icon.sha256 and ref.content_type == 'image/png'
    assert open(ref.path, 'rb').read() == PNG


def test_an_image_that_is_not_one_is_not_listed_and_an_unknown_name_is_not_served(env):
    env.game()
    env.picture(media_lib.ICON, data=b'<html>not an image</html>')
    library, title = one(env)
    assert title.icon is None
    assert library.media('0' * 64 + '.png') is None


def test_artwork_missing_from_the_disk_is_left_out(env):
    env.game()
    db.session.add(Media(title_id=TITLE, kind=media_lib.ICON, position=0, source='titledb', filename='gone.jpg'))
    db.session.commit()
    assert one(env)[1].icon is None


def test_a_new_picture_shows_without_a_restart(env):
    env.game()
    library = lib.OwnfoilLibrary()
    assert library.titles(['en'])[0].icon is None
    env.picture(media_lib.ICON)
    assert library.titles(['en'])[0].icon is not None


def test_release_dates(env):
    assert sheets.date_of('20260315') == '2026-03-15' and sheets.date_of('2026-03-15') == '2026-03-15'
    assert sheets.date_of('soon') is None and sheets.date_of(None) is None


def test_the_titles_routes_answer(env):
    env.game()
    env.picture(media_lib.ICON)
    service.init_app(env.app)
    service._state['key'] = None
    settings = dict(get_settings())
    settings['lantern'] = {**settings['lantern'], 'enabled': True, 'https_port': 0}
    settings['shop'] = {**settings['shop'], 'public': True}
    env.monkeypatch.setattr(service, 'get_settings', lambda: settings)
    client = env.app.test_client()
    secure = {'base_url': 'https://shop.example.org', 'environ_overrides': {'REMOTE_ADDR': '203.0.113.9'}}
    document = client.get('/.well-known/lantern', **secure).get_json()
    assert document['features']['titles'] == 1
    sheet = client.get(f'/api/lantern/v1/titles/{TITLE}?platform=switch', **secure)
    assert sheet.status_code == 200, sheet.data
    url = sheet.get_json()['icon']['url']
    image = client.get(url, **secure)
    assert image.status_code == 200 and image.data == PNG and image.headers['Content-Type'] == 'image/png'


def test_categories_become_genres_and_tags():
    assert sheets.genres_and_tags(['Action', 'Role-Playing', 'Indie', 'Multiplayer']) == (
        ['rpg', 'action'], ['Indie', 'Multiplayer'])
    assert sheets.genres_and_tags(['Indie']) == (None, ['Indie'])
    assert sheets.genres_and_tags(None) == (None, None)
