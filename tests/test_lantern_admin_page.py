"""Ownfoil's page for Lantern: for administrators, every change behind the page's token."""
import re

import pytest
from lantern.server.title_requests import StoredRequest

import settings as settings_mod
from db import LanternDevice, User, db
from lantern_server import accounts as acc, admin_page, service
from lantern_server.requests import OwnfoilRequests

TITLE = '0100AB1200010000'


@pytest.fixture
def page(tmp_path, monkeypatch):
    """The real app object (its templates need the whole app's routes), on an empty database."""
    from app import app
    from db import init_db
    monkeypatch.setattr(settings_mod, "CONFIG_FILE", str(tmp_path / "settings.yaml"))
    monkeypatch.setattr(settings_mod, "KEYS_FILE", str(tmp_path / "keys.txt"))
    monkeypatch.setattr(settings_mod, "_cached_settings", None)
    init_db(app)
    ctx = app.app_context()
    ctx.push()
    db.drop_all()
    db.create_all()
    admin = User(user='root', password='x', admin_access=True, shop_access=True, backup_access=True)
    plain = User(user='ada', password='x', admin_access=False, shop_access=True, backup_access=False)
    db.session.add_all([admin, plain])
    db.session.commit()
    from lantern.server.saves import FileSaveStore
    monkeypatch.setitem(service._stores, 'saves', FileSaveStore(tmp_path / 's'))
    service._state['key'] = None
    flipped = []
    monkeypatch.setattr(admin_page, 'set_lantern_enabled', flipped.append)
    yield app, admin, plain, flipped
    db.session.remove()
    ctx.pop()


def login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def token(client):
    return re.search(r'name="csrf" value="([^"]+)"', client.get('/admin/lantern').get_data(as_text=True)).group(1)


def test_a_visitor_who_is_not_signed_in_is_sent_to_sign_in(page):
    app, *_ = page
    assert app.test_client().get('/admin/lantern').status_code in (302, 401)


def test_a_user_who_is_not_an_administrator_is_refused(page):
    app, _, plain, _ = page
    client = app.test_client()
    login(client, plain)
    assert client.get('/admin/lantern').status_code == 403


def test_an_administrator_sees_the_page(page):
    app, admin, *_ = page
    client = app.test_client()
    login(client, admin)
    assert client.get('/admin/lantern').status_code == 200


def test_a_post_without_the_pages_token_does_nothing(page):
    app, admin, _, flipped = page
    client = app.test_client()
    login(client, admin)
    token(client)
    assert client.post('/admin/lantern', data={'action': 'toggle', 'enabled': '1', 'csrf': 'forged'}).status_code == 400
    assert flipped == []


def test_lantern_is_turned_on_and_off(page):
    app, admin, _, flipped = page
    client = app.test_client()
    login(client, admin)
    t = token(client)
    client.post('/admin/lantern', data={'action': 'toggle', 'enabled': '1', 'csrf': t})
    client.post('/admin/lantern', data={'action': 'toggle', 'csrf': t})
    assert flipped == [True, False]


def test_a_device_is_listed_and_revoked_and_an_invitation_made_and_revoked(page):
    app, admin, plain, _ = page
    name = acc.ensure_account(plain).name
    _, device = acc.OwnfoilAccounts().issue(name, 'My console', ['library'])
    client = app.test_client()
    login(client, admin)
    body = client.get('/admin/lantern').get_data(as_text=True)
    assert 'My console' in body
    t = token(client)
    made = client.post('/admin/lantern', data={'action': 'invite', 'user': name, 'hours': '2', 'devices': '2',
                                               'note': 'for Sam', 'csrf': t}).get_data(as_text=True)
    code = re.search(r'Invitation made: ([A-Z0-9-]+)', made).group(1)
    assert code in client.get('/admin/lantern').get_data(as_text=True)
    invitation = acc.OwnfoilAccounts().invitations()[0]
    client.post('/admin/lantern', data={'action': 'revoke_invitation', 'id': invitation.id, 'csrf': t})
    assert acc.OwnfoilAccounts().invitations() == []
    client.post('/admin/lantern', data={'action': 'revoke', 'id': device.id, 'csrf': t})
    assert LanternDevice.query.count() == 0


def test_an_invitation_with_nonsense_is_refused(page):
    app, admin, plain, _ = page
    name = acc.ensure_account(plain).name
    client = app.test_client()
    login(client, admin)
    answer = client.post('/admin/lantern', data={'action': 'invite', 'user': name, 'hours': '99999', 'devices': '1',
                                                 'csrf': token(client)}).get_data(as_text=True)
    assert 'cannot be made' in answer and acc.OwnfoilAccounts().invitations() == []


def test_a_request_is_answered(page):
    app, admin, plain, _ = page
    name = acc.ensure_account(plain).name
    OwnfoilRequests().add(StoredRequest(id='r_1', user=name, platform='switch', title_id=TITLE, status='open',
                                        created='2026-11-07T10:00:00Z', name='Lumen Drift'))
    client = app.test_client()
    login(client, admin)
    assert 'Lumen Drift' in client.get('/admin/lantern').get_data(as_text=True)
    t = token(client)
    client.post('/admin/lantern', data={'action': 'decline', 'id': 'r_1', 'reason': 'not for sale', 'csrf': t})
    [request] = OwnfoilRequests().requests()
    assert (request.status, request.reason) == ('declined', 'not for sale')
    again = client.post('/admin/lantern', data={'action': 'done', 'id': 'r_1', 'csrf': t}).get_data(as_text=True)
    assert 'not open any more' in again

