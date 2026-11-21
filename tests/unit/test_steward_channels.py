"""Step 19: the owner's chat channels to the steward (design-steward 7.2).

The steward's HOST relays what Slack / WeChat received (relay_owner_message,
bind_owner_channel, record_channel_delivery - steward only); the owner binds, unbinds
and picks the default channel on the dashboard (owner session only). An exact
approval phrase in an owner message decides a proposal through the dashboard's own
path. The HTTP tests run the real app with the write gate ON, like
tests/unit/test_steward_proposals.py."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import authz
from api.dependencies import get_db_manager
from api.main import app
from core import cf_access, drivers, state_channels
from core.drivers import DriverRegistry
from core.dsh_driver import DshDrivers
from core.state_commands import STEWARD_WRITE_ACTIONS, execute
from core.state_database import StateDatabase
from core.state_graph import StateGraphError, canonical
from core.state_service import StateService
from tests.unit.test_driver_inbox_answer_citation import BASE_ROW_KEYS

HOME = "aitelier-home"
OWNER = "owner@example.com"
PEPPER = "steward-channel-pepper-" * 3
SLACK_USER = "U0OWNER"
CREATE = {"project_id": "gamma", "title": "Gamma", "goal": "a new project", "seed_nodes": []}


@pytest.fixture
def world(tmp_path, monkeypatch):
    # The binding codes are keyed with the driver pepper secret file; here it is the
    # registry's pepper, whatever the ambient container mounts.
    monkeypatch.setattr(drivers, "read_pepper", lambda: PEPPER)
    db = StateDatabase(str(tmp_path / "state.sqlite"))
    registry = DriverRegistry(db, PEPPER)
    registry.seed("steward-channel-owner-cli-token")
    seeder = StateService(db, actor="fixture", project_read_trusted=True)
    for project in ("alpha", HOME):
        seeder.store.create_project(project, project.title())
    runtime = DshDrivers(db, registry, tmp_path / "drivers",
                         command=[sys.executable, "-c", "import time; time.sleep(90)"])
    steward = runtime._provision(HOME, "aitelier", "driver", "fixture")
    director = runtime._provision("alpha", "aitelier", "driver", "fixture")
    tokens = {name: (runtime.root / row["driver_id"] / "token").read_text().strip()
              for name, row in (("steward", steward), ("director", director))}

    def as_driver(row):
        return StateService(db, actor="driver:" + row["driver_id"], driver_id=row["driver_id"],
                            project_read_trusted=True)

    owner = StateService(db, actor="owner:" + OWNER, project_read_trusted=True)
    return type("World", (), dict(db=db, runtime=runtime, registry=registry, tokens=tokens, owner=owner,
                                  steward_row=steward, steward=as_driver(steward),
                                  director=as_driver(director)))


def bind(world, channel="slack", sender=SLACK_USER, key="bind-1"):
    code = world.owner.channels.issue_code(channel)["code"]
    return execute(world.steward, "bind_owner_channel",
                   {"request_key": key, "channel": channel, "sender_ref": sender, "code": code}, allow_write=True)


NOW = object()


def relay(world, body, ref, key=None, channel="slack", sender=SLACK_USER, service=None, channel_at=NOW):
    """A relay as the host sends it. Both shipped channels date every message, so the
    message is dated now unless the test says otherwise; channel_at=None sends none."""
    if channel_at is NOW:
        channel_at = state_channels._now().isoformat()
    return execute(service or world.steward, "relay_owner_message",
                   {"request_key": key or f"relay-{ref}", "channel": channel, "sender_ref": sender,
                    "channel_message_ref": ref, "body": body,
                    **({"channel_at": channel_at} if channel_at is not None else {})}, allow_write=True)


def plant_proposal(world, proposal_id="p-1", kind="create_project", payload=None):
    with world.db.get_connection() as conn:
        conn.execute("INSERT INTO state_steward_proposals(proposal_id,version,kind,payload_json,reason,status,"
                     "created_by,created_at) VALUES(?,1,?,?,'because','open',?,'t')",
                     (proposal_id, kind, json.dumps(payload or CREATE),
                      "driver:" + world.steward_row["driver_id"]))
        conn.commit()


def proposal(world, proposal_id="p-1"):
    with world.db.get_connection() as conn:
        out = dict(conn.execute("SELECT * FROM state_steward_proposals WHERE proposal_id=?",
                                (proposal_id,)).fetchone())
    out["result"] = json.loads(out["result_json"]) if out["result_json"] else None
    return out


def conversation(world):
    return [m for m in execute(world.owner, "list_user_conversation", {"project_id": HOME})["messages"]
            if m["sender_kind"] == "user"]


def refused(call, code):
    with pytest.raises(StateGraphError) as caught:
        call()
    assert str(caught.value).split(":", 1)[0] == code, str(caught.value)


def _render(digits, base):
    """The same six digits in another script's DECIMAL block (0xFF10 full-width,
    0x0660 Arabic-Indic, 0x0966 Devanagari)."""
    return "".join(chr(base + int(digit)) for digit in digits)


RENDERINGS = [("full_width", 0xFF10), ("arabic_indic", 0x0660), ("devanagari", 0x0966)]


# The renderings the backend folds but a \p{Nd} rule does not: superscripts are category No,
# and unicodedata.digit reads them as 0-9 (core/state_channels.py:ascii_digits).
SUPERSCRIPT = dict(zip("0123456789", "\u2070\u00b9\u00b2\u00b3\u2074\u2075\u2076\u2077\u2078\u2079"))


def folded(text, table=SUPERSCRIPT):
    """The same digits, written the way NFKC does not call a digit run: one char per digit."""
    return "".join(table.get(char, char) for char in text)


def precheck(world, body, ref, sender=SLACK_USER, channel="slack"):
    """The channel client's question before it writes the owner's words into its owed store
    (integrations/dsh-director/lib/channels.mjs -> channels.codeCheck -> this command's
    precheck mode)."""
    return execute(world.steward, "relay_owner_message",
                   {"request_key": f"relay-check-{ref}", "channel": channel, "sender_ref": sender,
                    "channel_message_ref": ref, "body": body, "precheck": True}, allow_write=True)


def test_the_store_text_precheck_answers_with_the_relays_own_rule_and_writes_nothing(world):
    """The channel client keeps the owner's own words in a durable owed store BEFORE it relays
    them, so a message whose answer times out (a start/stop decision runs longer than the
    client's own bound) is asked again after a restart instead of its line vanishing. That
    store must never hold a live binding code. The client cannot decide either question: a
    digit rule of its own is too wide (a proposal id carrying six digits in a row lost its
    line) or too narrow (a superscript code was written verbatim and then refused as the live
    code). So it asks, and the answer is this command's rule."""
    bind(world)
    before = len(conversation(world))
    # Ordinary text may be kept - including an approval phrase whose proposal id carries six
    # digits in a row: ids are 20 hex characters (core/state_steward.py:538), about 38% of them
    # carry such a run, and those are exactly the approvals the client-side rule silenced.
    for ref, body in (("p-1", "请帮我看看"), ("p-2", "同意 proposal prop-234567abcdef01234567")):
        assert precheck(world, body, ref) == {"store_text": True}, body
    # A live code may not be kept, in ANY rendering the backend folds - not only the Nd blocks
    # the client's guard used to know about.
    code = world.owner.channels.issue_code("slack")["code"]
    superscript = folded(code)
    assert superscript != code and superscript.isascii() is False
    for ref, body in (("c-1", code), ("c-2", f"binding code: {code} please bind"), ("c-3", superscript),
                      ("c-4", f"binding code: {superscript} please bind"), ("c-5", _render(code, 0xFF10))):
        assert precheck(world, body, ref) == {"store_text": False}, body
    # A stranger is answered false and learns nothing: the binding check runs FIRST, exactly as
    # in the relay, so a code-shaped answer cannot tell them their digits were a live code.
    for ref, body in (("s-1", code), ("s-2", "hello")):
        assert precheck(world, body, ref, sender="U-STRANGER") == {"store_text": False}
    # It reads: nothing reached the conversation, no source row was written, no ledger row was
    # recorded, and the code the owner issued is still live - a check never retires one.
    assert len(conversation(world)) == before
    with world.db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM state_message_sources").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM driver_inbox_requests WHERE operation LIKE 'relay%'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM state_owner_channel_bind_attempts").fetchone()[0] == 0
        live = conn.execute("SELECT COUNT(*) FROM state_owner_channel_codes WHERE retired_at IS NULL AND used_at IS NULL").fetchone()[0]
    assert live == 1, "the precheck retired nothing"
    # And the relay itself still refuses that body as the live code, retiring it then.
    refused(lambda: relay(world, f"binding code: {superscript} please bind", "c-6"), "code_sent_to_bound_account")


# --- binding -------------------------------------------------------------------------------

def test_a_code_the_owner_issued_binds_the_sender_once(world):
    issued = world.owner.channels.issue_code("slack")
    assert len(issued["code"]) == 6 and issued["code"].isdigit() and issued["expires_at"]
    with world.db.get_connection() as conn:
        stored = [r[0] for r in conn.execute("SELECT code_hash FROM state_owner_channel_codes")]
    assert stored and issued["code"] not in stored[0], "the code is kept hashed"
    args = {"request_key": "b1", "channel": "slack", "sender_ref": SLACK_USER, "code": issued["code"]}
    out = execute(world.steward, "bind_owner_channel", args, allow_write=True)
    assert out == {"bound": True, "channel": "slack", "sender_ref": SLACK_USER}
    assert execute(world.steward, "bind_owner_channel", args, allow_write=True) == out, "a replay"
    refused(lambda: execute(world.steward, "bind_owner_channel", {**args, "request_key": "b2"}, allow_write=True),
            "code_used")
    listed = execute(world.owner, "list_owner_channels", {})
    assert listed["channels"][0] == {"channel": "slack", "bound": True, "sender_ref": SLACK_USER,
                                     "bound_at": listed["channels"][0]["bound_at"], "revoked_at": None,
                                     "is_default": False}
    assert listed["channels"][1]["channel"] == "wechat" and not listed["channels"][1]["bound"]
    assert listed["default_channel"] is None


def test_a_wrong_expired_or_other_channel_code_binds_nothing(world, monkeypatch):
    code = world.owner.channels.issue_code("wechat")["code"]
    args = {"request_key": "b", "channel": "slack", "sender_ref": "intruder", "code": code}
    refused(lambda: execute(world.steward, "bind_owner_channel", args, allow_write=True), "invalid_code")
    wrong = f"{(int(code) + 1) % 10 ** 6:06d}"
    refused(lambda: execute(world.steward, "bind_owner_channel",
                            {**args, "request_key": "b2", "channel": "wechat", "code": wrong}, allow_write=True),
            "invalid_code")
    refused(lambda: execute(world.steward, "bind_owner_channel",
                            {**args, "request_key": "b3", "channel": "wechat", "code": "12345a"}, allow_write=True),
            "invalid_code")
    later = state_channels._now() + timedelta(minutes=10, seconds=1)
    monkeypatch.setattr(state_channels, "_now", lambda: later)
    refused(lambda: execute(world.steward, "bind_owner_channel",
                            {**args, "request_key": "b4", "channel": "wechat"}, allow_write=True), "code_expired")
    assert not any(c["bound"] for c in execute(world.owner, "list_owner_channels", {})["channels"])


def test_five_refused_attempts_retire_the_live_code_until_the_owner_issues_a_new_one(world, caplog):
    caplog.set_level(logging.WARNING, logger="core.state_channels")
    code = world.owner.channels.issue_code("slack")["code"]
    wrong = f"{(int(code) + 1) % 10 ** 6:06d}"
    other = world.owner.channels.issue_code("wechat")["code"]
    attempt = {"channel": "slack", "sender_ref": "intruder"}
    # Any refused attempt on the channel counts: a wrong code, a malformed one, another
    # channel's code - each a guess at the live code.
    for i, guess in enumerate([wrong, "12345a", other, wrong]):
        refused(lambda: execute(world.steward, "bind_owner_channel", {**attempt, "request_key": f"g{i}", "code": guess},
                                allow_write=True), "invalid_code")
    refused(lambda: execute(world.steward, "bind_owner_channel", {**attempt, "request_key": "g4", "code": wrong},
                            allow_write=True), "invalid_code")
    assert "retired after 5 refused binding attempts" in caplog.records[-1].getMessage()
    assert wrong not in caplog.records[-1].getMessage() and code not in caplog.records[-1].getMessage()
    # The sixth try, even with the right code and from the owner's own account, binds nothing.
    for key, sender in (("g5", "intruder"), ("g6", SLACK_USER)):
        refused(lambda: execute(world.steward, "bind_owner_channel",
                                {"request_key": key, "channel": "slack", "sender_ref": sender, "code": code},
                                allow_write=True), "code_retired")
    assert not execute(world.owner, "list_owner_channels", {})["channels"][0]["bound"]
    # The other channel's code is untouched; a new slack code binds.
    assert execute(world.steward, "bind_owner_channel", {"request_key": "w", "channel": "wechat",
                                                         "sender_ref": "wxid_owner", "code": other},
                   allow_write=True)["bound"]
    bind(world, key="fresh")
    assert execute(world.owner, "list_owner_channels", {})["channels"][0]["sender_ref"] == SLACK_USER


def test_four_refused_attempts_leave_the_code_live_and_none_count_without_one(world):
    # Guesses while the channel has no live code count against no later code.
    for i in range(5):
        refused(lambda: execute(world.steward, "bind_owner_channel", {"request_key": f"n{i}", "channel": "slack",
                                                                      "sender_ref": "x", "code": "000000"},
                                allow_write=True), "invalid_code")
    code = world.owner.channels.issue_code("slack")["code"]
    wrong = f"{(int(code) + 1) % 10 ** 6:06d}"
    for i in range(4):
        refused(lambda: execute(world.steward, "bind_owner_channel", {"request_key": f"g{i}", "channel": "slack",
                                                                      "sender_ref": "x", "code": wrong},
                                allow_write=True), "invalid_code")
    assert execute(world.steward, "bind_owner_channel", {"request_key": "ok", "channel": "slack",
                                                         "sender_ref": SLACK_USER, "code": code},
                   allow_write=True)["bound"]


# --- the stored code is keyed (a copy of the State file does not give it away) -------------

