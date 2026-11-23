"""A request's identity is kept for ONE get_service block, never longer (api.authz).

get_service resolves the identity and the driver-token lookup behind it once per
request (api.authz.identity_resolved_once). These pin the scope: a token revoked or
suspended between two requests stops authorizing at the next one, nothing is kept
outside a block, and two requests with different tokens never share an identity.
"""
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from api import authz
from api import state_graph_routers as routes
from core.db_manager import DBManager
from core.drivers import DriverRegistry


@pytest.fixture
def world(tmp_path, monkeypatch):
    db = DBManager(str(tmp_path / "state.db"))
    registry = DriverRegistry(db, "identity-scope-test-pepper-not-a-secret")
    tokens = {did: registry.register(did, did, actor="test")["token"] for did in ("ada", "bob")}
    lookups = []
    lookup = registry.lookup_token
    monkeypatch.setattr(registry, "lookup_token", lambda token: lookups.append(token) or lookup(token))
    monkeypatch.setattr(authz, "driver_registry", lambda: registry)
    # The write gate on, as in production: trust then follows the token.
    monkeypatch.setattr(authz, "gate_enabled", lambda: True)
    return SimpleNamespace(db=db, registry=registry, tokens=tokens, lookups=lookups)


def request_with(token):
    app = SimpleNamespace(state=SimpleNamespace(_test_mode=False), dependency_overrides={})
    return Request({"type": "http", "method": "GET", "path": "/api/state/query/get_attempt",
                    "headers": [(authz.DRIVER_TOKEN_HEADER.lower().encode(), token.encode())],
                    "query_string": b"", "app": app})


def test_a_token_suspended_between_two_requests_stops_authorizing(world):
    first = routes.get_service(request_with(world.tokens["ada"]), world.db, None)
    assert first.store.project_read_trusted is True
    assert first.driver_id == "ada"
    assert len(world.lookups) == 1, "one token lookup per request"

    revision = world.registry.get("ada")["revision"]
    world.registry.set_status("ada", "suspended", revision, "test", actor="test")
    second = routes.get_service(request_with(world.tokens["ada"]), world.db, None)
    assert len(world.lookups) == 2, "the second request must look the token up again"
    assert second.store.project_read_trusted is False
    assert second.driver_id is None


def test_outside_a_block_nothing_is_kept(world):
    request = request_with(world.tokens["ada"])
    assert authz.request_identity(request).driver_id == "ada"
    assert authz.request_identity(request).driver_id == "ada"
    assert len(world.lookups) == 2, "outside identity_resolved_once every call resolves anew"


def test_two_requests_with_different_tokens_get_their_own_identity(world):
    ada = routes.get_service(request_with(world.tokens["ada"]), world.db, None)
    bob = routes.get_service(request_with(world.tokens["bob"]), world.db, None)
    assert (ada.driver_id, bob.driver_id) == ("ada", "bob")
    with authz.identity_resolved_once():
        # One block, two requests: each still gets its own credential's identity.
        assert authz.request_identity(request_with(world.tokens["ada"])).driver_id == "ada"
        assert authz.request_identity(request_with(world.tokens["bob"])).driver_id == "bob"
