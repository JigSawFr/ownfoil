"""Differential updates: a file is cut into chunks when it is hashed, in the same read, and its index served."""
import hashlib
import os
import types

import pytest
from lantern import chunks

from db import Apps, Files, LanternChunks, Libraries, Titles, db
from lantern_server import hashing, library as lib
from settings import get_settings

from app import create_app

TITLE = '0100AB1200010000'
UPDATE = '0100AB1200010800'


def blob(label, size):
    out = bytearray()
    n = 0
    while len(out) < size:
        out += hashlib.sha256(f'{label}:{n}'.encode()).digest()
        n += 1
    return bytes(out[:size])


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
    settings = dict(get_settings())
    settings['lantern'] = {**settings['lantern'], 'enabled': True,
                           'deltas': {'enabled': True, 'types': ['update'], 'min': 1000, 'max': 10_000_000}}
    for module in (hashing, lib):
        monkeypatch.setattr(module, 'get_settings', lambda: settings)
    monkeypatch.setattr(hashing, 'enabled', lambda: True)
    monkeypatch.setattr(lib, '_name', lambda app_, file_: 'Lumen Drift')

    def seed(name, data, app_id, version, kind):
        path = games / name
        path.write_bytes(data)
        f = Files(filepath=str(path), library_id=library.id, folder=str(games), filename=name, extension='nsp',
                  size=len(data), mtime=os.path.getmtime(path))
        row = Apps(title_id=title.id, app_id=app_id, app_version=version, app_type=kind, owned=True)
        db.session.add_all([f, row])
        f.apps.append(row)
        db.session.commit()
        return f

    yield types.SimpleNamespace(seed=seed, settings=settings)
    ctx.pop()


def test_an_update_is_hashed_and_cut_in_one_read(env):
    data = blob('update', 300_000)
    f = env.seed('up.nsp', data, UPDATE, '65536', 'UPDATE')
    assert hashing.needs_hash(f) and hashing.hash_file(f)
    db.session.commit()
    assert f.sha256 == hashlib.sha256(data).hexdigest()
    row = db.session.get(LanternChunks, f.sha256)
    assert row.raw == chunks.build_index(data)[1] and row.ref_sha256 == hashlib.sha256(row.raw).hexdigest()
    assert not hashing.needs_hash(f)


def test_a_base_game_is_not_cut_when_only_updates_are_asked_for(env):
    f = env.seed('base.nsp', blob('base', 300_000), TITLE, '0', 'BASE')
    assert hashing.hash_file(f)
    db.session.commit()
    assert LanternChunks.query.count() == 0 and not hashing.wants_index(f)


def test_sizes_outside_the_window_are_not_cut(env):
    small = env.seed('s.nsp', b'x' * 500, UPDATE, '65536', 'UPDATE')
    assert not hashing.wants_index(small)
    env.settings['lantern']['deltas']['max'] = 100_000
    assert not hashing.wants_index(env.seed('big.nsp', blob('big', 300_000), '0100AB1200010801', '65536', 'UPDATE'))


def test_a_file_hashed_before_deltas_were_on_is_cut_later(env):
    env.settings['lantern']['deltas']['enabled'] = False
    f = env.seed('up.nsp', blob('update', 300_000), UPDATE, '65536', 'UPDATE')
    assert hashing.hash_file(f)
    db.session.commit()
    assert not hashing.needs_hash(f)
    env.settings['lantern']['deltas']['enabled'] = True
    assert hashing.needs_hash(f)  # hashed, but without its index
    assert hashing.hash_file(f)
    db.session.commit()
    assert not hashing.needs_hash(f) and LanternChunks.query.count() == 1


def test_the_app_carries_its_chunks_and_the_library_serves_the_index(env):
    data = blob('update', 300_000)
    f = env.seed('up.nsp', data, UPDATE, '65536', 'UPDATE')
    hashing.hash_file(f)
    db.session.commit()
    library = lib.OwnfoilLibrary()
    assert 'deltas' in library.families
    [app] = library.apps()
    assert app.chunks.url == f'/api/lantern/v1/chunks/sha256/{f.sha256}'
    raw = library.chunk_index(f.sha256)
    assert raw == chunks.build_index(data)[1] and app.chunks.size == len(raw)
    assert app.chunks.sha256 == hashlib.sha256(raw).hexdigest()
    assert library.chunk_index('0' * 64) is None
    env.settings['lantern']['deltas']['enabled'] = False
    assert 'deltas' not in library.families


def test_indexes_of_files_that_are_gone_are_forgotten(env):
    f = env.seed('up.nsp', blob('update', 300_000), UPDATE, '65536', 'UPDATE')
    hashing.hash_file(f)
    db.session.commit()
    assert hashing.prune_chunks() == 0
    db.session.delete(f)
    db.session.commit()
    assert hashing.prune_chunks() == 1 and LanternChunks.query.count() == 0
