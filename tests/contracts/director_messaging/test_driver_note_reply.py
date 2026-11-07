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


def test_reply_to_a_reply_keeps_the_root_thread_not_the_message_id(world):
    """The positive case above has thread_id == message_id on the cited note, so an
    implementation that set thread_id = reply_to_message_id would pass it too. Here
    the cited message is ITSELF a reply, so the root thread (the note) and the
    cited reply's own id are different, and the final answer must carry the root."""
    db, services = world
    note = _projectless_note(services["ada"], "bob")
    note_message = note["message_id"]
    with db.get_connection() as conn:
        root_thread = conn.execute("SELECT thread_id FROM driver_inbox_messages WHERE message_id=?",
                                   (note_message,)).fetchone()[0]
    # bob answers the note by citing its delivery: answer A sits on the note's thread.
    answer = services["bob"].driver_inbox.send_driver_message(
        "answer-to-note", "re: peer note", "first answer", target_driver_id="ada",
        reply_to_delivery_id=note["deliveries"][0]["delivery_id"])
    answer_message = answer["message_id"]
    answer_delivery = answer["deliveries"][0]["delivery_id"]
    assert answer_message != root_thread
    # ada answers THAT reply by citing the delivery bob's answer was delivered as.
    followup = services["ada"].driver_inbox.send_driver_message(
        "followup-to-answer", "re: re: peer note", "second answer", target_driver_id="bob",
        reply_to_delivery_id=answer_delivery)
    with db.get_connection() as conn:
        row = conn.execute("SELECT thread_id,reply_to_message_id FROM driver_inbox_messages "
                           "WHERE message_id=?", (followup["message_id"],)).fetchone()
    assert row["thread_id"] == root_thread
    assert row["reply_to_message_id"] == answer_message
    assert row["thread_id"] != row["reply_to_message_id"]


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


def test_the_command_entry_point_threads_a_reply_by_delivery_id(world):
    """Every witness above calls service.driver_inbox.send_driver_message directly,
    so nothing would notice if the field were missing from the SendDriverMessage
    model. Through `execute` the command model is validated with extra="forbid",
    so a missing field refuses every MCP caller. This drives the real command."""
    from core.state_commands import execute

    db, services = world
    note = _projectless_note(services["ada"], "bob")
    cited_message = note["message_id"]
    cited_delivery = note["deliveries"][0]["delivery_id"]
    with db.get_connection() as conn:
        cited_thread = conn.execute("SELECT thread_id FROM driver_inbox_messages WHERE message_id=?",
                                    (cited_message,)).fetchone()[0]
    result = execute(services["bob"], "send_driver_message",
                     {"request_key": "answer-via-command", "subject": "re: peer note",
                      "body": "answer body", "target_driver_id": "ada",
                      "reply_to_delivery_id": cited_delivery},
                     allow_write=True)
    answer_id = result["message_id"]
    with db.get_connection() as conn:
        row = conn.execute("SELECT thread_id,reply_to_message_id,sender_driver_id "
                           "FROM driver_inbox_messages WHERE message_id=?", (answer_id,)).fetchone()
    assert row["thread_id"] == cited_thread
    assert row["reply_to_message_id"] == cited_message
    assert row["sender_driver_id"] == "bob"


def test_driver_guide_names_both_reply_fields():
    """Scoped to the driver-inbox section. The guide already names
    reply_to_delivery_id and reply_to_message_id elsewhere (the project-inbox line
    and the send_user_message sentence), so a whole-guide containment check passes
    on the base tree and witnesses nothing. Slice the driver-inbox section only."""
    from core.state_driver_guide import STATE_DRIVER_GUIDE

    start = "Driver inbox (private; addressed to a driver):"
    end = "Private notebook ("
    assert STATE_DRIVER_GUIDE.count(start) == 1, "driver-inbox section marker moved"
    assert STATE_DRIVER_GUIDE.count(end) == 1, "section boundary moved"
    section = STATE_DRIVER_GUIDE[STATE_DRIVER_GUIDE.index(start):]
    section = section[:section.index(end)]
    assert "reply_to_delivery_id" in section
    assert "reply_to_message_id" in section
    assert "naming both fields is refused" in section
    assert "unknown_delivery" in section
