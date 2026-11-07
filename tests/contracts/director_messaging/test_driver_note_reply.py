"""A driver answers a project-less driver note by citing the DELIVERY it holds.

The entry point a driver actually holds is the delivery_id it was handed, not
the message_id. ``reply_to_delivery_id`` resolves that delivery ONLY among the
deliveries addressed to the calling driver, then takes the existing
``reply_to_message_id`` path with the delivery's message. Witnesses, through the
production entry points (``StateService`` and ``driver_inbox.send_driver_message``),
never a hand-built reply row:

1. Positive: a project-less note from ada to bob is answered by bob citing the
   DELIVERY id. The answer's thread is the cited message's thread, its
   reply_to_message_id is the cited message, one new delivery is for ada, none
   for bob.
2. Negative: a delivery addressed to another driver, an id present in no inbox,
   and a project-inbox delivery (state_director_deliveries) cited through the
   driver path each refuse with a named code and create nothing.
3. Guide: the rendered driver guide names both reply fields.
"""
import pytest

from core.drivers import DriverRegistry
from core.state_database import StateDatabase
from core.state_graph import StateGraphError
from core.state_service import StateService

PEPPER = "test-only-pepper-not-a-production-secret"


@pytest.fixture
def world(tmp_path):
    """Two projects, three drivers. ada is a member of alpha; bob and carol of beta.

    The project-less note below names no project at all; alpha and beta exist so
    the project-inbox refusal has a real project to address.
    """
    db = StateDatabase(str(tmp_path / "state.sqlite"))
    seed = StateService(db, actor="fixture", project_read_trusted=True)
    seed.create_project("alpha", "alpha")
    seed.create_project("beta", "beta")
    registry = DriverRegistry(db, PEPPER)
    for driver_id in ["ada", "bob", "carol"]:
        registry.register(driver_id, driver_id, actor="test")
    registry.set_membership("alpha", "ada", "member", 0, "fixture", actor="test")
    registry.set_membership("beta", "bob", "member", 0, "fixture", actor="test")
    registry.set_membership("beta", "carol", "member", 0, "fixture", actor="test")
    services = {driver_id: StateService(db, actor="driver:" + driver_id, driver_id=driver_id,
                                        project_read_trusted=True)
                for driver_id in ["ada", "bob", "carol"]}
    return db, services


def _counts(db):
    """(driver-inbox messages, driver-inbox deliveries, project-inbox deliveries)."""
    with db.get_connection() as conn:
        messages = conn.execute("SELECT COUNT(*) FROM driver_inbox_messages").fetchone()[0]
        driver = conn.execute("SELECT COUNT(*) FROM driver_inbox_deliveries").fetchone()[0]
        project = conn.execute("SELECT COUNT(*) FROM state_director_deliveries").fetchone()[0]
    return messages, driver, project


def _deliveries_for(db, driver_id):
    with db.get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM driver_inbox_deliveries WHERE target_driver_id=?",
                            (driver_id,)).fetchone()[0]


def _projectless_note(service, target, request_key="note"):
    """A driver-addressed note with NO project_id: the case the cited-message path
    could not reach from a delivery id alone."""
    return service.driver_inbox.send_driver_message(
        request_key, "peer note", "note body", target_driver_id=target)