def _unkeyed(code, channel="slack", sender=SLACK_USER):
    """Every unkeyed digest of `code` the backend ever stored or could store: the old
    code_hash, plain variants, and the old bind request hash (driver_inbox_requests)."""
    sha = lambda text: hashlib.sha256(text.encode("utf-8")).hexdigest()  # noqa: E731
    return {sha("owner-channel-code:" + code), sha(code), sha(f"owner-channel-code:{channel}:{code}"),
            sha(canonical([channel, sender, code])), sha(canonical(code))}


def _copy(world, tmp_path):
    """A read-only copy of the State file, as someone who can only read it would take it."""
    copy = tmp_path / "copy.sqlite"
    with sqlite3.connect(world.db.db_path) as src, sqlite3.connect(copy) as dst:
        src.backup(dst)
    return sqlite3.connect(f"file:{copy}?mode=ro", uri=True)


def _keyed(pepper, channel, code):
    """The stored digest: HMAC under a subkey of the pepper for this one use."""
    subkey = hmac.new(pepper.encode(), b"owner-channel-code", hashlib.sha256).digest()
    return hmac.new(subkey, f"owner-channel-code:{channel}:{code}".encode(), hashlib.sha256).hexdigest()


def test_the_stored_code_is_hmac_keyed_with_the_driver_pepper_not_an_unkeyed_digest(world):
    code = world.owner.channels.issue_code("slack")["code"]
    with world.db.get_connection() as conn:
        (stored,), = conn.execute("SELECT code_hash FROM state_owner_channel_codes").fetchall()
    assert stored == _keyed(PEPPER, "slack", code)
    assert stored not in _unkeyed(code), "an unkeyed digest of a 6-digit code is undone offline"
    assert _keyed("another-pepper-of-at-least-32-chars!", "slack", code) != stored, \
        "the digest changes with the pepper"
    # K-subkey: never the pepper itself as the key - the driver tokens' own scheme
    # (drivers.token_hash) stays a domain of its own.
    assert stored != drivers.token_hash(PEPPER, f"owner-channel-code:slack:{code}")


def test_a_db_copy_cannot_be_brute_forced_offline_for_the_live_code(world, tmp_path):
    """The verifier's repro (approval-semantics-and-secrets_f1): from a copy of the State
    file, try all 10^6 codes under the old formula against the live row. Before the fix
    this found the code in under a second, bound the copy holder's account and let it
    approve proposals over chat."""
    truth = world.owner.channels.issue_code("slack")["code"]
    conn = _copy(world, tmp_path)
    stored = {r[0] for r in conn.execute("SELECT code_hash FROM state_owner_channel_codes WHERE used_at IS NULL")}
    conn.close()
    assert len(stored) == 1
    prefix = hashlib.sha256(b"owner-channel-code:")
    recovered = None
    for i in range(10 ** 6):
        candidate = prefix.copy()
        candidate.update(b"%06d" % i)
        if candidate.hexdigest() in stored:
            recovered = f"{i:06d}"
            break
    assert recovered is None
    assert not stored & _unkeyed(truth)
    # Without the pepper the copy holder has nothing; the owner's code still binds.
    assert bind_message(world, truth, "m-own")["bound"]


def test_after_a_bind_no_table_holds_an_unkeyed_digest_of_the_code(world, tmp_path):
    code = world.owner.channels.issue_code("slack")["code"]
    assert bind_message(world, code, "m-1")["bound"]
    conn = _copy(world, tmp_path)
    forbidden, cells = _unkeyed(code), 0
    with world.db.get_connection() as live:
        assert live.execute("SELECT COUNT(*) FROM driver_inbox_requests WHERE operation='bind_owner_channel'"
                            ).fetchone()[0] == 1, "the bind's request ledger row is in the scan"
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        for row in conn.execute(f'SELECT * FROM "{table}"'):
            for cell in row:
                if isinstance(cell, str):
                    cells += 1
                    assert cell != code, table
                    assert not any(digest in cell for digest in forbidden), table
    conn.close()
    assert cells


def test_another_pepper_cannot_verify_the_code(world, monkeypatch):
    code = world.owner.channels.issue_code("slack")["code"]
    monkeypatch.setattr(drivers, "read_pepper", lambda: "a-different-pepper-of-32-characters-or-more")
    refused(lambda: execute(world.steward, "bind_owner_channel",
                            {"request_key": "b1", "channel": "slack", "sender_ref": SLACK_USER, "code": code},
                            allow_write=True), "invalid_code")
    monkeypatch.setattr(drivers, "read_pepper", lambda: PEPPER)
    assert execute(world.steward, "bind_owner_channel",
                   {"request_key": "b2", "channel": "slack", "sender_ref": SLACK_USER, "code": code},
                   allow_write=True)["bound"]


@pytest.mark.parametrize("pepper", [None, "too-short"])
def test_without_the_pepper_no_code_is_issued_or_tried(world, monkeypatch, pepper):
    code = world.owner.channels.issue_code("slack")["code"]
    monkeypatch.setattr(drivers, "read_pepper", lambda: pepper)
    refused(lambda: world.owner.channels.issue_code("slack"), "channel_secret_missing")
    for i in range(state_channels.BIND_FAILURES_MAX + 1):
        refused(lambda: execute(world.steward, "bind_owner_channel",
                                {"request_key": f"n{i}", "channel": "slack", "sender_ref": SLACK_USER,
                                 "code": code, "channel_message_ref": f"m{i}"}, allow_write=True),
                "channel_secret_missing")
    with world.db.get_connection() as conn:
        assert tuple(conn.execute("SELECT COUNT(*), MAX(failed_attempts) FROM state_owner_channel_codes")
                     .fetchone()) == (1, 0), "a missing secret issues nothing, keeps the live code and counts no guess"
        assert conn.execute("SELECT COUNT(*) FROM state_owner_channel_bind_attempts").fetchone()[0] == 0
    monkeypatch.setattr(drivers, "read_pepper", lambda: PEPPER)
    assert bind_message(world, code, "m0")["bound"], "the refused message binds once the secret is back"



def test_the_pepper_is_read_only_after_the_steward_check_and_the_duplicate_check(world, monkeypatch):
    """K-order: the pepper used to be read first of all, so a caller that is not the
    steward, and a binding message delivered again, read the secret file and could be
    answered channel_secret_missing instead of what they are."""
    code = world.owner.channels.issue_code("slack")["code"]
    assert bind_message(world, code, "m-1")["bound"]
    reads = []
    monkeypatch.setattr(drivers, "read_pepper", lambda: reads.append(1))
    refused(lambda: world.director.channels.bind_owner_channel(HOME, "b-x", "slack", "intruder", code, "m-x"),
            "steward_role_required")
    assert bind_message(world, code, "m-1", key="another") == {"bound": True, "channel": "slack",
                                                               "sender_ref": SLACK_USER, "duplicate": True}
    assert reads == [], "neither read the pepper"
    refused(lambda: bind_message(world, code, "m-2"), "channel_secret_missing")
    assert reads == [1]


def test_a_code_stored_unkeyed_before_the_upgrade_matches_nothing(world):
    """Rows written by the old code (an unkeyed sha256) are not accepted: they bind
    nothing, expire with their 10 minutes, and the next issued code deletes them."""
    old = "424242"
    moment = state_channels._now()
    with world.db.get_connection() as conn:
        conn.execute("INSERT INTO state_owner_channel_codes(code_hash,channel,issued_at,expires_at) VALUES(?,?,?,?)",
                     (hashlib.sha256(("owner-channel-code:" + old).encode()).hexdigest(), "slack",
                      moment.isoformat(), (moment + state_channels.CODE_TTL).isoformat()))
        conn.commit()
    refused(lambda: execute(world.steward, "bind_owner_channel",
                            {"request_key": "old", "channel": "slack", "sender_ref": SLACK_USER, "code": old},
                            allow_write=True), "invalid_code")
    code = world.owner.channels.issue_code("slack")["code"]
    with world.db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM state_owner_channel_codes").fetchone()[0] == 1
    assert execute(world.steward, "bind_owner_channel",
                   {"request_key": "new", "channel": "slack", "sender_ref": SLACK_USER, "code": code},
                   allow_write=True)["bound"]


def test_a_code_keyed_directly_with_the_pepper_before_the_subkey_matches_nothing(world):
    """Rows written while the code was keyed with the pepper itself (token_hash) match
    no code, like the unkeyed ones before them."""
    old = "424242"
    moment = state_channels._now()
    with world.db.get_connection() as conn:
        conn.execute("INSERT INTO state_owner_channel_codes(code_hash,channel,issued_at,expires_at) VALUES(?,?,?,?)",
                     (drivers.token_hash(PEPPER, f"owner-channel-code:slack:{old}"), "slack",
                      moment.isoformat(), (moment + state_channels.CODE_TTL).isoformat()))
        conn.commit()
    refused(lambda: execute(world.steward, "bind_owner_channel",
                            {"request_key": "old", "channel": "slack", "sender_ref": SLACK_USER, "code": old},
                            allow_write=True), "invalid_code")
    assert not execute(world.owner, "list_owner_channels", {})["channels"][0]["bound"]


def bind_message(world, code, ref, key=None, channel="slack", sender=SLACK_USER):
    """A binding attempt as the host sends it: with the channel message it came in."""
    return execute(world.steward, "bind_owner_channel",
                   {"request_key": key or f"bind-{ref}", "channel": channel, "sender_ref": sender, "code": code,
                    "channel_message_ref": ref}, allow_write=True)


def test_a_binding_message_delivered_again_after_it_bound_is_a_duplicate_not_an_owner_message(world):
    """A WeChat plugin restart replays the bridge's last 10 minutes. The owner's binding
    message is then from a BOUND sender, so the host relays it: it used to land in the
    steward's conversation as an owner message and wake the steward."""
    code = world.owner.channels.issue_code("slack")["code"]
    assert bind_message(world, code, "ts-code") == {"bound": True, "channel": "slack", "sender_ref": SLACK_USER}
    replayed = relay(world, code, "ts-code", key="after-restart")
    assert replayed == {"message_id": None, "duplicate": True, "approval": None}
    assert conversation(world) == []
    assert execute(world.steward, "list_driver_messages", {})["messages"] == [], "the steward is not woken"
    # The bind itself delivered again (any request_key) is a duplicate too, and binds nothing again.
    assert bind_message(world, code, "ts-code", key="other") == {"bound": True, "channel": "slack",
                                                                 "sender_ref": SLACK_USER, "duplicate": True}
    # The owner's next message is an ordinary one.
    assert relay(world, "hello", "ts-next")["duplicate"] is False and len(conversation(world)) == 1
    # Another sender's message with that ref is its own message (refused: not bound).
    refused(lambda: relay(world, code, "ts-code", key="stranger", sender="U-STRANGER"), "channel_sender_not_bound")


def test_a_refused_guess_delivered_again_is_not_counted_again(world):
    """Refusals used to be recorded nowhere, so each redelivery of a stranger's wrong
    guess counted again and a replay could retire the owner's live code."""
    code = world.owner.channels.issue_code("slack")["code"]
    wrong = f"{(int(code) + 1) % 10 ** 6:06d}"
    for i in range(state_channels.BIND_FAILURES_MAX - 1):
        refused(lambda: bind_message(world, wrong, f"guess-{i}", sender="U-STRANGER"), "invalid_code")
    for replay in range(3):
        for i in range(state_channels.BIND_FAILURES_MAX - 1):
            again = bind_message(world, wrong, f"guess-{i}", key=f"replay-{replay}-{i}", sender="U-STRANGER")
            assert again == {"bound": False, "channel": "slack", "sender_ref": "U-STRANGER", "duplicate": True,
                             "refusal": "invalid_code"}
            # ... and relayed (past the host's binding throttle) it is a duplicate, never an owner message.
            assert relay(world, wrong, f"guess-{i}", key=f"relay-{replay}-{i}", sender="U-STRANGER")["duplicate"]
    with world.db.get_connection() as conn:
        assert tuple(conn.execute("SELECT failed_attempts, retired_at FROM state_owner_channel_codes "
                                  "WHERE used_at IS NULL").fetchone()) == (state_channels.BIND_FAILURES_MAX - 1, None)
    assert bind_message(world, code, "ts-owner")["bound"], "the live code still binds the owner"
    assert conversation(world) == []


def test_binding_attempts_past_the_newest_kept_per_channel_are_deleted_by_the_next_attempt(world, monkeypatch):
    """The attempts table was never pruned: every binding message ever relayed stayed. It is
    pruned by count per channel, never by age: a WeChat plugin restart replays the bridge's
    whole ring, whatever its age."""
    monkeypatch.setattr(state_channels, "BIND_ATTEMPTS_KEPT", 3)
    code = world.owner.channels.issue_code("wechat")["code"]
    assert bind_message(world, code, "w-owner", channel="wechat", sender="wxid-owner")["bound"]
    for i in range(4):
        refused(lambda: bind_message(world, "000000", f"ts-{i}", sender="U-STRANGER"), "invalid_code")
    with world.db.get_connection() as conn:
        rows = {tuple(r) for r in conn.execute("SELECT channel, channel_message_ref "
                                               "FROM state_owner_channel_bind_attempts")}
    # Each attempt first keeps the channel's newest 3, then records itself.
    assert rows == {("wechat", "w-owner"), ("slack", "ts-0"), ("slack", "ts-1"), ("slack", "ts-2"), ("slack", "ts-3")}
    refused(lambda: bind_message(world, "000000", "ts-4", sender="U-STRANGER"), "invalid_code")
    with world.db.get_connection() as conn:
        rows = {tuple(r) for r in conn.execute("SELECT channel, channel_message_ref "
                                               "FROM state_owner_channel_bind_attempts")}
    assert rows == {("wechat", "w-owner"), *(("slack", f"ts-{i}") for i in range(1, 5))}, \
        "the channel's oldest attempt is gone; another channel's attempts are its own count"
    assert bind_message(world, code, "w-owner", key="again", channel="wechat", sender="wxid-owner") == {
        "bound": True, "channel": "wechat", "sender_ref": "wxid-owner", "duplicate": True}


def test_the_kept_attempts_outnumber_what_the_wechat_bridge_can_replay():
    ring = (Path(__file__).resolve().parents[2] / "integrations" / "wechat-bridge" / "bridge.mjs").read_text()
    assert "export const RING_SIZE = 500;" in ring
    assert isinstance(state_channels.BIND_ATTEMPTS_KEPT, int) and state_channels.BIND_ATTEMPTS_KEPT >= 2 * 500


