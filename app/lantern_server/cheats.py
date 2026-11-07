"""Cheats (SPEC.md 10): Atmosphere's text, for one build of a game.

Two places hold them. Clients with the `cheats.write` scope write sets, kept in Ownfoil's database. A folder
`cheats/<title id>/<build id>.txt` in the config folder is read as it is, as Atmosphere's own layout has it:
drop a file there and it is listed. Those are read only; Lantern never writes to the folder.
"""
import hashlib
import os
import re
import secrets
import time
from datetime import datetime, timezone

from lantern.models import CheatSet
from lantern.server.files import FileRef

from constants import CONFIG_DIR
from db import LanternCheat, db

PLATFORM = 'switch'
FOLDER = os.path.join(CONFIG_DIR, 'cheats')
CONTENT_TYPE = 'text/plain; charset=utf-8'
SIZE_MAX = 1 << 20  # a folder's file larger than this is not a set of cheats
_TITLE = re.compile(r'^[0-9A-F]{16}$')
_BUILD = re.compile(r'^[0-9A-Fa-f]{1,64}$')
_TTL = 5.0
_seen = {'at': 0.0, 'sets': []}


def _now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _set(cheat_id, title_id, build_id, source, note, updated, data):
    return CheatSet(id=cheat_id, platform=PLATFORM, title_id=title_id, build_id=build_id, source=source, note=note,
                    updated=updated, size=len(data), sha256=hashlib.sha256(data).hexdigest())


def folder_sets(folder=None):
    """[(CheatSet, bytes)] for the files of the folder."""
    folder = folder or FOLDER
    found = []
    if not os.path.isdir(folder):
        return found
    for title_id in sorted(os.listdir(folder)):
        directory = os.path.join(folder, title_id)
        if not _TITLE.match(title_id) or not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            stem, extension = os.path.splitext(name)
            path = os.path.join(directory, name)
            if extension != '.txt' or not _BUILD.match(stem) or not os.path.isfile(path) or os.path.islink(path):
                continue
            try:
                if os.path.getsize(path) > SIZE_MAX:
                    continue
                with open(path, 'rb') as handle:
                    data = handle.read()
                modified = datetime.fromtimestamp(os.path.getmtime(path), timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            except OSError:
                continue
            cheat_id = 'cs_' + hashlib.sha256(f'{title_id}/{stem.upper()}'.encode()).hexdigest()[:12]
            found.append((_set(cheat_id, title_id, stem.upper(), 'ownfoil', None, modified, data), data))
    return found


def _folder_cached():
    if time.monotonic() - _seen['at'] > _TTL:
        _seen['sets'], _seen['at'] = folder_sets(), time.monotonic()
    return _seen['sets']


def all_sets():
    """[(CheatSet, bytes)]: the sets clients wrote, then the folder's."""
    written = [(_set(row.id, row.title_id, row.build_id, row.source, row.note, row.updated, row.text.encode()),
                row.text.encode()) for row in LanternCheat.query.order_by(LanternCheat.updated, LanternCheat.id).all()]
    written_ids = {c.id for c, _ in written}
    return written + [pair for pair in _folder_cached() if pair[0].id not in written_ids]


def cheat_file(cheat_id):
    for cheat, data in all_sets():
        if cheat.id == cheat_id:
            return FileRef.of_bytes(data, CONTENT_TYPE)
    return None


def write(*, platform, title_id, build_id, source, note, text, replacing=None):
    row = db.session.get(LanternCheat, replacing) if replacing else None
    if row is None:
        row = LanternCheat(id=replacing or 'cs_' + secrets.token_urlsafe(9))
    row.platform, row.title_id, row.build_id, row.source, row.note = platform, title_id, build_id, source, note
    row.updated, row.text = _now(), text
    db.session.add(row)
    db.session.commit()
    return _set(row.id, row.title_id, row.build_id, row.source, row.note, row.updated, text.encode())


def delete(cheat_id):
    row = db.session.get(LanternCheat, cheat_id)
    if row is None:
        return False
    db.session.delete(row)
    db.session.commit()
    return True
