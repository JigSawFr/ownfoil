"""A static copy of the library (Lantern 21): files that any web server can serve, with no Ownfoil behind them.

    python -m lantern_server.export /data/export          (from the application's folder, e.g. in the container)

The folder gets `lantern.json`, the documents under `v1/`, and the files, each under its hash. Run it again
and the folder is brought up to date. Only the files Lantern lists are written, so a game without a current
hash is left out until it has one.
"""
import argparse
import sys

from lantern.static import export

from lantern_server.identity import lantern_uid
from lantern_server.library import OwnfoilLibrary
from settings import get_settings


def run(out, app):
    """Write the static library into `out`; returns the summary of what was written."""
    shop = get_settings()['shop']
    with app.app_context():
        library = OwnfoilLibrary()
        return export(library, out, uid=lantern_uid(), name=shop['name'] or 'Ownfoil', platforms=['switch'],
                      language=get_settings()['titles']['language'] or 'en')


def main(argv=None):
    parser = argparse.ArgumentParser(prog='lantern_server.export', description=__doc__.split('\n\n')[0])
    parser.add_argument('out', help='the folder to write the static library into')
    arguments = parser.parse_args(argv)
    from app import app
    summary = run(arguments.out, app)
    print(summary)
    return 0


if __name__ == '__main__':
    sys.exit(main())