def test_a_binding_message_replayed_days_later_is_still_a_duplicate_not_an_owner_message(world, monkeypatch):
    """The WeChat ring is replayed whole after a plugin restart, at any age. The owner's
    binding message came back two days later, after the attempts had been pruned by age,
    and landed in the steward's conversation as a new owner message."""
    code = world.owner.channels.issue_code("wechat")["code"]
    assert bind_message(world, code, "w-1", channel="wechat", sender="wxid-owner")["bound"]
    real = state_channels._now
    monkeypatch.setattr(state_channels, "_now", lambda: real() + timedelta(days=2))
    # Any binding attempt (here the owner binding Slack) runs the prune first.
    assert bind_message(world, world.owner.channels.issue_code("slack")["code"], "s-1")["bound"]
    again = relay(world, code, "w-1", key="relay:wechat:w-1", channel="wechat", sender="wxid-owner")
    assert again == {"message_id": None, "duplicate": True, "approval": None}
    assert conversation(world) == []


def test_a_strangers_wrong_guesses_replayed_days_later_do_not_count_against_a_fresh_code(world, monkeypatch):
    world.owner.channels.issue_code("wechat")
    for i in range(state_channels.BIND_FAILURES_MAX):
        refused(lambda: bind_message(world, f"00000{i}", f"x-{i}", channel="wechat", sender="wxid-stranger"),
                "invalid_code")
    real = state_channels._now
    monkeypatch.setattr(state_channels, "_now", lambda: real() + timedelta(days=2))
    fresh = world.owner.channels.issue_code("wechat")["code"]
    for i in range(state_channels.BIND_FAILURES_MAX):  # the ring replayed after a restart
        assert bind_message(world, f"00000{i}", f"x-{i}", key=f"replay-{i}", channel="wechat",
                            sender="wxid-stranger")["duplicate"]
    assert bind_message(world, fresh, "o-1", channel="wechat", sender="wxid-owner")["bound"], \
        "the owner's fresh code is not retired by replayed old guesses"


def test_a_new_code_retires_the_unused_one_and_a_rebind_replaces_the_account(world):
    first = world.owner.channels.issue_code("slack")["code"]
    world.owner.channels.issue_code("slack")
    refused(lambda: execute(world.steward, "bind_owner_channel", {"request_key": "b", "channel": "slack",
                                                                  "sender_ref": "U1", "code": first},
                            allow_write=True), "invalid_code")
    bind(world, sender="U1", key="b1")
    bind(world, sender="U2", key="b2")
    slack = execute(world.owner, "list_owner_channels", {})["channels"][0]
    assert slack["sender_ref"] == "U2" and slack["bound"]
    refused(lambda: relay(world, "hello", "m1", sender="U1"), "channel_sender_not_bound")


def test_unbind_and_the_default_channel_are_the_owners(world):
    bind(world)
    assert world.owner.channels.set_default("wechat") == {"default_channel": "wechat"}
    assert world.owner.channels.set_default("slack") == {"default_channel": "slack"}
    listed = execute(world.owner, "list_owner_channels", {})
    assert listed["default_channel"] == "slack"
    assert [c["is_default"] for c in listed["channels"]] == [True, False]
    world.owner.channels.unbind("slack")
    slack = execute(world.owner, "list_owner_channels", {})["channels"][0]
    assert not slack["bound"] and slack["revoked_at"] and slack["sender_ref"] == SLACK_USER
    world.owner.channels.set_default(None)
    assert execute(world.owner, "list_owner_channels", {})["default_channel"] is None
    for call in (lambda: world.steward.channels.issue_code("slack"), lambda: world.steward.channels.unbind("slack"),
                 lambda: world.steward.channels.set_default("slack")):
        refused(call, "owner_session_required")


# --- relay ---------------------------------------------------------------------------------

def test_an_unbound_sender_is_dropped_logged_without_its_body_and_wakes_no_one(world, caplog):
    caplog.set_level(logging.WARNING, logger="core.state_channels")
    refused(lambda: relay(world, "secret plans for the owner", "m1"), "channel_sender_not_bound")
    bind(world)
    refused(lambda: relay(world, "secret plans for the owner", "m2", sender="U-STRANGER"),
            "channel_sender_not_bound")
    assert conversation(world) == []
    assert execute(world.steward, "list_driver_messages", {})["messages"] == []
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 2 and all("channel_sender_not_bound" in line and "channel=slack" in line
                                   and "bytes=26" in line for line in lines), lines
    assert "U-STRANGER" in lines[1] and not any("secret" in line for line in lines)


def test_a_bound_owner_message_lands_like_a_dashboard_message_with_its_source(world):
    bind(world)
    out = relay(world, "what is running?", "slack-ts-1")
    assert out == {"message_id": out["message_id"], "duplicate": False, "approval": None}
    [message] = conversation(world)
    assert message["message_id"] == out["message_id"] and message["body"] == "what is running?"
    assert message["subject"] == "Message from the user"
    inbox = execute(world.steward, "list_driver_messages", {})["messages"]
    assert [m["message_id"] for m in inbox] == [out["message_id"]], "the steward is woken by the same delivery"
    dashboard = execute(world.owner, "send_user_conversation_message",
                        {"request_key": "d1", "project_id": HOME, "body": "from the dashboard"}, allow_write=True)
    sources = execute(world.owner, "message_sources",
                      {"message_ids": [out["message_id"], dashboard["message_id"]]})["sources"]
    assert list(sources) == [out["message_id"]], "no row = dashboard"
    assert sources[out["message_id"]] == {"direction": "in", "channel": "slack", "sender_ref": SLACK_USER,
                                          "channel_message_ref": "slack-ts-1",
                                          "at": sources[out["message_id"]]["at"], "error": None}
    assert execute(world.steward, "message_sources", {"message_ids": [out["message_id"]]})["sources"]


def _wake(world):
    import asyncio
    return asyncio.run(execute(world.steward, "wait_for_state_change", {
        "project_id": HOME, "include_driver_inbox": True, "timeout_seconds": 0}))


def test_the_steward_is_woken_with_each_owner_messages_channel_and_a_dashboard_message_has_none(world):
    """The owner's Slack '测试' (2026-11-20) reached the steward with no word of Slack: the
    source sat in the side table and no read on the way to the model selected it."""
    bind(world)
    bind(world, channel="wechat", sender="wxid-owner", key="bind-2")
    slack = relay(world, "from slack", "slack-ts-1")
    wechat = relay(world, "from wechat", "wx-1", channel="wechat", sender="wxid-owner")
    dashboard = execute(world.owner, "send_user_conversation_message",
                        {"request_key": "d1", "project_id": HOME, "body": "from the dashboard"}, allow_write=True)
    for rows in (execute(world.steward, "list_driver_messages", {})["messages"], _wake(world)["driver_inbox"]):
        by_id = {row["message_id"]: row for row in rows}
        assert list(by_id) == [slack["message_id"], wechat["message_id"], dashboard["message_id"]]
        assert by_id[slack["message_id"]]["source_channel"] == "slack"
        assert by_id[wechat["message_id"]]["source_channel"] == "wechat"
        assert "source_channel" not in by_id[dashboard["message_id"]], "no source row = dashboard, no key"
        # Only the channel's name, never the account or the platform's message ref; a
        # dashboard message keeps the plain row shape.
        assert set(by_id[dashboard["message_id"]]) == BASE_ROW_KEYS
        assert set(by_id[slack["message_id"]]) == BASE_ROW_KEYS | {"source_channel"}
        assert SLACK_USER not in json.dumps(rows) and "slack-ts-1" not in json.dumps(rows)


@pytest.mark.parametrize("code_channel", ["slack", "wechat"])
def test_a_live_code_sent_from_the_bound_account_is_refused_retired_and_never_written(world, tmp_path, caplog,
                                                                                      code_channel):
    """K-paste: the bound owner's six digits are relayed as a message. A live binding
    code sent that way was stored as the message body, and the relay's request hash in
    driver_inbox_requests was an unkeyed digest over it - a copy of the State file
    within the code's 10 minutes gave the code away."""
    from core.state_steward import _hash
    caplog.set_level(logging.WARNING, logger="core.state_channels")
    bind(world)
    code = world.owner.channels.issue_code(code_channel)["code"]
    refused(lambda: relay(world, f" {code}\n", "ts-code"), "code_sent_to_bound_account")
    assert conversation(world) == [], "the code is not written as a message"
    assert execute(world.steward, "list_driver_messages", {})["messages"] == [], "nor wakes the steward"
    assert "code retired" in caplog.text and code not in caplog.text
    # Redelivered (same key, or the channel message under another): a duplicate that writes nothing.
    for key in (None, "another-key"):
        assert relay(world, f" {code}\n", "ts-code", key=key) == {"message_id": None, "duplicate": True,
                                                                    "approval": None}
    assert conversation(world) == []
    conn = _copy(world, tmp_path)
    forbidden = _unkeyed(code) | {_hash(["slack", SLACK_USER, "ts-code", body]) for body in (f" {code}\n", code)}
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        for row in conn.execute(f'SELECT * FROM "{table}"'):
            for cell in row:
                if isinstance(cell, str):
                    assert code not in cell and not any(digest in cell for digest in forbidden), table
    conn.close()
    # Retired: the code binds no one now.
    refused(lambda: bind_message(world, code, "m-bind", channel=code_channel, sender="someone-else"), "code_retired")


# The shapes a live code leaks in today: the digits inside other text, wherever they sit
# (a label copied with the code, a sentence, a trailing thanks). Only the bare-digits body
# was checked before, so every one of these was written verbatim into the conversation.
CODE_IN_TEXT = ["binding code: {code}", "please use {code} to bind slack", "{code} thanks", "code {code}",
                "this is my code {code} ok?", " {code}\n", "{code}"]


@pytest.mark.parametrize("shape", CODE_IN_TEXT)
@pytest.mark.parametrize("code_channel", ["slack", "wechat"])
def test_a_live_code_anywhere_in_the_owners_message_is_refused_retired_and_never_written(
        world, tmp_path, caplog, shape, code_channel):
    """The rule core/state_channels wrote down ("a live binding code is never written")
    held only for a body that IS six digits: `binding code: 711833` from the bound account
    was written verbatim, so a copy of the State file within the code's 10 minutes handed
    the code to whoever read it - the copy holder then bound their own account and decided
    proposals. The check is on the digit runs a body carries, so every position is caught,
    the code is retired, the attempt is kept by its channel message, and nothing of the
    text (nor an unkeyed digest of it) reaches the State file."""
    from core.state_steward import _hash
    caplog.set_level(logging.WARNING, logger="core.state_channels")
    bind(world)
    code = world.owner.channels.issue_code(code_channel)["code"]
    body = shape.format(code=code)
    refused(lambda: relay(world, body, "ts-code"), "code_sent_to_bound_account")
    assert conversation(world) == [], "the code is not written as a message"
    assert execute(world.steward, "list_driver_messages", {})["messages"] == [], "nor wakes the steward"
    assert "code retired" in caplog.text and code not in caplog.text
    # Redelivered (same key, or the channel message under another): a duplicate that writes nothing.
    for key in (None, "another-key"):
        assert relay(world, body, "ts-code", key=key) == {"message_id": None, "duplicate": True, "approval": None}
    assert conversation(world) == []
    conn = _copy(world, tmp_path)
    forbidden = _unkeyed(code) | {_hash(["slack", SLACK_USER, "ts-code", body])}
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        for row in conn.execute(f'SELECT * FROM "{table}"'):
            for cell in row:
                if isinstance(cell, str):
                    assert code not in cell and not any(digest in cell for digest in forbidden), table
    conn.close()
    # Retired: the code binds no one now, from any account.
    refused(lambda: bind_message(world, code, "m-bind", channel=code_channel, sender="someone-else"), "code_retired")


@pytest.mark.parametrize("shape", ["wechat {code}", "binding code: {code}", "{code} thanks", "{code}"])
@pytest.mark.parametrize("code_channel", ["slack", "wechat"])
def test_a_live_code_typed_on_the_dashboard_is_refused_retired_and_never_written(world, tmp_path, caplog,
                                                                                shape, code_channel):
    """The other writer had no check at all: a live code typed into the dashboard
    conversation was stored in driver_inbox_messages.body AND in
    driver_inbox_requests.request_json (the request's payload), contradicting issue_code's
    own promise that the plaintext exists only in its answer. It is refused before the
    inbox writes anything, and the code - which the owner clearly meant to use - is retired."""
    caplog.set_level(logging.WARNING, logger="core.state_channels")
    bind(world)
    code = world.owner.channels.issue_code(code_channel)["code"]
    body = shape.format(code=code)
    refused(lambda: execute(world.owner, "send_user_conversation_message",
                            {"request_key": "d-code", "project_id": HOME, "body": body}, allow_write=True),
            "code_sent_to_bound_account")
    assert conversation(world) == [], "nothing is written"
    assert "code retired" in caplog.text and code not in caplog.text
    with world.db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM driver_inbox_requests WHERE request_key='d-code'").fetchone()[0] == 0
    conn = _copy(world, tmp_path)
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        for row in conn.execute(f'SELECT * FROM "{table}"'):
            for cell in row:
                if isinstance(cell, str):
                    assert code not in cell, table
    conn.close()
    refused(lambda: bind_message(world, code, "m-steal", channel=code_channel, sender="intruder"), "code_retired")
    # An ordinary dashboard message still goes through.
    written = execute(world.owner, "send_user_conversation_message",
                      {"request_key": "d-ok", "project_id": HOME, "body": "what is running?"}, allow_write=True)
    assert [m["body"] for m in conversation(world)] == ["what is running?"] and written["message_id"]


def test_a_live_code_in_the_dashboards_subject_is_refused_and_retired_like_one_in_the_body(world, tmp_path):
    """`send_owner_message` stores a heading on the same row as the body, and the sibling
    credential guard already scans both: the code check has to cover the same two fields, or
    a code typed as the heading is stored as it was typed."""
    bind(world)
    code = world.owner.channels.issue_code("slack")["code"]
    refused(lambda: execute(world.owner, "send_user_conversation_message",
                            {"request_key": "d-subject", "project_id": HOME, "subject": f"binding code {code}",
                             "body": "add it to the app"}, allow_write=True), "code_sent_to_bound_account")
    assert conversation(world) == [], "nothing is written"
    with world.db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM driver_inbox_requests WHERE request_key='d-subject'"
                            ).fetchone()[0] == 0
    conn = _copy(world, tmp_path)
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        for row in conn.execute(f'SELECT * FROM "{table}"'):
            for cell in row:
                if isinstance(cell, str):
                    assert code not in cell, table
    conn.close()
    refused(lambda: bind_message(world, code, "m-steal", sender="intruder"), "code_retired")


