"""Step 24.2b P5 (design-steward 7.8.12a "宿主入库", "隐私"; 7.8.18 "取暂存与ack", "名单"): the
hosts take a dashboard upload's staged bytes and acknowledge it.

- `GET /api/dsh-drivers/{project_id}/uploads/{upload_id}` streams the staged bytes to a driver
  that is a delivery target of the message carrying the upload; any other caller - and an
  unknown id - gets the same 404; a target whose file is already removed gets 410 `gone`;
- `ack_conversation_upload {upload_id, ref | refused}` records the store from the TOKEN's driver
  (the steward's memory user, a director's `d-<driver_id>`), never from the host; the first
  ack's store and ref (or refusal) are kept; the staged file is removed once every target has
  acknowledged;
- an upload nobody came for in 7 days is refused `gone` and its file removed;
- a refusal is shown on the message's attachment (the inbox read and the conversation read).

The real app runs with the write gate ON and test mode OFF and real driver tokens, as in
tests/unit/test_conversation_uploads.py, whose fixtures these reuse."""
from __future__ import annotations

import json
import os
from datetime import timedelta

from core import state_conversation_uploads as uploads
from core.dsh_driver import STEWARD_MEMORY_USER
from tests.unit.test_conversation_uploads import (HOME, OWNER, PNG, client, rows, staged,  # noqa: F401
                                                  upload, world)

BYTES = PNG + b"\x00fixture image bytes" * 100
REF1, REF2 = "m_00000000000000a1", "m_00000000000000a2"


def token_headers(token):
    return {"X-AItelier-Driver-Token": token}


def send(client, upload_ids, project="alpha", key="k1", body="see attached"):
    r = client.post("/api/state/commands/send_user_conversation_message",
                    json={"project_id": project, "request_key": key, "body": body, "attachments": upload_ids},
                    headers={"X-Test-Email": OWNER})
    assert r.status_code == 200, r.text
    return r.json()


def uploaded(client, project="alpha", body=BYTES):
    r = upload(client, body, project=project)
    assert r.status_code == 200, r.text
    return r.json()["upload_id"]


def fetch(client, token, upload_id, project="alpha"):
    return client.get(f"/api/dsh-drivers/{project}/uploads/{upload_id}", headers=token_headers(token))


def ack(client, token, upload_id, project="alpha", **outcome):
    return client.post("/api/state/commands/ack_conversation_upload",
                       json={"project_id": project, "upload_id": upload_id, **outcome},
                       headers=token_headers(token))


def row(world, upload_id):
    return next(r for r in rows(world) if r["upload_id"] == upload_id)


def driver_id(world, name):
    return world.registry.lookup_token(world.tokens[name])["driver_id"]


def second_target(world):
    """alpha gets a second member director with its own token: a message there has two targets."""
    token = world.registry.register("ada", "ada", actor="test")["token"]
    world.registry.set_membership("alpha", "ada", "member", 0, "fixture", actor="test")
    world.tokens["ada"] = token
    return token


# --- the fetch --------------------------------------------------------------------------------

def test_the_target_gets_the_bytes_streamed(world, client):
    upload_id = uploaded(client)
    send(client, [upload_id])
    r = fetch(client, world.tokens["director"], upload_id)
    assert r.status_code == 200, r.text
    assert r.content == BYTES
    assert r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-length"] == str(len(BYTES))
    # Fetching does not consume anything: the target may fetch again (a retry of its tick).
    assert fetch(client, world.tokens["director"], upload_id).content == BYTES


def test_anyone_but_a_delivery_target_gets_the_same_404_as_an_unknown_id(world, client):
    upload_id = uploaded(client)
    unsent = uploaded(client)
    send(client, [upload_id])
    unknown = fetch(client, world.tokens["director"], "u_ffffffffffffffff")
    assert unknown.status_code == 404, unknown.text
    others = {
        # the steward is a driver, but not a target of a message to alpha's director
        "non-target driver": fetch(client, world.tokens["steward"], upload_id),
        # the target itself, asking through another project's path
        "other project": fetch(client, world.tokens["director"], upload_id, project="beta"),
        # an upload no message carries yet
        "unsent": fetch(client, world.tokens["director"], unsent),
        "malformed id": fetch(client, world.tokens["director"], "u_XYZ"),
        # the owner's browser is a reader, not a host
        "owner": client.get(f"/api/dsh-drivers/alpha/uploads/{upload_id}", headers={"X-Test-Email": OWNER}),
    }
    for name, response in others.items():
        assert (response.status_code, response.content) == (unknown.status_code, unknown.content), name
    # An anonymous request never reaches the row.
    assert client.get(f"/api/dsh-drivers/alpha/uploads/{upload_id}").status_code == 403
    # ... and the same for the ack: a non-target's ack is the unknown id's answer, nothing written.
    unknown_ack = ack(client, world.tokens["director"], "u_ffffffffffffffff", ref=REF1)
    assert unknown_ack.status_code == 404, unknown_ack.text
    for response in (ack(client, world.tokens["steward"], upload_id, ref=REF1),
                     ack(client, world.tokens["director"], upload_id, project="beta", ref=REF1),
                     ack(client, world.tokens["director"], unsent, ref=REF1)):
        assert (response.status_code, response.content) == (unknown_ack.status_code, unknown_ack.content)
    assert row(world, upload_id)["state"] == "attached" and row(world, upload_id)["ref"] is None
    assert row(world, unsent)["state"] == "staged"
    assert upload_id in staged(world) and unsent in staged(world)


