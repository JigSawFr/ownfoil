"""The install's identity as Lantern writes it."""
import uuid

from lantern.uid import check_uid

from settings import get_server_uid


def lantern_uid():
    """The server's uid in Lantern's form, a lower-case UUID with its dashes (SPEC.md 4.2).

    Ownfoil keeps the same random UUID as 32 hex digits, which its own handshake and LAN discovery send as
    they are; this is that value, written the way Lantern wants it, so both name one server."""
    return check_uid(str(uuid.UUID(get_server_uid())))