def test_six_digits_that_are_not_the_live_code_are_ordinary_messages(world):
    """The check must not swallow ordinary text: a five-digit run, a full-width rendering of
    a code that is NOT live, another code broken up by a space, and an order number carry no
    six digits of a live code, so they are messages - and the live code stays live for the
    owner. (The live code broken up is NOT an ordinary message: a reader of the stored text
    recovers it just as well - test_a_live_code_broken_up_or_folded_... below.)"""
    bind(world)
    code = world.owner.channels.issue_code("slack")["code"]
    other = f"{(int(code) + 1) % 10 ** 6:06d}"
    split = f"{other[:3]} {other[3:]}"
    wide = "".join(chr(ord(digit) + 0xFEE0) for digit in other)
    bodies = [code[:5], split, wide, f"my code is {split}", "order 123456 shipped"]
    for index, body in enumerate(bodies):
        assert relay(world, body, f"ts-{index}")["message_id"]
    assert sorted(m["body"] for m in conversation(world)) == sorted(bodies)
    assert bind_message(world, code, "m-ok")["bound"], "the code is still live"


def test_the_digit_scripts_nfkc_does_not_fold_are_read_as_the_digits_they_are(world):
    """NFKC folds the full-width block and nothing else: Arabic-Indic and Devanagari digits
    come back unchanged, so a check that only normalised would read neither. A code written
    in one of those scripts is the same live code (the RENDERINGS cases above), and a
    non-live or five-digit run in one is still an ordinary message. Six messages are the
    channel's minute, so this case stays apart from the one above."""
    bind(world)
    code = world.owner.channels.issue_code("slack")["code"]
    other = f"{(int(code) + 1) % 10 ** 6:06d}"
    bodies = [_render(other, 0x0660), _render(code[:5], 0x0966)]
    for index, body in enumerate(bodies):
        assert relay(world, body, f"ts-script-{index}")["message_id"]
    assert sorted(m["body"] for m in conversation(world)) == sorted(bodies)
    assert bind_message(world, code, "m-script")["bound"], "the code is still live"


@pytest.mark.parametrize("writer", ["relay", "dashboard"])
def test_without_the_pepper_a_text_that_may_carry_a_live_code_is_refused_not_written(world, monkeypatch, writer):
    """The check cannot run without the pepper, so it must never run as "no code here": a
    live code is in the table, the secret goes missing, and both writers refuse
    channel_secret_missing with nothing written. Failing open would store the plaintext with
    only the digest left unguarded."""
    bind(world)
    code = world.owner.channels.issue_code("slack")["code"]
    monkeypatch.setattr(drivers, "read_pepper", lambda: None)
    if writer == "relay":
        refused(lambda: relay(world, f"binding code: {code}", "ts-nope"), "channel_secret_missing")
    else:
        refused(lambda: execute(world.owner, "send_user_conversation_message",
                                {"request_key": "d-nope", "project_id": HOME, "body": f"binding code: {code}"},
                                allow_write=True), "channel_secret_missing")
    assert conversation(world) == [], "nothing is written"


@pytest.mark.parametrize("name,base", RENDERINGS, ids=[name for name, _ in RENDERINGS])
@pytest.mark.parametrize("code_channel", ["slack", "wechat"])
def test_every_unicode_rendering_of_a_live_code_is_refused_on_both_writers(world, code_channel, name, base):
    """The leak is what a reader of the stored bytes recovers, and each of these renderings
    is one lookup away from the ASCII code: NFKC turns '７１１８３３' into '711833', and
    `unicodedata.decimal` turns '٧١١٨٣٣' (Arabic-Indic) and '७११८३३' (Devanagari) into
    7 1 1 8 3 3 - NFKC leaves BOTH decimal blocks alone (measured), so folding through the
    digits, not NFKC, is what catches them. Each is refused and retired by the relay AND by
    the dashboard writer, and neither the code nor its rendering is stored."""
    bind(world)
    for writer in ("relay", "dashboard"):
        code = world.owner.channels.issue_code(code_channel)["code"]
        rendered = _render(code, base)
        assert rendered != code
        body = f"绑定码：{rendered}"
        if writer == "relay":
            refused(lambda: relay(world, body, "ts-rendered"), "code_sent_to_bound_account")
        else:
            refused(lambda: execute(world.owner, "send_user_conversation_message",
                                    {"request_key": "d-rendered", "project_id": HOME, "body": body},
                                    allow_write=True), "code_sent_to_bound_account")
        assert conversation(world) == [], (name, writer)
        refused(lambda: bind_message(world, code, f"m-{writer}", channel=code_channel, sender="intruder"),
                "code_retired")


def test_a_relay_reads_the_pepper_only_for_the_bound_owners_six_digits(world, monkeypatch):
    """As for a bind (K-order): the code lookup's pepper read comes after the steward
    check and the binding check, never for a stranger's or a non-steward's relay - and
    never for a bound owner's text when no code is live (the live rows are read first),
    so ordinary text carrying six digits costs no secret read."""
    bind(world)
    reads = []
    monkeypatch.setattr(drivers, "read_pepper", lambda: reads.append(1) or PEPPER)
    refused(lambda: relay(world, "123456", "ts-1", sender="U-STRANGER"), "channel_sender_not_bound")
    refused(lambda: world.director.channels.relay_owner_message(HOME, "k", "slack", SLACK_USER, "ts-2", "123456"),
            "steward_role_required")
    assert reads == []
    relay(world, "123456", "ts-3")
    assert reads == [] and [m["body"] for m in conversation(world)] == ["123456"]
    # With a live code the same text IS checked, and the check reads the pepper once
    # (issue_code's own read is the digest's, so only the check's read is counted here).
    code = world.owner.channels.issue_code("wechat")["code"]
    reads.clear()
    refused(lambda: relay(world, f"code {code}", "ts-4"), "code_sent_to_bound_account")
    assert reads == [1]


def test_six_digits_that_are_no_live_code_are_an_ordinary_message(world):
    bind(world)
    code = world.owner.channels.issue_code("wechat")["code"]
    other = f"{(int(code) + 1) % 10 ** 6:06d}"
    relay(world, other, "ts-1")
    assert [m["body"] for m in conversation(world)] == [other]
    assert bind_message(world, code, "m-1", channel="wechat", sender="wxid_owner")["bound"], "the code is still live"


def test_a_relay_is_idempotent_by_request_key_and_by_channel_message(world):
    bind(world)
    first = relay(world, "hello", "ts-1", key="k1")
    assert first["duplicate"] is False
    assert relay(world, "hello", "ts-1", key="k1") == {**first, "duplicate": True}, \
        "a replay of the same request is a redelivery too"
    again = relay(world, "hello", "ts-1", key="k2")
    assert again == {**first, "duplicate": True}
    with pytest.raises(StateGraphError, match="idempotency_conflict"):
        relay(world, "other", "ts-9", key="k1")
    assert len(conversation(world)) == 1


def test_another_senders_message_with_the_owners_ref_is_judged_on_its_own(world):
    """The duplicate-by-ref answer belongs to the sender of the first message: an unbound
    sender relaying with the owner's channel_message_ref used to get the owner's first
    result back (the executed proposal record included) as duplicate: true."""
    bind(world)
    plant_proposal(world)
    first = relay(world, "同意 proposal p-1", "ts-1", key="owner")
    assert first["approval"]["outcome"] == "executed"
    refused(lambda: relay(world, "同意 proposal p-1", "ts-1", key="stranger", sender="U-STRANGER"),
            "channel_sender_not_bound")
    # The owner's own redelivery is still the first result.
    assert relay(world, "同意 proposal p-1", "ts-1", key="again") == {**first, "duplicate": True}
    # A new bound account's message that happens to carry the same ref is a new message.
    bind(world, sender="U-NEW", key="bind-new")
    second = relay(world, "hello", "ts-1", key="new", sender="U-NEW")
    assert second["duplicate"] is False and second["message_id"] != first["message_id"]
    assert len(conversation(world)) == 2


@pytest.mark.parametrize("body,accepted", [
    ("a" * 4096, True), ("a" * 4097, False),
    ("汉" * 1365 + "a", True), ("汉" * 1366, False)])   # 4096 / 4098 UTF-8 bytes
def test_the_body_is_bounded_at_4_kib_of_utf8(world, caplog, body, accepted):
    caplog.set_level(logging.WARNING, logger="core.state_channels")
    bind(world)
    if accepted:
        assert relay(world, body, "ts")["message_id"]
        return
    refused(lambda: relay(world, body, "ts"), "body_too_long")
    assert conversation(world) == []
    [line] = [r.getMessage() for r in caplog.records]
    assert f"bytes={len(body.encode())}" in line and body[:20] not in line


def test_invalid_channel_is_the_services_own_guard_behind_the_commands_field_check(world):
    """U-e: the command models type `channel` as Literal['slack', 'wechat'], so over the
    command boundary another channel is a field error (a codeless 422, which the host
    answers as invalid_request) before the service runs; `invalid_channel` is the
    service's guard for a direct caller. Both are pinned here."""
    bind(world)
    with pytest.raises(StateGraphError) as caught:
        relay(world, "hello", "ts-1", channel="irc")
    assert "'field': 'channel'" in str(caught.value) and not str(caught.value).startswith("invalid_channel")
    with pytest.raises(StateGraphError) as caught:
        report(world, "s1", channel="irc", connected=True)
    assert "'field': 'channel'" in str(caught.value)
    channels = world.steward.channels
    refused(lambda: channels.relay_owner_message(HOME, "k1", "irc", SLACK_USER, "ts-2", "hello"), "invalid_channel")
    refused(lambda: channels.report_channel_status(HOME, "k2", "irc", True), "invalid_channel")
    assert conversation(world) == []


def test_an_empty_body_is_refused(world):
    bind(world)
    refused(lambda: relay(world, " \n　", "ts"), "empty_body")


def test_a_lone_surrogate_is_refused_as_bad_input_not_a_crash_that_holds_the_lane(world, caplog):
    """JSON "\\ud800" decodes to a lone UTF-16 surrogate, which UTF-8 cannot encode: it
    used to raise UnicodeEncodeError (a 500, which the host takes as an outage and the
    WeChat plugin retries for 10 minutes, every later message held behind it). Now it is
    refused like any other bad input, before anything is hashed, measured or stored."""
    caplog.set_level(logging.WARNING, logger="core.state_channels")
    for sender in ("U-STRANGER", SLACK_USER):
        refused(lambda: relay(world, "hi \ud800 secret", f"ts-{sender}", sender=sender), "invalid_body")
    bind(world)
    refused(lambda: relay(world, "hi \ud800 secret", "ts-b"), "invalid_body")
    # The bounded fields are refused by their schema (a validation 422), never a crash.
    for call in (lambda: relay(world, "hi", "ts-\udfff", key="k1"),
                 lambda: relay(world, "hi", "ts-s", key="k2", sender="U\ud800"),
                 lambda: execute(world.steward, "bind_owner_channel", {"request_key": "b", "channel": "slack",
                                                                       "sender_ref": "U\ud800", "code": "123456"},
                                 allow_write=True)):
        with pytest.raises(StateGraphError, match="unable to parse raw data as a unicode string"):
            call()
    refused(lambda: report(world, "s1", detail="scan \ud800"), "invalid_request")
    refused(lambda: report(world, "s2", qr_svg='<svg xmlns="http://www.w3.org/2000/svg">\ud800</svg>'),
            "qr_svg_refused")
    assert conversation(world) == []
    lines = [r.getMessage() for r in caplog.records]
    assert lines and all("invalid_body" in line for line in lines) and not any("secret" in line for line in lines)
    # A surrogate pair is ordinary text.
    assert relay(world, "ok \U0001F600", "ts-ok")["message_id"]


def test_a_lone_surrogate_over_http_is_a_422_refusal(world, client):
    steward = {"X-AItelier-Driver-Token": world.tokens["steward"], "Content-Type": "application/json"}
    raw = ('{"request_key": "r", "channel": "slack", "sender_ref": "U-STRANGER", "channel_message_ref": "ts", '
           '"body": "hi \\ud800"}')
    r = client.post("/api/state/commands/relay_owner_message", headers=steward, content=raw.encode())
    assert r.status_code == 422 and r.json()["detail"].startswith("invalid_body"), r.text


def test_the_rate_limit_counts_accepted_messages_per_channel(world, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="core.state_channels")
    bind(world)
    bind(world, channel="wechat", sender="wxid_owner", key="bind-2")
    for i in range(6):
        relay(world, f"m{i}", f"ts-{i}")
    refused(lambda: relay(world, "seventh", "ts-6"), "rate_limited")
    assert "rate_limited" in caplog.records[-1].getMessage() and "seventh" not in caplog.records[-1].getMessage()
    relay(world, "the other channel has its own budget", "w-1", channel="wechat", sender="wxid_owner")
    later = state_channels._now() + timedelta(seconds=61)
    monkeypatch.setattr(state_channels, "_now", lambda: later)
    relay(world, "a minute later", "ts-7")
    # The hour: 60 accepted in the last hour refuse the 61st; older ones do not count.
    monkeypatch.setenv("AITELIER_CHANNEL_RATE_PER_MINUTE", "1000")
    with world.db.get_connection() as conn:
        for i in range(60 - 7):
            conn.execute("INSERT INTO state_message_sources(message_id,direction,channel,sender_ref,"
                         "channel_message_ref,at) VALUES(?,'in','slack',?,?,?)",
                         (f"planted-{i}", SLACK_USER, f"p-{i}", (later - timedelta(minutes=30)).isoformat()))
        conn.execute("INSERT INTO state_message_sources(message_id,direction,channel,sender_ref,"
                     "channel_message_ref,at) VALUES('old','in','slack',?,'old',?)",
                     (SLACK_USER, (later - timedelta(minutes=61)).isoformat()))
        conn.commit()
    refused(lambda: relay(world, "sixty-first", "ts-8"), "rate_limited")
    monkeypatch.setenv("AITELIER_CHANNEL_RATE_PER_HOUR", "61")
    relay(world, "the env override raises it", "ts-9")


