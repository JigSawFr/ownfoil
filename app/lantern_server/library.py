"""What Ownfoil lists to Lantern: its library, read from its database.

Ownfoil's workers are separate processes and the database is where their work surfaces, so the library
is not kept in memory by this process: it is projected from the `files` and `apps` tables, and the
projection is kept until a cheap fingerprint of those tables changes.
"""
import logging
import os
import re
import threading

from lantern import schema
from lantern.models import App, AppIncludes
from lantern.server.events import LibraryChange
from lantern.server.files import FileRef
from sqlalchemy import func

from constants import APP_TYPE_BASE, APP_TYPE_DLC, APP_TYPE_UPD, TITLES_DB_FILE
import db as db_models
from db import Apps, Files, Media, Titles, app_files, db
from lantern_server import journal

logger = logging.getLogger('main')

PLATFORM = 'switch'
FORMATS = frozenset({'nsp', 'nsz', 'xci', 'xcz'})
INCLUDES_MAX = 16  # SPEC.md 6.1

_TYPES = {APP_TYPE_BASE: 'base', APP_TYPE_UPD: 'update', APP_TYPE_DLC: 'dlc'}
_RANK = {'base': 0, 'update': 1, 'dlc': 2}
_ID = re.compile(r'^[0-9A-F]{16}$')


def fingerprint():
    """A value that changes whenever what the library lists could have: files, hashes, apps, links."""
    session = db.session
    hashed = (session.query(func.count(Files.id), func.coalesce(func.sum(Files.id), 0),
                            func.coalesce(func.max(Files.sha256_mtime), 0))
              .filter(Files.sha256.isnot(None), Files.sha256_size == Files.size,
                      Files.sha256_mtime == Files.mtime).one())
    apps = session.query(func.count(Apps.id), func.coalesce(func.sum(Apps.id), 0),
                         func.coalesce(func.count(Apps.display_version), 0)).one()
    links = session.query(func.count()).select_from(app_files).scalar()
    artwork = session.query(func.count(Media.id), func.coalesce(func.max(Media.id), 0)).one()
    try:
        titledb_stamp = os.stat(TITLES_DB_FILE).st_mtime_ns
    except OSError:
        titledb_stamp = 0
    return tuple(hashed), tuple(apps), links, tuple(artwork), titledb_stamp


def _iso(moment):
    return moment.strftime('%Y-%m-%dT%H:%M:%SZ') if moment else None


def _version(text):
    return int(text) if text and str(text).isdigit() else 0


def project(base):
    """(apps, files) for the files that have a current hash: the App documents, and by file id what to serve."""
    rows = (db.session.query(Files, Apps, Titles.title_id)
            .join(app_files, app_files.c.file_id == Files.id)
            .join(Apps, Apps.id == app_files.c.app_id)
            .join(Titles, Titles.id == Apps.title_id)
            .filter(Files.sha256.isnot(None), Files.sha256_size == Files.size,
                    Files.sha256_mtime == Files.mtime, Files.extension.in_(FORMATS))
            .order_by(Files.id, Apps.id).all())
    per_file = {}
    for file, app, title_id in rows:
        kind = _TYPES.get(app.app_type)
        if kind is None or not _ID.match(title_id or '') or not _ID.match(app.app_id or ''):
            continue
        per_file.setdefault(file.id, (file, []))[1].append((kind, app, title_id))
    apps, files, seen_hash, seen_package = [], {}, set(), set()
    for file, contents in per_file.values():
        contents.sort(key=lambda c: (_RANK[c[0]], c[1].app_id, _version(c[1].app_version)))
        kind, main, title_id = contents[0]
        if file.sha256 in seen_hash:
            continue  # the same bytes under another name
        package = (main.app_id, _version(main.app_version), file.extension)
        if package in seen_package:
            continue
        includes = [AppIncludes(app_id=a.app_id, version_code=_version(a.app_version))
                    for _, a, _ in contents[1:INCLUDES_MAX + 1]]
        document = App(
            id='a_' + file.sha256[:12], platform=PLATFORM, title_id=title_id, app_id=main.app_id,
            type=kind, version_code=_version(main.app_version), version_name=main.display_version or None,
            name=_name(main, file), size=file.size, format=file.extension,
            includes=includes or None, sha256=file.sha256, added=_iso(file.added_at),
            file=f'{base}/v1/files/f_{file.download_token}')
        problems = schema.definition('app', document.to_json())
        if problems:
            logger.warning(f'Lantern: not listing {file.filename} ({"; ".join(problems[:2])})')
            continue
        seen_hash.add(file.sha256)
        seen_package.add(package)
        apps.append(document)
        files['f_' + file.download_token] = (file.filepath, file.size, file.mtime, file.sha256)
    return apps, files


