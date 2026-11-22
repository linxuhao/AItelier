"""Post-compaction guidance must carry the NEWEST rows, not the oldest eight.

The guidance block is a resume prompt. A standing rule delivered ten minutes ago
is one the driver has not worked under yet; the first eight deliveries of the
project are exactly what every earlier compaction already showed. So both
slices — the project inbox (standing + unacked broadcast transients) and the
driver's own standing notices — have to be selected from the NEWEST end, and the
3000-character budget has to give way at the OLD end.

Everything below runs the real entry chain (`StateService.driver_postcompact_guidance`
and `DriverInbox.active_standing()` over real rows); the only stand-in is a
ticking clock, so the guidance's `(created_at, message_id)` ordering is decided
by delivery order rather than by a uuid4 tie.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from core.director_messaging import SQLiteDirectorMessaging
from core.drivers import DriverRegistry
from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_service import StateService
from tests.support import state_template


class _Ticking:
    """One strictly increasing instant per call, shared by both writers."""

    def __init__(self):
        self.instant = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def stamp(self):
        self.instant += timedelta(seconds=1)
        return self.instant

    def isoformat(self):
        return self.stamp().isoformat()


@pytest.fixture
def services(tmp_path, monkeypatch, state_db_copy):
    db = StateDatabase(state_db_copy(state_template.state_registry, tmp_path / "state.sqlite"))
    seed = StateService(db, actor="fixture", project_read_trusted=True)
    seed.create_project("alpha", "alpha")
    seed.create_project("beta", "beta")
    registry = DriverRegistry(db, "test-only-pepper-not-a-production-secret")
    for driver_id in ("ada", "bob"):
        registry.register(driver_id, driver_id, actor="test")
    for project, members in (("alpha", ["ada"]), ("beta", ["bob"])):
        for driver_id in members:
            registry.set_membership(project, driver_id, "member", 0, "fixture", actor="test")
    clock = _Ticking()
    services = {driver_id: StateService(db, actor="driver:" + driver_id, driver_id=driver_id,
                                        project_read_trusted=True)
                for driver_id in ("ada", "bob")}
    # The project mailbox stamps created_at from the sender's injected clock, the
    # driver inbox from `core.driver_inbox.now`; one shared tick puts both
    # sources on a single timeline in delivery order.
    services["ada"].director_messages = SQLiteDirectorMessaging(
        services["ada"].store, "driver:ada", clock=clock.stamp,
        project_read_trusted=True, driver_id="ada")
    monkeypatch.setattr("core.driver_inbox.now", clock.isoformat)
    return services


def send(service, *, request_key, subject, body="work", **kwargs):
    """Target `beta` from `alpha` and insist the write actually happened."""
    envelope = execute(service, "send_director_message", {
        "sender_project_id": "alpha", "target_project_id": "beta",
        "protocol_version": "v3", "director_identity": "ada",
        "request_key": request_key, "subject": subject, "body": body, **kwargs,
    }, allow_write=True)
    assert envelope["schema"] == "aitelier.director-messaging.v3", envelope
    return envelope["result"]


def notify(service, *, request_key, subject, body="own body"):
    """One standing notice into another driver's private inbox."""
    sent = service.driver_inbox.send_driver_message(
        request_key, subject, body, target_driver_id="bob", delivery_mode="standing")
    assert sent["deliveries"][0]["seq"] >= 1, sent
    return sent


def projected_subjects(guidance):
    """The subjects the block carries, in the order it carries them."""
    return [json.loads(line)["subject"] for line in guidance["projection"].splitlines()]


def test_project_standing_slice_keeps_the_newest_eight(services):
    for index in range(12):
        send(services["ada"], request_key=f"standing-{index}",
             subject=f"standing-{index:02d}", delivery_mode="standing")

    guidance = services["bob"].driver_postcompact_guidance("beta", standing_only=True)

    assert guidance["included"] == 8
    assert guidance["omitted"] == 4
    # The newest eight, still in the block's deterministic ascending order.
    assert projected_subjects(guidance) == [f"standing-{index:02d}" for index in range(4, 12)]


def test_union_of_standing_and_unacked_broadcasts_keeps_the_newest_eight(services):
    for index in range(10):
        send(services["ada"], request_key=f"standing-{index}",
             subject=f"standing-{index:02d}", delivery_mode="standing")
    for index in range(10):
        send(services["ada"], request_key=f"broadcast-{index}",
             subject=f"broadcast-{index:02d}", ack_mode="broadcast")

    guidance = services["bob"].driver_postcompact_guidance("beta")

    assert guidance["included"] == 8
    assert guidance["omitted"] == 20 - 8
    # Both slices are drained to their own newest rows, then the union keeps its
    # tail: the ten oldest standing rules are gone, including the newest of what
    # an oldest-first read of either slice would have offered.
    assert projected_subjects(guidance) == [f"broadcast-{index:02d}" for index in range(2, 10)]


