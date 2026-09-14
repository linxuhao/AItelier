from uuid import uuid1, uuid4

import pytest

from contracts.director_messaging.v1.fake import InMemoryDirectorMessaging


@pytest.mark.parametrize("invalid_id", [
    "not-a-uuid",
    str(uuid1()),
    str(uuid4()).upper(),
])
def test_fake_rejects_noncanonical_non_v4_ids_from_supported_factory(invalid_id):
    provider = InMemoryDirectorMessaging(
        ["alpha", "beta"], "transport-a", id_factory=lambda: invalid_id)
    with pytest.raises(RuntimeError, match="lowercase canonical UUIDv4"):
        provider.send_director_message(
            "alpha", "director", "request", "subject", "body", "beta")
    assert provider.list_director_messages("beta")["result"]["items"] == []
