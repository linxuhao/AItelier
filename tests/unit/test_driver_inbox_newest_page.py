"""The inbox reader's newest-N page, matched_total/has_more and status filter.

`list_driver_messages` answered ONE ascending forward page and nothing else: a
driver polling with `after` could walk its backlog but could not ask for "the
newest N" in one call, and could not tell a 3-row answer from a 3,000-row one -
the page length was the only number the reply carried. The director-side reader
(`core/director_messaging.py:list_director_messages`) had already answered
`matched_total`/`has_more`; the driver's own inbox did not.

What is pinned here:

* the DEFAULT call is byte for byte today's page - same `messages` order, same
  `next_after`, same `truncated` - and only gains the two additive keys, so no
  caller that ignores them can be moved;
* `newest=True` answers the NEWEST `limit` deliveries in one call, newest first,
  over the SAME filtered set (so `after` and `statuses` still bound it);
* `matched_total` is the size of the matched set, not of the page, for an
  unfiltered AND for a filtered call, and `has_more` says whether any matched row
  is outside the page - both read as COUNT(*) over the page's own WHERE clause,
  never by fetching every row into Python (a `limit` far below the number of
  planted rows is what makes that difference observable: a page-derived total
  would read `limit`);
* `statuses` narrows the page and the total to `unread`/`acknowledged`/`resolved`
  rows, and an unknown, empty or duplicated status list is refused;
* the row contract is untouched: both modes hand back the SAME row dicts (one
  delivery read two ways), `citation_fields` still projects `segments`/`answers`
  and the `*_json` storage columns still never leave the reader;
* the existing validation (`after<0`, `limit` outside 1..100) still refuses.

The fixture plants SEVEN deliveries to one driver - more than every `limit` used
here - and settles some of them, so an honest total and an honest window are
both non-trivial: a page-shaped total, a total that ignored the status filter,
or a newest window taken from the wrong end all fail below.
"""
from __future__ import annotations

import pytest

from core.drivers import DriverRegistry
from core.state_commands import ListDriverMessages, execute
from core.state_database import StateDatabase
from core.state_graph import StateGraphError
from core.state_service import StateService

# Rows planted per test: more than any `limit` used here, so a total computed
# from the page cannot accidentally agree with the real one.
ROWS = 7

# The inbox row as its callers already receive it (pinned as a literal in
# tests/unit/test_driver_inbox_answer_citation.py): `d.*` plus the message's
# readable columns, nothing else.
BASE_ROW_KEYS = {
    "delivery_id", "message_id", "target_driver_id", "seq", "status", "version",
    "thread_id", "sender_driver_id", "project_id", "kind", "delivery_mode",
    "subject", "body", "refs_json"}

BASE_RESPONSE_KEYS = {"driver_id", "messages", "next_after", "truncated"}


@pytest.fixture
def world(tmp_path):
    db = StateDatabase(str(tmp_path / "state.sqlite"))
    seed = StateService(db, actor="fixture", project_read_trusted=True)
    seed.create_project("alpha", "Alpha")
    registry = DriverRegistry(db, "inbox-newest-page-pepper-not-a-secret")
    for driver_id in ("ada", "bob"):
        registry.register(driver_id, driver_id, actor="test")
        registry.set_membership("alpha", driver_id, "member", 0, "fixture", actor="test")
    return {"db": db, "path": str(tmp_path / "state.sqlite"),
            "ada": StateService(db, actor="driver:ada", driver_id="ada",
                                project_read_trusted=True),
            "bob": StateService(db, actor="driver:bob", driver_id="bob",
                                project_read_trusted=True)}


def deliver(world, count=ROWS):
    """`count` deliveries to bob, seq 1..count, returned in seq order."""
    sent = []
    for index in range(1, count + 1):
        message = world["ada"].driver_inbox.send_driver_message(
            f"row-{index}", f"row-{index}", f"body-{index}", target_driver_id="bob")
        sent.append(message["deliveries"][0])
    return sent


def inbox(world, **arguments):
    """The reader through the action surface, the way a driver calls it."""
    return execute(world["bob"], "list_driver_messages", arguments)


def seqs(result):
    return [row["seq"] for row in result["messages"]]


def settle(world, delivery, status, version=1, request_key="settle"):
    """Acknowledge then resolve, through the command surface the driver uses."""
    service = world["bob"]
    if status == "acknowledged":
        return execute(service, "acknowledge_driver_message",
                       {"delivery_id": delivery["delivery_id"], "expected_version": version,
                        "request_key": request_key}, allow_write=True)
    assert status == "resolved"
    execute(service, "acknowledge_driver_message",
            {"delivery_id": delivery["delivery_id"], "expected_version": 1,
             "request_key": request_key + "-ack"}, allow_write=True)
    return execute(service, "resolve_driver_message",
                   {"delivery_id": delivery["delivery_id"], "expected_version": 2,
                    "request_key": request_key + "-resolve"}, allow_write=True)


