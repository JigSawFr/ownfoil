"""Requests for games (SPEC.md 11), kept in Ownfoil's database.

LanternApp does the protocol's work (one open request per title, the events, marking a request done when its
game arrives, erasing an account's requests when it is deleted); this is only where they are kept.
"""
from lantern.server.title_requests import StoredRequest

from db import LanternRequest, db


def _stored(row):
    return StoredRequest(id=row.id, user=row.user, platform=row.platform, title_id=row.title_id, status=row.status,
                         created=row.created, name=row.name, updated=row.updated, reason=row.reason)


class OwnfoilRequests:
    """Lantern's `RequestStore` over the `lantern_requests` table."""

    def requests(self, user=None):
        query = LanternRequest.query
        if user is not None:
            query = query.filter_by(user=user)
        try:
            return [_stored(r) for r in query.order_by(LanternRequest.created, LanternRequest.id).all()]
        finally:
            db.session.rollback()

    def add(self, request):
        row = db.session.get(LanternRequest, request.id) or LanternRequest(id=request.id)
        row.user, row.platform, row.title_id, row.status = request.user, request.platform, request.title_id, request.status
        row.created, row.name, row.updated, row.reason = request.created, request.name, request.updated, request.reason
        db.session.add(row)
        db.session.commit()

    def set_status(self, request_id, status, *, reason=None, updated=None):
        row = db.session.get(LanternRequest, request_id)
        if row is None:
            return None
        row.status, row.reason, row.updated = status, reason, updated
        db.session.commit()
        return _stored(row)

    def remove(self, user, request_id):
        row = db.session.get(LanternRequest, request_id)
        if row is None or row.user != user:
            return None
        stored = _stored(row)
        db.session.delete(row)
        db.session.commit()
        return stored

    def forget(self, user):
        LanternRequest.query.filter_by(user=user).delete(synchronize_session=False)
        db.session.commit()
