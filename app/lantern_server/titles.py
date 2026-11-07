"""What Ownfoil says of each game to Lantern (SPEC.md 7): titledb's sheet, and the artwork it keeps locally.

Images are served by Ownfoil itself under their SHA-256 (SPEC.md 7.1), never from a CDN: the ones its
`media` table holds a local copy of are listed, the others are left out. A title whose sheet Lantern refuses
falls back to what is certain, its name.
"""
import logging
import os
import re
import threading

from lantern import schema
from lantern.digest import sha256_file
from lantern.models import Image, Players, Title

import media as media_lib
import titledb
from db import Media
from lantern_server.library import PLATFORM
from settings import get_settings

logger = logging.getLogger('main')

SCREENSHOTS_MAX = 20
TAGS_MAX = 8
#: Lantern's fixed list of genres (SPEC.md 7.1), and the words of the eShop's categories that say each one.
_GENRES = (
    ('platformer', ('platform',)),
    ('rpg', ('role-playing', 'rpg')),
    ('shooter', ('shooter', 'shoot')),
    ('fighting', ('fighting', "beat 'em up")),
    ('racing', ('racing', 'driving')),
    ('sports', ('sport', 'football', 'soccer', 'golf', 'basketball', 'tennis', 'baseball')),
    ('strategy', ('strategy', 'tactic')),
    ('simulation', ('simulation', 'simulator', 'life')),
    ('puzzle', ('puzzle',)),
    ('music', ('music', 'rhythm')),
    ('party', ('party',)),
    ('education', ('educational', 'education')),
    ('adventure', ('adventure', 'visual novel')),
    ('action', ('action', 'roguelike', 'metroidvania', 'stealth', 'survival')),
)
_DATE = re.compile(r'^(\d{4})-?(\d{2})-?(\d{2})$')
_SIGNATURES = (
    (b'\xff\xd8\xff', 'jpg', 'image/jpeg'),
    (b'\x89PNG\r\n\x1a\n', 'png', 'image/png'),
    (b'GIF8', 'gif', 'image/gif'),
)
_hashes = {}  # (path, size, mtime_ns) -> (sha256, extension, content type)
_lock = threading.Lock()


def sniff(path):
    """(extension, content type) of an image from its first bytes, None when it is not one Lantern lists."""
    with open(path, 'rb') as handle:
        head = handle.read(16)
    for prefix, extension, content_type in _SIGNATURES:
        if head.startswith(prefix):
            return extension, content_type
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'webp', 'image/webp'
    return None


def image_of(path, width=None, height=None):
    """(Image document, name, (path, size, content type)) of a stored picture, None when it cannot be served.
    The picture is hashed once for as long as the file stays as it is."""
    try:
        info = os.stat(path)
        key = (path, info.st_size, info.st_mtime_ns)
        with _lock:
            found = _hashes.get(key)
        if found is None:
            kind = sniff(path)
            if kind is None:
                return None
            found = (sha256_file(path).hexdigest(), *kind)
            with _lock:
                _hashes[key] = found
    except OSError:
        return None
    sha256, extension, content_type = found
    name = f'{sha256}.{extension}'
    return name, (path, info.st_size, content_type, sha256), width, height


def genres_and_tags(categories):
    """(genres, tags) from titledb's categories: the genres of Lantern's list they say, the rest as tags."""
    labels = [c.strip() for c in categories or [] if isinstance(c, str) and c.strip()]
    genres, said = [], set()
    for genre, words in _GENRES:
        for label in labels:
            if any(word in label.lower() for word in words):
                said.add(label)
                if genre not in genres:
                    genres.append(genre)
    tags = [label[:64] for label in labels if label not in said][:TAGS_MAX]
    return genres or None, tags or None


def date_of(text):
    """A release date as Lantern writes it (YYYY-MM-DD), from titledb's YYYYMMDD or the same with dashes."""
    found = _DATE.match(str(text or ''))
    return '-'.join(found.groups()) if found else None


def pictures(title_id, base):
    """(icon, banner, screenshots, files): the title's local artwork as Image documents, and by name how to
    serve each. Ownfoil's originals are stored under hashed names in the media folder."""
    slots = {}
    for row in Media.query.filter_by(title_id=title_id).order_by(Media.kind, Media.position).all():
        if row.kind in media_lib.KINDS and media_lib.have(row.kind, row.filename):
            slots.setdefault(row.kind, []).append(row)
    files, out = {}, {}
    for kind, rows in slots.items():
        images = []
        for row in rows[:SCREENSHOTS_MAX if kind == media_lib.SCREENSHOT else 1]:
            stored = image_of(media_lib.media_path(kind, media_lib.ORIGINAL, row.filename), row.width, row.height)
            if stored is None:
                continue
            name, served, width, height = stored
            files[name] = served
            images.append(Image(url=f'{base}/v1/media/{name}', width=width, height=height, sha256=served[3]))
        out[kind] = images
    first = lambda kind: (out.get(kind) or [None])[0]  # noqa: E731
    return first(media_lib.ICON), first(media_lib.BANNER), out.get(media_lib.SCREENSHOT) or None, files


def sheet(title_id, name, base):
    """(Title, files): what titledb and the local artwork say of one game, named `name` when titledb does not."""
    try:
        record = titledb.store.get_title_record(title_id) or {}
    except Exception:  # titledb absent or being rebuilt
        record = {}
    icon, banner, screenshots, files = pictures(title_id, base)
    players = record.get('number_of_players')
    genres, tags = genres_and_tags(record.get('category'))
    title = Title(
        platform=PLATFORM, title_id=title_id, name=record.get('name') or name,
        language=get_settings()['titles']['language'] or None,
        summary=record.get('intro') or None, description=record.get('description') or None,
        developer=record.get('developer') or None, publisher=record.get('publisher') or None,
        release=date_of(record.get('release_date')),
        genres=genres, tags=tags,
        players=Players(max=int(players)) if str(players or '').isdigit() and int(players) > 0 else None,
        icon=icon, banner=banner, screenshots=screenshots)
    if schema.definition('title', title.to_json()):
        logger.warning(f'Lantern: the sheet of {title_id} is refused, it keeps to its name')
        title = Title(platform=PLATFORM, title_id=title_id, name=name)
    return title, files


def project(apps, base):
    """(titles, media) for the games the apps belong to: one sheet per title id, named for its base game."""
    names = {}
    for app in sorted(apps, key=lambda a: {'base': 1, 'update': 0, 'dlc': 0}.get(a.type, 0)):
        names[app.title_id] = app.name or app.title_id  # the base comes last, and names the game
    titles, files = [], {}
    for title_id, name in sorted(names.items()):
        found, served = sheet(title_id, name, base)
        titles.append(found)
        files.update(served)
    return titles, files
