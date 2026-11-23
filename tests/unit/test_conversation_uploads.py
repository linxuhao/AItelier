"""The dashboard's conversation upload route (step 24.2b, design-steward 7.8.12a "上传").

`POST /api/dsh-drivers/{project_id}/conversation/uploads` (name in `X-AItelier-Upload-Name`) takes ONE file as the raw
request body. These run the real app with the write gate ON and test mode OFF, and
authenticate with real driver tokens through the registry (as tests/unit/
test_steward_read_only.py does); the owner's browser is an allowlisted Access email.

- who: no writer identity -> 403 (the gate); a director token -> `user_identity_required`
  (it is not the project's user); a steward token -> `steward_read_only` (the gate: the path
  has six parts, not one of its runtime endpoints);
- cross-site: no `X-AItelier-Upload: 1` -> 400; `Sec-Fetch-Site` present and not
  `same-origin` -> 403 (same-origin and absent pass);
- streamed sniff: the kind comes from the first bytes, never the name or Content-Type; the
  kind's cap -> 413 with nothing left in staging; outside the table -> 415 `type`;
- 20 per project per hour counted from `state_conversation_uploads` -> 429 for the 21st while
  another project's first is accepted; staging over its total -> 507;
- an accepted file is `<staging>/u_<16 hex>`, 0600, with a `staged` row.
"""
from __future__ import annotations

import copy
import json
import os
import re
import sqlite3
import stat
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from api import authz
from api.dependencies import get_db_manager
from api.main import app
from core import cf_access, dsh_driver, media_library, media_types
from core import state_conversation_uploads as uploads
from core.drivers import DriverRegistry
from core.dsh_driver import DshDrivers, load_roles
from core.state_database import StateDatabase
from core.state_service import StateService
from tests.support import state_template

HOME = "aitelier-home"
OWNER = "owner@example.test"
PEPPER = "upload-fixture-pepper-" * 3
STEWARD = {"route": "driver", "reasoning_effort": "high", "compaction": {"thresholdRatio": 0.4},
           "prompt": "steward.md", "presented": ["ask_owner", "close_question"],
           "forbidden": ["subagent"], "presented_only": True}
MIB = 1024 * 1024
PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452")
PDF = b"%PDF-1.7\n%fixture\n"
ZIP = bytes.fromhex("504b0304140006000800") + b"\0" * 22   # a .docm is a zip too
ELF = bytes.fromhex("7f454c4602010100") + b"\0" * 24
MZ = bytes.fromhex("4d5a90000300000004") + b"\0" * 23
UPLOAD_ID = re.compile(r"u_[0-9a-f]{16}")


@pytest.fixture
def world(tmp_path, monkeypatch, state_db_copy):
    table = copy.deepcopy(load_roles())
    table["steward"] = copy.deepcopy(STEWARD)
    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps(table))
    monkeypatch.setattr(dsh_driver, "ROLES_FILE", roles)
    db = StateDatabase(state_db_copy(state_template.state_registry_dsh, tmp_path / "state.sqlite"))
    registry = DriverRegistry(db, PEPPER)
    registry.seed("upload-fixture-owner-cli-token-0123456789")
    owner = StateService(db, actor="fixture", project_read_trusted=True)
    for project in ("alpha", "beta", HOME):
        owner.store.create_project(project, project.title())
    runtime = DshDrivers(db, registry, tmp_path / "drivers",
                         command=[sys.executable, "-c", "import time; time.sleep(90)"])
    steward = runtime._provision(HOME, "aitelier", "driver", "fixture")
    director = runtime._provision("alpha", "aitelier", "driver", "fixture")
    tokens = {name: (runtime.root / row["driver_id"] / "token").read_text().strip()
              for name, row in (("steward", steward), ("director", director))}
    staging = tmp_path / "media-staging"
    monkeypatch.setattr(media_library, "staging_dir", lambda: staging)
    return type("World", (), dict(db=db, registry=registry, runtime=runtime, tokens=tokens,
                                  staging=staging))


