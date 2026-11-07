"""The page where a signed-in Ownfoil user approves a device that asked to pair (SPEC.md 5.2, 5.3).

The device shows a code; the person types it here, signed in to Ownfoil, after checking that the key shown
here is the one the device shows. Only then is the device given a token, for that user's own account and
no more than their access allows. The page approves nothing for a visitor who is not signed in.
"""
import html
import secrets
import threading
from urllib.parse import parse_qs, quote

import flask
from flask_login import current_user
from lantern.server.http import ServerRequest, html_response
from lantern.server.pairing import Limiter
from lantern.spki import short as short_fingerprint

from lantern_server import accounts

ATTEMPTS = Limiter(10, 60.0)  # codes tried per user and minute: a code is short-lived but not long
_lock = threading.Lock()


def _csrf():
    if 'lantern_csrf' not in flask.session:
        flask.session['lantern_csrf'] = secrets.token_urlsafe(24)
    return flask.session['lantern_csrf']


def _page(request, body, status=200):
    return html_response(request, '<!doctype html><html lang="en"><head><meta charset="utf-8">'
                                  '<meta name="referrer" content="no-referrer"><title>Pair a device</title></head>'
                                  f'<body><h1>Pair a device</h1>{body}</body></html>', status)


def _key(lantern):
    return f'<p>This server\'s key: <code>{html.escape(short_fingerprint(lantern.spki))}</code></p>' \
        if lantern.spki else '<p>This server\'s key is not shown here (a reverse proxy holds it).</p>'


def _form(lantern, code, message=''):
    note = f'<p>{html.escape(message)}</p>' if message else ''
    return (f'{note}{_key(lantern)}<p>Compare it with the one the device shows, then type the code it shows.</p>'
            f'<form method="post" action="{html.escape(lantern.verify)}">'
            f'<input type="hidden" name="csrf" value="{_csrf()}">'
            f'<label>Code <input name="code" value="{html.escape(code)}" autocomplete="off"></label>'
            '<button name="action" value="look">Continue</button></form>')


def _decision(lantern, entry):
    scopes = ', '.join(entry.scopes) or 'nothing'
    return (f'{_key(lantern)}<p>A device called <strong>{html.escape(entry.device)}</strong> asks to pair with your '
            f'account, for: {html.escape(scopes)}.</p>'
            f'<form method="post" action="{html.escape(lantern.verify)}">'
            f'<input type="hidden" name="csrf" value="{_csrf()}">'
            f'<input type="hidden" name="code" value="{html.escape(entry.code)}">'
            '<button name="action" value="approve">Approve</button> '
            '<button name="action" value="refuse">Refuse</button></form>')


def _form_fields(request):
    """The fields of a form posted to the page (the toolkit has read the body; Flask's own form is empty)."""
    parsed = parse_qs((request.body or b'').decode('utf-8', 'replace'), keep_blank_values=True, max_num_fields=20)
    return {name: values[-1] for name, values in parsed.items()}


def page(lantern, request: ServerRequest):
    """The approval page; runs inside the Flask request, which holds the Ownfoil session."""
    form = _form_fields(request) if flask.request.method == 'POST' else {}
    code = (form.get('code') or request.query.get('code') or request.query.get('invitation') or '')[:64]
    if not current_user.is_authenticated:
        back = quote(f'{lantern.verify}?code={quote(code)}' if code else lantern.verify, safe='')
        # The page itself shows the server's key before anyone signs in (SPEC.md 5.3), and approves nothing.
        return _page(request, f'{_key(lantern)}<p>Sign in to Ownfoil to approve a device.</p>'
                              f'<p><a href="/login?next={back}">Sign in</a></p>')
    user = current_user._get_current_object()
    if not accounts.allowed_scopes(user):
        return _page(request, '<p>Your Ownfoil access does not let a device pair with your account.</p>', 403)
    if flask.request.method not in ('GET', 'HEAD', 'POST'):
        return _page(request, '<p>Not supported.</p>', 405)
    if flask.request.method != 'POST':
        return _page(request, _form(lantern, code))
    if not secrets.compare_digest(form.get('csrf', ''), _csrf()):
        return _page(request, _form(lantern, code, 'The page expired; try again.'), 400)
    if not code:
        return _page(request, _form(lantern, code, 'Type the code the device shows.'), 400)
    wait = ATTEMPTS.hit(f'user:{user.id}')
    if wait:
        return _page(request, _form(lantern, '', 'Too many tries; wait a moment.'), 429)
    entry = lantern.desk.by_code(code)
    if entry is None:
        return _page(request, _form(lantern, '', 'No device waits with that code (it may have expired).'), 404)
    action = form.get('action')
    if action == 'refuse':
        lantern.desk.refuse(entry)
        return _page(request, '<p>The device was refused.</p>')
    if action != 'approve':
        return _page(request, _decision(lantern, entry))
    account = accounts.ensure_account(user)
    granted = lantern.desk.approve(entry, account.name)
    return _page(request, f'<p>The device is paired, for: {html.escape(", ".join(granted) or "nothing")}.</p>')
