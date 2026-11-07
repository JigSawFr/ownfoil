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
    """Whether this request may be answered as Lantern: over TLS, or from this very machine."""
    return request.is_secure or is_loopback(request.remote_addr or '')


def build():
    """A LanternApp for the shop as it is configured now."""
    shop = get_settings()['shop']
    return LanternApp(
        OwnfoilLibrary(BASE),
        uid=lantern_uid(),
        name=shop['name'] or 'Ownfoil',
        motd=shop['motd'] or None,
        base=BASE,
        software=('Ownfoil', APP_VERSION),
        platforms=['switch'],
        public=bool(shop['public']),
        secret=_vault.get('cursor'),
    )


def current():
    """The LanternApp, rebuilt only when what it shows of the shop changed."""
    shop = get_settings()['shop']
    key = (shop['name'], shop['motd'], bool(shop['public']))
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
    return to_response(lantern.handle(request))


def init_app(app):
    """Register Lantern's routes: the well-known document and everything under <base>/v1."""
    app.add_url_rule('/.well-known/lantern', 'lantern_well_known', view, methods=METHODS)
    app.add_url_rule(BASE + '/v1', 'lantern_v1', view, methods=METHODS)
    app.add_url_rule(BASE + '/v1/', 'lantern_v1_slash', view, methods=METHODS)
    app.add_url_rule(BASE + '/v1/<path:rest>', 'lantern_routes', view, methods=METHODS)