def test_a_body_carrying_a_live_code_is_refused_as_a_code_even_when_the_channels_minute_is_spent(world):
    """The channel client stores a TRANSIENTLY refused message in a durable owed store
    (integrations/dsh-director/lib/channels.mjs writes it ahead of the host's answer), so
    `rate_limited` must never be the answer to a body that carries a live code: the code
    check runs first, retires the code, and the answer is terminal - nothing to persist."""
    bind(world)
    code = world.owner.channels.issue_code("slack")["code"]
    for i in range(6):
        relay(world, f"m{i}", f"ts-{i}")
    refused(lambda: relay(world, f"binding code: {code}", "ts-code"), "code_sent_to_bound_account")
    # the conversation reads newest first (created_at DESC), and the refused body is not in it
    assert [m["body"] for m in conversation(world)] == [f"m{i}" for i in reversed(range(6))]
    refused(lambda: bind_message(world, code, "m-after", sender="intruder"), "code_retired")


# --- steward only / owner only ----------------------------------------------------------------

def test_the_channel_commands_are_the_stewards_and_the_reads_the_owners_or_the_stewards(world):
    bind(world)
    args = {"relay_owner_message": {"request_key": "x", "project_id": HOME, "channel": "slack",
                                    "sender_ref": SLACK_USER, "channel_message_ref": "r", "body": "b"},
            "bind_owner_channel": {"request_key": "x", "project_id": HOME, "channel": "slack",
                                   "sender_ref": SLACK_USER, "code": "123456"},
            "record_channel_delivery": {"request_key": "x", "project_id": HOME, "channel": "slack",
                                        "message_id": "m"}}
    assert set(args) <= STEWARD_WRITE_ACTIONS
    for caller in (world.owner, world.director):
        for action, arguments in args.items():
            refused(lambda: execute(caller, action, arguments, allow_write=True), "steward_role_required")
    refused(lambda: execute(world.director, "list_owner_channels", {}), "owner_or_steward_required")
    refused(lambda: execute(world.director, "message_sources", {"message_ids": []}), "owner_or_steward_required")
    assert execute(world.steward, "list_owner_channels", {})["channels"][0]["bound"]
    # The model never approves: nothing the steward may call decides a proposal.
    assert not {a for a in STEWARD_WRITE_ACTIONS if "approve" in a or "decline" in a}


def test_a_delivery_is_recorded_once_for_the_stewards_own_message(world):
    sent = execute(world.steward, "send_user_message", {"request_key": "s1", "subject": "Hi", "body": "hello",
                                                         "project_id": HOME}, allow_write=True)
    args = {"request_key": "d1", "message_id": sent["message_id"], "channel": "slack",
            "channel_message_ref": "171.2"}
    out = execute(world.steward, "record_channel_delivery", args, allow_write=True)
    assert out == {"recorded": True, "message_id": sent["message_id"], "channel": "slack"}
    assert execute(world.steward, "record_channel_delivery", args, allow_write=True) == out
    with pytest.raises(StateGraphError, match="delivery_recorded"):
        execute(world.steward, "record_channel_delivery", {**args, "request_key": "d2", "error": "x"},
                allow_write=True)
    source = execute(world.owner, "message_sources", {"message_ids": [sent["message_id"]]})["sources"]
    assert source[sent["message_id"]]["direction"] == "out" and source[sent["message_id"]]["error"] is None
    owner_said = execute(world.owner, "send_user_conversation_message",
                         {"request_key": "o", "project_id": HOME, "body": "hi"}, allow_write=True)
    with pytest.raises(StateGraphError, match="unknown_message"):
        execute(world.steward, "record_channel_delivery",
                {"request_key": "d3", "message_id": owner_said["message_id"], "channel": "wechat",
                 "error": "send failed"}, allow_write=True)


# --- channel status (the host's report, the owner's dashboard) ---------------------------------

QR = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 29 29" shape-rendering="crispEdges">'
      '<path fill="#fff" d="M0 0h29v29H0z"/><path d="M4 4h7v1H4zM4 5h1v1H4z"/></svg>')


def report(world, key, service=None, **fields):
    return execute(service or world.steward, "report_channel_status",
                   {"request_key": key, "project_id": HOME, "channel": "wechat", "connected": False, **fields},
                   allow_write=True)


def test_the_hosts_status_report_reaches_the_owners_list_and_only_there(world):
    assert all(c["status"] is None and c["qr_svg"] is None
               for c in world.owner.channels.list_owner_channels(with_status=True)["channels"]), "nothing reported"
    out = report(world, "s1", detail="scan needed", qr_svg=QR)
    assert out == {"recorded": True, "channel": "wechat", "connected": False, "at": out["at"],
                   "down_since": out["at"], "up_due": False, "alerted_since": None, "alert_attempts": 0}
    assert report(world, "s1", detail="scan needed", qr_svg=QR) == out, "a replay"
    listed = {c["channel"]: c for c in world.owner.channels.list_owner_channels(with_status=True)["channels"]}
    assert listed["wechat"]["status"] == {"connected": False, "detail": "scan needed", "at": out["at"],
                                          "down_since": out["at"], "alerted_at": None, "alert_via": None,
                                          "alert_error": None}
    assert listed["wechat"]["qr_svg"] == QR
    assert listed["slack"]["status"] is None and listed["slack"]["qr_svg"] is None
    # A later report replaces the row: logged in, the QR is gone.
    report(world, "s2", connected=True)
    wechat = world.owner.channels.list_owner_channels(with_status=True)["channels"][1]
    assert wechat["status"]["connected"] is True and wechat["status"]["detail"] is None and wechat["qr_svg"] is None
    # The State query (the steward's, and the owner's) never carries the status or the QR.
    for caller in (world.steward, world.owner):
        assert all("status" not in c and "qr_svg" not in c
                   for c in execute(caller, "list_owner_channels", {})["channels"])
    refused(lambda: world.steward.channels.list_owner_channels(with_status=True), "owner_session_required")


def test_the_wechat_bridges_own_drawing_of_a_login_qr_is_accepted(world):
    # integrations/wechat-bridge/qr-svg.test.mjs pins this file to the bridge's real output.
    drawn = (Path(__file__).resolve().parents[2] / "integrations" / "wechat-bridge" / "test-fixtures"
             / "login-qr.svg").read_text(encoding="utf-8")
    report(world, "real", detail="scan needed", qr_svg=drawn)
    assert world.owner.channels.list_owner_channels(with_status=True)["channels"][1]["qr_svg"] == drawn


@pytest.mark.parametrize("svg", [
    QR,
    '<svg xmlns="http://www.w3.org/2000/svg" width="29" height="29"><g fill="#000"><rect x="4" y="4" width="7" '
    'height="1"/></g></svg>',
    '  <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 2 2">\n <path stroke="#000" stroke-width="1" '
    'd="M0 0.5h2"/>\n</svg>\n',
])
def test_a_plain_svg_of_shapes_is_accepted(world, svg):
    report(world, "plain", qr_svg=svg)
    assert world.owner.channels.list_owner_channels(with_status=True)["channels"][1]["qr_svg"] == svg


def test_only_the_steward_reports_a_status(world):
    assert "report_channel_status" in STEWARD_WRITE_ACTIONS
    for caller in (world.owner, world.director):
        refused(lambda: report(world, "x", service=caller, qr_svg=QR), "steward_role_required")
    with world.db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM state_owner_channel_status").fetchone()[0] == 0


@pytest.mark.parametrize("svg", [
    '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><svg:script>alert(1)</svg:script></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><rect ONCLICK = "x"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><a href="https://example.com"><rect/></a></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><use xlink:href="#x"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><foreignObject><div/></foreignObject></svg>',
    '<!DOCTYPE svg [<!ENTITY x "&#60;script&#62;">]><svg>&x;</svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><!ENTITY x "y"></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><?xml-stylesheet href="x.css"?></svg>',
    '<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"></svg>',
    '<html><svg></svg></html>', '<svgx></svgx>', 'not an image', '<svg xmlns="x">',
    # What the old denylist let through (step 19 review): it is an allowlist now.
    '<svg xmlns="http://www.w3.org/2000/svg"><style>@import url(https://example.com/x.css);</style></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><rect style="fill:url(https://example.com/x)"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><path fill="url(https://example.com/x)" d="M0 0h1"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><animate attributeName="href" values="javascript:alert(1)"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
    '<set attributeName="xlink:href" to="javascript:alert(1)"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg" xml:base="https://example.com/"><path d="M0 0h1"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><iframe src="https://example.com"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"></svg><p>x</p><svg xmlns="http://www.w3.org/2000/svg"></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"></svg>trailing text',
    '<svg xmlns="http://www.w3.org/2000/svg"><image width="1" height="1"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><svg/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0h1">text</path></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><h:div xmlns:h="http://www.w3.org/1999/xhtml"/></svg>',
    '<svg><path d="M0 0h1"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0h1" fill="&#35;000"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><!-- a comment --></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0h1" transform="rotate(1)"/></svg>',
    # Only the default SVG namespace is declared: no prefixed SVG, no other vocabulary.
    '<s:svg xmlns:s="http://www.w3.org/2000/svg"><s:path d="M0 0h1"/></s:svg>',
    '<svg xmlns="http://www.w3.org/2000/svg" xmlns:s="http://www.w3.org/2000/svg"><s:path d="M0 0h1"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg" xmlns:h="http://www.w3.org/1999/xhtml"><path d="M0 0h1"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"><path d="M0 0h1"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><g xmlns:x="urn:x"><path d="M0 0h1"/></g></svg>',
])
def test_a_qr_that_is_not_a_plain_svg_is_refused_and_never_quoted(world, svg, caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(StateGraphError) as caught:
        report(world, "bad", qr_svg=svg)
    assert str(caught.value).startswith("qr_svg_refused"), str(caught.value)
    assert svg not in str(caught.value) and svg not in caplog.text
    with world.db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM state_owner_channel_status").fetchone()[0] == 0


def test_the_status_is_bounded_and_its_contents_are_never_logged(world, caplog):
    caplog.set_level(logging.DEBUG)
    head, tail = '<svg xmlns="http://www.w3.org/2000/svg">', "</svg>"
    largest = head + "<g/>" * ((state_channels.QR_SVG_MAX_BYTES - len(head) - len(tail)) // 4) + tail
    largest += " " * (state_channels.QR_SVG_MAX_BYTES - len(largest))
    assert len(largest.encode()) == state_channels.QR_SVG_MAX_BYTES
    report(world, "big", qr_svg=largest)
    refused(lambda: report(world, "bigger", qr_svg=largest + " "), "qr_svg_too_large")
    report(world, "d300", detail="d" * 300)
    refused(lambda: report(world, "d301", detail="SECRET-DETAIL" + "d" * 288), "detail_too_long")
    with pytest.raises(StateGraphError, match="'field': 'connected'"):
        execute(world.steward, "report_channel_status", {"request_key": "c", "project_id": HOME, "channel": "wechat",
                                                         "connected": "yes"},
                allow_write=True)
    report(world, "fine", detail="SECRET-DETAIL scan needed", qr_svg=QR)
    assert "SECRET-DETAIL" not in caplog.text and QR not in caplog.text and "<svg" not in caplog.text


# --- chat approval -----------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    "同意 proposal p-1。", "同意proposal p-1", "同意　proposal p-1", " ok", "同意 proposal P-1",
    "同意  proposal p-1", "请 同意 proposal p-1", "同意 proposal p-1 谢谢", "approve proposal p-1"])
def test_anything_but_the_exact_phrase_is_an_ordinary_message(world, body):
    bind(world)
    plant_proposal(world)
    out = relay(world, body, "ts")
    assert out["approval"] is None and out["message_id"]
    assert proposal(world)["status"] == "open" and proposal(world)["version"] == 1


def test_the_exact_phrase_approves_as_the_bound_account_through_the_dashboards_path(world):
    bind(world)
    plant_proposal(world)
    out = relay(world, "  同意 proposal p-1\n", "ts-approve")
    approval = out["approval"]
    assert approval["proposal_id"] == "p-1" and approval["action"] == "approve"
    assert approval["outcome"] == "executed" and approval["result"]["status"] == "done", approval
    row = proposal(world)
    assert row["status"] == "done" and row["decided_by"] == f"owner:slack:{SLACK_USER}"
    assert {k: row["result"][k] for k in ("channel", "sender_ref", "channel_message_ref", "phrase")} == {
        "channel": "slack", "sender_ref": SLACK_USER, "channel_message_ref": "ts-approve",
        "phrase": "同意 proposal p-1"}
    with world.db.get_connection() as conn:
        assert conn.execute("SELECT 1 FROM state_projects WHERE project_id='gamma'").fetchone()
    assert any("p-1" in m["subject"] for m in execute(world.steward, "list_driver_messages", {})["messages"])
    assert relay(world, "  同意 proposal p-1\n", "ts-approve", key="other") == {**out, "duplicate": True}, \
        "a redelivered message answers the first result and decides nothing again"


def test_an_approval_replayed_under_its_request_key_says_duplicate_and_decides_nothing_again(world):
    bind(world)
    plant_proposal(world)
    out = relay(world, "同意 proposal p-1", "ts-approve", key="k")
    assert out["duplicate"] is False and out["approval"]["outcome"] == "executed"
    assert relay(world, "同意 proposal p-1", "ts-approve", key="k") == {**out, "duplicate": True}
    assert len(conversation(world)) == 1 and proposal(world)["status"] == "done"


def ledger(world):
    """The relay's two request-ledger records, as stored."""
    with world.db.get_connection() as conn:
        return {op: json.loads(result) for op, result in conn.execute(
            "SELECT operation,result_json FROM driver_inbox_requests WHERE operation LIKE 'relay_owner_message%'")}


class Stopped(BaseException):
    """The process stopping (a restart, a container recreate) at that point."""


