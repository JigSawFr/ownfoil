"""Requests for games, kept in Ownfoil's database: the SDK's contract, then the protocol through Ownfoil."""
import pytest
from lantern.server.contracts import REQUESTS_CONTRACT

from db import LanternRequest, User, db
from lantern_server import accounts as acc, service
from lantern_server.requests import OwnfoilRequests
from settings import get_settings

from app import create_app

SECURE = {'base_url': 'https://shop.example.org', 'environ_overrides': {'REMOTE_ADDR': '203.0.113.9'}}
TITLE = '0100AB1200010000'


@pytest.mark.parametrize('check', REQUESTS_CONTRACT, ids=lambda c: c.__name__)
def test_the_contract_every_store_of_requests_passes(tmp_path, check):
    app = create_app(f"sqlite:///{tmp_path/'contract.db'}")
    with app.app_context():
        db.create_all()
        check(OwnfoilRequests())


def _settings():
    settings = dict(get_settings())
    settings['lantern'] = {**settings['lantern'], 'enabled': True, 'https_port': 0}
    return settings


@pytest.fixture
def shop(tmp_path, monkeypatch):
    app = create_app(f"sqlite:///{tmp_path/'test.db'}")
    ctx = app.app_context()
    ctx.push()
    db.create_all()
    monkeypatch.setattr(service, 'get_settings', _settings)
    service.init_app(app)
    service._state['key'] = None
    yield app
    db.session.remove()
    ctx.pop()


def token_for(name, admin=False):
    user = User(user=name, password='x', admin_access=admin, shop_access=True, backup_access=False)
    db.session.add(user)
    db.session.commit()
    account = acc.ensure_account(user).name
    token, _ = acc.OwnfoilAccounts().issue(account, 'console', acc.allowed_scopes(user))
    return user, {'Authorization': 'Bearer ' + token, 'Lantern-Platform': 'switch'}


def test_a_device_asks_for_a_game_and_the_admin_decides(shop):
    lea, asking = token_for('lea')
    _, deciding = token_for('root', admin=True)
    client = shop.test_client()
    made = client.post('/api/lantern/v1/requests', json={'schema': 1, 'platform': 'switch', 'title_id': TITLE,
                                                         'name': 'Lumen Drift'}, headers=asking, **SECURE)
    assert made.status_code == 201, made.data
    request_id = made.get_json()['id']
    assert [r['status'] for r in client.get('/api/lantern/v1/requests', headers=asking, **SECURE).get_json()['requests']] == ['open']
    asked_again = client.post('/api/lantern/v1/requests', json={'schema': 1, 'platform': 'switch', 'title_id': TITLE},
                              headers=asking, **SECURE)
    assert asked_again.get_json()['id'] == request_id  # the same game asked twice is one request
    decided = client.post(f'/api/lantern/v1/admin/requests/{request_id}',
                          json={'schema': 1, 'decision': 'decline', 'reason': 'not for sale'}, headers=deciding, **SECURE)
    assert decided.status_code in (200, 204), decided.data
    row = db.session.get(LanternRequest, request_id)
    assert (row.status, row.reason) == ('declined', 'not for sale')


def test_the_requests_go_with_their_user(shop):
    lea, asking = token_for('lea')
    shop.test_client().post('/api/lantern/v1/requests', json={'schema': 1, 'platform': 'switch', 'title_id': TITLE},
                            headers=asking, **SECURE)
    assert LanternRequest.query.count() == 1
    service.forget_user(lea.id)
    assert LanternRequest.query.count() == 0
