"""Step 24.2b P7 (design-steward 7.8.12a "出站", "导演给业主发媒体" items 3, 4, 8; pending 19;
7.8.18 "导演给业主发媒体" 后端 + 显示): a driver's message to its user carries library refs.

`send_user_message` takes `attachments: [m_ ref]`, at most 10. Which library subdirectory is
read comes from the TOKEN's driver, never the request:

- a director: `d-<driver_id>/`, the row's audience `project:<this project>`, its file present;
- the steward: its memory user's subdirectory, audience `owner`, in its own project only.

Anything else - another director's ref, another project's, the steward's ref sent by a director,
an unknown ref, a director's ref sent by the steward - refuses the whole message
`invalid_attachment` and writes nothing. 11 refs is `invalid_request`. A request without
attachments stores and replays today's payload byte for byte; the same key with the same refs
answers the same message_id.

The display route then serves a director-sent `m_` from `d-<sender_driver_id>/`; a ref of the
director's subdirectory that no message it sent names (a dashboard upload it ingested) is 404,
and a director's ref asked for on the steward's project is 404.

The real app runs with the write gate ON and real driver tokens, with the fixtures of
tests/unit/test_conversation_uploads.py; the media root is a temporary AITELIER_MEDIA_ROOT."""
from __future__ import annotations

import hashlib
import json
import os

import pytest

from core.dsh_driver import STEWARD_MEMORY_USER
from core.state_graph import StateGraphError
from core.state_service import StateService
from tests.unit.test_conversation_upload_ack import driver_id, token_headers
from tests.unit.test_conversation_uploads import HOME, OWNER, PDF, PNG, client, world  # noqa: F401

IMAGE = PNG + b"\x00outbound fixture image" * 40
PAPER = PDF + b"outbound fixture paper\n" * 40
REF_A, REF_B, REF_C = "m_00000000000000d1", "m_00000000000000d2", "m_00000000000000d3"


@pytest.fixture
def media_root(tmp_path, monkeypatch):
    root = tmp_path / "media"
    root.mkdir()
    monkeypatch.setenv("AITELIER_MEDIA_ROOT", str(root))
    return root


def shelve(root, store, ref, data, audience, mime="image/png", source="generated", channel="director"):
    """One library row of writer `store` as the host's ingest writes it, and its file."""
    sha = hashlib.sha256(data).hexdigest()
    rel = f"{store}/2026-11/{sha}.bin"
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    with open(root / store / "index.jsonl", "a", encoding="utf-8") as index:
        index.write(json.dumps({"ref": ref, "sha256": sha, "mime": mime, "size": len(data), "audience": audience,
                                "source": source, "channel": channel, "file": rel,
                                "at": "2026-11-23T00:00:00.000Z"}) + "\n")
    return path


def director_store(world, name="director"):
    return "d-" + driver_id(world, name)


def second_director(world):
    """alpha gets another member director with its own token and its own subdirectory."""
    token = world.registry.register("ada", "ada", actor="test")["token"]
    world.registry.set_membership("alpha", "ada", "member", 0, "fixture", actor="test")
    world.tokens["ada"] = token


def say(client, token, refs, project="alpha", key="out-1", body="see", **extra):
    return client.post("/api/state/commands/send_user_message",
                       json={"project_id": project, "request_key": key, "subject": "files", "body": body,
                             **({} if refs is None else {"attachments": refs}), **extra},
                       headers=token_headers(token))


def sql(world, query, args=()):
    with world.db.get_connection() as conn:
        return [dict(r) for r in conn.execute(query, args)]


def sent_rows(world):
    return sql(world, "SELECT * FROM driver_inbox_messages WHERE audience='user' ORDER BY created_at")


def stored_payload(world, key):
    return sql(world, "SELECT payload_json FROM driver_inbox_requests WHERE operation='send_user' AND request_key=?",
               (key,))[0]["payload_json"]


def show(client, project, media_id):
    return client.get(f"/api/dsh-drivers/{project}/conversation/media/{media_id}", headers={"X-Test-Email": OWNER})


def conversation(world, project):
    owner = StateService(world.db, actor="owner:" + OWNER, project_read_trusted=True)
    return owner.driver_inbox.list_user_conversation(project)["messages"]


# --- accepted ---------------------------------------------------------------------------------

