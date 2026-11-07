"""Sync data in Ownfoil's database: the SDK's contract for a store of sync data, then restarts and the
protocol through Ownfoil."""
import pytest
from lantern import sync as syncing
from lantern.server.contracts import SYNC_CONTRACT

from db import LanternSyncItem, User, db
from lantern_server import accounts as acc, service
from lantern_server.sync import OwnfoilSync
from settings import get_settings

from app import create_app

SECURE = {'base_url': 'https://shop.example.org', 'environ_overrides': {'REMOTE_ADDR': '203.0.113.9'}}


@pytest.mark.parametrize('check', SYNC_CONTRACT, ids=lambda c: c.__name__)
def test_the_contract_every_store_of_sync_data_passes(tmp_path, check):
    app = create_app(f"sqlite:///{tmp_path/'contract.db'}")
    with app.app_context():
        db.create_all()
        check(OwnfoilSync(limit=3))


def test_what_is_kept_survives_a_restart_and_forgetting_leaves_the_others(tmp_path):
    from datetime import datetime, timezone
    app = create_app(f"sqlite:///{tmp_path/'test.db'}")
    now = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
    with app.app_context():
        db.create_all()
        store = OwnfoilSync()
        generation, _, _ = store.merge('lea', 'wishes', store.overview('lea')[0],
                                       [syncing.Item('a', '2026-11-07T11:00:00.000Z', 1, False)], if_match=None,
                                       now=now, skew=300)
        store.merge('ada', 'wishes', store.overview('ada')[0],
                    [syncing.Item('b', '2026-11-07T11:00:00.000Z', 2, False)], if_match=None, now=now, skew=300)
        db.session.remove()
        again = OwnfoilSync()
        assert again.read('lea', 'wishes', None)[0] == generation
        assert [i.key for i in again.read('lea', 'wishes', None)[2]] == ['a']
        again.forget('lea')
        assert LanternSyncItem.query.filter_by(user='lea').count() == 0
        assert [i.key for i in again.read('ada', 'wishes', None)[2]] == ['b']


def _settings():
    settings = dict(get_settings())
    settings['lantern'] = {**settings['lantern'], 'enabled': True, 'https_port': 0}
    return settings


def test_a_wish_list_is_shared_between_the_devices_of_an_account(tmp_path, monkeypatch):
    app = create_app(f"sqlite:///{tmp_path/'test.db'}")
    with app.app_context():
        db.create_all()
        monkeypatch.setattr(service, 'get_settings', _settings)
        service.init_app(app)
        service._state['key'] = None
        user = User(user='lea', password='x', admin_access=False, shop_access=True, backup_access=True)
        db.session.add(user)
        db.session.commit()
        account = acc.ensure_account(user).name
        one = {'Authorization': 'Bearer ' + acc.OwnfoilAccounts().issue(account, 'one', acc.allowed_scopes(user))[0]}
        two = {'Authorization': 'Bearer ' + acc.OwnfoilAccounts().issue(account, 'two', acc.allowed_scopes(user))[0]}
        client = app.test_client()
        overview = client.get('/api/lantern/v1/sync', headers=one, **SECURE)
        assert overview.status_code == 200, overview.data
        generation = overview.get_json()['generation']
        put = client.patch('/api/lantern/v1/sync/wishes', headers=one, **SECURE, json={
            'schema': 1, 'generation': generation,
            'items': [{'key': 'switch/0100AB1200010000', 'updated': '2026-11-07T11:00:00.000Z',
                       'value': {'platform': 'switch', 'title_id': '0100AB1200010000', 'note': 'soon'}}]})
        assert put.status_code == 200, put.data
        seen = client.get('/api/lantern/v1/sync/wishes', headers=two, **SECURE).get_json()
        assert [i['key'] for i in seen['items']] == ['switch/0100AB1200010000']
