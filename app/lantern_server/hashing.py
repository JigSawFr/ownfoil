"""File hashes for Lantern, which lists a file only once its SHA-256 is known (SPEC.md 6.1).

A hash is kept on the `files` row with the size and mtime it was computed under. It counts only while
they are still the row's: a file that is replaced, touched or rewritten by the organizer or the
compressor stops matching, and is hashed again by the pipeline's `lantern_hash` stage.
"""
import logging
import os

from lantern.digest import CHUNK, Sha256

from settings import get_settings

logger = logging.getLogger('main')


def enabled():
    return bool(get_settings()['lantern']['enabled'])


def is_hashed(file):
    """Whether the row holds a hash for the file as it is recorded now."""
    return (file.sha256 is not None
            and file.sha256_size == file.size
            and file.sha256_mtime == file.mtime)


def needs_hash(file, mgmt=None):
    """Pipeline stage test: Lantern is on and the file has no current hash."""
    return enabled() and file.size is not None and file.mtime is not None and not is_hashed(file)


def hash_file(file, progress=None):
    """Compute the SHA-256 of `file.filepath` and record it on the row (the caller commits).

    Returns True when recorded; False when the file is not the one the row describes - its size or
    mtime differ from the row's, or changed while it was read - and nothing is recorded: the scan or
    the watcher refreshes the row, and the pipeline hashes the file then. OSError propagates."""
    before = os.stat(file.filepath)
    if (before.st_size, before.st_mtime) != (file.size, file.mtime):
        return False
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
    after = os.stat(file.filepath)
    if (after.st_size, after.st_mtime) != (before.st_size, before.st_mtime) or digest.size != before.st_size:
        return False
    file.sha256 = digest.hexdigest()
    file.sha256_size = before.st_size
    file.sha256_mtime = before.st_mtime
    return True