# --- the ack ----------------------------------------------------------------------------------

def test_with_two_targets_the_file_survives_the_first_ack_and_is_gone_after_the_second(world, client):
    second_target(world)
    upload_id = uploaded(client)
    send(client, [upload_id])
    director = driver_id(world, "director")
    assert sorted(json.loads(row(world, upload_id)["pending_json"])) == sorted(["ada", director])
    assert fetch(client, world.tokens["director"], upload_id).content == BYTES
    first = ack(client, world.tokens["director"], upload_id, ref=REF1)
    assert first.status_code == 200, first.text
    assert first.json() == {"upload_id": upload_id, "project_id": "alpha", "state": "stored", "waiting": 1,
                            "store": "d-" + director, "ref": REF1}
    assert upload_id in staged(world)
    stored = row(world, upload_id)
    assert (stored["state"], stored["store"], stored["ref"]) == ("stored", "d-" + director, REF1)
    assert json.loads(stored["pending_json"]) == ["ada"]
    # The second target still gets the bytes, and its ack removes the staged file.
    assert fetch(client, world.tokens["ada"], upload_id).content == BYTES
    second = ack(client, world.tokens["ada"], upload_id, ref=REF2)
    assert second.status_code == 200, second.text
    assert upload_id not in staged(world)
    # The first ack's store and ref are the ones kept.
    done = row(world, upload_id)
    assert (done["state"], done["store"], done["ref"], json.loads(done["pending_json"])) == \
        ("stored", "d-" + director, REF1, [])
    assert second.json()["ref"] == REF1 and second.json()["waiting"] == 0
    # The file is gone now: a target is told so; a repeated ack changes nothing.
    r = fetch(client, world.tokens["ada"], upload_id)
    assert (r.status_code, r.json()["detail"]) == (410, "gone")
    again = ack(client, world.tokens["director"], upload_id, refused="too_large")
    assert again.status_code == 200 and again.json()["state"] == "stored"
    assert row(world, upload_id) == done


def test_with_two_targets_a_first_ack_that_refuses_ends_the_waiting_and_drops_the_file(world, client):
    """C3: the refusal goes onto the message row both targets read, and a host does not fetch an
    attachment marked refused - so the second target is not waited for: the staged file goes at
    the first ack, not after the 7-day sweep. The second target's late ack changes nothing."""
    second_target(world)
    upload_id = uploaded(client)
    send(client, [upload_id])
    first = ack(client, world.tokens["director"], upload_id, refused="too_large")
    assert first.status_code == 200, first.text
    assert first.json() == {"upload_id": upload_id, "project_id": "alpha", "state": "refused", "waiting": 0,
                            "refused": "too_large"}
    assert upload_id not in staged(world)
    got = row(world, upload_id)
    assert (got["state"], got["refused"], json.loads(got["pending_json"])) == ("refused", "too_large", [])
    # The second target reads the refusal (and so does not fetch); a fetch would be gone anyway.
    inbox = client.post("/api/state/query/list_driver_messages", json={}, headers=token_headers(world.tokens["ada"]))
    (entry,) = [m for m in inbox.json()["messages"] if m.get("attachments")]
    assert entry["attachments"][0]["refused"] == "too_large"
    r = fetch(client, world.tokens["ada"], upload_id)
    assert (r.status_code, r.json()["detail"]) == (410, "gone")
    late = ack(client, world.tokens["ada"], upload_id, ref=REF2)
    assert late.status_code == 200 and late.json()["state"] == "refused" and late.json()["waiting"] == 0
    assert row(world, upload_id) == got


