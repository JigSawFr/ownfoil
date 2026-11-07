"""The change journal: the SDK's contract for a change log, then what is Ownfoil's own - changes found by
comparing the projection with a baseline, kept across restarts, told to the event streams."""
import datetime
import json
import os
import types

import pytest
from lantern.server.contracts import CHANGELOG_CONTRACT

import titledb
from db import Apps, Files, LanternChange, LanternMeta, Libraries, Titles, db
from lantern_server import hashing, journal, library as lib
from lantern_server.journal import OwnfoilChangeLog

from app import create_app

TITLE = '0100AB1200010000'


@pytest.fixture
def app(tmp_path):
    app = create_app(f"sqlite:///{tmp_path/'test.db'}")
    ctx = app.app_context()
    ctx.push()
    db.create_all()
    yield app
    db.session.remove()
    ctx.pop()


@pytest.mark.parametrize('check', CHANGELOG_CONTRACT, ids=lambda c: c.__name__)
def test_the_contract_every_change_log_passes(tmp_path, check):
    app = create_app(f"sqlite:///{tmp_path/'contract.db'}")
    with app.app_context():
        db.create_all()
        check(OwnfoilChangeLog(keep=3))


def test_positions_and_the_epoch_survive_a_restart(app):
    log = OwnfoilChangeLog(keep=10)
    epoch = log.epoch
    log.record('added', 'a1', 'switch')
    log.record('added', 'a2', 'switch')
    db.session.remove()
    again = OwnfoilChangeLog(keep=10)
    assert again.epoch == epoch and again.seq == 2 and again.floor == 0
    again.record('added', 'a3', 'switch')
    assert again.seq == 3 and [k for _, k in again.since(2, ('added',), None)] == ['a3']


def test_forgetting_keeps_the_positions_growing(app):
    log = OwnfoilChangeLog(keep=2)
    for n in range(5):
        log.record('added', f'a{n}', 'switch')
    assert (log.seq, log.floor) == (5, 3) and LanternChange.query.count() == 2


@pytest.fixture
def shop(app, tmp_path, monkeypatch):
    games = tmp_path / 'games'
    games.mkdir()
    library = Libraries(path=str(games))
    title = Titles(title_id=TITLE)
    db.session.add_all([library, title])
    db.session.commit()
    monkeypatch.setattr(hashing, 'enabled', lambda: True)
    monkeypatch.setattr(lib, '_name', lambda app_, file_: 'Lumen Drift')
    monkeypatch.setattr(titledb.store, 'get_title_record', lambda title_id: {'name': 'Lumen Drift'})

    def add(name, app_id, version, kind):
        path = games / name
        path.write_bytes(name.encode())
        f = Files(filepath=str(path), library_id=library.id, folder=str(games), filename=name,
                  extension='nsp', size=len(name), mtime=os.path.getmtime(path),
                  added_at=datetime.datetime(2026, 1, 1))
        row = Apps.query.filter_by(app_id=app_id, app_version=version).first() or Apps(
            title_id=title.id, app_id=app_id, app_version=version, app_type=kind, owned=True)
        db.session.add_all([f, row])
        f.apps.append(row)
        db.session.commit()
        assert hashing.hash_file(f)
        db.session.commit()
        return f

    return types.SimpleNamespace(add=add)


def test_what_changes_is_recorded_and_told(shop):
    library = lib.OwnfoilLibrary()
    heard = []
    library.subscribe(heard.append)
    library.refresh()
    assert library.journal().position() == 0 and heard == []
    first = shop.add('base.nsp', TITLE, '0', 'BASE')
    library.refresh()
    journal_ = library.journal()
    assert journal_.position() == 2  # the app, and the game's sheet
    apps, removed = journal_.app_changes(0, 'switch')
    assert [a.app_id for a in apps] == [TITLE] and removed == []
    titles, gone = journal_.title_changes(0, 'switch', ['en'])
    assert [t.title_id for t in titles] == [TITLE] and gone == []
    assert sorted((c.family, c.title_id) for c in heard) == [('apps', TITLE), ('titles', TITLE)]
    heard.clear()
    library.refresh()
    assert heard == [] and journal_.position() == 2  # nothing changed, nothing recorded
    db.session.delete(first)
    db.session.commit()
    library.refresh()
    apps, removed = journal_.app_changes(2, 'switch')
    assert apps == [] and len(removed) == 1
    titles, gone = journal_.title_changes(2, 'switch', ['en'])
    assert titles == [] and [(g.platform, g.title_id) for g in gone] == [('switch', TITLE)]
    assert ('apps', TITLE) in [(c.family, c.title_id) for c in heard]


def test_a_new_process_finds_the_changes_made_while_it_was_away(shop):
    first = lib.OwnfoilLibrary()
    first.refresh()
    shop.add('base.nsp', TITLE, '0', 'BASE')
    db.session.remove()
    second = lib.OwnfoilLibrary()  # a restart: no memory of the projection, only the baseline
    second.refresh()
    assert second.journal().position() == 2
    third = lib.OwnfoilLibrary()
    third.refresh()
    assert third.journal().position() == 2  # and not recorded twice


def test_an_edited_sheet_is_a_title_change_only(shop, monkeypatch):
    library = lib.OwnfoilLibrary()
    shop.add('base.nsp', TITLE, '0', 'BASE')
    library.refresh()
    before = library.journal().position()
    monkeypatch.setattr(titledb.store, 'get_title_record', lambda title_id: {'name': 'Lumen Drift Deluxe'})
    monkeypatch.setattr(lib, '_name', lambda app_, file_: 'Lumen Drift')
    library._print = None  # as when titles.db is rebuilt
    library.refresh()
    assert library.journal().position() == before + 1
    assert library.journal().app_changes(before, 'switch') == ([], [])


def test_a_baseline_of_a_newer_schema_is_left_alone(shop):
    library = lib.OwnfoilLibrary()
    shop.add('base.nsp', TITLE, '0', 'BASE')
    db.session.add(LanternMeta(key='baseline', value=json.dumps({'schema': 99, 'apps': {'x': 'y'}})))
    db.session.commit()
    library.refresh()
    assert library.journal().position() == 0
    assert json.loads(db.session.get(LanternMeta, 'baseline').value)['schema'] == 99


def test_a_baseline_with_keys_it_does_not_know_is_read(shop):
    db.session.add(LanternMeta(key='baseline', value=json.dumps(
        {'schema': 1, 'apps': {}, 'titles': {}, 'something': 'new'})))
    db.session.commit()
    assert journal.read_baseline() == ({}, {})


def test_a_baseline_rewritten_keeps_the_keys_it_does_not_know(shop):
    db.session.add(LanternMeta(key='baseline', value=json.dumps({'schema': 1, 'apps': {}, 'titles': {}, 'extra': 7})))
    db.session.commit()
    shop.add('base.nsp', TITLE, '0', 'BASE')
    lib.OwnfoilLibrary().refresh()
    document = json.loads(db.session.get(LanternMeta, 'baseline').value)
    assert document['extra'] == 7 and len(document['apps']) == 1
