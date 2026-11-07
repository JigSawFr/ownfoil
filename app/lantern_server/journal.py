"""The change journal Lantern's clients read to learn what changed since they last looked (SPEC.md 6.4, 7.3),
kept in Ownfoil's database so that a client's `since` token outlives a restart.

Ownfoil's library is projected from its tables, not edited through calls, so changes are found by comparing the
projection with a baseline kept beside the journal: each app added or removed, each sheet changed.
"""
import hashlib
import json
import logging
import secrets

from lantern.models import RemovedTitle
from sqlalchemy import func

from db import LanternChange, LanternMeta, db

logger = logging.getLogger('main')

KEEP = 10000
BASELINE_SCHEMA = 1


def _get(key, default=None):
    row = db.session.get(LanternMeta, key)
    return row.value if row is not None else default


def _put(key, value):
    row = db.session.get(LanternMeta, key)
    if row is None:
        db.session.add(LanternMeta(key=key, value=str(value)))
    else:
        row.value = str(value)


class OwnfoilChangeLog:
    """`ChangeLog` over the `lantern_journal` table: positions only grow, the epoch lasts as long as the database."""

    def __init__(self, keep=KEEP):
        if keep < 1:
            raise ValueError('keep is 1 or more')
        self.keep = keep

    @property
    def epoch(self):
        value = _get('epoch')
        if value is None:
            value = secrets.token_hex(4)
            _put('epoch', value)
            db.session.commit()
        return value

    @property
    def seq(self):
        return int(_get('seq', 0))

    @property
    def floor(self):
        return int(_get('floor', 0))

    def record(self, kind, key, platform):
        position = self.seq + 1
        db.session.add(LanternChange(position=position, kind=kind, key=key, platform=platform))
        _put('seq', position)
        gone = position - self.keep
        if gone > self.floor:
            LanternChange.query.filter(LanternChange.position <= gone).delete(synchronize_session=False)
            _put('floor', gone)
        db.session.commit()

    def since(self, after, kinds, platform):
        query = LanternChange.query.filter(LanternChange.position > after, LanternChange.kind.in_(tuple(kinds)))
        if platform is not None:
            query = query.filter(LanternChange.platform == platform)
        return [(row.kind, row.key) for row in query.order_by(LanternChange.position).all()]


def sheet_token(title):
    """A short stable digest of a sheet, to notice that it changed."""
    return hashlib.sha256(json.dumps(title.to_json(), sort_keys=True).encode()).hexdigest()[:16]


def read_baseline():
    """(apps, titles) as last recorded, or None when a newer version wrote them (left as they are)."""
    text = _get('baseline')
    if text is None:
        return {}, {}
    try:
        document = json.loads(text)
    except ValueError:
        return {}, {}
    if not isinstance(document, dict) or not isinstance(document.get('schema'), int):
        return {}, {}
    if document['schema'] > BASELINE_SCHEMA:
        return None
    apps, titles = document.get('apps'), document.get('titles')
    return (apps if isinstance(apps, dict) else {}), (titles if isinstance(titles, dict) else {})


def compare(log, apps, titles, platform):
    """Record what changed since the baseline and make the new one. `apps`: App documents; `titles`: Title
    documents. Returns [(family, title_id)] of the changes, for the events."""
    baseline = read_baseline()
    if baseline is None:
        logger.warning('Lantern: the journal baseline is of a newer version; changes are not recorded.')
        return []
    before_apps, before_titles = baseline
    now_apps = {a.id: a.title_id for a in apps}
    now_titles = {f'{platform}/{t.title_id}': sheet_token(t) for t in titles}
    changes = []
    for app_id, title_id in now_apps.items():
        if app_id not in before_apps:
            log.record('added', app_id, platform)
            changes.append(('apps', title_id))
    for app_id, title_id in before_apps.items():
        if app_id not in now_apps:
            log.record('removed', app_id, platform)
            changes.append(('apps', title_id))
    for key, token in now_titles.items():
        if before_titles.get(key) != token:
            log.record('title', key, platform)
            changes.append(('titles', key.partition('/')[2]))
    for key in before_titles:
        if key not in now_titles:
            log.record('title', key, platform)
            changes.append(('titles', key.partition('/')[2]))
    if changes or not before_apps and not before_titles:
        try:
            document = json.loads(_get('baseline') or '{}')
        except ValueError:
            document = {}
        if not isinstance(document, dict):
            document = {}
        document.update(schema=BASELINE_SCHEMA, apps=now_apps, titles=now_titles)  # unknown keys are kept
        _put('baseline', json.dumps(document))
        db.session.commit()
    return changes


class OwnfoilJournal:
    """The `Journal` clients ask: the log's positions, and what the current projection says of each change."""

    def __init__(self, log, library):
        self.log, self.library = log, library

    @property
    def epoch(self):
        return self.log.epoch

    def position(self):
        return self.log.seq

    def oldest(self):
        return self.log.floor

    def app_changes(self, after, platform):
        apps = {a.id: a for a in self.library.apps()}
        events = self.log.since(after, ('added', 'removed'), platform)
        added = list(dict.fromkeys(k for kind, k in events if kind == 'added' and k in apps))
        removed = sorted({k for kind, k in events if kind == 'removed' and k not in apps})
        return [apps[a] for a in added], removed

    def title_changes(self, after, platform, languages):
        sheets = {f'{t.platform}/{t.title_id}': t for t in self.library.titles(languages)}
        keys = sorted({k for _, k in self.log.since(after, ('title',), platform)})
        changed, removed = [], []
        for key in keys:
            if key in sheets:
                changed.append(sheets[key])
            else:
                p, _, title_id = key.partition('/')
                removed.append(RemovedTitle(platform=p, title_id=title_id))
        return changed, removed
