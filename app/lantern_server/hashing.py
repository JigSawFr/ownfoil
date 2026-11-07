"""File hashes for Lantern, which lists a file only once its SHA-256 is known (SPEC.md 6.1).

A hash is kept on the `files` row with the size and mtime it was computed under. It counts only while
they are still the row's: a file that is replaced, touched or rewritten by the organizer or the
compressor stops matching, and is hashed again by the pipeline's `lantern_hash` stage.
"""
import hashlib
import logging
import os

from lantern import chunks
from lantern.digest import CHUNK, Sha256

from constants import APP_TYPE_UPD
from db import LanternChunks, db
from settings import get_settings

logger = logging.getLogger('main')


def enabled():
    return bool(get_settings()['lantern']['enabled'])


def wants_index(file):
    """Whether the file is to be cut into chunks for differential updates (Lantern 22): the setting is on, and the
    file is of a type and a size it names."""
    deltas = get_settings()['lantern']['deltas']
    if not deltas['enabled'] or not deltas['min'] <= (file.size or 0) <= deltas['max']:
        return False
    if chunks.size_for(file.size) is None:
        return False
    wanted = {t.upper() for t in deltas['types']}
    return any((app.app_type or '').upper() in wanted for app in file.apps)


def has_index(file):
    return file.sha256 is not None and db.session.get(LanternChunks, file.sha256) is not None


_RANK = {'BASE': 0, 'UPDATE': 1, 'DLC': 2}


def queue_rank(file):
    """Where a file stands in the queue of work: bases first, then updates and add-ons, the smallest first,
    so that a library is usable early while the large files are still being read."""
    kinds = [_RANK.get((app.app_type or '').upper(), 3) for app in file.apps]
    return (min(kinds) if kinds else 3, file.size or 0)


def is_hashed(file):
    """Whether the row holds a hash for the file as it is recorded now."""
    return (file.sha256 is not None
            and file.sha256_size == file.size
            and file.sha256_mtime == file.mtime)


def needs_hash(file, mgmt=None):
    """Pipeline stage test: Lantern is on and the file has no current hash."""
    if not enabled() or file.size is None or file.mtime is None:
        return False
    return not is_hashed(file) or (wants_index(file) and not has_index(file))


def hash_file(file, progress=None):
    """Compute the SHA-256 of `file.filepath` and record it on the row (the caller commits).

    Returns True when recorded; False when the file is not the one the row describes - its size or
    mtime differ from the row's, or changed while it was read - and nothing is recorded: the scan or
    the watcher refreshes the row, and the pipeline hashes the file then. OSError propagates."""
    before = os.stat(file.filepath)
    if (before.st_size, before.st_mtime) != (file.size, file.mtime):
        return False
    index = None
    if wants_index(file):
        found = chunks.index_file(file.filepath, progress=_percent(progress, before.st_size))
        digest_hex, read, index = found.sha256, found.size, found.cdc
    else:
        digest = Sha256()
        last = -1
        with open(file.filepath, 'rb') as handle:
            while block := handle.read(CHUNK):
                digest.update(block)
                if progress and before.st_size:
                    pct = digest.size * 100 // before.st_size
                    if pct != last:
                        last = pct
                        progress(pct)
        digest_hex, read = digest.hexdigest(), digest.size
    after = os.stat(file.filepath)
    if (after.st_size, after.st_mtime) != (before.st_size, before.st_mtime) or read != before.st_size:
        return False
    file.sha256 = digest_hex
    file.sha256_size = before.st_size
    file.sha256_mtime = before.st_mtime
    if index is not None:
        _, raw = index
        row = db.session.get(LanternChunks, digest_hex) or LanternChunks(sha256=digest_hex)
        row.ref_size, row.ref_sha256, row.raw = len(raw), hashlib.sha256(raw).hexdigest(), raw
        db.session.add(row)
    return True


def _percent(progress, size):
    """A progress callback of bytes read, as one of percent."""
    if progress is None or not size:
        return None
    state = {'last': -1}

    def report(done):
        pct = done * 100 // size
        if pct != state['last']:
            state['last'] = pct
            progress(pct)
    return report


def prune_chunks():
    """Forget the chunk indexes of files that are gone or were replaced."""
    from db import Files
    kept = db.session.query(Files.sha256).filter(Files.sha256.isnot(None))
    gone = LanternChunks.query.filter(~LanternChunks.sha256.in_(kept)).delete(synchronize_session=False)
    db.session.commit()
    return gone
