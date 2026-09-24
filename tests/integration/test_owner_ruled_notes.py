"""The owner's notes ruling (2026-09-22, note://aitelier/546f3b521eca) holds.

Criterion ``the-owner-ruled-notes-stay-public-and-unopened-notes-stay-shut``,
for an ANONYMOUS caller, on a fresh app with the product State router and on
``api.main.app``:

1. an OPENED project: the six note reads answer 200 with the note text
   (``check_driver_note_index`` answers with counts only);
2. an UNOPENED project and a project that does not exist: the same six reads
   are refused, and the two refusals are byte-identical;
3. every in-scope reader of the row-verdict matrix - including reading a whole
   notebook table with no ``project_id`` filter - gets no unopened-project
   notebook canary;
4. ``list_director_messages`` and ``events`` stay refused on the opened project.

The six reads are the notebook reads the action table calls public; the test
asserts that set rather than trusting a hand-written one.
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi import Depends

from api.state_graph_routers import get_service
from core.state_commands import read_visibility
from core.state_privacy import PRIVATE_STATE_TABLES
from core.state_service import StateService
from tests.support import state_canaries as C
from tests.unit.test_every_private_table_reader_is_judged_at_the_read import (
    IN_SCOPE, Absent, outcome)

NOTEBOOK_TABLES = C.NOTEBOOK_TABLES
NOTE_READS = ("get_driver_note", "driver_note_history", "search_driver_note_history",
              "get_driver_note_entry", "check_driver_note_index", "driver_note_index")
COUNTS_ONLY = {"check_driver_note_index"}
MISSING = "canary-nobody-created-this"
SHUT_ENTRY = "c0ffee0c0ffe"   # the entry id tests/support/state_canaries.py plants
_REFUSAL = '{"detail":"This State DAG record is not available."}'
APPS = ["fresh", "main"]


def _open_entry(db) -> str:
    raw = sqlite3.connect(C.path_of(db))
    try:
        return raw.execute("SELECT entry_id FROM state_driver_note_entries WHERE project_id=?",
                           (C.OPEN,)).fetchone()[0]
    finally:
        raw.close()


def _args(action, project_id, entry_id):
    args = {"project_id": project_id}
    if action == "get_driver_note_entry":
        args["entry_id"] = entry_id
    return args


def _client(tmp_path, monkeypatch, mode, name):
    C.arm(monkeypatch)
    db = C.new_database(tmp_path, f"{name}-{mode}.sqlite")
    app = C.app_for(mode, db)
    client = C.client_for(app, mode)
    assert C.request_is_anonymous(app, client), "the per-request service is not anonymous"
    return db, app, client


def _query(client, action, args):
    response = client.post(f"/api/state/query/{action}", json=args)
    return response.status_code, response.content


def test_the_six_note_reads_are_the_public_notebook_reads():
    print("NOTE_READS visibility =", {a: read_visibility(a) for a in NOTE_READS})
    assert all(read_visibility(a) == "public" for a in NOTE_READS)
    assert read_visibility("list_director_messages") == "private"
    assert read_visibility("events") == "private"
    assert set(NOTEBOOK_TABLES) <= set(PRIVATE_STATE_TABLES)


@pytest.mark.parametrize("mode", APPS)
def test_item1_an_opened_projects_notes_are_public(tmp_path, monkeypatch, mode):
    db, app, client = _client(tmp_path, monkeypatch, mode, "item1")
    entry = _open_entry(db)
    failed = []
    try:
        for action in NOTE_READS:
            status, body = _query(client, action, _args(action, C.OPEN, entry))
            text = body.decode()
            carries = C.OPEN_NOTE in text
            print(f"ITEM1 app={mode} action={action} project=opened status={status} "
                  f"carries_note_text={carries} body={text[:90]!r}")
            wants_text = action not in COUNTS_ONLY
            if status != 200 or (wants_text and not carries) or not body:
                failed.append(action)
    finally:
        C.release(app)
    print(f"ITEM1 app={mode}: {len(NOTE_READS)} note reads x 1 opened project x 1 app; "
          f"failed={failed}")
    assert not failed, f"{mode}: note reads that did not answer with the opened note: {failed}"


@pytest.mark.parametrize("mode", APPS)
def test_item2_unopened_and_missing_projects_are_refused_identically(tmp_path, monkeypatch, mode):
    _, app, client = _client(tmp_path, monkeypatch, mode, "item2")
    failed = []
    try:
        for action in NOTE_READS:
            shut = _query(client, action, _args(action, C.SHUT, SHUT_ENTRY))
            missing = _query(client, action, _args(action, MISSING, SHUT_ENTRY))
            identical = shut == missing
            print(f"ITEM2 app={mode} action={action} unopened={shut[0]} missing={missing[0]} "
                  f"byte_identical={identical} body={shut[1][:80]!r}")
            leaked = C.leaked_tables(shut[1].decode())
            if (shut[0] != 403 or shut[1].decode() != _REFUSAL or not identical
                    or leaked):
                failed.append((action, shut[0], missing[0], identical, leaked))
    finally:
        C.release(app)
    print(f"ITEM2 app={mode}: {len(NOTE_READS)} note reads x 2 project states "
          f"(unopened, missing) x 1 app = {2 * len(NOTE_READS)} requests; failed={failed}")
    assert not failed, f"{mode}: note reads not refused byte-identically: {failed}"


def _notebook_cells(run):
    cells = {}
    for shape in IN_SCOPE:
        for table in NOTEBOOK_TABLES:
            cells[(shape.__name__, table)] = run(shape, table)
    return cells


def _judge_item3(label, cells):
    for (shape, table), (result, detail) in sorted(cells.items()):
        print(f"ITEM3 {label} shape={shape} table={table} outcome={result} {detail}".rstrip())
    tally = {}
    for result, _ in cells.values():
        tally[result] = tally.get(result, 0) + 1
    print(f"ITEM3 TALLY {label}: {len(IN_SCOPE)} in-scope shapes x {len(NOTEBOOK_TABLES)} "
          f"notebook tables x 1 anonymous identity = {len(cells)} cells; {sorted(tally.items())}")
    leaked = sorted(f"{s}/{t}" for (s, t), (r, _) in cells.items() if r == "LEAKED")
    errors = sorted((k, v[1]) for k, v in cells.items() if v[0] == "ERROR")
    assert not errors, f"measurement failures (not refusals): {errors}"
    assert not leaked, f"{label}: unopened-project notebook canaries delivered by: {leaked}"


def test_item3_no_in_scope_reader_gets_an_unopened_note_in_process(tmp_path):
    db = C.new_database(tmp_path, "item3.sqlite")
    assert all(C.canary_presence(db.db_path)[t] for t in NOTEBOOK_TABLES)
    anonymous = StateService(db, actor="anonymous", project_read_trusted=False)
    _judge_item3("in-process", _notebook_cells(lambda s, t: outcome(s, anonymous, t)))


@pytest.mark.parametrize("mode", APPS)
def test_item3_no_in_scope_reader_gets_an_unopened_note_over_http(tmp_path, monkeypatch, mode):
    _, app, client = _client(tmp_path, monkeypatch, mode, "item3")
    shapes = {shape.__name__: shape for shape in IN_SCOPE}

    def reader(shape: str, table: str, svc=Depends(get_service)):
        try:
            return {"rows": repr(shapes[shape](svc, table))}
        except Absent as exc:
            return {"absent": str(exc)}

    def run(shape, table):
        try:
            response = client.get(f"/owner-notes/{shape.__name__}/{table}")
            status, text = response.status_code, response.text
        except Exception as exc:
            return "ERROR", f"{type(exc).__name__}: {str(exc)[:120]}"
        if status == 403:
            return "REFUSED", ""
        if status == 200 and '"absent"' in text:
            return "ABSENT", text[:100]
        if status == 200:
            return ("LEAKED" if C.canary(table) in text else "CLEAN"), ""
        return "ERROR", f"{status} {text[:120]}"

    try:
        C.mount(app, "/owner-notes/{shape}/{table}", reader, guarded=False)
        cells = _notebook_cells(run)
    finally:
        C.unmount(app, reader)
        C.release(app)
    _judge_item3(f"http-{mode}", cells)


@pytest.mark.parametrize("mode", APPS)
def test_item4_mailbox_and_events_stay_refused_on_an_opened_project(tmp_path, monkeypatch, mode):
    _, app, client = _client(tmp_path, monkeypatch, mode, "item4")
    outcomes = {}
    try:
        for action in ("list_director_messages", "events"):
            status, body = _query(client, action, {"project_id": C.OPEN})
            text = body.decode()
            delivered = C.leaked_tables(text) + (["natural body"] if "natural body" in text else [])
            outcomes[action] = (status, delivered)
            print(f"ITEM4 app={mode} action={action} project=opened status={status} "
                  f"delivered={delivered} body={body[:80]!r}")
    finally:
        C.release(app)
    assert all(o == (403, []) for o in outcomes.values()), outcomes