def test_a_director_sends_its_own_ref_for_this_project(world, client, media_root):
    store = director_store(world)
    shelve(media_root, store, REF_A, IMAGE, "project:alpha")
    shelve(media_root, store, REF_B, PAPER, "project:alpha", mime="application/pdf")
    r = say(client, world.tokens["director"], [REF_A, REF_B])
    assert r.status_code == 200, r.text
    message_id = r.json()["message_id"]
    expected = [
        {"id": REF_A, "kind": "image", "mime": "image/png", "size": len(IMAGE), "name": f"image-{REF_A[2:10]}.png"},
        {"id": REF_B, "kind": "file", "mime": "application/pdf", "size": len(PAPER),
         "name": f"file-{REF_B[2:10]}.pdf"},
    ]
    (row,) = sent_rows(world)
    assert row["message_id"] == message_id and row["sender_driver_id"] == driver_id(world, "director")
    assert json.loads(row["attachments_json"]) == expected
    shown = next(m for m in conversation(world, "alpha") if m["message_id"] == message_id)
    assert shown["attachments"] == expected


def test_a_library_row_name_is_kept(world, client, media_root):
    store = director_store(world)
    shelve(media_root, store, REF_A, IMAGE, "project:alpha")
    with open(media_root / store / "index.jsonl", encoding="utf-8") as index:
        line = json.loads(index.readline())
    (media_root / store / "index.jsonl").write_text(json.dumps({**line, "name": "frame-12.png"}) + "\n")
    assert say(client, world.tokens["director"], [REF_A]).status_code == 200
    assert json.loads(sent_rows(world)[0]["attachments_json"])[0]["name"] == "frame-12.png"


def test_the_steward_sends_its_own_owner_ref_in_its_project(world, client, media_root):
    shelve(media_root, STEWARD_MEMORY_USER, REF_A, IMAGE, "owner", source="in", channel="wechat")
    r = say(client, world.tokens["steward"], [REF_A], project=HOME)
    assert r.status_code == 200, r.text
    assert json.loads(sent_rows(world)[0]["attachments_json"])[0]["id"] == REF_A


# --- refused, nothing written -----------------------------------------------------------------

def _no_row_after(world, client, token, refs, project="alpha"):
    before = sql(world, "SELECT COUNT(*) AS n FROM driver_inbox_messages")[0]["n"]
    r = say(client, token, refs, project=project, key="refused-1")
    assert r.status_code == 422 and str(r.json()["detail"]).startswith("invalid_attachment"), r.text
    assert sql(world, "SELECT COUNT(*) AS n FROM driver_inbox_messages")[0]["n"] == before
    assert sql(world, "SELECT 1 FROM driver_inbox_requests WHERE request_key='refused-1'") == []


@pytest.mark.parametrize("case", [
    "another_directors_ref", "another_projects_audience", "steward_ref_from_a_director", "unknown_ref",
    "file_gone", "repeated_ref", "malformed_ref", "one_bad_of_three",
])
def test_a_director_ref_that_is_not_its_own_for_this_project_refuses_the_whole_message(
        world, client, media_root, case):
    own = director_store(world)
    shelve(media_root, own, REF_A, IMAGE, "project:alpha")
    refs = {
        "another_directors_ref": [REF_B],
        "another_projects_audience": [REF_B],
        "steward_ref_from_a_director": [REF_B],
        "unknown_ref": ["m_00000000000000ff"],
        "file_gone": [REF_B],
        "repeated_ref": [REF_A, REF_A],
        "malformed_ref": ["m_XYZ"],
        "one_bad_of_three": [REF_A, REF_B, REF_C],
    }[case]
    if case == "another_directors_ref":
        second_director(world)
        shelve(media_root, director_store(world, "ada"), REF_B, IMAGE + b"ada", "project:alpha")
    elif case == "another_projects_audience":
        shelve(media_root, own, REF_B, IMAGE + b"beta", "project:beta")
    elif case == "steward_ref_from_a_director":
        shelve(media_root, STEWARD_MEMORY_USER, REF_B, IMAGE + b"owner", "owner")
    elif case == "file_gone":
        os.unlink(shelve(media_root, own, REF_B, IMAGE + b"gone", "project:alpha"))
    elif case == "one_bad_of_three":
        shelve(media_root, own, REF_B, IMAGE + b"b", "project:alpha")
        shelve(media_root, own, REF_C, IMAGE + b"c", "project:beta")
    _no_row_after(world, client, world.tokens["director"], refs)
    # Nothing was recorded under the key: the corrected message goes out under it.
    assert say(client, world.tokens["director"], [REF_A], key="refused-1").status_code == 200


def test_the_steward_sending_a_director_ref_is_refused(world, client, media_root):
    # The ref is in the director's subdirectory, even labelled for the owner: the steward's
    # token reads only its own subdirectory.
    shelve(media_root, director_store(world), REF_A, IMAGE, "owner")
    _no_row_after(world, client, world.tokens["steward"], [REF_A], project=HOME)