@pytest.mark.parametrize("key", ["relay-ts-approve", "another-key"])
def test_a_stop_between_the_message_and_the_decision_is_decided_by_the_redelivery(world, monkeypatch, key):
    """races-and-failure-paths_f1 / approval-semantics_f3: the message and its ledger
    records committed with approval: null, then the process stopped before the decision.
    The redelivery (same key, or the same channel message under another) answered
    {duplicate: true, approval: null} - no line, the proposal open for good."""
    bind(world)
    plant_proposal(world)

    def stop(*args, **kwargs):
        raise Stopped()
    with monkeypatch.context() as patched:
        patched.setattr(world.steward.channels, "_decide", stop)
        with pytest.raises(Stopped):
            relay(world, "同意 proposal p-1", "ts-approve")
    assert proposal(world)["status"] == "open"
    assert all(r["approval"] is None for r in ledger(world).values()), "committed before the decision"
    assert world.steward.steward.recover() == {"settled": 0}, "recover() does not revisit an open proposal"
    again = relay(world, "同意 proposal p-1", "ts-approve", key=key)
    assert again["approval"] and again["approval"]["outcome"] == "executed", again
    assert again["duplicate"] is True and again["decided_on_replay"] is True, "the channel sends this line"
    assert again["message_id"] == conversation(world)[0]["message_id"] and len(conversation(world)) == 1
    row = proposal(world)
    assert row["status"] == "done" and row["decided_by"] == f"owner:slack:{SLACK_USER}"
    answer = {"message_id": again["message_id"], "duplicate": False, "approval": again["approval"]}
    assert all(r == answer for r in ledger(world).values()), "the ledger is repaired"
    assert relay(world, "同意 proposal p-1", "ts-approve", key="third") == {**answer, "duplicate": True}, \
        "a true duplicate from now on"
    assert proposal(world)["version"] == row["version"], "decided once"


def test_a_failed_ledger_write_after_the_decision_keeps_the_answer_for_the_redelivery(world, monkeypatch):
    """The other half of the window: the decision ran, then the ledger write failed
    (StateWriteBusy). The proposal was done but the ledger said approval: null forever."""
    from contextlib import contextmanager

    from core.state_graph import StateWriteBusy
    bind(world)
    plant_proposal(world)
    store, armed = world.steward.store, {"on": False}
    real_transaction, real_decide = store.transaction, world.steward.channels._decide

    def decide_then_arm(*args, **kwargs):
        out = real_decide(*args, **kwargs)
        armed["on"] = True  # the next write transaction is the ledger's
        return out

    @contextmanager
    def busy(*, write=False, notebook=None):
        if write and armed["on"]:
            armed["on"] = False
            raise StateWriteBusy("state_write_busy: another writer held the State write lock")
        with real_transaction(write=write, notebook=notebook) as conn:
            yield conn
    with monkeypatch.context() as patched:
        patched.setattr(world.steward.channels, "_decide", decide_then_arm)
        patched.setattr(store, "transaction", busy)
        with pytest.raises(StateWriteBusy):  # a failure: the channel holds the message and retries
            relay(world, "同意 proposal p-1", "ts-approve")
    decided = proposal(world)
    assert decided["status"] == "done"
    again = relay(world, "同意 proposal p-1", "ts-approve")
    assert again["duplicate"] is True and again["decided_on_replay"] is True, again
    assert again["approval"] == {"proposal_id": "p-1", "action": "approve", "outcome": "executed",
                                 "kind": "create_project", "target": "gamma",
                                 "result": world.owner.steward.get_proposal("p-1")}, \
        "the decision this message made, not proposal_not_open"
    assert proposal(world)["version"] == decided["version"] and proposal(world)["decided_at"] == decided["decided_at"]
    answer = {"message_id": again["message_id"], "duplicate": False, "approval": again["approval"]}
    assert all(r == answer for r in ledger(world).values()), "the ledger is repaired"
    assert relay(world, "同意 proposal p-1", "ts-approve", key="k2") == {**answer, "duplicate": True}


def test_a_stop_while_the_approval_executes_is_answered_once_recover_settles_it(world, monkeypatch):
    """Stopped after the decision began (proposal executing): a redelivery while it runs
    answers no approval and decides nothing; after recover() settles the proposal the
    redelivery answers it as this message's executed decision."""
    bind(world)
    plant_proposal(world)

    def stop(*args, **kwargs):
        raise Stopped()
    with monkeypatch.context() as patched:
        patched.setattr(world.steward.steward, "execute_create_project", stop)
        with pytest.raises(Stopped):
            relay(world, "同意 proposal p-1", "ts-approve")
    assert proposal(world)["status"] == "executing"
    running = relay(world, "同意 proposal p-1", "ts-approve")
    # No line yet - and retryable, so the channel asks again instead of counting it sent.
    assert running == {"message_id": running["message_id"], "duplicate": True, "approval": None,
                       "approval_pending": True}
    assert world.steward.steward.recover() == {"settled": 1}
    assert proposal(world)["status"] == "done"
    settled = relay(world, "同意 proposal p-1", "ts-approve")
    assert settled["decided_on_replay"] is True and settled["approval"]["outcome"] == "executed"
    assert settled["approval"]["result"]["status"] == "done" and settled["approval"]["target"] == "gamma"
    assert relay(world, "同意 proposal p-1", "ts-approve") == {"message_id": settled["message_id"], "duplicate": True,
                                                              "approval": settled["approval"]}


def test_a_delivery_while_the_decision_runs_is_retryable_not_terminal(world):
    """The window the review measured: the host's State call times out at 30 s while the
    decision keeps running, so the channel asks again - and was answered
    {duplicate: true, approval: null}, which both plugins take as terminal (they only
    retry `unavailable`). The decision then executed and the owner was told nothing. The
    answer is now marked approval_pending: retryable, so the channel asks again and gets
    the decision's line once the ledger holds it."""
    import threading
    bind(world)
    plant_proposal(world)
    gate, begun = threading.Event(), threading.Event()
    real = world.steward.steward.execute_create_project

    def slow(proposal_id):
        begun.set()
        gate.wait(10)
        return real(proposal_id)

    world.steward.steward.execute_create_project = slow
    first = {}
    worker = threading.Thread(target=lambda: first.update(out=relay(world, "同意 proposal p-1", "ts-approve",
                                                                   key="relay-A")))
    worker.start()
    try:
        assert begun.wait(10), "the first delivery began the decision"
        assert proposal(world)["status"] == "executing"
        second = relay(world, "同意 proposal p-1", "ts-approve", key="relay-B")
        assert second["duplicate"] is True and second["approval"] is None
        assert second.get("approval_pending") is True, second
    finally:
        gate.set()
        worker.join(20)
    assert first["out"]["approval"]["outcome"] == "executed"
    # The retry after that answer carries the decision, so the line is sent (duplicate or not).
    third = relay(world, "同意 proposal p-1", "ts-approve", key="relay-C")
    assert third["duplicate"] is True and third["approval"]["outcome"] == "executed"
    assert proposal(world)["status"] == "done" and proposal(world)["version"] == 3
    assert len(conversation(world)) == 1, "one message, decided once"


def _service(world):
    """The service one request builds (api.state_graph_routers.get_service): the start's scan
    runs here only when the start left the path open for it."""
    return StateService(world.db, actor="driver:" + world.steward_row["driver_id"],
                        driver_id=world.steward_row["driver_id"], project_read_trusted=True)


def _restart(world, monkeypatch):
    """A new process over the same State file: the start's scan runs again."""
    monkeypatch.setattr(state_channels, "_START_SETTLED", set())
    return _service(world)


def _restore_ledger(world, answers):
    """Put the relay records back as a stop between the decision and its ledger write leaves
    them (the decision itself is never re-run by this)."""
    with world.db.get_connection() as conn:
        for operation, answer in answers.items():
            conn.execute("UPDATE driver_inbox_requests SET result_json=? WHERE operation=?",
                         (json.dumps(answer), operation))
        conn.commit()


def test_a_start_settles_the_relay_record_a_stop_left_pending(world, monkeypatch):
    """f3's first window: the message and its `pending` ledger records commit, then the
    process stops before the decision. recover() settles proposals, never the ledger, so
    the approval stayed open forever and even a redelivery's answer was never recorded.
    A start now settles the pending relay records: the approval is decided (as the
    message that carried it) and both records hold the answer, so the redelivery the
    channel sends gets it."""
    bind(world)
    plant_proposal(world)

    def stop(*args, **kwargs):
        raise Stopped()
    with monkeypatch.context() as patched:
        patched.setattr(world.steward.channels, "_decide", stop)
        with pytest.raises(Stopped):
            relay(world, "同意 proposal p-1", "ts-approve")
    assert proposal(world)["status"] == "open" and proposal(world)["decided_by"] is None
    assert all("pending" in answer and answer["approval"] is None for answer in ledger(world).values())
    # The restart: building the steward's service runs the start's scan.
    fresh = _restart(world, monkeypatch)
    assert fresh.channels is not None
    assert proposal(world)["status"] == "done", "the start decided the approval the stop left"
    assert proposal(world)["decided_by"] == f"owner:slack:{SLACK_USER}"
    answers = ledger(world)
    assert all(answer["approval"] and answer["approval"]["outcome"] == "executed" for answer in answers.values()), \
        answers
    assert all(answer["duplicate"] is False and "pending" not in answer for answer in answers.values())
    # A redelivery now answers the decision (the channel's one line), and decides nothing again.
    again = relay(world, "同意 proposal p-1", "ts-approve")
    assert again["duplicate"] is True and again["approval"]["outcome"] == "executed"
    assert proposal(world)["version"] == 3 and len(conversation(world)) == 1


def test_a_start_settles_a_record_left_pending_after_the_decision_began(world, monkeypatch):
    """The other half of the start's repair: a stop after decide_begin leaves the
    proposal executing, so the start cannot answer yet - it leaves the record pending,
    recover() settles the proposal, and the next service of the same start repairs the
    ledger. Besides, the decision the stop left executed does not go unanswered."""
    bind(world)
    plant_proposal(world)

    def stop(*args, **kwargs):
        raise Stopped()
    with monkeypatch.context() as patched:
        patched.setattr(world.steward.steward, "execute_create_project", stop)
        with pytest.raises(Stopped):
            relay(world, "同意 proposal p-1", "ts-approve")
    assert proposal(world)["status"] == "executing"
    assert all("pending" in answer for answer in ledger(world).values())
    assert _restart(world, monkeypatch) is not None
    assert all("pending" in answer for answer in ledger(world).values()), "the decision is still executing"
    assert world.steward.steward.recover() == {"settled": 1}
    assert proposal(world)["status"] == "done"
    # The same start, another request: it settles the ledger BECAUSE the deferral re-opened
    # the path (_START_SETTLED.discard). Delete that and the record stays pending for good.
    _service(world)
    answers = ledger(world)
    assert all(answer["approval"] and answer["approval"]["outcome"] == "executed" for answer in answers.values()), \
        answers
    assert all("pending" not in answer for answer in answers.values())


def test_a_start_settles_a_driver_approval_whose_decision_already_ran(world, monkeypatch):
    """The start's scan runs from StateChannels.__init__, where `driver_runner` is always
    None (api/state_graph_routers.get_service installs it after building the service), so it
    used to park EVERY driver-kind record - including the one whose decision the stop had
    already executed. Reading that decision needs no runtime (_own_decision reads the row),
    so the scan answers it now, and only an undecided one waits for the redelivery."""
    bind(world)
    plant_proposal(world, kind="start_driver", payload={"project_id": "alpha"})
    with monkeypatch.context() as patched:  # the message commits; the process stops before the decision
        patched.setattr(world.steward.channels, "_settle", lambda *args, **kwargs: None)
        relay(world, "同意 proposal p-1", "ts-approve")
    stopped = ledger(world)
    assert all("pending" in answer and answer["approval"] is None for answer in stopped.values())
    # The decision runs inside a request (which has the runtime); the stop lands before its
    # ledger write, so the proposal is done while the ledger still says pending.
    calls = []

    def runner(kind, project_id, actor):
        calls.append((kind, project_id, actor))
        return {"project_id": project_id, "phase": "starting"}
    world.steward.steward.driver_runner = runner
    answered = world.steward.channels._settle(dict(stopped["relay_owner_message"]), "slack", SLACK_USER, "ts-approve")
    assert answered["approval"]["outcome"] == "executed"
    assert calls == [("start_driver", "alpha", f"owner:slack:{SLACK_USER}")]
    world.steward.steward.driver_runner = None
    _restore_ledger(world, stopped)
    assert all("pending" in answer for answer in ledger(world).values())
    _restart(world, monkeypatch)  # the start, with no driver runtime anywhere yet
    answers = ledger(world)
    assert all(answer["approval"] and answer["approval"]["outcome"] == "executed" for answer in answers.values()), \
        answers
    assert all(answer["duplicate"] is False and "pending" not in answer for answer in answers.values())
    assert proposal(world)["status"] == "done"


def test_a_driver_record_the_start_cannot_decide_is_parked_once_not_rescanned(world, monkeypatch):
    """A driver kind that is still OPEN needs the runtime to decide, so the start parks it
    for the channel's redelivery - it never answers driver_runtime_unavailable, which would
    tell the owner the approval failed right before recover() runs it. The count is records,
    not messages: the relay wrote two ledger rows (`relay_owner_message` and its `:ref`) and
    both stay pending. Parked is NOT deferred: the start's path stays settled, so the ledger
    scan does not run on every request of that start."""
    bind(world)
    plant_proposal(world, kind="start_driver", payload={"project_id": "alpha"})
    with monkeypatch.context() as patched:
        patched.setattr(world.steward.channels, "_settle", lambda *args, **kwargs: None)
        relay(world, "同意 proposal p-1", "ts-approve")
    scans, results = [], []
    original = state_channels.StateChannels.settle_pending_relays

    def counting(service):
        result = original(service)
        scans.append(1)
        results.append(result)
        return result
    monkeypatch.setattr(state_channels.StateChannels, "settle_pending_relays", counting)
    _restart(world, monkeypatch)  # the start's scan
    assert len(ledger(world)) == 2, "one relay writes two ledger records, both still pending"
    assert results and results[0]["parked"] == 2 and results[0]["deferred"] == 0, results
    assert all("pending" in answer for answer in ledger(world).values())
    assert proposal(world)["status"] == "open", "not answered as a refusal while recover() may run it"
    _service(world), _service(world)  # two more requests of the same start
    assert scans == [1], "parked once per start, not re-scanned on every request"
    # The redelivery is what decides it, inside a request that has the runtime.
    world.steward.steward.driver_runner = lambda kind, project_id, actor: {"project_id": project_id}
    out = relay(world, "同意 proposal p-1", "ts-approve")
    assert out["duplicate"] is True and out.get("decided_on_replay") is True
    assert out["approval"]["outcome"] == "executed" and proposal(world)["status"] == "done"


