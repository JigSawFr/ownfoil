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
from lantern.server.saves import FileSaveStore, SaveLimits
from lantern.server.secrets_file import SecretsFile

from constants import APP_VERSION, CONFIG_DIR
from lantern_server.identity import lantern_uid
from lantern_server import hashing, pairing_page, tls
from lantern_server.accounts import OwnfoilAccounts
from lantern_server.requests import OwnfoilRequests
from lantern_server.sync import OwnfoilSync
from lantern_server.library import OwnfoilLibrary
from settings import get_settings

logger = logging.getLogger('main')

BASE = '/api/lantern'

_lock = threading.RLock()  # build() runs under it and asks for the shared stores
_state = {'key': None, 'lantern': None}
_vault = SecretsFile(os.path.join(CONFIG_DIR, 'lantern-secrets.json'))
_stores = {}  # what Lantern keeps beside the database, made once and shared by every LanternApp


def saves():
    """The backups of saves (SPEC.md 9), in a folder of the config: one store for the life of the process."""
    with _lock:
        if 'saves' not in _stores:
            _stores['saves'] = FileSaveStore(os.path.join(CONFIG_DIR, 'lantern-saves'))
        return _stores['saves']


def forget_user(user_id):
    """An Ownfoil user is going: what Lantern kept for their account goes with them."""
    from db import LanternAccount
    account = LanternAccount.query.filter_by(user_id=user_id).first()
    if account is not None:
        saves().forget(account.name)
        OwnfoilRequests().forget(account.name)
        OwnfoilSync().forget(account.name)


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
        cors_origins=[o for o in settings['lantern']['cors_origins'] if isinstance(o, str)],
        grant_seed=_vault.get('grant', 32),  # signs the grants peers check; it never leaves this server
        spki=None if behind_proxy else own_key,
        scheme='https' if behind_proxy else None,
        accounts=OwnfoilAccounts(),
        pair_page=lambda request: pairing_page.page(lantern, request),
        saves=saves(),
        requests=OwnfoilRequests(),
        sync=OwnfoilSync(),
        save_limits=SaveLimits(),
    )
    return lantern


def current():
    """The LanternApp, rebuilt only when what it shows of the shop changed."""
    shop = get_settings()['shop']
    settings = get_settings()
    key = (shop['name'], shop['motd'], bool(shop['public']), bool(settings['lantern']['proxy']),
           settings['lantern']['https_port'], tuple(settings['lantern']['cors_origins']))
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
    from lantern_server import admin_page
    admin_page.init_app(app)


POLL_SECONDS = 2.0
PRUNE_EVERY = 150  # rounds: every five minutes
_watcher = None


class Watcher(threading.Thread):
    """Looks at the database now and then, so that the event streams hear of a change when it happens and not
    when the next client happens to ask: Ownfoil's workers are other processes and tell nobody."""

    def __init__(self, app, interval=POLL_SECONDS):
        super().__init__(daemon=True, name='lantern-watch')
        self.app, self.interval = app, interval
        self._stop_event = threading.Event()

    def run(self):
        rounds = 0
        while not self._stop_event.wait(self.interval):
            rounds += 1
            try:
                with self.app.app_context():
                    if get_settings()['lantern']['enabled']:
                        current().library.refresh()
                        if rounds % PRUNE_EVERY == 0:
                            hashing.prune_chunks()
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
    stop_dnssd()
    with _lock:
        watcher, _watcher = _watcher, None
    if watcher is not None:
        watcher.stop()


_zeroconf = {'handle': None, 'key': None}


def reconcile_dnssd(app):
    """Announce the TLS port on the local network (DNS-SD) while Lantern is on and serving it, and stop when not."""
    from lantern.server.discovery import advertise_dnssd
    from utils import get_lan_ip
    from lantern_server import tls as listener
    settings = get_settings()
    port = listener._server.port if listener._server is not None else 0
    address = get_lan_ip() if port else None
    with _lock:
        key = (settings['lantern']['dnssd'] and settings['lantern']['enabled'] and address and port,
               settings['shop']['name'])
        if key == _zeroconf['key']:
            return
        _stop_dnssd()
        _zeroconf['key'] = key
        if not key[0]:
            return
        try:
            with app.app_context():
                _zeroconf['handle'] = advertise_dnssd(current(), port, address)
            logger.info(f'Lantern: announced on the local network, {address} port {port}.')
        except Exception as e:  # no multicast, no zeroconf: the server still works, found by its address
            logger.warning(f'Lantern: could not announce on the local network ({e}).')


def _stop_dnssd():
    handle, _zeroconf['handle'] = _zeroconf['handle'], None
    if handle is not None:
        try:
            handle.close()
        except Exception:
            logger.exception('Lantern: could not stop announcing.')


def stop_dnssd():
    with _lock:
        _stop_dnssd()
        _zeroconf['key'] = None