@pytest.fixture
def client(world, monkeypatch):
    import api.main as main
    monkeypatch.setattr(cf_access, "_TEAM_DOMAIN", "team.cloudflareaccess.com")
    monkeypatch.setattr(cf_access, "_AUD", "test-aud")
    monkeypatch.setattr(cf_access, "email_from_request_headers",
                        lambda headers, cookies=None: headers.get("X-Test-Email"))
    monkeypatch.setattr(authz, "WRITERS", {OWNER})
    monkeypatch.setattr(authz, "driver_registry", lambda: world.registry)
    monkeypatch.setattr(main, "_ALLOW_EXTERNAL", True)
    app.dependency_overrides[get_db_manager] = lambda: world.db
    try:
        with TestClient(app) as c:
            # After startup, so the lifespan still takes its test-mode path.
            monkeypatch.setattr(app.state, "_test_mode", False, raising=False)
            monkeypatch.setattr(app.state, "dsh_drivers", world.runtime, raising=False)
            assert authz.gate_enabled() and app.state._test_mode is False
            yield c
    finally:
        app.dependency_overrides.pop(get_db_manager, None)


OWNER_HEADERS = {"X-Test-Email": OWNER, "X-AItelier-Upload": "1"}


def upload(client, body, *, project="alpha", name="photo.png", headers=None,
           content_type="application/octet-stream"):
    sent = dict(OWNER_HEADERS if headers is None else headers)
    sent.setdefault("Content-Type", content_type)
    if name is not None:
        sent.setdefault("X-AItelier-Upload-Name", quote(name, safe=""))
    return client.post(f"/api/dsh-drivers/{project}/conversation/uploads", content=body, headers=sent)


def rows(world, project=None):
    conn = sqlite3.connect(world.db.db_path)
    conn.row_factory = sqlite3.Row
    try:
        sql = "SELECT * FROM state_conversation_uploads"
        args = ()
        if project is not None:
            sql, args = sql + " WHERE project_id=?", (project,)
        return [dict(r) for r in conn.execute(sql + " ORDER BY created_at", args)]
    finally:
        conn.close()


def staged(world):
    return sorted(os.listdir(world.staging)) if world.staging.exists() else []


def chunked(data, size=MIB):
    """The body as several chunks, so the route really judges a stream."""
    def gen():
        for at in range(0, len(data), size):
            yield data[at:at + size]
    return gen()


def refusal(response):
    return response.status_code, response.json()["detail"]


# --- who -----------------------------------------------------------------------------------

def test_no_writer_identity_is_refused_by_the_gate(world, client):
    r = upload(client, PNG, headers={"X-AItelier-Upload": "1"})
    assert r.status_code == 403, r.text
    assert r.json()["code"] == authz.WRITE_DENIED_NOT_AUTHENTICATED
    # An Access email that is not a writer is refused too.
    r = upload(client, PNG, headers={"X-Test-Email": "stranger@example.test", "X-AItelier-Upload": "1"})
    assert r.status_code == 403 and r.json()["code"] == authz.WRITE_DENIED_NOT_A_WRITER, r.text
    assert rows(world) == [] and staged(world) == []


def test_a_director_token_is_not_the_projects_user(world, client):
    r = upload(client, PNG, headers={"X-AItelier-Driver-Token": world.tokens["director"],
                                     "X-AItelier-Upload": "1"})
    assert refusal(r) == (403, "user_identity_required"), r.text
    assert rows(world) == [] and staged(world) == []


def test_a_steward_token_is_refused_by_the_gate(world, client):
    path = f"/api/dsh-drivers/{HOME}/conversation/uploads"
    assert len(path.split("/")) == 6   # not the 5-part runtime endpoint shape it may write
    for project in (HOME, "alpha"):
        r = upload(client, PNG, project=project,
                   headers={"X-AItelier-Driver-Token": world.tokens["steward"], "X-AItelier-Upload": "1"})
        assert r.status_code == 403, r.text
        assert r.json()["code"] == authz.STEWARD_READ_ONLY
    assert rows(world) == [] and staged(world) == []


def test_an_unknown_project_is_not_found(world, client):
    r = upload(client, PNG, project="nowhere")
    assert r.status_code == 404, r.text
    assert rows(world) == [] and staged(world) == []