def test_the_store_comes_from_the_token_never_from_the_host(world, client):
    # The steward's project: the steward is the delivery target, its store is its memory user.
    upload_id = uploaded(client, project=HOME)
    send(client, [upload_id], project=HOME)
    assert fetch(client, world.tokens["steward"], upload_id, project=HOME).content == BYTES
    # A host that names a store is refused: the request has no such field.
    claimed = ack(client, world.tokens["steward"], upload_id, project=HOME, ref=REF1, store="d-someone")
    assert claimed.status_code == 422, claimed.text
    assert row(world, upload_id)["state"] == "attached" and upload_id in staged(world)
    r = ack(client, world.tokens["steward"], upload_id, project=HOME, ref=REF1)
    assert r.status_code == 200, r.text
    got = row(world, upload_id)
    assert (got["state"], got["store"], got["ref"]) == ("stored", STEWARD_MEMORY_USER, REF1)
    assert upload_id not in staged(world)


def test_an_ack_carries_exactly_one_of_ref_and_refused(world, client):
    upload_id = uploaded(client)
    send(client, [upload_id])
    for outcome in ({}, {"ref": REF1, "refused": "gone"}, {"ref": "m_short"}, {"refused": "because"}):
        r = ack(client, world.tokens["director"], upload_id, **outcome)
        assert r.status_code == 422, (outcome, r.text)
    assert row(world, upload_id)["state"] == "attached" and upload_id in staged(world)
    assert ack(client, world.tokens["director"], upload_id, ref=REF1).status_code == 200


def test_an_ack_refused_too_large_is_stored_and_shown(world, client):
    upload_id = uploaded(client)
    sent = send(client, [upload_id])
    r = ack(client, world.tokens["director"], upload_id, refused="too_large")
    assert r.status_code == 200, r.text
    got = row(world, upload_id)
    assert (got["state"], got["refused"], got["store"], got["ref"]) == ("refused", "too_large", None, None)
    assert upload_id not in staged(world)
    shown = {"id": upload_id, "kind": "image", "mime": "image/png", "size": len(BYTES), "name": "photo.png",
             "refused": "too_large"}
    # The wake's read (the director's inbox) and the panel's conversation read both show it.
    inbox = client.post("/api/state/query/list_driver_messages", json={},
                        headers=token_headers(world.tokens["director"]))
    assert inbox.status_code == 200, inbox.text
    entry = next(m for m in inbox.json()["messages"] if m["message_id"] == sent["message_id"])
    assert entry["attachments"] == [shown]
    talk = client.get("/api/dsh-drivers/alpha/conversation", headers={"X-Test-Email": OWNER})
    assert talk.status_code == 200, talk.text
    entry = next(m for m in talk.json()["messages"] if m["message_id"] == sent["message_id"])
    assert entry["attachments"] == [shown]


# --- 7 days -----------------------------------------------------------------------------------

def test_an_upload_nobody_fetched_for_7_days_is_refused_gone_and_its_file_removed(world, client, monkeypatch):
    upload_id = uploaded(client)
    never_sent = uploaded(client)
    sent = send(client, [upload_id])
    recent = uploads._now()
    # Just under 7 days: nothing happens.
    monkeypatch.setattr(uploads, "_now", lambda: recent + timedelta(days=7) - timedelta(minutes=5))
    assert fetch(client, world.tokens["director"], upload_id).content == BYTES
    monkeypatch.setattr(uploads, "_now", lambda: recent + timedelta(days=7, minutes=1))
    r = fetch(client, world.tokens["director"], upload_id)
    assert (r.status_code, r.json()["detail"]) == (410, "gone")
    got = row(world, upload_id)
    assert (got["state"], got["refused"], json.loads(got["pending_json"])) == ("refused", "gone", [])
    assert row(world, never_sent)["state"] == "expired"
    assert staged(world) == []
    # The message shows the refusal; a late ack is answered with what is recorded.
    talk = client.get("/api/dsh-drivers/alpha/conversation", headers={"X-Test-Email": OWNER})
    entry = next(m for m in talk.json()["messages"] if m["message_id"] == sent["message_id"])
    assert entry["attachments"][0]["refused"] == "gone"
    late = ack(client, world.tokens["director"], upload_id, ref=REF1)
    assert late.status_code == 200 and late.json()["state"] == "refused" and late.json()["refused"] == "gone"
    assert row(world, upload_id)["ref"] is None


def test_a_stored_upload_another_target_never_fetched_stops_waiting_after_7_days(world, client, monkeypatch):
    second_target(world)
    upload_id = uploaded(client)
    send(client, [upload_id])
    assert ack(client, world.tokens["director"], upload_id, ref=REF1).status_code == 200
    recent = uploads._now()
    monkeypatch.setattr(uploads, "_now", lambda: recent + timedelta(days=7, minutes=1))
    r = fetch(client, world.tokens["ada"], upload_id)
    assert (r.status_code, r.json()["detail"]) == (410, "gone")
    got = row(world, upload_id)
    assert (got["state"], got["ref"], json.loads(got["pending_json"])) == ("stored", REF1, [])
    assert not os.path.exists(world.staging / upload_id)