def settled_world(world):
    """1 and 2 acknowledged, 3 resolved, 4..7 unread: three statuses at once."""
    deliveries = deliver(world)
    for delivery in deliveries[0:2]:
        settle(world, delivery, "acknowledged", request_key="ack-" + delivery["delivery_id"])
    settle(world, deliveries[2], "resolved", request_key="res-" + deliveries[2]["delivery_id"])
    return deliveries


# ── the default page is today's page ────────────────────────────────────────

def test_the_default_call_is_todays_ascending_page_plus_the_two_new_keys(world):
    deliver(world)
    first = inbox(world, limit=3)
    # The additive keys are the ONLY difference: today's four, unchanged.
    assert set(first) == BASE_RESPONSE_KEYS | {"matched_total", "has_more"}
    assert first["driver_id"] == "bob"
    assert seqs(first) == [1, 2, 3]
    assert first["next_after"] == 3 and first["truncated"] is True
    # The forward keyset keeps walking the backlog exactly as before.
    second = inbox(world, after=first["next_after"], limit=3)
    assert seqs(second) == [4, 5, 6] and second["next_after"] == 6
    assert second["truncated"] is True
    third = inbox(world, after=second["next_after"], limit=3)
    assert seqs(third) == [7] and third["next_after"] == 7 and third["truncated"] is False
    done = inbox(world, after=third["next_after"], limit=3)
    assert done["messages"] == [] and done["next_after"] == 7 and done["truncated"] is False


def test_the_default_mode_is_what_the_signature_says_it_is(world):
    """`newest` is false by default at BOTH layers, and says so."""
    assert ListDriverMessages.model_fields["newest"].default is False
    assert ListDriverMessages.model_fields["statuses"].default is None
    deliver(world, count=2)
    implicit = inbox(world)
    explicit = inbox(world, newest=False, statuses=None)
    assert implicit == explicit
    assert seqs(implicit) == [1, 2]


# ── the newest-N page, in one call ─────────────────────────────────────────

def test_the_newest_page_answers_the_newest_n_newest_first(world):
    deliver(world)                                     # 7 rows, limit 3
    newest = inbox(world, limit=3, newest=True)
    assert seqs(newest) == [7, 6, 5]
    assert newest["matched_total"] == ROWS and newest["has_more"] is True
    assert newest["truncated"] is True
    # Nothing newer than 7 exists, so forward polling resumes past it.
    assert newest["next_after"] == 7
    assert newest["driver_id"] == "bob"


def test_the_newest_page_covers_the_backlog_when_the_limit_is_large_enough(world):
    deliver(world)
    whole = inbox(world, limit=100, newest=True)
    assert seqs(whole) == [7, 6, 5, 4, 3, 2, 1]
    assert whole["matched_total"] == ROWS
    assert whole["has_more"] is False and whole["truncated"] is False
    assert whole["next_after"] == 7


def test_both_modes_hand_back_the_same_rows(world):
    """`newest` chooses a window and an order - it never changes a row."""
    deliver(world)
    ascending = {row["seq"]: row for row in inbox(world, limit=100)["messages"]}
    descending = inbox(world, limit=100, newest=True)["messages"]
    assert [row["seq"] for row in descending] == list(range(ROWS, 0, -1))
    for row in descending:
        assert row == ascending[row["seq"]]


# ── matched_total / has_more: the matched set, not the page ────────────────

def test_matched_total_counts_the_backlog_not_the_page(world):
    deliver(world)
    page = inbox(world, limit=2, newest=True)
    assert seqs(page) == [7, 6]
    assert page["matched_total"] == ROWS, "a page-derived total would read 2"
    assert page["has_more"] is True
    # Ascending too: the page is 2 rows, the matched set is everything above `after`.
    forward = inbox(world, after=1, limit=2)
    assert seqs(forward) == [2, 3]
    assert forward["matched_total"] == ROWS - 1 and forward["has_more"] is True


def test_matched_total_and_has_more_follow_the_status_filter(world):
    settled_world(world)                # 1,2 acknowledged; 3 resolved; 4..7 unread
    unread = inbox(world, limit=2, newest=True, statuses=["unread"])
    assert seqs(unread) == [7, 6]
    assert unread["matched_total"] == 4, "matched_total must reflect the filter"
    assert unread["has_more"] is True
    every_unread = inbox(world, limit=2, after=2, statuses=["unread"])
    assert seqs(every_unread) == [4, 5]
    assert every_unread["matched_total"] == 4 and every_unread["next_after"] == 5
    # A filter whose rows all fit the page says so.
    resolved = inbox(world, limit=100, newest=True, statuses=["resolved"])
    assert seqs(resolved) == [3]
    assert resolved["matched_total"] == 1 and resolved["has_more"] is False
    assert resolved["truncated"] is False
    # Two statuses at once, ascending, over everything.
    settled = inbox(world, limit=100, statuses=["acknowledged", "resolved"])
    assert seqs(settled) == [1, 2, 3]
    assert settled["matched_total"] == 3 and settled["has_more"] is False