# --- cross-site ----------------------------------------------------------------------------

def test_the_upload_header_is_required(world, client):
    r = upload(client, PNG, headers={"X-Test-Email": OWNER})
    assert refusal(r) == (400, "upload_header_required"), r.text
    r = upload(client, PNG, headers={"X-Test-Email": OWNER, "X-AItelier-Upload": "yes"})
    assert refusal(r) == (400, "upload_header_required"), r.text
    assert rows(world) == []


@pytest.mark.parametrize("site", ["cross-site", "same-site", "none"])
def test_a_request_not_from_this_origin_is_refused(world, client, site):
    r = upload(client, PNG, headers={**OWNER_HEADERS, "Sec-Fetch-Site": site})
    assert refusal(r) == (403, "cross_site"), r.text
    assert rows(world) == [] and staged(world) == []


def test_same_origin_and_an_absent_sec_fetch_site_pass(world, client):
    assert upload(client, PNG, headers={**OWNER_HEADERS, "Sec-Fetch-Site": "same-origin"}).status_code == 200
    assert upload(client, PNG).status_code == 200
    assert len(rows(world, "alpha")) == 2


# --- streamed sniff and caps ---------------------------------------------------------------

def test_an_accepted_file_is_staged_0600_with_kind_and_mime_from_its_bytes(world, client):
    body = PNG + b"\0" * 100
    # The client says PDF by name and by Content-Type; the bytes say PNG.
    r = upload(client, body, name="scan.pdf", content_type="application/pdf")
    assert r.status_code == 200, r.text
    answer = r.json()
    assert UPLOAD_ID.fullmatch(answer["upload_id"])
    assert answer == {"upload_id": answer["upload_id"], "kind": "image", "mime": "image/png",
                      "size": len(body), "name": "scan.pdf"}
    assert staged(world) == [answer["upload_id"]]
    path = world.staging / answer["upload_id"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_bytes() == body
    [row] = rows(world)
    assert {k: row[k] for k in ("upload_id", "project_id", "user", "kind", "mime", "size", "name", "state",
                                "refused", "message_id", "store", "ref", "pending_json")} == {
        "upload_id": answer["upload_id"], "project_id": "alpha", "user": "owner:" + OWNER,
        "kind": "image", "mime": "image/png", "size": len(body), "name": "scan.pdf", "state": "staged",
        "refused": None, "message_id": None, "store": None, "ref": None, "pending_json": None}
    assert datetime.fromisoformat(row["created_at"]) > datetime.now(timezone.utc) - timedelta(minutes=1)


def test_a_pdf_is_a_file_and_the_steward_project_takes_uploads_from_the_owner(world, client):
    r = upload(client, PDF, project=HOME, name="report.pdf")
    assert r.status_code == 200, r.text
    assert (r.json()["kind"], r.json()["mime"]) == ("file", "application/pdf")


def test_one_byte_over_the_image_cap_is_413_and_leaves_no_partial(world, client):
    body = PNG + b"\0" * (20 * MIB + 1 - len(PNG))
    r = upload(client, chunked(body))
    assert refusal(r) == (413, "too_large"), r.text
    assert staged(world) == [] and rows(world) == []
    # Exactly the cap is accepted.
    r = upload(client, chunked(body[:-1]))
    assert r.status_code == 200 and r.json()["size"] == 20 * MIB, r.text


def test_the_kind_comes_from_the_bytes_so_a_24_mib_png_named_pdf_is_413(world, client):
    body = PNG + b"\0" * (24 * MIB - len(PNG))
    r = upload(client, chunked(body), name="big.pdf", content_type="application/pdf")
    assert refusal(r) == (413, "too_large"), r.text
    assert staged(world) == [] and rows(world) == []
    # The same size as a real PDF is within the file cap (25 MiB).
    r = upload(client, chunked(PDF + b"\0" * (24 * MIB - len(PDF))), name="big.pdf")
    assert r.status_code == 200 and r.json()["kind"] == "file", r.text


@pytest.mark.parametrize("body,name", [(ZIP, "macro.docm"), (ZIP, "bundle.zip"), (ELF, "photo.png"),
                                       (MZ, "photo.png"), (b"", "empty.png"), (b"GIF8", "short.gif")],
                         ids=["docm", "zip", "elf-as-png", "exe-as-png", "empty", "truncated-gif"])
def test_anything_outside_the_type_table_is_415(world, client, body, name):
    r = upload(client, body, name=name, content_type="image/png")
    assert refusal(r) == (415, "type"), r.text
    assert staged(world) == [] and rows(world) == []


@pytest.mark.parametrize("name", ["", "a" * 256, "bad\nname.png", "tab\tname.png"],
                         ids=["empty", "too-long", "newline", "tab"])
def test_a_bad_name_is_refused(world, client, name):
    r = upload(client, PNG, name=name)
    assert r.status_code == 422, r.text
    assert staged(world) == [] and rows(world) == []


def test_the_name_travels_in_its_header_never_the_query(world, client):
    """Nit PRIV-2: the access log writes the path with its query, so the name is the
    percent-encoded `X-AItelier-Upload-Name` header; a `?name=` is not read at all."""
    r = upload(client, PNG, name="体检 报告.png")
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "体检 报告.png" and rows(world)[0]["name"] == "体检 报告.png"
    sent = {**OWNER_HEADERS, "Content-Type": "image/png"}
    r = client.post("/api/dsh-drivers/alpha/conversation/uploads", params={"name": "scan.png"}, content=PNG, headers=sent)
    assert refusal(r) == (422, "invalid_name")
    r = upload(client, PNG, name=None, headers={**OWNER_HEADERS, "X-AItelier-Upload-Name": "%FF%FE.png"})
    assert refusal(r) == (422, "invalid_name")
    assert len(rows(world)) == 1


# --- per-project hourly count and staging total -------------------------------------------

def test_the_21st_upload_in_an_hour_is_429_per_project(world, client):
    for _ in range(20):
        assert upload(client, PNG).status_code == 200
    r = upload(client, PNG)
    assert refusal(r) == (429, "rate_limited"), r.text
    assert 1 <= int(r.headers["Retry-After"]) <= 3600
    assert len(rows(world, "alpha")) == 20 and len(staged(world)) == 20
    # Another project's first in the same hour is accepted.
    assert upload(client, PNG, project="beta").status_code == 200
    assert len(rows(world, "beta")) == 1


def test_the_count_is_read_from_the_table_and_only_covers_the_last_hour(world, client):
    old = (datetime.now(timezone.utc) - timedelta(minutes=61)).isoformat(timespec="microseconds")
    recent = (datetime.now(timezone.utc) - timedelta(minutes=59)).isoformat(timespec="microseconds")
    conn = sqlite3.connect(world.db.db_path)
    try:
        for n in range(20):
            conn.execute("INSERT INTO state_conversation_uploads(upload_id,project_id,user,kind,mime,size,"
                         "name,state,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                         (f"u_{n:016x}", "alpha", "owner:" + OWNER, "image", "image/png", 1, "x.png",
                          "stored", old))
        conn.commit()
        assert upload(client, PNG).status_code == 200   # 20 rows, all older than an hour
        for n in range(19):
            conn.execute("INSERT INTO state_conversation_uploads(upload_id,project_id,user,kind,mime,size,"
                         "name,state,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                         (f"u_{100 + n:016x}", "alpha", "owner:" + OWNER, "image", "image/png", 1, "x.png",
                          "attached", recent))
        conn.commit()
    finally:
        conn.close()
    # 19 from 59 minutes ago + 1 just now: the next is the 21st in the hour, whatever the
    # rows' state, and it may retry once the oldest of them leaves the window.
    r = upload(client, PNG)
    assert refusal(r) == (429, "rate_limited"), r.text
    assert 55 <= int(r.headers["Retry-After"]) <= 61


