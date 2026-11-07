"""Ownfoil's own page for Lantern (`/admin/lantern`): turn it on, see and revoke devices and invitations,
answer requests, see the room the backups take. For administrators; every change is a POST with the page's token."""
import secrets
from datetime import datetime, timedelta, timezone

import flask
from lantern.server.accounts import AccountError, utc
from lantern.server.http import ProblemError
from lantern.server.title_requests import STATUSES

from auth import access_required, admin_account_created
from db import LanternAccount, db
from lantern_server import service
from lantern_server.accounts import OwnfoilAccounts
from lantern_server.requests import OwnfoilRequests
from settings import get_settings, set_lantern_enabled
from utils import human_size


def _csrf():
    if 'lantern_admin_csrf' not in flask.session:
        flask.session['lantern_admin_csrf'] = secrets.token_urlsafe(24)
    return flask.session['lantern_admin_csrf']


def _saves():
    rows = []
    store = service.saves()
    for account in LanternAccount.query.order_by(LanternAccount.name).all():
        kept = store.saves(account.name)
        if kept:
            rows.append((account.name, len(kept), human_size(sum(s.size for s in kept))))
    return rows


def _act(form):
    """Do what the form asks; returns a message to show."""
    action, accounts = form.get('action'), OwnfoilAccounts()
    if action == 'toggle':
        set_lantern_enabled(form.get('enabled') == '1')
        return 'Saved.'
    if action == 'revoke':
        return 'Device revoked.' if accounts.revoke(form.get('id', '')) else 'No such device.'
    if action == 'revoke_invitation':
        return 'Invitation revoked.' if accounts.revoke_invitation(form.get('id', '')) else 'No such invitation.'
    if action == 'invite':
        try:
            hours, devices = int(form.get('hours', '24')), int(form.get('devices', '1'))
            if not (1 <= hours <= 720 and 1 <= devices <= 20):
                raise ValueError
            scopes = accounts.user(form.get('user', '')).scopes
            made = accounts.create_invitation(form['user'], scopes, utc(datetime.now(timezone.utc) + timedelta(hours=hours)),
                                              devices, note=(form.get('note') or '')[:200] or None)
        except (ValueError, AttributeError, KeyError, AccountError):
            return 'That invitation cannot be made.'
        return f'Invitation made: {made.code}'
    if action in ('done', 'decline'):
        lantern = service.current()
        status = 'done' if action == 'done' else 'declined'
        assert status in STATUSES
        try:
            found = lantern.answer_request(form.get('id', ''), status, (form.get('reason') or '')[:500] or None)
        except ProblemError:
            found = None
        return f'Request marked {status}.' if found else 'That request is not open any more.'
    return ''


@access_required('admin')
def page():
    message = ''
    if flask.request.method == 'POST':
        if not secrets.compare_digest(flask.request.form.get('csrf', ''), _csrf()):
            return 'The page expired; reload it.', 400
        message = _act(flask.request.form)
    accounts = OwnfoilAccounts()
    settings = get_settings()['lantern']
    return flask.render_template(
        'lantern.html', title='Lantern', csrf=_csrf(), message=message, enabled=settings['enabled'],
        https_port=settings['https_port'], devices=accounts.devices(), invitations=accounts.invitations(),
        users=[u.name for u in accounts.users()], requests=OwnfoilRequests().requests(), saves=_saves(),
        admin_account_created=admin_account_created())


def init_app(app):
    app.add_url_rule('/admin/lantern', 'lantern_admin', page, methods=['GET', 'POST'])
