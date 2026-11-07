"""Lantern's accounts, devices and invitations, kept in Ownfoil's database (SPEC.md 5.1, 5.5, 5.7).

An Ownfoil user has a Lantern account, made when they first pair a device. It is capped by what the user may
do (their shop, backup and admin access) and goes with them when they are deleted. Accounts made through
Lantern's administration API have no Ownfoil user: their scopes are exactly what they were given.

What is kept of a token is its hash. Everything here runs inside the Flask app context.
"""
import functools
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta, timezone

from lantern.models import RatingCap
from lantern.server.accounts import (ACCOUNT_NAME, INVITATION_SYMBOLS, SCOPES, AccountError, Device, Invitation,
                                     User as LanternUser, new_code, new_device_id, new_token, ordered_scopes,
                                     parse_time, token_hash, typed_code, utc)
from sqlalchemy.exc import IntegrityError

from db import LanternAccount, LanternDevice, LanternInvitation, User, db

# What each kind of Ownfoil access lets a Lantern account do.
SHOP_SCOPES = ('library', 'cheats', 'requests')
BACKUP_SCOPES = ('saves', 'sync.read', 'sync.write')


def allowed_scopes(user):
    """The scopes an Ownfoil user's access allows."""
    if user.has_admin_access():
        return ordered_scopes(SCOPES)
    scopes = set()
    if user.has_shop_access():
        scopes.update(SHOP_SCOPES)
    if user.has_backup_access():
        scopes.update(BACKUP_SCOPES)
    return ordered_scopes(scopes)


def account_name_for(user):
    """A Lantern account name (SPEC.md 5.7) for an Ownfoil user name, unique among the accounts."""
    base = re.sub(r'[^A-Za-z0-9._-]', '_', user.user or '')[:56] or 'user'
    name = base
    if LanternAccount.query.filter_by(name=name).first() is not None:
        name = f'{base}-{user.id}'
    return name


def ensure_account(user):
    """The Lantern account of an Ownfoil user, made with the user's allowed scopes if they have none yet."""
    account = LanternAccount.query.filter_by(user_id=user.id).first()
    if account is None:
        account = LanternAccount(name=account_name_for(user), user_id=user.id,
                                 scopes=','.join(allowed_scopes(user)), state='active', created=utc())
        db.session.add(account)
        db.session.commit()
    return account


def _scopes(text):
    return tuple(s for s in (text or '').split(',') if s)


def _cap(text):
    return RatingCap.from_json(json.loads(text)) if text else None


def _cap_text(cap):
    return json.dumps(cap.to_json()) if cap is not None else None


def _effective(account):
    """An account's scopes: those it was given, within what its Ownfoil user may do."""
    given = ordered_scopes(_scopes(account.scopes))
    if account.user is None:
        return given
    return tuple(s for s in given if s in allowed_scopes(account.user))


def _user(account):
    return LanternUser(name=account.name, scopes=_effective(account), state=account.state, created=account.created)


def _device(row):
    cut = _effective(row.account)
    return Device(id=row.id, token_hash=row.token_hash, user=row.account.name,
                  device=row.name, scopes=tuple(s for s in ordered_scopes(_scopes(row.scopes)) if s in cut),
                  created=row.created, key=row.key, rating_cap=_cap(row.rating_cap),
                  invitation=row.invitation, expires=row.expires, last_used=row.last_used)


def _invitation(row):
    return Invitation(id=row.id, code=row.code, user=row.account.name, scopes=ordered_scopes(_scopes(row.scopes)),
                      expires=row.expires, devices=row.devices, used=row.used, rating_cap=_cap(row.rating_cap),
                      note=row.note, created=row.created)


def _live(expires, now=None):
    if expires is None:
        return True
    until = parse_time(expires)
    return until is not None and until > (now or datetime.now(timezone.utc))