def test_an_office_document_is_judged_by_its_zip_directory_once_all_of_it_is_in(world, client):
    """Step 24.2d (design-steward 7.8.12c "六"): a docx, xlsx or pptx is a file of its own mime,
    whatever it is named; a macro file, two main parts or an OLE file is 415 with nothing staged."""
    from tests.unit.office_zips import ooxml, samples, zip_from
    for ext, name in (("docx", "合同.docx"), ("xlsx", "x.png"), ("pptx", "slides.pptx")):
        r = upload(client, chunked(ooxml(ext), 64), name=name)
        assert r.status_code == 200, r.text
        assert r.json()["kind"] == "file" and r.json()["mime"] == media_types.identify(ooxml(ext))["mime"], r.json()
    before = staged(world)
    for about in ("a docm: a macro project anywhere", "two main parts", "an encrypted entry"):
        body = zip_from(next(s for s in samples() if s["about"] == about))
        assert refusal(upload(client, chunked(body, 64), name="x.docx")) == (415, "type"), about
    assert refusal(upload(client, bytes.fromhex("d0cf11e0a1b11ae1") + b"\0" * 600, name="old.doc")) == (415, "type")
    assert staged(world) == before


def test_a_refused_upload_is_not_counted(world, client):
    for _ in range(25):
        assert upload(client, ZIP, name="macro.docm").status_code == 415
    assert upload(client, PNG).status_code == 200


