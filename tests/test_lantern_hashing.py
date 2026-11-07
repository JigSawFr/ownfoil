"""File hashes for Lantern: when the pipeline asks for one, and when a recorded hash stops counting."""
import hashlib
import os
import types

import pytest

import tasks
from db import db, Files, Libraries
from lantern_server import hashing

from app import create_app

DATA = b"RAWDATA" * 1000


@pytest.fixture
def env(tmp_path, monkeypatch):
    app = create_app(f"sqlite:///{tmp_path/'test.db'}")
    lib_dir = tmp_path / "games"
    lib_dir.mkdir()
    ctx = app.app_context()
    ctx.push()
    db.create_all()
    library = Libraries(path=str(lib_dir))
    db.session.add(library)
    db.session.commit()
    monkeypatch.setattr(hashing, "enabled", lambda: True)

    def seed(name="Game.nsp", data=DATA):
        path = lib_dir / name
        path.write_bytes(data)
        f = Files(filepath=str(path), library_id=library.id, folder=str(lib_dir), filename=name,
                  extension=name.rsplit(".", 1)[-1], size=len(data), mtime=os.path.getmtime(path))
        db.session.add(f)
        db.session.commit()
        return f

    yield types.SimpleNamespace(app=app, lib_dir=lib_dir, seed=seed, monkeypatch=monkeypatch)
    ctx.pop()


def test_lantern_is_off_by_default(env):
    env.monkeypatch.undo()
    from settings import get_settings
    assert get_settings()['lantern']['enabled'] is False
    assert hashing.needs_hash(env.seed()) is False


def test_a_new_file_needs_a_hash_and_gets_it(env):
    f = env.seed()
    assert hashing.needs_hash(f) is True
    reported = []
    assert hashing.hash_file(f, progress=reported.append) is True
    assert f.sha256 == hashlib.sha256(DATA).hexdigest()
    assert (f.sha256_size, f.sha256_mtime) == (f.size, f.mtime)
    assert hashing.needs_hash(f) is False
    assert reported and reported[-1] == 100 and reported == sorted(reported)


def test_a_hash_stops_counting_when_the_row_changes(env):
    f = env.seed()
    hashing.hash_file(f)
    f.size += 1  # the file was replaced, the row refreshed
    assert hashing.needs_hash(f) is True
    f.size -= 1
    f.mtime += 5
    assert hashing.needs_hash(f) is True


def test_a_file_that_is_not_the_one_the_row_describes_is_not_hashed(env):
    f = env.seed()
    f.mtime -= 10  # the row is stale: the file was touched since
    assert hashing.hash_file(f) is False
    assert f.sha256 is None


def test_a_file_that_changes_while_it_is_read_is_not_recorded(env, monkeypatch):
    f = env.seed()
    real_stat = os.stat
    calls = []

    def stat(path, *args, **kwargs):
        info = real_stat(path, *args, **kwargs)
        calls.append(path)
        if len(calls) >= 2:  # the check after the read
            return types.SimpleNamespace(st_size=info.st_size, st_mtime=info.st_mtime + 1)
        return info

    monkeypatch.setattr(hashing.os, "stat", stat)
    assert hashing.hash_file(f) is False
    assert f.sha256 is None


def test_the_pipeline_hashes_last_and_only_when_lantern_is_on(env):
    f = env.seed()
    stage = next(s for s in tasks.STAGES if s.name == 'lantern_hash')
    assert tasks.STAGES[-1] is stage and stage.task == 'lantern_hash_file'
    assert stage.applies(f, {}) is True
    env.monkeypatch.setattr(hashing, "enabled", lambda: False)
    assert stage.applies(f, {}) is False


def test_the_task_records_the_hash_and_goes_on_with_the_pipeline(env):
    f = env.seed()
    queued = []
    env.monkeypatch.setattr(tasks, "enqueue_task", lambda name, data=None, **kw: queued.append((name, data)))
    tasks.lantern_hash_file_task(f.id)
    assert db.session.get(Files, f.id).sha256 == hashlib.sha256(DATA).hexdigest()
    assert queued == [('process_file', {'file_id': f.id})]


def test_the_task_leaves_a_changed_or_vanished_file_alone(env):
    f = env.seed()
    f.mtime -= 10
    db.session.commit()
    queued = []
    env.monkeypatch.setattr(tasks, "enqueue_task", lambda name, data=None, **kw: queued.append(name))
    tasks.lantern_hash_file_task(f.id)
    assert db.session.get(Files, f.id).sha256 is None and queued == []
    os.remove(f.filepath)
    tasks.lantern_hash_file_task(f.id)
    assert queued == []
    tasks.lantern_hash_file_task(99999)  # no such row
    assert queued == []