def test_a_status_with_no_rows_answers_an_empty_page_and_a_zero_total(world):
    deliver(world, count=2)             # every delivery is still unread
    none = inbox(world, limit=100, newest=True, statuses=["acknowledged", "resolved"])
    assert none["messages"] == [] and none["matched_total"] == 0
    assert none["has_more"] is False and none["truncated"] is False
    assert none["next_after"] == 2
    unread = inbox(world, limit=100, newest=True, statuses=["unread"])
    assert seqs(unread) == [2, 1] and unread["matched_total"] == 2


def test_the_total_and_the_after_window_are_the_same_window(world):
    """`after` bounds matched_total exactly as it bounds the page."""
    deliver(world)
    empty = inbox(world, after=ROWS, newest=True)
    assert empty["messages"] == [] and empty["matched_total"] == 0
    assert empty["has_more"] is False and empty["next_after"] == ROWS
    tail = inbox(world, after=ROWS - 2, newest=True)
    assert seqs(tail) == [7, 6] and tail["matched_total"] == 2 and tail["has_more"] is False


# ── the status filter's own grammar ────────────────────────────────────────

@pytest.mark.parametrize("statuses", [[], ["missing"], ["unread", "unread"], "unread", [None]])
def test_an_unknown_empty_or_duplicated_status_list_is_refused(world, statuses):
    deliver(world, count=1)
    with pytest.raises(StateGraphError, match="invalid_request"):
        world["bob"].driver_inbox.list_driver_messages(statuses=statuses)
    # Through the action surface the command model refuses it first, and the
    # refusal names the field instead of handing back a page.
    with pytest.raises(StateGraphError, match="statuses"):
        execute(world["bob"], "list_driver_messages", {"statuses": statuses})


def test_the_existing_validation_is_kept(world):
    deliver(world, count=1)
    reader = world["bob"].driver_inbox.list_driver_messages
    for arguments in ({"after": -1}, {"limit": 0}, {"limit": 101}, {"newest": 1}):
        with pytest.raises(StateGraphError, match="invalid_request"):
            reader(**arguments)
    with pytest.raises(StateGraphError):
        execute(world["bob"], "list_driver_messages", {"limit": 101})


# ── the row contract is untouched ──────────────────────────────────────────

def test_a_plain_row_keeps_its_base_shape_in_both_modes(world):
    deliver(world, count=2)
    for result in (inbox(world, limit=100), inbox(world, limit=100, newest=True)):
        for row in result["messages"]:
            assert set(row) == BASE_ROW_KEYS, row
            assert "segments" not in row and "answers" not in row
            assert "segments_json" not in row and "answers_json" not in row


def test_the_citation_projection_still_reaches_the_newest_page(world):
    """`segments`/`answers` are projected by `citation_fields` in either mode."""
    deliver(world, count=2)             # so the two modes differ in more than one row
    asked = execute(world["ada"], "send_user_message",
                    {"request_key": "ask", "project_id": "alpha", "subject": "Report",
                     "body": "may I restart?",
                     "segments": [{"id": "q1", "kind": "question", "text": "may I restart?"}]},
                    allow_write=True)
    user = StateService(world["db"], actor="owner:user@example.test",
                        project_read_trusted=True)
    execute(user, "send_user_conversation_message",
            {"request_key": "answer", "project_id": "alpha", "body": "yes",
             "answers": [{"message_id": asked["message_id"], "segment_id": "q1"}]},
            allow_write=True)
    forward = inbox(world, limit=100)["messages"]
    backward = inbox(world, limit=100, newest=True)["messages"]
    assert len(backward) == 3
    assert [row["message_id"] for row in backward] == [row["message_id"] for row in forward][::-1]
    # The user's reply cites the question, and carries that citation to BOTH reads.
    reply = next(row for row in backward if "answers" in row)
    assert reply["answers"] == [{"message_id": asked["message_id"], "segment_id": "q1"}]
    assert reply == next(row for row in forward if row["seq"] == reply["seq"])
    for row in backward:
        assert "segments_json" not in row and "answers_json" not in row
        assert "withdraws_json" not in row


# ── the other caller of this reader: the waiting read ──────────────────────

async def test_wait_for_driver_inbox_still_reads_the_forward_page(world):
    deliver(world, count=2)
    waited = await world["bob"].driver_inbox.wait_for_driver_inbox(after=0, timeout_seconds=0)
    assert seqs(waited) == [1, 2]
    assert waited["next_after"] == 2 and waited["truncated"] is False
    assert waited["matched_total"] == 2 and waited["has_more"] is False
