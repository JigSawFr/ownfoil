"""The Lantern SDK is installed and Ownfoil's identity fits its rules."""
import uuid

import pytest
from lantern._version import SPEC_DRAFT
from lantern.uid import is_valid_uid

from lantern_server.identity import lantern_uid
from settings import get_server_uid


def test_the_sdk_follows_a_known_draft():
    assert SPEC_DRAFT == "0.11"


def test_the_servers_uid_is_a_valid_lantern_uid():
    uid = lantern_uid()
    assert is_valid_uid(uid)
    assert uuid.UUID(uid).hex == get_server_uid()


def test_a_uid_that_is_not_a_random_uuid_is_refused(monkeypatch):
    monkeypatch.setattr("lantern_server.identity.get_server_uid", lambda: uuid.uuid1().hex)
    with pytest.raises(ValueError):
        lantern_uid()