@pytest.mark.parametrize("age,outcome", [(timedelta(minutes=9, seconds=59), "executed"),
                                         (timedelta(minutes=10, seconds=1), "refused"),
                                         (timedelta(hours=3), "refused")])
def test_an_approval_written_on_the_channel_over_ten_minutes_ago_is_refused(world, age, outcome):
    bind(world)
    plant_proposal(world)
    written = (state_channels._now() - age).isoformat()
    out = relay(world, "同意 proposal p-1", "ts", channel_at=written)
    assert out["message_id"] and [m["message_id"] for m in conversation(world)] == [out["message_id"]], \
        "the message itself is written either way"
    assert out["approval"]["outcome"] == outcome, out
    if outcome == "refused":
        assert out["approval"]["refusal"] == "approval_too_old"
        assert proposal(world)["status"] == "open" and proposal(world)["decided_by"] is None
    else:
        assert proposal(world)["status"] == "done"


@pytest.mark.parametrize("ahead,outcome,refusal", [(timedelta(minutes=1), "executed", None),
                                                   (timedelta(hours=1), "refused", "approval_clock_skew"),
                                                   (timedelta(days=365), "refused", "approval_clock_skew"),
                                                   (-timedelta(hours=1), "refused", "approval_too_old")])
def test_an_approval_dated_ahead_of_now_by_more_than_two_minutes_is_refused(world, ahead, outcome, refusal):
    """The age check had no future bound: channel_at = now + 365 days executed, so a
    skewed or wrong channel clock switched the stale-approval guard off."""
    bind(world)
    plant_proposal(world)
    out = relay(world, "同意 proposal p-1", "ts", channel_at=(state_channels._now() + ahead).isoformat())
    assert out["message_id"] and out["approval"]["outcome"] == outcome, out
    assert out["approval"].get("refusal") == refusal
    assert proposal(world)["status"] == ("done" if outcome == "executed" else "open")


def test_an_undated_approval_is_refused_approval_undated_and_the_message_is_still_written(world):
    """approval-semantics_f2: the age and skew bounds applied only when channel_at was
    sent, so an undated phrase - from any channel that does not date its messages -
    executed however late it arrived. An approval now needs the channel's time."""
    bind(world)
    plant_proposal(world)
    out = relay(world, "同意 proposal p-1", "ts-1", channel_at=None)
    assert out == {"message_id": out["message_id"], "duplicate": False,
                   "approval": {"proposal_id": "p-1", "action": "approve", "outcome": "refused",
                                "refusal": "approval_undated"}}
    assert [m["message_id"] for m in conversation(world)] == [out["message_id"]], "the message is written either way"
    assert proposal(world)["status"] == "open" and proposal(world)["decided_by"] is None
    declined = relay(world, "拒绝 proposal p-1", "ts-2", channel_at=None)["approval"]
    assert declined["refusal"] == "approval_undated" and proposal(world)["status"] == "open"
    assert relay(world, "同意 proposal p-1", "ts-1", channel_at=None) == {**out, "duplicate": True}
    dated = relay(world, "同意 proposal p-1", "ts-3", channel_at=state_channels._now().isoformat())
    assert dated["approval"]["outcome"] == "executed" and proposal(world)["status"] == "done"


def test_an_ordinary_message_needs_no_channel_time(world):
    bind(world)
    plant_proposal(world)
    out = relay(world, "what is running?", "ts-1", channel_at=None)
    assert out == {"message_id": out["message_id"], "duplicate": False, "approval": None}
    assert [m["body"] for m in conversation(world)] == ["what is running?"]


def test_channel_at_without_an_offset_is_utc_and_garbage_is_refused(world):
    bind(world)
    plant_proposal(world)
    naive = (state_channels._now() - timedelta(minutes=30)).replace(tzinfo=None).isoformat()
    assert relay(world, "拒绝 proposal p-1", "ts-1", channel_at=naive)["approval"]["refusal"] == "approval_too_old"
    refused(lambda: relay(world, "hello", "ts-2", channel_at="yesterday"), "invalid_request")
    # Not part of the request's identity: a redelivery stamped anew is still the same message.
    first = relay(world, "hello", "ts-3", key="k", channel_at="2026-11-18T10:00:00Z")
    assert relay(world, "hello", "ts-3", key="k", channel_at="2026-11-18T10:00:05Z") == {**first, "duplicate": True}


def test_the_decline_phrase_declines_as_the_bound_account(world):
    bind(world, channel="wechat", sender="wxid_owner")
    plant_proposal(world)
    out = relay(world, "拒绝 proposal p-1", "w-1", channel="wechat", sender="wxid_owner")
    assert out["approval"]["outcome"] == "executed" and out["approval"]["action"] == "decline"
    row = proposal(world)
    assert row["status"] == "declined" and row["decided_by"] == "owner:wechat:wxid_owner"
    assert row["result"]["phrase"] == "拒绝 proposal p-1"


def test_an_executed_decision_names_the_stored_proposals_kind_and_target(world):
    """The channel's answer line says what was decided: the kind and project the stored
    proposal row carries, nothing taken from the message."""
    bind(world)
    plant_proposal(world, "p-1")
    plant_proposal(world, "p-2", kind="archive_project", payload={"project_id": "alpha"})
    approved = relay(world, "同意 proposal p-1", "ts-1")["approval"]
    assert (approved["outcome"], approved["kind"], approved["target"]) == ("executed", "create_project", "gamma")
    declined = relay(world, "拒绝 proposal p-2", "ts-2")["approval"]
    assert (declined["outcome"], declined["kind"], declined["target"]) == ("executed", "archive_project", "alpha")
    refused_again = relay(world, "同意 proposal p-2", "ts-3")["approval"]
    assert refused_again["outcome"] == "refused" and "kind" not in refused_again and "target" not in refused_again


def test_a_decided_or_changed_proposal_is_refused_and_left_as_it_is(world, monkeypatch):
    bind(world)
    plant_proposal(world, "p-1")
    world.owner.steward.decline("p-1", 1)
    out = relay(world, "同意 proposal p-1", "ts-1")
    assert out["approval"] == {"proposal_id": "p-1", "action": "approve", "outcome": "refused",
                               "refusal": "proposal_not_open"}
    assert proposal(world, "p-1")["status"] == "declined"
    # The version current when the message was written is the one approved: a change
    # landing between the message and the decision fails it.
    plant_proposal(world, "p-2")
    original = world.steward.steward.decide_begin

    def bumped(*args, **kwargs):
        with world.db.get_connection() as conn:
            conn.execute("UPDATE state_steward_proposals SET version=version+1 WHERE proposal_id='p-2'")
            conn.commit()
        return original(*args, **kwargs)
    monkeypatch.setattr(world.steward.steward, "decide_begin", bumped)
    out = relay(world, "同意 proposal p-2", "ts-2")
    assert out["approval"]["refusal"] == "proposal_changed"
    assert proposal(world, "p-2")["status"] == "open"


def test_m2_an_unbound_sender_never_approves(world, monkeypatch):
    bind(world)
    plant_proposal(world)
    relay(world, "hello", "ts-1")
    world.owner.channels.unbind("slack")
    refused(lambda: relay(world, "同意 proposal p-1", "ts-2"), "channel_sender_not_bound")
    assert proposal(world)["status"] == "open"
    # The re-read inside the deciding transaction: unbound between the message and
    # the decision -> refused, proposal open.
    bind(world, key="bind-again")
    original = world.steward.steward.decide_begin

    def unbound_meanwhile(*args, **kwargs):
        world.owner.channels.unbind("slack")
        return original(*args, **kwargs)
    monkeypatch.setattr(world.steward.steward, "decide_begin", unbound_meanwhile)
    out = relay(world, "同意 proposal p-1", "ts-3")
    assert out["approval"]["outcome"] == "refused" and out["approval"]["refusal"] == "channel_sender_not_bound"
    row = proposal(world)
    assert row["status"] == "open" and row["version"] == 1 and row["decided_by"] is None


def test_m2_the_deciding_transaction_rereads_the_binding_itself(world):
    bind(world)
    plant_proposal(world)
    carrier = {"channel": "slack", "sender_ref": "U-OTHER", "channel_message_ref": "x", "phrase": "p"}
    refused(lambda: world.steward.steward.decide_begin("p-1", 1, "owner:slack:U-OTHER", carrier, carrier),
            "channel_sender_not_bound")
    refused(lambda: world.steward.steward.decide_decline("p-1", 1, "owner:slack:U-OTHER", carrier, carrier),
            "channel_sender_not_bound")
    assert proposal(world)["status"] == "open"


def test_a_dashboard_phrase_decides_as_the_owner_session_unchanged(world):
    plant_proposal(world)
    out = execute(world.owner, "send_user_conversation_message",
                  {"request_key": "d1", "project_id": HOME, "body": "同意 proposal p-1"}, allow_write=True)
    assert out["approval"]["outcome"] == "executed"
    row = proposal(world)
    assert row["status"] == "done" and row["decided_by"] == "owner:" + OWNER
    assert row["result"]["phrase"] == "同意 proposal p-1" and row["result"]["message_id"] == out["message_id"]
    assert "channel" not in row["result"]
    plant_proposal(world, "p-2", payload={**CREATE, "project_id": "delta"})
    elsewhere = execute(world.owner, "send_user_conversation_message",
                        {"request_key": "d2", "project_id": "alpha", "body": "同意 proposal p-2"}, allow_write=True)
    assert "approval" not in elsewhere and proposal(world, "p-2")["status"] == "open", \
        "only the steward's conversation decides"


def test_stop_driver_with_live_work_needs_the_dashboard(world):
    bind(world)
    world.runtime.update("alpha", enabled=1, phase="running", subagents_json=json.dumps(["child-1"]))
    plant_proposal(world, kind="stop_driver", payload={"project_id": "alpha"})
    out = relay(world, "同意 proposal p-1", "ts")
    assert out["approval"]["refusal"] == "needs_dashboard_confirmation"
    assert proposal(world)["status"] == "open"


def test_a_driver_proposal_runs_the_driver_entry_or_is_refused_without_one(world):
    bind(world)
    plant_proposal(world, kind="start_driver", payload={"project_id": "alpha"})
    out = relay(world, "同意 proposal p-1", "ts-1")
    assert out["approval"]["refusal"] == "driver_runtime_unavailable" and proposal(world)["status"] == "open"
    calls = []

    def runner(kind, project_id, actor):
        calls.append((kind, project_id, actor))
        return {"project_id": project_id, "phase": "starting"}
    world.steward.steward.driver_runner = runner
    out = relay(world, "同意 proposal p-1", "ts-2")
    assert out["approval"]["result"]["status"] == "done"
    assert calls == [("start_driver", "alpha", f"owner:slack:{SLACK_USER}")]
    plant_proposal(world, "p-2", kind="stop_driver", payload={"project_id": "alpha"})
    relay(world, "同意 proposal p-2", "ts-3")
    row = proposal(world, "p-2")
    assert row["status"] == "done" and calls[-1][0] == "stop_driver"
    assert row["result"]["confirm_interrupt"] is False and row["result"]["channel"] == "slack", \
        "the confirmation record keeps the chat record"


def test_an_approval_whose_driver_run_fails_is_answered_as_the_failed_proposal(world):
    """The failed-run branch of the decision: the driver entry raises, the proposal ends
    'failed' with the error, and the answer carries that failed proposal (the channel
    says "approved, but it failed")."""
    bind(world)
    plant_proposal(world, kind="start_driver", payload={"project_id": "alpha"})

    def runner(kind, project_id, actor):
        raise RuntimeError("the driver would not start")
    world.steward.steward.driver_runner = runner
    approval = relay(world, "同意 proposal p-1", "ts-1")["approval"]
    assert (approval["outcome"], approval["kind"], approval["target"]) == ("executed", "start_driver", "alpha")
    assert approval["result"]["status"] == "failed"
    assert approval["result"]["result"]["error"] == "RuntimeError: the driver would not start"
    row = proposal(world)
    assert row["status"] == "failed" and row["decided_by"] == f"owner:slack:{SLACK_USER}"
    assert row["result"]["channel_message_ref"] == "ts-1", "the chat record stays on the failed row"


@pytest.mark.parametrize("kind,archived_before,status", [("archive_project", False, "done"),
                                                         ("unarchive_project", True, "done"),
                                                         ("archive_project", True, "failed")])
def test_an_archive_approval_runs_the_archive_as_the_bound_account(world, kind, archived_before, status):
    """The archive branch of the decision: archive / unarchive run through the store's
    archive write, recorded as the chat account that approved; a project already in
    the asked state fails the proposal, untouched."""
    bind(world)
    if archived_before:
        world.owner.store.set_project_archive("alpha", True, "owner:" + OWNER, "before")
    plant_proposal(world, kind=kind, payload={"project_id": "alpha"})
    approval = relay(world, "同意 proposal p-1", "ts-1")["approval"]
    assert (approval["outcome"], approval["kind"], approval["target"]) == ("executed", kind, "alpha")
    assert approval["result"]["status"] == status and proposal(world)["status"] == status
    wanted = kind == "archive_project"
    assert world.owner.store.is_project_archived("alpha") == (wanted if status == "done" else archived_before)
    with world.db.get_connection() as conn:
        actor = conn.execute("SELECT actor FROM state_project_archive WHERE project_id='alpha'").fetchone()[0]
    assert actor == (f"owner:slack:{SLACK_USER}" if status == "done" else "owner:" + OWNER)


# --- HTTP ------------------------------------------------------------------------------------

@pytest.fixture
def client(world, monkeypatch):
    import api.main as main
    monkeypatch.setattr(cf_access, "_TEAM_DOMAIN", "team.cloudflareaccess.com")
    monkeypatch.setattr(cf_access, "_AUD", "test-aud")
    monkeypatch.setattr(authz, "WRITERS", {OWNER})
    monkeypatch.setattr(authz.cf_access, "email_from_request_headers",
                        lambda headers, cookies=None: headers.get("X-Test-Email"))
    monkeypatch.setattr(authz, "driver_registry", lambda: world.registry)
    monkeypatch.setattr(main, "_ALLOW_EXTERNAL", True)
    app.dependency_overrides[get_db_manager] = lambda: world.db
    try:
        with TestClient(app) as c:
            monkeypatch.setattr(app.state, "_test_mode", False, raising=False)
            monkeypatch.setattr(app.state, "dsh_drivers", world.runtime, raising=False)
            assert authz.gate_enabled()
            yield c
    finally:
        app.dependency_overrides.pop(get_db_manager, None)


