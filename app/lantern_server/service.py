"""Lantern's routes on Ownfoil's Flask app.

They are registered once, and answer only while `lantern.enabled` is on and the request came over TLS
(SPEC.md 2: a server must not serve Lantern in plain HTTP outside loopback). The `LanternApp` behind
them is rebuilt when the shop's name, message or visibility change.
"""
import ipaddress
import logging
import os
import threading

import flask
from lantern.server.app import LanternApp
from lantern.server.flask import METHODS, to_request, to_response
from lantern.server.http import ServerResponse
from lantern.server.secrets_file import SecretsFile

from constants import APP_VERSION, CONFIG_DIR
from lantern_server.identity import lantern_uid
from lantern_server import pairing_page, tls
from lantern_server.accounts import OwnfoilAccounts
from lantern_server.library import OwnfoilLibrary
from settings import get_settings

logger = logging.getLogger('main')

BASE = '/api/lantern'

_lock = threading.Lock()
_state = {'key': None, 'lantern': None}
_vault = SecretsFile(os.path.join(CONFIG_DIR, 'lantern-secrets.json'))


def is_loopback(address):
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def served_securely(request):
    """Whether this request may be answered as Lantern: over TLS (here, or at the reverse proxy the settings
    name), or from this very machine."""
    if request.is_secure or is_loopback(request.remote_addr or ''):
        return True
    return bool(get_settings()['lantern']['proxy']) and request.headers.get('X-Forwarded-Proto') == 'https'


def build():
    """A LanternApp for the shop as it is configured now."""
    settings = get_settings()
    shop = settings['shop']
    behind_proxy = bool(settings['lantern']['proxy'])
    own_key = tls.spki() if settings['lantern']['https_port'] and os.path.exists(tls.CERT_FILE) else None
    lantern = LanternApp(
        OwnfoilLibrary(BASE),
        uid=lantern_uid(),
        name=shop['name'] or 'Ownfoil',
        motd=shop['motd'] or None,
        base=BASE,
        software=('Ownfoil', APP_VERSION),
        platforms=['switch'],
        public=bool(shop['public']),
        secret=_vault.get('cursor'),
        spki=None if behind_proxy else own_key,
        scheme='https' if behind_proxy else None,
        accounts=OwnfoilAccounts(),
        pair_page=lambda request: pairing_page.page(lantern, request),
    )
    return lantern


def current():
    """The LanternApp, rebuilt only when what it shows of the shop changed."""
    shop = get_settings()['shop']
    settings = get_settings()
    key = (shop['name'], shop['motd'], bool(shop['public']), bool(settings['lantern']['proxy']),
           settings['lantern']['https_port'])
    with _lock:
        if _state['key'] != key:
            _state['lantern'], _state['key'] = build(), key
        return _state['lantern']


def view(rest=''):
    if not get_settings()['lantern']['enabled'] or not served_securely(flask.request):
        flask.abort(404)
    lantern = current()
    request = to_request(flask.request, lantern.body_limit)
    if isinstance(request, ServerResponse):  # already answered (a body too large)
        return to_response(request)
    response = to_response(lantern.handle(request))
    # A stream (events) is read after this function returns, and still needs the app context for the database.
    response.response = flask.stream_with_context(response.response)
    return response


def init_app(app):
    """Register Lantern's routes: the well-known document and everything under <base>/v1."""
    app.add_url_rule('/.well-known/lantern', 'lantern_well_known', view, methods=METHODS)
    app.add_url_rule(BASE + '/v1', 'lantern_v1', view, methods=METHODS)
    app.add_url_rule(BASE + '/v1/', 'lantern_v1_slash', view, methods=METHODS)
    app.add_url_rule(BASE + '/v1/<path:rest>', 'lantern_routes', view, methods=METHODS)
    # The page where a signed-in Ownfoil user approves a device (SPEC.md 5.2).
    app.add_url_rule('/pair', 'lantern_pair', view, methods=['GET', 'POST'])


POLL_SECONDS = 2.0
_watcher = None


class Watcher(threading.Thread):
    """Looks at the database now and then, so that the event streams hear of a change when it happens and not
    when the next client happens to ask: Ownfoil's workers are other processes and tell nobody."""

    def __init__(self, app, interval=POLL_SECONDS):
        super().__init__(daemon=True, name='lantern-watch')
        self.app, self.interval = app, interval
        self._stop_event = threading.Event()

    def run(self):
        while not self._stop_event.wait(self.interval):
            try:
                with self.app.app_context():
                    if get_settings()['lantern']['enabled']:
                        current().library.refresh()
            except Exception:  # a hiccup (a locked database, titledb being rebuilt) must not end the watch
                logger.exception('Lantern: could not look at the library')

    def stop(self):
        self._stop_event.set()
        self.join(timeout=5)


def start_watching(app):
    """Start the watcher once; it does nothing while Lantern is off."""
    global _watcher
    with _lock:
        if _watcher is None:
            _watcher = Watcher(app)
            _watcher.start()


def stop_watching():
    global _watcher
    with _lock:
        watcher, _watcher = _watcher, None
    if watcher is not None:
        watcher.stop()