def test_staging_over_its_total_is_507(world, client, monkeypatch):
    monkeypatch.setattr(uploads, "STAGING_TOTAL_BYTES", 1000)
    world.staging.mkdir()
    (world.staging / "u_00000000000000aa").write_bytes(b"\0" * 1000)
    r = upload(client, PNG)
    assert refusal(r) == (507, "staging_full"), r.text
    assert staged(world) == ["u_00000000000000aa"] and rows(world) == []
    # Room for less than the body: refused while it arrives, nothing left behind.
    (world.staging / "u_00000000000000aa").write_bytes(b"\0" * 900)
    r = upload(client, chunked(PNG + b"\0" * 200, size=50))
    assert refusal(r) == (507, "staging_full"), r.text
    assert staged(world) == ["u_00000000000000aa"] and rows(world) == []
    # Room enough: accepted.
    assert upload(client, PNG + b"\0" * 50).status_code == 200


def test_the_staging_directory_is_created_when_missing(world, client):
    assert not world.staging.exists()
    assert upload(client, PNG).status_code == 200
    assert world.staging.is_dir()


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root writes through any mode")
def test_a_staging_directory_the_backend_cannot_write_is_503_not_a_500(world, client):
    """OPS-2: a bind source Docker created as root (the directory was missing before `up -d`)
    answers 503 staging_unavailable, with no row and nothing staged - not an unhandled 500."""
    world.staging.mkdir(mode=0o500)
    try:
        r = upload(client, PNG)
        assert refusal(r) == (503, "staging_unavailable"), r.text
        assert rows(world) == []
    finally:
        world.staging.chmod(0o700)
    assert staged(world) == []
    assert upload(client, PNG).status_code == 200


def test_the_hour_is_counted_again_inside_the_commit(world, client):
    """Nit TQ-2: two uploads that both passed admit() as the 20th cannot both be kept - the
    second's commit counts again in its write transaction: 429, no row, no file left."""
    for _ in range(uploads.RATE_PER_HOUR - 1):
        assert upload(client, PNG).status_code == 200
    world.staging.mkdir(exist_ok=True)
    service = StateService(world.db, actor="owner:" + OWNER, project_read_trusted=True)
    assert uploads.admit(service.store, "alpha", world.staging) >= 0
    raced = uploads.new_upload_id()
    partial = world.staging / (raced + ".part")
    partial.write_bytes(PNG)
    # The other upload that passed admit() commits first: the 20th row.
    assert upload(client, PNG).status_code == 200
    with pytest.raises(uploads.UploadRefused) as refused:
        uploads.commit(service.store, project_id="alpha", user="owner:" + OWNER, upload_id=raced,
                       entry={"kind": "image", "mime": "image/png"}, size=len(PNG), name="raced.png",
                       partial=str(partial), directory=world.staging)
    assert (refused.value.status, refused.value.code) == (429, "rate_limited")
    assert raced not in [r["upload_id"] for r in rows(world)]
    assert not partial.exists() and not (world.staging / raced).exists()
