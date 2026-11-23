"""Step 24.2b P6 (design-steward 7.8.12a "显示"; 7.8.18 "显示"): the panel's display route.

`GET /api/dsh-drivers/{project_id}/conversation/media/{id}` serves one attachment of THIS
project's conversation to a project reader:

- headers as workspace_raw: the type the magic bytes decided (the upload row's or the library
  index row's mime), nosniff, `default-src 'none'; style-src 'unsafe-inline'`, private caching,
  `inline` for images and voice, `attachment` otherwise (PDF);
- who: the conversation read's verdict - a non-reader, an anonymous request and a request
  carrying `X-AItelier-Read-As` are 403, even on a public project;
- `u_...`: from staging before the first host's ack, from the library (that ack's store and ref)
  after it;
- `m_...` on the steward's project: only when a message the steward sent, or a relayed message's
  channel source row, names it; from the steward's subdirectory;
- `m_...` on a director's project: only from a message a director of this project sent, from
  that sender's `d-<driver_id>`; a steward-subdirectory ref there is 404;
- an index `file` with `..` or a symlink leaving the subdirectory is 404; a library row (or file)
  cleaned is 410.

The real app runs with the write gate ON and test mode OFF, with the fixtures of
tests/unit/test_conversation_uploads.py; the media root is a temporary AITELIER_MEDIA_ROOT."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3

import pytest

from core.dsh_driver import STEWARD_MEMORY_USER
from core.state_commands import execute
from core.state_service import StateService
from tests.unit.test_conversation_upload_ack import ack, driver_id, send
from tests.unit.test_conversation_uploads import (HOME, OWNER, PDF, PNG, client, staged,  # noqa: F401
                                                  upload, world)

IMAGE = PNG + b"\x00display fixture image" * 50
OGG = b"OggS" + b"\x00display fixture voice" * 50
PAPER = PDF + b"display fixture paper\n" * 50
CSP = "default-src 'none'; style-src 'unsafe-inline'"
REF_A, REF_B, REF_C = "m_00000000000000c1", "m_00000000000000c2", "m_00000000000000c3"


@pytest.fixture
def media_root(tmp_path, monkeypatch):
    root = tmp_path / "media"
    root.mkdir()
    monkeypatch.setenv("AITELIER_MEDIA_ROOT", str(root))
    return root


def shelve(root, store, ref, data, mime="image/png", file=None):
    """One library row of writer `store` (as the host's ingest writes it), and its file."""
    sha = hashlib.sha256(data).hexdigest()
    rel = file or f"{store}/2026-11/{sha}.bin"
    if file is None:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (root / store).mkdir(parents=True, exist_ok=True)
    with open(root / store / "index.jsonl", "a", encoding="utf-8") as index:
        index.write(json.dumps({"ref": ref, "sha256": sha, "mime": mime, "size": len(data), "audience": "owner",
                                "source": "in", "channel": "wechat", "file": rel,
                                "at": "2026-11-23T00:00:00.000Z"}) + "\n")
    return root / rel


def show(client, project, media_id, headers=None):
    return client.get(f"/api/dsh-drivers/{project}/conversation/media/{media_id}",
                      headers={"X-Test-Email": OWNER} if headers is None else headers)


def uploaded(client, body, project="alpha", name="photo.png"):
    r = upload(client, body, project=project, name=name)
    assert r.status_code == 200, r.text
    return r.json()["upload_id"]


def driver_service(world, name):
    did = driver_id(world, name)
    return StateService(world.db, actor="driver:" + did, driver_id=did, project_read_trusted=True)


def driver_says(world, name, project, refs, key="out-1"):
    """A message the driver sent its user, carrying library refs (the row as 24.2b's outbound
    attachments write it; the outbound field itself is a later package)."""
    sent = execute(driver_service(world, name), "send_user_message", {
        "project_id": project, "request_key": key, "subject": "files", "body": "see"}, allow_write=True)
    items = [{"id": ref, "kind": "image", "mime": "image/png", "size": 1, "name": f"{ref}.png"} for ref in refs]
    raw(world, "UPDATE driver_inbox_messages SET attachments_json=? WHERE message_id=?",
        (json.dumps(items), sent["message_id"]))
    return sent["message_id"]


def relayed(world, client, refs):
    """A user message on the steward's project whose inbound channel source row carries refs."""
    message_id = send(client, [], project=HOME, key="relay", body="[图片]")["message_id"]
    files = [{"kind": "image", "ref": ref, "mime": "image/png", "size": 1} for ref in refs]
    raw(world, "INSERT INTO state_message_sources(message_id,direction,channel,sender_ref,channel_message_ref,"
               "at,attachments_json) VALUES(?,'in','wechat','owner-ref','c1','2026-11-23T00:00:00Z',?)",
        (message_id, json.dumps(files)))
    return message_id


def raw(world, sql, args=()):
    conn = sqlite3.connect(world.db.db_path)
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


# --- the headers ------------------------------------------------------------------------------

@pytest.mark.parametrize("body,name,mime,disposition", [
    (IMAGE, "photo.png", "image/png", "inline"),
    (OGG, "voice.ogg", "audio/ogg", "inline"),
    (PAPER, "paper.pdf", "application/pdf", "attachment"),
])
def test_a_project_reader_gets_the_bytes_with_each_header(world, client, media_root, body, name, mime,
                                                         disposition):
    upload_id = uploaded(client, body, name=name)
    r = show(client, "alpha", upload_id)
    assert r.status_code == 200, r.text
    assert r.content == body
    assert r.headers["content-type"] == mime
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-security-policy"] == CSP
    assert r.headers["cache-control"] == "private"
    assert r.headers["content-disposition"] == disposition
    assert r.headers["content-length"] == str(len(body))


def test_the_library_mime_is_the_one_served(world, client, media_root):
    """A library file is served with its index row's mime, whatever the upload row said."""
    upload_id = uploaded(client, IMAGE)
    send(client, [upload_id])
    shelve(media_root, "d-" + driver_id(world, "director"), REF_A, OGG, mime="audio/ogg")
    assert ack(client, world.tokens["director"], upload_id, ref=REF_A).status_code == 200
    r = show(client, "alpha", upload_id)
    assert (r.status_code, r.content, r.headers["content-type"], r.headers["content-disposition"]) == \
        (200, OGG, "audio/ogg", "inline")


# --- who --------------------------------------------------------------------------------------

def test_a_non_reader_an_anonymous_request_and_a_public_read_are_403_even_on_a_public_project(
        world, client, media_root):
    upload_id = uploaded(client, IMAGE)
    send(client, [upload_id])
    # The project is opened to the public: still no bytes for any of these.
    StateService(world.db, actor="fixture", project_read_trusted=True).store.set_project_access(
        "alpha", "public", "fixture")
    for label, headers in {
        "non-reader": {"X-Test-Email": "stranger@example.test"},
        "anonymous": {},
        "public read": {"X-Test-Email": OWNER, "X-AItelier-Read-As": "public"},
    }.items():
        r = show(client, "alpha", upload_id, headers=headers)
        assert r.status_code == 403, (label, r.text)
        assert IMAGE not in r.content, label
    assert show(client, "alpha", upload_id).status_code == 200


def test_no_driver_token_gets_the_bytes_on_any_project(world, client, media_root):
    """PRIV-1: a registered driver token passes require_reader, but no host reads through the
    panel's route - the steward's token, a director's, and a driver that is no member of the
    project all get 403, on the steward's project (an owner upload, a relayed channel image)
    and on a director's (a director's sent ref)."""
    from tests.unit.test_conversation_upload_ack import token_headers
    world.tokens["outsider"] = world.registry.register("outsider", "outsider", actor="test")["token"]
    home_upload = uploaded(client, IMAGE, project=HOME, name="medical-scan.png")
    send(client, [home_upload], project=HOME, key="home-1")
    shelve(media_root, STEWARD_MEMORY_USER, REF_A, IMAGE)
    relayed(world, client, [REF_A])
    shelve(media_root, "d-" + driver_id(world, "director"), REF_B, IMAGE)
    driver_says(world, "director", "alpha", [REF_B])
    for project, media_id in ((HOME, home_upload), (HOME, REF_A), ("alpha", REF_B)):
        assert show(client, project, media_id).status_code == 200, (project, media_id)
        for name in ("steward", "director", "outsider"):
            r = show(client, project, media_id, headers=token_headers(world.tokens[name]))
            assert r.status_code == 403, (project, media_id, name, r.status_code)
            assert IMAGE not in r.content


def test_the_verdict_is_judged_in_open_for_display_itself(world, client, media_root):
    """TQ-1 and PRIV-1, below the route: a public-view or anonymous service, any driver service,
    and a person who is not the owner on the steward's project are refused by the function
    (require_reader is not what refuses them); that person still reads a director's project."""
    from core import media_library
    from core.state_commands import ProjectPrivate
    from core.state_conversation_uploads import open_for_display
    upload_id = uploaded(client, IMAGE)
    send(client, [upload_id])
    home_upload = uploaded(client, IMAGE, project=HOME)
    directory = media_library.staging_dir()
    # Opened to the public, so nothing below the verdict (the store's unopened-project check)
    # refuses the public view or the anonymous service on its own.
    StateService(world.db, actor="fixture", project_read_trusted=True).store.set_project_access(
        "alpha", "public", "fixture")
    refused = {
        "public view": (StateService(world.db, actor="owner:" + OWNER, project_read_trusted=False), "alpha", upload_id),
        "anonymous": (StateService(world.db, actor="anonymous", project_read_trusted=False), "alpha", upload_id),
        "driver": (driver_service(world, "director"), "alpha", upload_id),
        "steward driver": (driver_service(world, "steward"), HOME, home_upload),
        "reader on the steward's project": (StateService(world.db, actor="reader@example.test", project_read_trusted=True),
                                            HOME, home_upload),
    }
    for label, (service, project, media_id) in refused.items():
        with pytest.raises(ProjectPrivate):
            open_for_display(service, project, media_id, directory)
        assert label
    reader = StateService(world.db, actor="reader@example.test", project_read_trusted=True)
    handle, mime, kind, size = open_for_display(reader, "alpha", upload_id, directory)
    with handle:
        assert (handle.read(), mime, kind, size) == (IMAGE, "image/png", "image", len(IMAGE))
    owner = StateService(world.db, actor="owner:" + OWNER, project_read_trusted=True)
    handle, *_ = open_for_display(owner, HOME, home_upload, directory)
    handle.close()


def test_an_upload_nobody_came_for_in_7_days_is_gone_on_the_display_route_too(world, client, media_root):
    """Nit C4: the sweep also runs from the display route, so a stopped director's upload is
    410 here (and refused gone in the conversation) without another upload, fetch or ack."""
    upload_id = uploaded(client, IMAGE)
    send(client, [upload_id])
    assert show(client, "alpha", upload_id).status_code == 200
    raw(world, "UPDATE state_conversation_uploads SET created_at='2026-01-01T00:00:00.000000+00:00' WHERE upload_id=?",
        (upload_id,))
    assert show(client, "alpha", upload_id).status_code == 410
    (entry,) = next(m for m in StateService(world.db, actor="owner:" + OWNER, project_read_trusted=True)
                    .driver_inbox.list_user_conversation("alpha")["messages"] if m.get("attachments"))["attachments"]
    assert entry["refused"] == "gone"


# --- u_ ---------------------------------------------------------------------------------------

def test_an_upload_is_served_from_staging_before_the_ack_and_from_the_library_after(world, client, media_root):
    upload_id = uploaded(client, IMAGE)
    # Before any message: the input area plays and previews the finished upload.
    assert show(client, "alpha", upload_id).content == IMAGE
    send(client, [upload_id])
    assert show(client, "alpha", upload_id).content == IMAGE
    # The host stored it; the library copy differs from staging so the source is visible.
    stored = IMAGE + b"library copy"
    shelve(media_root, "d-" + driver_id(world, "director"), REF_A, stored)
    assert ack(client, world.tokens["director"], upload_id, ref=REF_A).status_code == 200
    assert upload_id not in staged(world)
    r = show(client, "alpha", upload_id)
    assert (r.status_code, r.content) == (200, stored)
    # Another project's path does not reach this project's upload.
    assert show(client, "beta", upload_id).status_code == 404


def test_an_upload_whose_library_row_or_file_was_cleaned_is_410(world, client, media_root):
    first, second = uploaded(client, IMAGE), uploaded(client, IMAGE + b"2")
    send(client, [first, second])
    store = "d-" + driver_id(world, "director")
    path = shelve(media_root, store, REF_A, IMAGE)
    shelve(media_root, store, REF_B, IMAGE + b"2")
    assert ack(client, world.tokens["director"], first, ref=REF_A).status_code == 200
    assert ack(client, world.tokens["director"], second, ref=REF_B).status_code == 200
    assert show(client, "alpha", first).status_code == 200
    # The 30-day cleanup removes the file ...
    os.unlink(path)
    r = show(client, "alpha", first)
    assert (r.status_code, r.json()["detail"]) == (410, "gone")
    # ... and its index row too: still gone, not unknown.
    (media_root / store / "index.jsonl").write_text("")
    assert show(client, "alpha", second).status_code == 410


def test_a_refused_upload_and_a_removed_staging_file_are_410(world, client, media_root):
    refused, lost = uploaded(client, IMAGE), uploaded(client, IMAGE + b"lost")
    send(client, [refused, lost])
    assert ack(client, world.tokens["director"], refused, refused="too_large").status_code == 200
    assert show(client, "alpha", refused).status_code == 410
    os.unlink(world.staging / lost)
    assert show(client, "alpha", lost).status_code == 410


# --- m_ ---------------------------------------------------------------------------------------

def test_steward_project_refs_named_by_the_conversation_come_from_the_steward_subdirectory(
        world, client, media_root):
    shelve(media_root, STEWARD_MEMORY_USER, REF_A, IMAGE)
    shelve(media_root, STEWARD_MEMORY_USER, REF_B, OGG, mime="audio/ogg")
    shelve(media_root, STEWARD_MEMORY_USER, REF_C, PAPER, mime="application/pdf")
    driver_says(world, "steward", HOME, [REF_A])
    relayed(world, client, [REF_B])
    sent = show(client, HOME, REF_A)
    assert (sent.status_code, sent.content, sent.headers["content-type"]) == (200, IMAGE, "image/png")
    channel = show(client, HOME, REF_B)
    assert (channel.status_code, channel.content, channel.headers["content-type"]) == (200, OGG, "audio/ogg")
    # In the steward's library, but no message of this conversation names it.
    assert show(client, HOME, REF_C).status_code == 404


def test_a_ref_not_in_this_projects_conversation_is_404(world, client, media_root):
    shelve(media_root, STEWARD_MEMORY_USER, REF_A, IMAGE)
    driver_says(world, "steward", HOME, [REF_A])
    assert show(client, HOME, REF_A).status_code == 200
    # The steward's ref asked for on a director's project, and malformed ids.
    for project, media_id in (("alpha", REF_A), ("alpha", "m_00000000000000ff"), ("alpha", "m_XYZ"),
                              ("alpha", "x_00000000000000c1"), (HOME, "u_00000000000000ff")):
        r = show(client, project, media_id)
        assert (r.status_code, r.json()["detail"]) == (404, "unknown_media"), (project, media_id)


def test_director_project_refs_come_only_from_its_directors_own_messages_and_subdirectory(
        world, client, media_root):
    director = "d-" + driver_id(world, "director")
    shelve(media_root, director, REF_A, IMAGE)
    shelve(media_root, STEWARD_MEMORY_USER, REF_B, OGG, mime="audio/ogg")
    shelve(media_root, director, REF_C, PAPER, mime="application/pdf")
    driver_says(world, "director", "alpha", [REF_A, REF_B])
    r = show(client, "alpha", REF_A)
    assert (r.status_code, r.content) == (200, IMAGE)
    # A steward-subdirectory ref named by the director's own row (the outbound check refuses
    # sending one): the store is the sender's d-<driver_id>, never the steward's, and that
    # subdirectory has no such row - it reads as cleaned, and the steward's bytes never leave.
    r = show(client, "alpha", REF_B)
    assert (r.status_code, r.json()["detail"]) == (410, "gone")
    # In the director's subdirectory (e.g. a dashboard upload it ingested) but in no message it sent.
    assert show(client, "alpha", REF_C).status_code == 404
    # A user message naming a ref does not count on a director's project.
    raw(world, "UPDATE driver_inbox_messages SET attachments_json=? WHERE message_id=?",
        (json.dumps([{"id": REF_C, "kind": "file", "mime": "application/pdf", "size": 1, "name": "p.pdf"}]),
         send(client, [], key="user-row")["message_id"]))
    assert show(client, "alpha", REF_C).status_code == 404


def test_a_steward_ref_on_a_director_project_is_404(world, client, media_root):
    shelve(media_root, STEWARD_MEMORY_USER, REF_A, IMAGE)
    relayed(world, client, [REF_A])
    assert show(client, HOME, REF_A).status_code == 200
    assert show(client, "alpha", REF_A).status_code == 404


# --- the index file must stay in its subdirectory --------------------------------------------

def test_an_index_file_with_dotdot_or_a_symlink_escape_is_404(world, client, media_root, tmp_path):
    outside = tmp_path / "outside.png"
    outside.write_bytes(IMAGE)
    (media_root / "d-other").mkdir()
    (media_root / "d-other" / "x.png").write_bytes(IMAGE)
    escaping = media_root / STEWARD_MEMORY_USER / "2026-11" / "escape.png"
    escaping.parent.mkdir(parents=True)
    os.symlink(outside, escaping)
    shelve(media_root, STEWARD_MEMORY_USER, REF_A, IMAGE, file=f"{STEWARD_MEMORY_USER}/../d-other/x.png")
    shelve(media_root, STEWARD_MEMORY_USER, REF_B, IMAGE, file=f"{STEWARD_MEMORY_USER}/2026-11/escape.png")
    shelve(media_root, STEWARD_MEMORY_USER, REF_C, IMAGE, file="d-other/x.png")
    driver_says(world, "steward", HOME, [REF_A, REF_B, REF_C])
    for ref in (REF_A, REF_B, REF_C):
        r = show(client, HOME, ref)
        assert (r.status_code, r.json()["detail"]) == (404, "unknown_media"), ref
