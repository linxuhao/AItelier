"""Standing-only selector for driver_postcompact_guidance (owner ruling 2026-10-15).

Real service, real generated schema and the real entry chain: standing_only=True
skips the broadcast transient query and its count and projects only the current
project standing plus the authenticated driver's own standing notices. The
typed optional default False preserves the original aggregate exactly. Every
guidance read below proves it mutated nothing.
"""

import pytest

from core.drivers import DriverRegistry
from core.state_commands import describe, execute
from core.state_database import StateDatabase
from core.state_graph import StateGraphError
from core.state_service import StateService


@pytest.fixture
def peers(tmp_path):
    db = StateDatabase(str(tmp_path / "state.sqlite"))
    seed = StateService(db, actor="fixture", project_read_trusted=True)
    seed.create_project("alpha", "alpha")
    seed.create_project("beta", "beta")
    registry = DriverRegistry(db, "test-only-pepper-not-a-production-secret")
    for did in ["ada", "bob", "carol"]:
        registry.register(did, did, actor="test")
    for project, drivers in [("alpha", ["ada"]), ("beta", ["bob", "carol"])]:
        for did in drivers:
            registry.set_membership(project, did, "member", 0, "fixture", actor="test")
    return registry, {did: StateService(db, actor="driver:" + did, driver_id=did,
                                        project_read_trusted=True)
                      for did in ["ada", "bob", "carol"]}


def send(s, **kw):
    return execute(s, "send_director_message", {"sender_project_id": "alpha",
        "target_project_id": "beta", "protocol_version": "v3", "director_identity": "ada",
        "request_key": "message", "subject": "notice", "body": "work", **kw}, allow_write=True)