def test_projectless_note_is_answered_in_thread_by_citing_the_delivery(world):
    db, services = world
    note = _projectless_note(services["ada"], "bob")
    cited_message = note["message_id"]
    cited_delivery = note["deliveries"][0]["delivery_id"]
    with db.get_connection() as conn:
        cited_thread = conn.execute("SELECT thread_id FROM driver_inbox_messages WHERE message_id=?",
                                    (cited_message,)).fetchone()[0]
    messages_before, deliveries_before, _ = _counts(db)
    ada_before = _deliveries_for(db, "ada")
    bob_before = _deliveries_for(db, "bob")

    answer = services["bob"].driver_inbox.send_driver_message(
        "answer-by-delivery", "re: peer note", "answer body", target_driver_id="ada",
        reply_to_delivery_id=cited_delivery)

    answer_id = answer["message_id"]
    with db.get_connection() as conn:
        row = conn.execute("SELECT thread_id,reply_to_message_id,sender_driver_id,project_id "
                           "FROM driver_inbox_messages WHERE message_id=?", (answer_id,)).fetchone()
    assert row["thread_id"] == cited_thread
    assert row["reply_to_message_id"] == cited_message
    assert row["sender_driver_id"] == "bob"
    assert row["project_id"] is None
    # Exactly one new message and one new delivery; it is addressed to ada, the
    # cited message's sender, and bob receives nothing new.
    messages_after, deliveries_after, _ = _counts(db)
    assert messages_after == messages_before + 1
    assert deliveries_after == deliveries_before + 1
    assert _deliveries_for(db, "ada") == ada_before + 1
    assert _deliveries_for(db, "bob") == bob_before


def test_refuses_delivery_addressed_to_another_driver(world):
    db, services = world
    # ada addresses carol. bob, who is not the addressee, cites it.
    note = _projectless_note(services["ada"], "carol")
    cited_delivery = note["deliveries"][0]["delivery_id"]
    before = _counts(db)
    with pytest.raises(StateGraphError) as refused:
        services["bob"].driver_inbox.send_driver_message(
            "wrong-addressee", "re", "body", target_driver_id="ada",
            reply_to_delivery_id=cited_delivery)
    assert str(refused.value).split(":", 1)[0].strip() == "unknown_delivery", str(refused.value)
    assert _counts(db) == before


def test_refuses_id_present_in_no_inbox(world):
    db, services = world
    before = _counts(db)
    with pytest.raises(StateGraphError) as refused:
        services["bob"].driver_inbox.send_driver_message(
            "no-such-delivery", "re", "body", target_driver_id="ada",
            reply_to_delivery_id="00000000-0000-4000-8000-000000000000")
    assert str(refused.value).split(":", 1)[0].strip() == "unknown_delivery", str(refused.value)
    assert _counts(db) == before


def test_refuses_project_inbox_delivery_cited_through_the_driver_path(world):
    db, services = world
    # ada, a member of alpha, sends a PROJECT-inbox message to beta. Its delivery
    # lives in state_director_deliveries, not in any driver inbox. bob cites that
    # delivery id through the DRIVER path. It must refuse, and must not fall back
    # to the project reply path (which would thread it into beta's project inbox).
    sent = services["ada"].director_messages.send_director_message(
        "alpha", "ada", "to-beta", "subject", "body", target_project_id="beta")
    project_delivery = sent["result"]["deliveries"][0]["delivery_id"]
    before = _counts(db)
    with pytest.raises(StateGraphError) as refused:
        services["bob"].driver_inbox.send_driver_message(
            "project-delivery-via-driver", "re", "body", target_driver_id="ada",
            reply_to_delivery_id=project_delivery)
    assert str(refused.value).split(":", 1)[0].strip() == "unknown_delivery", str(refused.value)
    assert _counts(db) == before


def test_refuses_both_reply_fields_rather_than_guessing(world):
    db, services = world
    note = _projectless_note(services["ada"], "bob")
    before = _counts(db)
    with pytest.raises(StateGraphError) as refused:
        services["bob"].driver_inbox.send_driver_message(
            "both-fields", "re", "body", target_driver_id="ada",
            reply_to_message_id=note["message_id"],
            reply_to_delivery_id=note["deliveries"][0]["delivery_id"])
    assert str(refused.value).split(":", 1)[0].strip() == "invalid_request", str(refused.value)
    assert _counts(db) == before


def test_driver_guide_names_both_reply_fields():
    from core.state_driver_guide import STATE_DRIVER_GUIDE

    assert "reply_to_message_id" in STATE_DRIVER_GUIDE
    assert "reply_to_delivery_id" in STATE_DRIVER_GUIDE
