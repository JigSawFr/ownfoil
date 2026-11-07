"""Sync data (SPEC.md 20), kept in Ownfoil's database: what a user's devices share, such as a wish list.

The same rules as the SDK's own stores: items merged one by one by the rule of 20.4, a revision per
collection, deletions kept `retention` (30 days at least, 20.5), and a `generation` that changes only when
the data is erased. Written in SQL as theirs is; one lock serializes the reads and writes of this process.
"""
import json
import threading
from datetime import datetime, timedelta, timezone

from lantern import sync as syncing
from lantern.server.http import ProblemError, bad_request
from lantern.server.sync import ITEMS, new_generation
from sqlalchemy import text

from db import db

_ITEM = 'key, updated, value, deleted, rev, kept'


def _item(row):
    value = row[2]
    return syncing.Item(str(row[0]), str(row[1]), json.loads(value) if isinstance(value, str) else None,
                        bool(row[3]), int(row[4]))


class OwnfoilSync:
    """Lantern's `SyncStore` over the `lantern_sync_*` tables."""

    def __init__(self, limit=ITEMS, *, retention=timedelta(days=syncing.TOMBSTONE_DAYS)):
        if not 1 <= limit <= syncing.ITEMS_LIMIT:
            raise ValueError('limits.sync_items is 1 to 100,000 (SPEC.md 13)')
        self.limit, self.retention = limit, retention
        self._lock = threading.RLock()

    def _run(self, work):
        """One transaction: the work, then commit; anything raised undoes it."""
        with self._lock:
            try:
                result = work()
                db.session.commit()
                return result
            except BaseException:
                db.session.rollback()
                raise

    @staticmethod
    def _execute(sql, **params):
        return db.session.execute(text(sql), params)

    def _generation(self, user):
        row = self._execute('SELECT generation FROM lantern_sync_accounts WHERE user = :user', user=user).fetchone()
        if row is not None:
            return str(row[0])
        generation = new_generation()
        self._execute('INSERT INTO lantern_sync_accounts (user, generation) VALUES (:user, :generation)',
                      user=user, generation=generation)
        return generation

    def _collection(self, user, name):
        """(rev, floor) of the collection, the deletions past their retention dropped first."""
        self._execute('INSERT OR IGNORE INTO lantern_sync_collections (user, name, rev, floor) '
                      'VALUES (:user, :name, 0, 0)', user=user, name=name)
        cutoff = (datetime.now(timezone.utc) - self.retention).isoformat()
        gone = self._execute('SELECT MAX(rev) FROM lantern_sync_items WHERE user = :user AND collection = :name '
                             'AND deleted = 1 AND kept < :cutoff', user=user, name=name, cutoff=cutoff).fetchone()[0]
        if gone is not None:
            self._execute('DELETE FROM lantern_sync_items WHERE user = :user AND collection = :name '
                          'AND deleted = 1 AND kept < :cutoff', user=user, name=name, cutoff=cutoff)
            self._execute('UPDATE lantern_sync_collections SET floor = MAX(floor, :gone) '
                          'WHERE user = :user AND name = :name', user=user, name=name, gone=gone)
        rev, floor = self._execute('SELECT rev, floor FROM lantern_sync_collections WHERE user = :user '
                                   'AND name = :name', user=user, name=name).fetchone()
        return int(rev), int(floor)

    def overview(self, user):
        def work():
            out = {}
            for name in syncing.COLLECTIONS:
                rev, _ = self._collection(user, name)
                live = self._execute('SELECT COUNT(*) FROM lantern_sync_items WHERE user = :user AND '
                                     'collection = :name AND deleted = 0', user=user, name=name).fetchone()[0]
                out[name] = (rev, int(live))
            return self._generation(user), out
        return self._run(work)

    def read(self, user, collection, since):
        def work():
            generation = self._generation(user)
            rev, floor = self._collection(user, collection)
            base = f'SELECT {_ITEM} FROM lantern_sync_items WHERE user = :user AND collection = :collection'
            if since is None:
                rows = self._execute(base + ' AND deleted = 0 ORDER BY rowid', user=user,
                                     collection=collection).fetchall()
            else:
                if since > rev:
                    raise bad_request('This revision was never given: read the collection whole.')
                if since < floor:
                    raise ProblemError(410, 'gone', 'Its deletions are gone: read the collection whole.')
                rows = self._execute(base + ' AND rev > :since ORDER BY rowid', user=user, collection=collection,
                                     since=since).fetchall()
            return generation, rev, [_item(r) for r in rows]
        return self._run(work)

    def merge(self, user, collection, generation, items, *, if_match, now, skew):
        def work():
            have_generation = self._generation(user)
            rev, _ = self._collection(user, collection)
            if generation != have_generation:
                raise ProblemError(409, 'conflict',
                                   'This sync data was deleted since: ask the user again (SPEC.md 20.3).')
            if if_match is not None and if_match.strip() != f'"{have_generation}-{rev}"':
                raise ProblemError(412, 'precondition_failed',
                                   'The collection changed since it was read: read it again.',
                                   title='Precondition failed')
            known = {str(r[0]) for r in self._execute('SELECT key FROM lantern_sync_items WHERE user = :user '
                                                      'AND collection = :collection', user=user,
                                                      collection=collection)}
            if len(known) + sum(1 for i in items if i.key not in known) > self.limit:
                raise ProblemError(413, 'too_large', f'A collection holds {self.limit} items at most here.')
            out = []
            for sent in items:
                row = self._execute(f'SELECT {_ITEM} FROM lantern_sync_items WHERE user = :user AND '
                                    'collection = :collection AND key = :key', user=user, collection=collection,
                                    key=sent.key).fetchone()
                kept, replaced = syncing.merge(_item(row) if row else None, sent, now=now, skew=skew)
                if replaced:
                    rev += 1
                    kept = syncing.Item(kept.key, kept.updated, kept.value, kept.deleted, rev)
                    self._execute(
                        'INSERT INTO lantern_sync_items (user, collection, key, updated, value, deleted, rev, kept) '
                        'VALUES (:user, :collection, :key, :updated, :value, :deleted, :rev, :kept) '
                        'ON CONFLICT(user, collection, key) DO UPDATE SET updated = excluded.updated, '
                        'value = excluded.value, deleted = excluded.deleted, rev = excluded.rev, '
                        'kept = excluded.kept',
                        user=user, collection=collection, key=kept.key, updated=kept.updated,
                        value=None if kept.value is None else json.dumps(kept.value), deleted=int(kept.deleted),
                        rev=rev, kept=now.astimezone(timezone.utc).isoformat())
                out.append(kept)
            self._execute('UPDATE lantern_sync_collections SET rev = :rev WHERE user = :user AND name = :name',
                          rev=rev, user=user, name=collection)
            return have_generation, rev, out
        return self._run(work)

    def erase(self, user, collection=None):
        def work():
            self._generation(user)
            if collection is None:
                self._execute('DELETE FROM lantern_sync_items WHERE user = :user', user=user)
                self._execute('DELETE FROM lantern_sync_collections WHERE user = :user', user=user)
            else:
                self._execute('DELETE FROM lantern_sync_items WHERE user = :user AND collection = :collection',
                              user=user, collection=collection)
                self._execute('DELETE FROM lantern_sync_collections WHERE user = :user AND name = :collection',
                              user=user, collection=collection)
            generation = new_generation()
            self._execute('UPDATE lantern_sync_accounts SET generation = :generation WHERE user = :user',
                          generation=generation, user=user)
            return generation
        return self._run(work)

    def forget(self, user):
        def work():
            for table in ('lantern_sync_items', 'lantern_sync_collections', 'lantern_sync_accounts'):
                self._execute(f'DELETE FROM {table} WHERE user = :user', user=user)  # noqa: S608
        self._run(work)