def test_driver_standing_notices_keep_the_newest_eight(services):
    for index in range(12):
        notify(services["ada"], request_key=f"own-{index}", subject=f"own-{index:02d}")

    inbox = services["bob"].driver_inbox.active_standing()

    assert set(inbox) == {"items", "matched_total"}
    assert inbox["matched_total"] == 12
    # Newest eight by delivery seq, returned in the stable ascending order this
    # reader has always documented (oldest -> newest).
    assert [item["seq"] for item in inbox["items"]] == [5, 6, 7, 8, 9, 10, 11, 12]
    assert [item["subject"] for item in inbox["items"]] == [f"own-{index:02d}" for index in range(4, 12)]

    guidance = services["bob"].driver_postcompact_guidance("beta", standing_only=True)
    assert guidance["included"] == 8
    assert guidance["omitted"] == 4
    assert projected_subjects(guidance) == [f"own-{index:02d}" for index in range(4, 12)]


def test_budget_trim_gives_way_at_the_old_end(services):
    body = "boundary " * 40  # 360 characters, so every excerpt spends its full 320
    for index in range(12):
        send(services["ada"], request_key=f"long-{index}",
             subject=f"long-{index:02d}", body=body, delivery_mode="standing")

    guidance = services["bob"].driver_postcompact_guidance("beta", standing_only=True)

    assert len(guidance["projection"]) <= 3000
    assert guidance["included"] < 8  # the budget really did trim the newest slice
    # What survives is still a suffix of the newest slice: the trim dropped rows
    # from the OLD end, never the newest one.
    assert projected_subjects(guidance) == [
        f"long-{index:02d}" for index in range(12 - guidance["included"], 12)]
    assert guidance["omitted"] == 12 - guidance["included"]


def test_newest_slice_survives_more_rows_than_one_reader_page(services):
    # 105 > the reader's 100-row page ceiling: a newest-first selection has to
    # drain the stream, not just reverse the first page.
    for index in range(105):
        send(services["ada"], request_key=f"page-{index}",
             subject=f"page-{index:03d}", delivery_mode="standing")

    guidance = services["bob"].driver_postcompact_guidance("beta", standing_only=True)

    assert guidance["included"] == 8
    assert guidance["omitted"] == 105 - 8
    assert projected_subjects(guidance) == [f"page-{index:03d}" for index in range(97, 105)]


# -- The fallback projection a host renders itself ---------------------------
#
# `.codex/hooks/postcompact_driver_state.py` renders this block locally when the
# server does not advertise driver_postcompact_guidance, and
# `SQLiteDirectorMessaging.project_active_standing` is the same projection on
# the server side of that contract. It had the same two defects: the OLDEST
# standing rows were selected, and the budget trimmed the newest of them.


def fallback_rows(projection):
    """The `[omitted_active_standing=` marker and the rows above it."""
    lines = projection.splitlines()
    marker = lines[-1] if lines and lines[-1].startswith("[omitted_active_standing=") else ""
    return (lines[:-1] if marker else lines), marker


def test_fallback_projection_keeps_the_newest_standing_and_broadcast_rows(services):
    for index in range(10):
        send(services["ada"], request_key=f"standing-{index}",
             subject=f"standing-{index:02d}", delivery_mode="standing")
    for index in range(6):
        send(services["ada"], request_key=f"broadcast-{index}",
             subject=f"broadcast-{index:02d}", ack_mode="broadcast")

    lines, marker = fallback_rows(
        services["bob"].director_messages.project_active_standing("beta"))

    payloads = [json.loads(line) for line in lines]
    # 16 candidates (standing seq 1..10, broadcasts 11..16): the newest eight.
    assert [payload["delivery_seq"] for payload in payloads] == list(range(9, 17))
    assert [payload["subject"] for payload in payloads] == (
        ["standing-08", "standing-09"] + [f"broadcast-{index:02d}" for index in range(6)])
    assert marker == "[omitted_active_standing=8]"


def test_fallback_projection_counts_every_omitted_broadcast(services):
    for index in range(12):
        send(services["ada"], request_key=f"broadcast-{index}",
             subject=f"broadcast-{index:02d}", ack_mode="broadcast")

    lines, marker = fallback_rows(
        services["bob"].director_messages.project_active_standing("beta"))

    assert [json.loads(line)["delivery_seq"] for line in lines] == list(range(5, 13))
    # The marker counts what the projection left out over the WHOLE broadcast
    # set, not over one page of it: the four oldest are the ones omitted.
    assert marker == "[omitted_active_standing=4]"


def test_fallback_projection_budget_trims_the_oldest_rows(services):
    body = "boundary " * 40  # 360 characters, so every excerpt spends its full 320
    for index in range(12):
        send(services["ada"], request_key=f"long-{index}",
             subject=f"long-{index:02d}", body=body, delivery_mode="standing")

    projection = services["bob"].director_messages.project_active_standing("beta")
    lines, marker = fallback_rows(projection)

    assert len(projection) <= 3000
    assert 1 <= len(lines) < 8  # the budget really did trim the newest slice
    # A suffix of the newest slice: the trim dropped the OLD end, so the newest
    # row is still there and the oldest carried row is the one about to go.
    assert [json.loads(line)["delivery_seq"] for line in lines] == list(
        range(12 - len(lines) + 1, 13))
    assert marker == f"[omitted_active_standing={12 - len(lines)}]"