def _snapshot(db):
    """Every table, read-only: the no-DB-mutation proof compares this whole map."""
    with db.get_connection() as conn:
        names = [r["name"] for r in
                 conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {n: [tuple(r) for r in conn.execute(f'SELECT * FROM "{n}"')] for n in names}


def test_default_false_keeps_aggregate_and_true_skips_broadcast_only(peers):
    _, s = peers
    send(s["ada"], ack_mode="broadcast", request_key="broadcast-1",
         subject="broadcast subject")
    send(s["ada"], delivery_mode="standing", request_key="standing-1",
         subject="project standing subject")
    s["ada"].driver_inbox.send_driver_message("ordinary", "ordinary subject",
                                              "ordinary body", target_driver_id="bob")
    s["ada"].driver_inbox.send_driver_message("private-standing", "private standing subject",
                                              "private standing body", target_driver_id="bob",
                                              delivery_mode="standing")
    before = _snapshot(s["bob"].db)

    default = execute(s["bob"], "driver_postcompact_guidance", {"project_id": "beta"})
    assert default["schema"] == "aitelier.driver-guidance.v1"
    assert default["standing_only"] is False
    assert default["driver_id"] == "bob"
    for marker in ("broadcast subject", "project standing subject", "private standing subject"):
        assert marker in default["projection"]
    assert "ordinary subject" not in default["projection"]

    standing_only = execute(s["bob"], "driver_postcompact_guidance",
                            {"project_id": "beta", "standing_only": True})
    assert standing_only["standing_only"] is True
    assert "project standing subject" in standing_only["projection"]
    assert "private standing subject" in standing_only["projection"]
    assert "broadcast subject" not in standing_only["projection"]
    assert "ordinary subject" not in standing_only["projection"]

    carol = execute(s["carol"], "driver_postcompact_guidance", {"project_id": "beta"})
    assert "broadcast subject" in carol["projection"]
    assert "private standing subject" not in carol["projection"]
    assert "ordinary subject" not in carol["projection"]
    carol_only = execute(s["carol"], "driver_postcompact_guidance",
                         {"project_id": "beta", "standing_only": True})
    assert "broadcast subject" not in carol_only["projection"]
    assert "private standing subject" not in carol_only["projection"]

    for envelope in (default, standing_only, carol, carol_only):
        assert envelope["included"] <= 8
        assert len(envelope["projection"]) <= 3000
        assert envelope["omitted"] >= 0
    assert _snapshot(s["bob"].db) == before


def test_standing_only_bounds_match_the_contract(peers):
    _, s = peers
    for i in range(12):
        send(s["ada"], delivery_mode="standing", request_key=f"standing-{i}",
             subject=f"standing subject {i}")
    guidance = execute(s["bob"], "driver_postcompact_guidance",
                       {"project_id": "beta", "standing_only": True})
    assert guidance["included"] == 8
    assert guidance["omitted"] == 4
    assert len(guidance["projection"]) <= 3000
    assert guidance["projection"].count("standing subject") == 8


def test_operation_schema_advertises_typed_optional_standing_only():
    operation = describe()["operations"]["driver_postcompact_guidance"]
    assert operation["mutates"] is False
    schema = operation["arguments"]
    assert schema["properties"]["standing_only"]["type"] == "boolean"
    assert schema["properties"]["standing_only"]["default"] is False
    assert "standing_only" not in schema.get("required", [])


def test_standing_only_is_typed_and_optional(peers):
    _, s = peers
    default = execute(s["bob"], "driver_postcompact_guidance", {"project_id": "beta"})
    assert default["standing_only"] is False
    with pytest.raises(StateGraphError):
        execute(s["bob"], "driver_postcompact_guidance",
                {"project_id": "beta", "standing_only": None})


def test_unauthenticated_and_owner_callers_keep_the_baseline_verdict(tmp_path):
    db = StateDatabase(str(tmp_path / "state.sqlite"))
    seed = StateService(db, actor="fixture", project_read_trusted=True)
    seed.create_project("beta", "beta")
    with pytest.raises(StateGraphError, match="private_guidance"):
        seed.driver_postcompact_guidance("beta")
    owner = StateService(db, actor="owner:human", project_read_trusted=True)
    result = owner.driver_postcompact_guidance("beta")
    assert result["driver_id"] is None and result["standing_only"] is False
    assert result["projection"] == "" and result["included"] == 0


def test_standing_only_never_queries_or_counts_broadcast_transients(peers, monkeypatch):
    _, s = peers
    for i in range(10):
        send(s["ada"], ack_mode="broadcast", request_key=f"broadcast-{i}",
             subject=f"broadcast subject {i}")
    send(s["ada"], delivery_mode="standing", request_key="standing-1",
         subject="project standing subject")
    send(s["ada"], request_key="ordinary-project", subject="ordinary project transient")
    s["ada"].driver_inbox.send_driver_message("own-standing", "own standing subject",
                                              "own standing body", target_driver_id="bob",
                                              delivery_mode="standing")
    s["ada"].driver_inbox.send_driver_message("own-ordinary", "own ordinary subject",
                                              "own ordinary body", target_driver_id="bob")
    s["ada"].driver_inbox.send_driver_message("carol-standing", "carol standing subject",
                                              "carol standing body", target_driver_id="carol",
                                              delivery_mode="standing")
    before = _snapshot(s["bob"].db)
    calls = []
    real = s["bob"].director_messages.list_director_messages

    def spy(*args, **kwargs):
        calls.append((kwargs.get("delivery_mode"), kwargs.get("ack_mode")))
        return real(*args, **kwargs)

    monkeypatch.setattr(s["bob"].director_messages, "list_director_messages", spy)
    only = execute(s["bob"], "driver_postcompact_guidance",
                   {"project_id": "beta", "standing_only": True})
    assert calls == [("standing", None)]
    assert only["included"] == 2 and only["omitted"] == 0
    assert "project standing subject" in only["projection"]
    assert "own standing subject" in only["projection"]
    for absent in ("broadcast subject", "ordinary project transient", "own ordinary subject",
                   "carol standing subject"):
        assert absent not in only["projection"]

    calls.clear()
    default = execute(s["bob"], "driver_postcompact_guidance", {"project_id": "beta"})
    assert calls == [("standing", None), ("transient", "broadcast")]
    # 1 project standing + 10 unacked broadcasts + 1 own standing, eight shown.
    assert default["included"] == 8 and default["omitted"] == 12 - 8
    assert "ordinary project transient" not in default["projection"]
    assert "carol standing subject" not in default["projection"]
    assert _snapshot(s["bob"].db) == before