def _name(app, file):
    """The game's name as titledb gives it, else the file's own name."""
    import titledb
    try:
        record = titledb.store.get_title_record(app.app_id)
    except Exception:  # titledb absent or being rebuilt
        record = None
    name = (record or {}).get('name')
    return name or os.path.splitext(file.filename)[0]


class OwnfoilLibrary:
    """Lantern's `Library` over Ownfoil's database. Call it inside the Flask app context."""

    def __init__(self, base='/api/lantern'):
        self.base = base.rstrip('/')
        self.families = frozenset({'library', 'titles', 'changes', 'cheats'})
        self._log = journal.OwnfoilChangeLog()
        self._journal = journal.OwnfoilJournal(self._log, self)
        self._listeners = []
        self._lock = threading.Lock()
        self._print = None
        self._apps = []
        self._files = {}
        self._titles = []
        self._media = {}

    def _current(self):
        """The projection, rebuilt only when the database moved since the last one."""
        with self._lock:
            print_ = fingerprint()
            if print_ != self._print:
                from lantern_server import titles as sheets
                self._apps, self._files = project(self.base)
                self._titles, self._media = sheets.project(self._apps, self.base)
                self._print = print_
                changes = journal.compare(self._log, self._apps, self._titles, PLATFORM)
                for family, title_id in changes:
                    for listener in list(self._listeners):
                        listener(LibraryChange(family, PLATFORM, title_id))
            return self._apps, self._files

    def apps(self):
        return list(self._current()[0])

    def titles(self, languages):
        self._current()
        return list(self._titles)

    def media(self, name):
        """A picture of a title, served under its SHA-256 (SPEC.md 7.1)."""
        self._current()
        entry = self._media.get(name)
        if entry is None:
            return None
        path, size, content_type, sha256 = entry
        try:
            info = os.stat(path)
        except OSError:
            return None
        return FileRef(path, size, sha256, content_type) if info.st_size == size else None

    def homebrew(self, languages):
        return []

    def cheats(self):
        from lantern_server import cheats
        try:
            return [cheat for cheat, _ in cheats.all_sets()]
        finally:
            db.session.rollback()

    def file(self, file_id):
        """The file to serve, if it is still the one that was hashed."""
        entry = self._current()[1].get(file_id)
        if entry is None:
            return None
        path, size, mtime, sha256 = entry
        try:
            info = os.stat(path)
        except OSError:
            return None
        if (info.st_size, info.st_mtime) != (size, mtime):
            return None
        return FileRef(path, size, sha256)

    def cheat_file(self, cheat_id):
        from lantern_server import cheats
        try:
            return cheats.cheat_file(cheat_id)
        finally:
            db.session.rollback()

    def write_cheat(self, *, platform, title_id, build_id, source, note, text, replacing=None):
        from lantern_server import cheats
        written = cheats.write(platform=platform, title_id=title_id, build_id=build_id, source=source, note=note,
                               text=text, replacing=replacing)
        self._tell(LibraryChange('cheats', platform, title_id))
        return written

    def delete_cheat(self, cheat_id):
        from lantern_server import cheats
        row = db.session.get(db_models.LanternCheat, cheat_id)
        gone = row is not None
        found = (row.platform, row.title_id) if gone else None
        if gone:
            cheats.delete(cheat_id)
            self._tell(LibraryChange('cheats', *found))
        return gone

    def _tell(self, change):
        for listener in list(self._listeners):
            listener(change)

    def journal(self):
        return self._journal

    def subscribe(self, listener):
        """Calls `listener` with each change of the library, as it is found (the event `library`)."""
        self._listeners.append(listener)

    def refresh(self):
        """Look at the database now: what changed is recorded and told to the listeners."""
        self._current()