def reading(method):
    """A read of the accounts ends its transaction: a stream of events asks about its token for as long
    as it lasts, and a transaction left open would hold SQLite's write lock off for all that time."""
    @functools.wraps(method)
    def read(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        finally:
            db.session.rollback()
    return read


class OwnfoilAccounts:
    """Lantern's `Accounts` over Ownfoil's database."""

    def _account(self, name):
        return LanternAccount.query.filter_by(name=name).first()

    # Accounts.

    @reading
    def user(self, name):
        account = self._account(name)
        return _user(account) if account is not None else None

    @reading
    def users(self):
        return [_user(a) for a in LanternAccount.query.order_by(LanternAccount.name).all()]

    def create_user(self, name, scopes, state='active'):
        if not ACCOUNT_NAME.match(name):
            raise AccountError(400, 'bad_request', "name is 1 to 64 letters, digits, '.', '_' and '-'.")
        account = LanternAccount(name=name, scopes=','.join(ordered_scopes(scopes)), state=state, created=utc())
        db.session.add(account)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            raise AccountError(409, 'conflict', f'An account named {name} exists.')
        return _user(account)

    def update_user(self, name, *, state=None, scopes=None):
        account = self._account(name)
        if account is None:
            raise AccountError(404, 'not_found', 'No such account.')
        if state is not None:
            account.state = state
        if scopes is not None:
            account.scopes = ','.join(ordered_scopes(scopes))
            for device in account.devices:
                device.scopes = ','.join(s for s in _scopes(device.scopes) if s in _scopes(account.scopes))
        db.session.commit()
        return _user(account)

    def delete_user(self, name):
        account = self._account(name)
        if account is None:
            raise AccountError(404, 'not_found', 'No such account.')
        if account.user is not None:
            raise AccountError(409, 'conflict', 'This account belongs to an Ownfoil user: delete the user instead.')
        db.session.delete(account)
        db.session.commit()

    # Devices.

    @reading
    def device_of(self, token):
        row = LanternDevice.query.filter_by(token_hash=token_hash(token)).first()
        if row is None or not _live(row.expires):
            return None
        return _device(row)

    def issue(self, user, device, scopes, *, key=None, rating_cap=None, invitation=None, lifetime=None):
        account = self._account(user)
        if account is None:
            raise AccountError(404, 'not_found', 'No such account.')
        token = new_token()
        row = LanternDevice(
            id=new_device_id(), account_id=account.id, name=device, token_hash=token_hash(token),
            scopes=','.join(ordered_scopes(scopes)), key=key, rating_cap=_cap_text(rating_cap),
            invitation=invitation, created=utc(),
            expires=utc(datetime.now(timezone.utc) + timedelta(seconds=lifetime)) if lifetime else None)
        db.session.add(row)
        db.session.commit()
        return token, _device(row)

    @reading
    def devices(self, user=None):
        query = LanternDevice.query.join(LanternAccount)
        if user is not None:
            query = query.filter(LanternAccount.name == user)
        rows = [r for r in query.all() if _live(r.expires)]
        return [_device(r) for r in sorted(rows, key=lambda r: (r.account.name, r.created, r.id))]

    def revoke(self, device_id):
        row = db.session.get(LanternDevice, device_id)
        if row is None:
            return False
        db.session.delete(row)
        db.session.commit()
        return True

    # Invitations.

    def create_invitation(self, user, scopes, expires, devices, *, rating_cap=None, note=None):
        account = self._account(user)
        if account is None:
            raise AccountError(404, 'not_found', 'No such account.')
        while True:
            code = new_code(INVITATION_SYMBOLS)
            if LanternInvitation.query.filter_by(code_key=typed_code(code)).first() is None:
                break
        row = LanternInvitation(
            id='i_' + secrets.token_urlsafe(9), account_id=account.id, code=code, code_key=typed_code(code),
            scopes=','.join(ordered_scopes(scopes)), expires=expires, devices=devices,
            rating_cap=_cap_text(rating_cap), note=note, created=utc())
        db.session.add(row)
        db.session.commit()
        return _invitation(row)

    @reading
    def invitations(self):
        rows = [r for r in LanternInvitation.query.all() if _invitation(r).redeemable()]
        return [_invitation(r) for r in sorted(rows, key=lambda r: (r.created, r.id))]

    def revoke_invitation(self, invitation_id):
        row = db.session.get(LanternInvitation, invitation_id)
        if row is None:
            return False
        db.session.delete(row)
        db.session.commit()
        return True

    def redeem(self, code):
        typed = typed_code(code)
        row = LanternInvitation.query.filter_by(code_key=typed).first()
        if row is None or not hmac.compare_digest(row.code_key, typed) or not _invitation(row).redeemable():
            return None
        # Counted in one statement, so that devices redeeming it together never pass `devices`.
        counted = (LanternInvitation.query.filter(LanternInvitation.id == row.id, LanternInvitation.used < row.devices)
                   .update({LanternInvitation.used: LanternInvitation.used + 1}, synchronize_session=False))
        db.session.commit()
        if counted != 1:
            return None
        db.session.refresh(row)
        return _invitation(row)