OWNER_HEADERS = {"X-Test-Email": OWNER}


def test_the_channel_routes_are_the_owner_sessions_alone(world, client):
    routes = [("GET", "/api/steward/channels", None), ("POST", "/api/steward/channels/slack/code", None),
              ("DELETE", "/api/steward/channels/slack", None),
              ("PUT", "/api/steward/channels/default", {"channel": "slack"}),
              ("GET", "/api/steward/message-sources?ids=a,b", None)]
    for name in ("steward", "director"):
        for method, path, body in routes:
            r = client.request(method, path, json=body, headers={"X-AItelier-Driver-Token": world.tokens[name]})
            assert r.status_code == 403, (name, method, path, r.status_code, r.text)
    code = client.post("/api/steward/channels/wechat/code", headers=OWNER_HEADERS)
    assert code.status_code == 200 and len(code.json()["code"]) == 6, code.text
    assert client.put("/api/steward/channels/default", json={"channel": "wechat"},
                      headers=OWNER_HEADERS).json() == {"default_channel": "wechat"}
    assert client.put("/api/steward/channels/default", json={"channel": "email"},
                      headers=OWNER_HEADERS).status_code == 422
    assert client.post("/api/steward/channels/email/code", headers=OWNER_HEADERS).status_code == 422
    listed = client.get("/api/steward/channels", headers=OWNER_HEADERS).json()
    assert listed["default_channel"] == "wechat" and [c["channel"] for c in listed["channels"]] == ["slack", "wechat"]
    assert client.delete("/api/steward/channels/wechat", headers=OWNER_HEADERS).status_code == 200
    assert client.get("/api/steward/message-sources?ids=a,b", headers=OWNER_HEADERS).json() == {"sources": {}}
    too_many = ",".join(f"m{i}" for i in range(201))
    assert client.get(f"/api/steward/message-sources?ids={too_many}", headers=OWNER_HEADERS).status_code == 422


def test_without_the_pepper_the_code_route_answers_503_channel_secret_missing(world, client, monkeypatch):
    monkeypatch.setattr(drivers, "read_pepper", lambda: None)
    r = client.post("/api/steward/channels/slack/code", headers=OWNER_HEADERS)
    assert r.status_code == 503 and r.json()["detail"].startswith("channel_secret_missing"), r.text


def test_the_owner_sees_the_reported_status_and_qr_over_http_and_no_one_else_does(world, client):
    steward = {"X-AItelier-Driver-Token": world.tokens["steward"]}
    r = client.post("/api/state/commands/report_channel_status", headers=steward,
                    json={"request_key": "s", "channel": "wechat", "connected": False, "detail": "scan needed",
                          "qr_svg": QR})
    assert r.status_code == 200, r.text
    bad = client.post("/api/state/commands/report_channel_status", headers=steward,
                      json={"request_key": "s2", "channel": "wechat", "connected": False,
                            "qr_svg": '<svg onload="x"></svg>'})
    assert bad.status_code == 422 and bad.json()["detail"].startswith("qr_svg_refused"), bad.text
    for name in ("steward", "director"):
        r = client.get("/api/steward/channels", headers={"X-AItelier-Driver-Token": world.tokens[name]})
        assert r.status_code == 403 and "<svg" not in r.text, (name, r.text)
    wechat = client.get("/api/steward/channels", headers=OWNER_HEADERS).json()["channels"][1]
    assert wechat["channel"] == "wechat" and wechat["qr_svg"] == QR
    assert wechat["status"]["connected"] is False and wechat["status"]["detail"] == "scan needed"
    r = client.post("/api/state/query/list_owner_channels", headers=steward, json={})
    assert r.status_code == 200 and "<svg" not in r.text and "scan needed" not in r.text, r.text


def test_a_relayed_start_approval_runs_the_driver_manager_on_the_apps_loop(world, client, monkeypatch):
    calls = []

    async def start(project_id, *, provider="aitelier", model=None, actor):
        calls.append((project_id, provider, model, actor))
        world.runtime.update(project_id, enabled=1, phase="starting")
        return world.runtime.public(world.runtime.get(project_id))
    monkeypatch.setattr(world.runtime, "start", start)
    steward = {"X-AItelier-Driver-Token": world.tokens["steward"]}
    code = client.post("/api/steward/channels/slack/code", headers=OWNER_HEADERS).json()["code"]
    r = client.post("/api/state/commands/bind_owner_channel", headers=steward,
                    json={"request_key": "b", "channel": "slack", "sender_ref": SLACK_USER, "code": code})
    assert r.status_code == 200, r.text
    plant_proposal(world, kind="start_driver", payload={"project_id": "alpha"})
    r = client.post("/api/state/commands/relay_owner_message", headers=steward,
                    json={"request_key": "r", "channel": "slack", "sender_ref": SLACK_USER,
                          "channel_message_ref": "ts", "body": "同意 proposal p-1",
                          "channel_at": state_channels._now().isoformat()})
    assert r.status_code == 200, r.text
    assert r.json()["approval"]["result"]["status"] == "done", r.text
    assert calls == [("alpha", "aitelier", None, f"owner:slack:{SLACK_USER}")]
    r = client.post("/api/state/commands/relay_owner_message", headers=steward,
                    json={"request_key": "r2", "channel": "slack", "sender_ref": "U-STRANGER",
                          "channel_message_ref": "ts2", "body": "hi"})
    assert r.status_code == 422 and r.json()["detail"].startswith("channel_sender_not_bound"), r.text


# --- a live code in ANY owner text: the driver-inbox writer, split, broken up or folded -------

def _issue(world, monkeypatch, channel, code):
    """issue_code with a code the test chooses (so a shape can be built around it)."""
    with monkeypatch.context() as patch:
        patch.setattr(state_channels.secrets, "randbelow", lambda _n: int(code))
        assert world.owner.channels.issue_code(channel)["code"] == code
    return code


def _counts(world):
    with world.db.get_connection() as conn:
        return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("driver_inbox_requests", "driver_inbox_messages", "state_message_sources")}


def _digit_windows_any(text):
    """An independent reader: every digit-valued character after NFKC, in order, everything
    else dropped, and each 6-window of that - what someone holding a copy recovers."""
    import unicodedata
    digits = "".join(str(unicodedata.digit(ch)) for ch in unicodedata.normalize("NFKC", text)
                     if unicodedata.digit(ch, None) is not None)
    return {digits[i:i + 6] for i in range(len(digits) - 5)}


def _db_holds(world, tmp_path, code):
    conn, hits = _copy(world, tmp_path), []
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        for row in conn.execute(f'SELECT * FROM "{table}"'):
            hits += [(table, cell[:120]) for cell in row if isinstance(cell, str) and code in _digit_windows_any(cell)]
    conn.close()
    return hits


def _send_conversation(world, body, key, subject=None):
    return execute(world.owner, "send_user_conversation_message",
                   {"request_key": key, "project_id": HOME, "body": body,
                    **({"subject": subject} if subject is not None else {})}, allow_write=True)


def _send_driver(world, body, key, subject="Dashboard message"):
    """The dashboard's message to a driver, as web/src/lib/api.ts messageDshDriver sends it."""
    return execute(world.owner, "send_driver_message",
                   {"request_key": key, "target_driver_id": world.steward_row["driver_id"], "project_id": HOME,
                    "subject": subject, "body": body, "kind": "note", "delivery_mode": "transient"},
                   allow_write=True)


@pytest.mark.parametrize("body", ["binding code: {code}", "{code}", "wechat {code} thanks"])
def test_a_live_code_in_the_owners_dashboard_message_to_a_driver_is_refused_retired_and_never_written(
        world, tmp_path, caplog, monkeypatch, body):
    """send_driver_message is the third writer of owner text (the dashboard's message to a
    driver, the steward included) and had no check: 'binding code: 711833' was stored in
    driver_inbox_messages.body and driver_inbox_requests.request_json, and the code stayed live
    for whoever read the State file."""
    caplog.set_level(logging.WARNING, logger="core.state_channels")
    code = _issue(world, monkeypatch, "slack", "711833")
    before = _counts(world)
    refused(lambda: _send_driver(world, body.format(code=code), "dm-1"), "code_sent_to_bound_account")
    assert _counts(world) == before, "no message, no request record, no source row"
    assert _db_holds(world, tmp_path, code) == []
    assert "code retired" in caplog.text and code not in caplog.text
    refused(lambda: bind_message(world, code, "m-steal", sender="U-INTRUDER"), "code_retired")
    # An ordinary message to the driver still goes through.
    assert _send_driver(world, "what is running?", "dm-2")["message_id"]


def test_a_live_code_in_the_driver_messages_subject_is_refused_like_one_in_the_body(world, tmp_path, monkeypatch):
    code = _issue(world, monkeypatch, "wechat", "711833")
    refused(lambda: _send_driver(world, "please add it", "dm-s", subject=f"code {code}"), "code_sent_to_bound_account")
    assert _db_holds(world, tmp_path, code) == []
    refused(lambda: bind_message(world, code, "m-steal", channel="wechat", sender="wxid-intruder"), "code_retired")


@pytest.mark.parametrize("subject,body", [("binding code 711", "833 please bind"), ("711", "833"),
                                          ("code 71183", "3")])
@pytest.mark.parametrize("writer", ["conversation", "driver_message"])
def test_a_live_code_split_over_the_subject_and_the_body_is_refused(world, tmp_path, monkeypatch, writer, subject,
                                                                    body):
    """The subject and the body land on one row: a code split between them is read back by
    joining the two, so it is scanned as one text."""
    code = _issue(world, monkeypatch, "slack", "711833")
    send = _send_conversation if writer == "conversation" else _send_driver
    before = _counts(world)
    refused(lambda: send(world, body, "split-1", subject=subject), "code_sent_to_bound_account")
    assert _counts(world) == before
    assert _db_holds(world, tmp_path, code) == []
    refused(lambda: bind_message(world, code, "m-steal", sender="U-INTRUDER"), "code_retired")


_SUPER = dict(zip("0123456789", "\u2070\u00b9\u00b2\u00b3\u2074\u2075\u2076\u2077\u2078\u2079"))
# A live code with something between its digits, or in a rendering NFKC turns into its digits
# with punctuation between them. A reader of the stored text recovers every one of them.
BROKEN_UP = [
    ("space_inside", lambda c: f"my code is {c[:3]} {c[3:]}"),
    ("dashes_inside", lambda c: "-".join(c)),
    ("newline_inside", lambda c: f"{c[:3]}\n{c[3:]}"),
    ("tab_inside", lambda c: f"{c[:3]}\t{c[3:]}"),
    ("u3000_inside", lambda c: f"{c[:3]}\u3000{c[3:]}"),
    ("zwj_between", lambda c: "\u200d".join(c)),
    ("zwsp_between", lambda c: "\u200b".join(c)),
    ("keycaps", lambda c: "".join(d + "\ufe0f\u20e3" for d in c)),
    ("parenthesized", lambda c: "".join(chr(0x2474 + int(d) - 1) for d in c)),
    ("digit_full_stop", lambda c: "".join(chr(0x2488 + int(d) - 1) for d in c)),
    ("comma_digit", lambda c: "".join(chr(0x1F101 + int(d)) for d in c)),
    ("superscript_spaced", lambda c: " ".join(_SUPER[d] for d in c)),
    # Folded forms that were already caught stay caught.
    ("dingbat_sans_circled", lambda c: "".join(chr(0x2780 + int(d) - 1) for d in c)),
    ("longer_run", lambda c: f"ref 4{c}9123"),
]


@pytest.mark.parametrize("label,shape", BROKEN_UP, ids=[label for label, _ in BROKEN_UP])
@pytest.mark.parametrize("writer", ["relay", "conversation", "driver_message"])
def test_a_live_code_broken_up_or_folded_is_refused_retired_and_never_written(world, tmp_path, monkeypatch, writer,
                                                                              label, shape):
    """digit_windows took windows inside each unbroken run of digits only, so '711 833',
    '7-1-1-8-3-3', a newline, a TAB, U+3000, a zero-width joiner or space, keycaps and the
    enclosed forms NFKC turns into '(7)(1)..', '7.1..' or '7,1,..' were written verbatim - and
    the code stayed live: a reader of the State file bound their own account with it."""
    bind(world)
    code = _issue(world, monkeypatch, "wechat", "711833")
    body = shape(code)
    before = _counts(world)
    if writer == "relay":
        refused(lambda: relay(world, body, "ts-code"), "code_sent_to_bound_account")
    elif writer == "conversation":
        refused(lambda: _send_conversation(world, body, "d-code"), "code_sent_to_bound_account")
    else:
        refused(lambda: _send_driver(world, body, "dm-code"), "code_sent_to_bound_account")
    assert _counts(world) == before, (label, "rows written")
    assert _db_holds(world, tmp_path, code) == [], label
    refused(lambda: bind_message(world, code, "m-steal", channel="wechat", sender="wxid-intruder"), "code_retired")


def test_a_db_copy_holds_no_live_code_after_every_writer_refused_one(world, tmp_path, monkeypatch):
    """After each writer has refused a code - the relay in three shapes, the conversation
    writer and the message to a driver - a copy of the State file holds none of them in any
    digit form, in any cell, nor in the raw bytes of the file and its journal."""
    bind(world)
    codes = []
    for i, (channel, body) in enumerate([("slack", "binding code: {}"), ("wechat", "{} thanks"), ("slack", "x{}x")]):
        code = _issue(world, monkeypatch, channel, f"{314159 + i}")
        codes.append(code)
        refused(lambda: relay(world, body.format(code), f"ts-{i}"), "code_sent_to_bound_account")
    codes.append(_issue(world, monkeypatch, "slack", "271828"))
    refused(lambda: _send_conversation(world, codes[-1], "d"), "code_sent_to_bound_account")
    codes.append(_issue(world, monkeypatch, "slack", "161803"))
    refused(lambda: _send_driver(world, f"code {codes[-1]}", "dm"), "code_sent_to_bound_account")
    assert {code: _db_holds(world, tmp_path, code) for code in codes} == {code: [] for code in codes}
    base = Path(world.db.db_path)
    raw = b"".join(path.read_bytes() for path in base.parent.glob(base.name + "*") if path.is_file())
    assert not [code for code in codes if code.encode() in raw]
