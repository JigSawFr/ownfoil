"""Pairing a device through Ownfoil's own sign-in: the approval page, and the token it ends with."""
import re
import time

import pytest

from db import LanternAccount, LanternDevice, User, db
from lantern_server import pairing_page, service
from settings import get_settings

from app import create_app

SECURE = {'base_url': 'https://shop.example.org', 'environ_overrides': {'REMOTE_ADDR': '203.0.113.9'}}


def _settings():
    settings = dict(get_settings())
    settings['lantern'] = {**settings['lantern'], 'enabled': True, 'https_port': 0}
    settings['shop'] = {**settings['shop'], 'public': False}
    return settings


@pytest.fixture
def shop(tmp_path, monkeypatch):
    app = create_app(f"sqlite:///{tmp_path/'test.db'}")
    app.config['SECRET_KEY'] = 'test'
    ctx = app.app_context()
    ctx.push()
    db.create_all()
    monkeypatch.setattr(service, 'get_settings', _settings)
    monkeypatch.setattr(pairing_page.ATTEMPTS, 'count', 1000)
    service.init_app(app)
    service._state['key'] = None
    # The login page the sign-in link points at is not under test.
    yield app
    db.session.remove()
    ctx.pop()


def user(name='lea', **access):
    row = User(user=name, password='x', admin_access=False, shop_access=access.get('shop', True),
               backup_access=access.get('backup', False))
    db.session.add(row)
    db.session.commit()
    return row


def sign_in(client, row):
    with client.session_transaction(base_url=SECURE['base_url']) as session:
        session['_user_id'] = str(row.id)
        session['_fresh'] = True


def start(client, scopes=('library',)):
    response = client.post('/api/lantern/v1/pairing', json={'schema': 1, 'device': 'My console', 'scopes': list(scopes)},
                           **SECURE)
    assert response.status_code == 200, response.data
    return response.get_json()


def csrf(client, code):
    page = client.get(f'/pair?code={code}', **SECURE).get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', page).group(1)


def test_a_visitor_who_is_not_signed_in_approves_nothing(shop):
    client = shop.test_client()
    pairing = start(client)
    page = client.get(f"/pair?code={pairing['code']}", **SECURE)
    assert page.status_code == 200 and '/login?next=' in page.get_data(as_text=True)
    assert 'name="action"' not in page.get_data(as_text=True)
    refused = client.post('/pair', data={'code': pairing['code'], 'action': 'approve', 'csrf': 'x'}, **SECURE)
    assert 'Sign in' in refused.get_data(as_text=True) and LanternDevice.query.count() == 0


def test_a_user_without_access_cannot_pair_a_device(shop):
    client = shop.test_client()
    sign_in(client, user(shop=False))
    assert client.get('/pair', **SECURE).status_code == 403


def test_the_page_shows_the_key_and_the_code_form(shop):
    client = shop.test_client()
    sign_in(client, user())
    page = client.get('/pair?code=ABCD', **SECURE)
    body = page.get_data(as_text=True)
    assert page.status_code == 200 and 'value="ABCD"' in body and 'name="csrf"' in body
    assert page.headers['Content-Security-Policy'].startswith("default-src 'none'")


def test_a_device_is_paired_and_its_token_works_for_its_users_account(shop):
    client = shop.test_client()
    lea = user()
    sign_in(client, lea)
    pairing = start(client, scopes=('library', 'saves'))
    token_csrf = csrf(client, pairing['code'])
    decision = client.post('/pair', data={'code': pairing['code'], 'action': 'look', 'csrf': token_csrf}, **SECURE)
    assert 'My console' in decision.get_data(as_text=True) and 'name="action" value="approve"' in decision.get_data(as_text=True)
    done = client.post('/pair', data={'code': pairing['code'], 'action': 'approve', 'csrf': token_csrf}, **SECURE)
    assert done.status_code == 200 and 'library' in done.get_data(as_text=True)
    time.sleep(0.01)
    document = client.get(pairing['poll'], **SECURE)
    assert document.status_code == 200, document.data
    granted = document.get_json()
    assert granted['scopes'] == ['library']  # no backup access, so no saves
    account = LanternAccount.query.filter_by(user_id=lea.id).one()
    assert LanternDevice.query.one().account_id == account.id
    handshake = shop.test_client().get('/.well-known/lantern', headers={'Authorization': 'Bearer ' + granted['token']}, **SECURE)
    assert handshake.get_json()['account']['state'] == 'active'
    assert handshake.get_json()['account']['scopes'] == ['library']
    apps = shop.test_client().get('/api/lantern/v1/apps?platform=switch', headers={'Authorization': 'Bearer ' + granted['token']}, **SECURE)
    assert apps.status_code == 200


def test_a_device_can_be_refused(shop):
    client = shop.test_client()
    sign_in(client, user())
    pairing = start(client)
    token = csrf(client, pairing['code'])
    refused = client.post('/pair', data={'code': pairing['code'], 'action': 'refuse', 'csrf': token}, **SECURE)
    assert 'refused' in refused.get_data(as_text=True) and LanternDevice.query.count() == 0
    assert client.get(pairing['poll'], **SECURE).status_code == 410


def test_a_post_without_the_pages_token_is_refused(shop):
    client = shop.test_client()
    sign_in(client, user())
    pairing = start(client)
    csrf(client, pairing['code'])
    forged = client.post('/pair', data={'code': pairing['code'], 'action': 'approve', 'csrf': 'forged'}, **SECURE)
    assert forged.status_code == 400 and LanternDevice.query.count() == 0


def test_an_unknown_code_pairs_nothing(shop):
    client = shop.test_client()
    sign_in(client, user())
    token = csrf(client, 'X')
    missing = client.post('/pair', data={'code': 'NOPE-NOPE', 'action': 'approve', 'csrf': token}, **SECURE)
    assert missing.status_code == 404


def test_codes_cannot_be_guessed_at_speed(shop, monkeypatch):
    monkeypatch.setattr(pairing_page.ATTEMPTS, 'count', 2)
    pairing_page.ATTEMPTS._hits.clear()
    client = shop.test_client()
    sign_in(client, user())
    token = csrf(client, 'X')
    codes = [client.post('/pair', data={'code': 'AAAA-BBBB', 'action': 'look', 'csrf': token}, **SECURE).status_code
             for _ in range(4)]
    assert codes == [404, 404, 429, 429]


def test_the_shop_that_is_not_public_asks_for_a_token(shop):
    response = shop.test_client().get('/api/lantern/v1/apps?platform=switch', **SECURE)
    assert response.status_code == 401

