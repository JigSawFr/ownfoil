"""Save backups: kept in a folder of the config for the accounts with backup access, and gone with their user."""
import base64
import hashlib
import io
import zipfile

import pytest

from db import LanternAccount, User, db
from lantern_server import accounts as acc, pairing_page, service
from settings import get_settings
from lantern.server.saves import FileSaveStore

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
    ctx = app.app_context()
    ctx.push()
    db.create_all()
    monkeypatch.setattr(service, 'get_settings', _settings)
    monkeypatch.setitem(service._stores, 'saves', FileSaveStore(tmp_path / 'saves'))
    service.init_app(app)
    service._state['key'] = None
    yield app
    db.session.remove()
    ctx.pop()


def token_for(name, **access):
    user = User(user=name, password='x', admin_access=False, shop_access=access.get('shop', True),
                backup_access=access.get('backup', False))
    db.session.add(user)
    db.session.commit()
    account = acc.ensure_account(user).name
    token, _ = acc.OwnfoilAccounts().issue(account, 'console', acc.allowed_scopes(user))
    return user, account, {'Authorization': 'Bearer ' + token}


def test_the_saves_family_is_offered_to_backup_access_only(shop):
    _, _, backup = token_for('lea', backup=True)
    _, _, shopper = token_for('ada')
    client = shop.test_client()
    mine = client.get('/.well-known/lantern', headers=backup, **SECURE).get_json()
    theirs = client.get('/.well-known/lantern', headers=shopper, **SECURE).get_json()
    assert mine['features']['saves'] == 1 and theirs['features']['saves'] == 0


def archive():
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as z:
        z.writestr('save.dat', b'save bytes' * 10)
    return out.getvalue()


def back_up(client, headers, data):
    digest = 'sha-256=:' + base64.b64encode(hashlib.sha256(data).digest()).decode() + ':'
    return client.post('/api/lantern/v1/saves/0100AB1200010000', data=data,
                       headers={**headers, 'Content-Digest': digest, 'Content-Type': 'application/zip',
                                'Lantern-Platform': 'switch', 'Lantern-Save-Kind': 'device'}, **SECURE)


def test_a_save_is_backed_up_and_listed_for_its_account_only(shop):
    _, _, lea = token_for('lea', backup=True)
    _, _, mia = token_for('mia', backup=True)
    data = archive()
    client = shop.test_client()
    sent = back_up(client, lea, data)
    assert sent.status_code == 201, sent.data
    listed = client.get('/api/lantern/v1/saves?platform=switch', headers=lea, **SECURE).get_json()
    assert [s['sha256'] for s in listed['saves']] == [hashlib.sha256(data).hexdigest()]
    others = client.get('/api/lantern/v1/saves?platform=switch', headers=mia, **SECURE).get_json()
    assert others['saves'] == []


def test_a_save_without_the_right_scope_is_refused(shop):
    _, _, shopper = token_for('ada')
    assert back_up(shop.test_client(), shopper, archive()).status_code in (403, 404, 405)


def test_the_backups_go_with_their_user(shop):
    user, account, lea = token_for('lea', backup=True)
    assert back_up(shop.test_client(), lea, archive()).status_code == 201
    assert service.saves().saves(account)
    service.forget_user(user.id)
    assert service.saves().saves(account) == []