def test_the_steward_ref_outside_the_owner_audience_or_its_project_is_refused(world, client, media_root):
    shelve(media_root, STEWARD_MEMORY_USER, REF_A, IMAGE, "group:wechat:g1")
    shelve(media_root, STEWARD_MEMORY_USER, REF_B, IMAGE + b"owner", "owner")
    _no_row_after(world, client, world.tokens["steward"], [REF_A], project=HOME)
    # Its owner ref in a director's project: nothing there could ever show it.
    _no_row_after(world, client, world.tokens["steward"], [REF_B], project="alpha")


def test_eleven_attachments_are_invalid_request(world, client, media_root):
    refs = [f"m_{i:016x}" for i in range(11)]
    r = say(client, world.tokens["director"], refs)
    assert r.status_code == 422 and "attachments" in json.dumps(r.json()["detail"]), r.text
    did = driver_id(world, "director")
    director = StateService(world.db, actor="driver:" + did, driver_id=did, project_read_trusted=True)
    for bad in (refs, [7], "m_0000000000000001"):
        with pytest.raises(StateGraphError, match="^invalid_request$"):
            director.driver_inbox.send_user_message("k11", "files", "see", "alpha", attachments=bad)
    assert sent_rows(world) == []


# --- idempotency ------------------------------------------------------------------------------

def test_a_request_without_attachments_replays_byte_identically(world, client, media_root):
    first = say(client, world.tokens["director"], None, key="plain")
    assert first.status_code == 200, first.text
    did = driver_id(world, "director")
    today = json.dumps(["files", "see", "alpha", {}, did], sort_keys=True)
    assert stored_payload(world, "plain") == today
    assert sent_rows(world)[0]["attachments_json"] is None
    # An explicit empty list is the same request.
    again = say(client, world.tokens["director"], [], key="plain")
    assert again.json() == first.json()
    assert stored_payload(world, "plain") == today
    shelve(media_root, director_store(world), REF_A, IMAGE, "project:alpha")
    conflict = say(client, world.tokens["director"], [REF_A], key="plain")
    assert "idempotency_conflict" in str(conflict.json()["detail"]), conflict.text


def test_the_same_key_and_refs_answer_the_same_message(world, client, media_root):
    path = shelve(media_root, director_store(world), REF_A, IMAGE, "project:alpha")
    first = say(client, world.tokens["director"], [REF_A], key="shot")
    assert first.status_code == 200, first.text
    # A retry answers the stored message, even once the file was cleaned in between.
    os.unlink(path)
    again = say(client, world.tokens["director"], [REF_A], key="shot")
    assert again.json()["message_id"] == first.json()["message_id"]
    assert len(sent_rows(world)) == 1


# --- display ----------------------------------------------------------------------------------

def test_a_director_sent_ref_is_served_from_the_senders_subdirectory(world, client, media_root):
    store = director_store(world)
    shelve(media_root, store, REF_A, IMAGE, "project:alpha")
    # The same ref in the steward's subdirectory, other bytes: never the one served here.
    shelve(media_root, STEWARD_MEMORY_USER, REF_A, PAPER, "owner", mime="application/pdf")
    # A dashboard upload the director ingested: in its subdirectory, in no message it sent.
    shelve(media_root, store, REF_B, IMAGE + b"upload", "project:alpha", source="in", channel="dashboard")
    assert say(client, world.tokens["director"], [REF_A]).status_code == 200
    r = show(client, "alpha", REF_A)
    assert (r.status_code, r.content, r.headers["content-type"]) == (200, IMAGE, "image/png")
    for project, ref in (("alpha", REF_B), (HOME, REF_A)):
        r = show(client, project, ref)
        assert (r.status_code, r.json()["detail"]) == (404, "unknown_media"), (project, ref)


def test_two_directors_refs_each_come_from_their_own_subdirectory(world, client, media_root):
    second_director(world)
    shelve(media_root, director_store(world), REF_A, IMAGE, "project:alpha")
    shelve(media_root, director_store(world, "ada"), REF_B, PAPER, "project:alpha", mime="application/pdf")
    assert say(client, world.tokens["director"], [REF_A]).status_code == 200
    assert say(client, world.tokens["ada"], [REF_B], key="ada-1").status_code == 200
    assert show(client, "alpha", REF_A).content == IMAGE
    paper = show(client, "alpha", REF_B)
    assert (paper.status_code, paper.content, paper.headers["content-disposition"]) == (200, PAPER, "attachment")
