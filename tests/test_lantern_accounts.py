"""Lantern's accounts in Ownfoil's database: the SDK's contract for a store of accounts, then what is
Ownfoil's own - accounts tied to Ownfoil users, capped by their access, going with them."""
import pytest
from lantern.server.contracts import ACCOUNTS_CONTRACT
from lantern.server.accounts import AccountError

from db import LanternAccount, LanternDevice, User, db
from lantern_server import accounts as acc
from lantern_server.accounts import OwnfoilAccounts

from app import create_app


@pytest.fixture
def store(tmp_path):
    app = create_app(f"sqlite:///{tmp_path/'test.db'}")
    ctx = app.app_context()
    ctx.push()
    db.create_all()
    yield OwnfoilAccounts()
    db.session.remove()
    ctx.pop()


def make_user(name='Léa Martin', **access):
    user = User(user=name, password='x', admin_access=access.get('admin', False),
                shop_access=access.get('shop', False), backup_access=access.get('backup', False))
    db.session.add(user)
    db.session.commit()
    return user


@pytest.mark.parametrize('check', ACCOUNTS_CONTRACT, ids=lambda c: c.__name__)
def test_the_contract_every_store_of_accounts_passes(tmp_path, check):
    app = create_app(f"sqlite:///{tmp_path/'contract.db'}")
    with app.app_context():
        db.create_all()
        check(OwnfoilAccounts())


def test_what_each_kind_of_access_allows(store):
    assert acc.allowed_scopes(make_user('a')) == ()
    assert acc.allowed_scopes(make_user('b', shop=True)) == ('library', 'cheats', 'requests')
    assert acc.allowed_scopes(make_user('c', backup=True)) == ('saves', 'sync.read', 'sync.write')
    assert 'admin' in acc.allowed_scopes(make_user('d', admin=True))
    assert acc.allowed_scopes(make_user('e', shop=True, backup=True)) == (
        'library', 'saves', 'cheats', 'requests', 'sync.read', 'sync.write')


def test_an_ownfoil_user_gets_one_account_named_for_them(store):
    user = make_user('Léa Martin', shop=True)
    account = acc.ensure_account(user)
    assert account.name == 'L_a_Martin' and acc.ensure_account(user).id == account.id
    assert store.user('L_a_Martin').scopes == ('library', 'cheats', 'requests')


def test_two_users_whose_names_become_the_same_get_two_accounts(store):
    first = acc.ensure_account(make_user('Léa', shop=True))
    second = acc.ensure_account(make_user('L_a', shop=True))
    assert first.name == 'L_a' and second.name != first.name


def test_an_account_is_capped_by_its_users_access(store):
    user = make_user('lea', shop=True, backup=True)
    name = acc.ensure_account(user).name
    token, _ = store.issue(name, 'console', ['library', 'saves'])
    assert store.device_of(token).scopes == ('library', 'saves')
    user.backup_access = False
    db.session.commit()
    assert store.user(name).scopes == ('library', 'cheats', 'requests')
    assert store.device_of(token).scopes == ('library',)
    user.backup_access = True
    db.session.commit()
    assert store.device_of(token).scopes == ('library', 'saves')  # nothing was lost, only held back


def test_deleting_the_user_takes_the_account_and_its_devices(store):
    user = make_user('lea', shop=True)
    name = acc.ensure_account(user).name
    token, _ = store.issue(name, 'console', ['library'])
    store.create_invitation(name, ['library'], '2099-01-01T00:00:00Z', 1)
    db.session.delete(user)
    db.session.commit()
    assert store.user(name) is None and store.device_of(token) is None
    assert store.invitations() == [] and LanternDevice.query.count() == 0


def test_an_account_of_an_ownfoil_user_is_not_deleted_through_lantern(store):
    name = acc.ensure_account(make_user('lea', shop=True)).name
    with pytest.raises(AccountError) as refused:
        store.delete_user(name)
    assert refused.value.status == 409 and store.user(name) is not None


def test_only_the_hash_of_a_token_is_kept(store):
    store.create_user('lea', ['library'])
    token, _ = store.issue('lea', 'console', ['library'])
    assert token not in LanternDevice.query.one().token_hash
    assert LanternDevice.query.one().token_hash == acc.token_hash(token)


def test_what_is_kept_survives_a_new_store(store):
    store.create_user('lea', ['library'])
    token, device = store.issue('lea', 'console', ['library'], key=b'k' * 32)
    db.session.remove()
    again = OwnfoilAccounts().device_of(token)
    assert again.id == device.id and again.key == b'k' * 32
