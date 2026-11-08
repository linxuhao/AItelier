"""A gate run that queued behind a live render owner and was then REFUSED.

That is the most contended request there is: it waited in the harness's render
queue for its whole `render_wait_timeout_sec` and was not run. The harness
reports that wait under the QUEUE's own names -- `waited_sec` and
`waited_for_owner_ids` (`docker/godot/godot_harness.py:_wait_timed_out`, the
409 payload) -- while the queue's SUCCESS path reports it as
`render_owner_wait_sec` / `render_owner_waited_for_owner_ids`. The relay's
answer whitelist used to carry only the success spelling plus a bare
`waited_sec`, so a refused run reached `admission_summary` with no owners and
no seconds, and the render-contention block called that a measured absence
(`state: "none"`).

What these tests pin, in the two poles the block can honestly be read at:

  * the refusal shape yields its owners in `queued_behind` and its seconds in
    `render_owner_wait_sec`, and `impl._render_contention(...)` reads
    `state: "waited"` naming the owner -- the half that was missing;
  * neither spelling present names nobody and zero seconds (an answer that
    refused at once without queueing is NOT contention), and the success
    spelling still gives exactly the owners and the summed wait it gave
    before the change.

The refusal payload is built by the REAL harness (`_wait_timed_out`) and
folded off the REAL relay's record (`AdmissionRelay`, the production
whitelist); no node/npm, no render and no engine run in these. The one thing
kept hand-built is the record shape of an unrelated `waited_sec`, which is the
negative pole this file exists to keep shut.
"""
from __future__ import annotations

import importlib.util
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from aitelier import gate_admission
from aitelier.tools.run_tests import impl as rt

HARNESS = Path(__file__).resolve().parents[2] / "docker" / "godot" / "godot_harness.py"

# Two live holders this run's request queued behind, in the order the queue
# named them, and a wait long enough that no floor can hide it.
WAITED_FOR = ["owner-3f9c", "owner-7d21"]
WAITED_SEC = 812.5
WAIT_TIMEOUT_SEC = 1500.0


# ── the harness's own refusal payload ──────────────────────────────────────

def _harness(tmp_path, monkeypatch):
    """The REAL harness module, for the refusal payload it really emits.

    Loaded exactly as the gate rig loads it (`tests/gate_fixture.py`): from the
    source file, with its durable state pointed at a temp directory. Nothing
    is served and no render runs -- only `_wait_timed_out` is called.
    """
    monkeypatch.setenv("GODOT_LIFECYCLE_DB", str(tmp_path / "owners.sqlite3"))
    monkeypatch.setenv("GODOT_DEPLOYMENT_LOCK",
                       str(tmp_path / "deployment-admission.lock"))
    monkeypatch.setenv("GODOT_RENDER_EFFECT_LOCK", str(tmp_path / "render-effect.lock"))
    spec = importlib.util.spec_from_file_location("godot_harness_refusal_wait", HARNESS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _refusal_payload(tmp_path, monkeypatch, *, waited_for=None, waited=WAITED_SEC,
                     timeout=WAIT_TIMEOUT_SEC):
    """The 409 body a request refused after queueing really carries."""
    gh = _harness(tmp_path, monkeypatch)
    return gh._wait_timed_out(waited, timeout,
                              list(WAITED_FOR if waited_for is None else waited_for))


# ── the engine side of the wire ────────────────────────────────────────────

class _Builder:
    """A builder answering its POSTs from a script of `(status, payload)`.

    The last answer repeats. `GET /lifecycle` is 404: no owner table to read,
    so the relay sends no interim responses and a test never races one.
    """

    def __init__(self, *answers):
        self.answers = list(answers)
        self.posts: list[dict] = []
        self.server = None
        self.base = ""

    def start(self):
        builder = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def _send(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self._send(404, {"error": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                builder.posts.append(json.loads(self.rfile.read(length) or b"{}"))
                code, payload = (builder.answers.pop(0) if len(builder.answers) > 1
                                 else builder.answers[0])
                self._send(code, payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def _through_relay(base: str, requests: list[dict]) -> tuple[list[dict], list]:
    """Send each body through the REAL relay; return its records and answers."""
    relay = gate_admission.AdmissionRelay(base, render_wait_sec=5,
                                          upstream_timeout=30)
    url = relay.start()
    answers = []
    try:
        for body in requests:
            req = urllib.request.Request(url + "/script",
                                         data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"},
                                         method="POST")
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    answers.append((r.status, json.loads(r.read())))
            except urllib.error.HTTPError as exc:
                answers.append((exc.code, json.loads(exc.read())))
    finally:
        relay.stop()
    return relay.snapshot(), answers


def _gate_request(operation: str) -> dict:
    """One render request as the repository gate sends it."""
    return {"project_dir": "/tmp/proj", "project_id": "repo", "run_id": "run-1",
            "operation_id": operation, "scripts": ["res://tests/a.gd"]}


# ── (a) the refusal keeps what it waited ───────────────────────────────────

def test_a_refused_after_waiting_gate_keeps_its_owners_and_its_wait(tmp_path,
                                                                  monkeypatch):
    """The harness refuses a request whose queue window ran out; the run it
    belongs to still names WHO it queued behind and HOW LONG, so the
    render-contention block reads `waited` instead of `none`."""
    payload = _refusal_payload(tmp_path, monkeypatch)
    assert payload["error"] == "render owner wait timed out", payload
    builder = _Builder((409, payload)).start()
    try:
        records, answers = _through_relay(builder.base, [_gate_request("op-refused")])
    finally:
        builder.stop()

    (status, answer), = answers
    assert status == 409 and answer["error"] == "render owner wait timed out"

    # The relay's own record of that answer carries BOTH the seconds and the
    # owners: the whitelist is what stood between the engine and the summary.
    (record,) = records
    assert record["outcome"] == gate_admission.NOT_ADMITTED
    assert record["waited_sec"] == WAITED_SEC, record
    assert record["waited_for_owner_ids"] == WAITED_FOR, record

    admission = gate_admission.admission_summary(records)
    assert admission["state"] == gate_admission.NOT_ADMITTED
    assert admission["queued_behind"] == WAITED_FOR
    assert admission["render_owner_wait_sec"] == WAITED_SEC

    # The half that was missing: the block a reader and the report see.
    contention = rt._render_contention(admission)
    assert contention == {"owners": WAITED_FOR, "wait_sec": WAITED_SEC,
                          "state": "waited"}, contention
    sentence = rt._contention_sentence(contention)
    assert sentence.startswith("Render contention:"), sentence
    assert f"{WAITED_SEC:.3f}s" in sentence and WAITED_FOR[-1] in sentence, sentence


def test_a_refusal_behind_a_just_answered_request_is_still_counted(tmp_path,
                                                                  monkeypatch):
    """A run that queued and was admitted once, then queued again and was
    refused: the refused request adds its own wait and owner on top, and no
    owner is listed twice."""
    builder = _Builder(
        (200, {"passed": True, "results": [], "summary": "ok",
               "render_owner_wait_sec": 2.5,
               "render_owner_waited_for_owner_ids": [WAITED_FOR[0]]}),
        (409, _refusal_payload(tmp_path, monkeypatch)),
    ).start()
    try:
        records, answers = _through_relay(
            builder.base, [_gate_request("op-first"), _gate_request("op-refused")])
    finally:
        builder.stop()

    assert [status for status, _ in answers] == [200, 409]
    admission = gate_admission.admission_summary(records)
    assert admission["state"] == gate_admission.NOT_ADMITTED
    assert admission["queued_behind"] == WAITED_FOR
    assert admission["render_owner_wait_sec"] == round(2.5 + WAITED_SEC, 3)
    assert rt._render_contention(admission) == {
        "owners": WAITED_FOR, "wait_sec": round(2.5 + WAITED_SEC, 3),
        "state": "waited"}


# ── (b) the honest poles ───────────────────────────────────────────────────

def test_a_refusal_that_never_waited_names_no_contention():
    """A holder that needs reconciliation is refused AT ONCE, not queued
    behind: the answer names an owner and no wait at all. Nobody waited, so
    the block is the clean default -- a refusal is not a wait."""
    refused_at_once = {"seq": 0, "route": "/script", "method": "POST",
                       "outcome": gate_admission.NOT_ADMITTED, "status": 409,
                       "delivered": True, "owner_kind": "owner_lost",
                       "owner_id": "owner-dead", "needs_reconciliation": True,
                       "error": "render owner exists"}
    admission = gate_admission.admission_summary([refused_at_once])
    assert admission["state"] == gate_admission.NOT_ADMITTED
    assert admission["queued_behind"] == []
    assert admission["render_owner_wait_sec"] == 0.0
    assert rt._render_contention(admission) == {
        "owners": [], "wait_sec": 0.0, "state": "none"}


def test_the_success_spelling_is_unchanged():
    """The queue's admitted path spells the wait `render_owner_*`; it must
    still give exactly the owner list and the summed wait it gave before the
    refusal spelling was read, and it is never counted twice."""
    records = [
        {"seq": 0, "route": "/script", "method": "POST", "outcome": "answered",
         "status": 200, "delivered": True, "render_owner_wait_sec": 180.9249,
         "render_owner_waited_for_owner_ids": ["owner-A"]},
        {"seq": 1, "route": "/script", "method": "POST", "outcome": "answered",
         "status": 200, "delivered": True, "render_owner_wait_sec": 2.5,
         "render_owner_waited_for_owner_ids": ["owner-A", "owner-B"]},
        {"seq": 2, "route": "/script", "method": "POST", "outcome": "answered",
         "status": 200, "delivered": True, "render_owner_wait_sec": 0.0115,
         "render_owner_waited_for_owner_ids": []},
    ]
    admission = gate_admission.admission_summary(records)
    assert admission["queued_behind"] == ["owner-A", "owner-B"]
    assert admission["render_owner_wait_sec"] == round(180.9249 + 2.5 + 0.0115, 3)
    assert rt._render_contention(admission) == {
        "owners": ["owner-A", "owner-B"],
        "wait_sec": round(180.9249 + 2.5 + 0.0115, 3), "state": "waited"}
    # The uncontended pole: the same spelling with nobody and nothing.
    assert gate_admission.admission_summary([records[2]]) == {
        "requests": [records[2]], "queued_behind": [],
        "render_owner_wait_sec": round(0.0115, 3), "state": "answered",
        "last_route": "/script"}
    assert rt._render_contention(
        gate_admission.admission_summary([records[2]])) == {
        "owners": [], "wait_sec": 0.0, "state": "none"}


# ── the fallback is CONSTRAINED: another wait is another wait ──────────────

@pytest.mark.parametrize("record", [
    # A wait on a route that is not the render queue's: nothing about it is
    # render contention, whatever the seconds and whatever it queued behind.
    {"seq": 0, "route": "/compile", "method": "POST",
     "outcome": gate_admission.NOT_ADMITTED, "status": 409, "delivered": True,
     "waited_sec": 900.0, "waited_for_owner_ids": ["something-else"],
     "wait_timeout_sec": 900.0, "deployment_fence_held": False},
    # A render route, an ANSWERED request: a wait here is not the queue's
    # refusal, so the queue's spelling is not this record's to claim.
    {"seq": 0, "route": "/script", "method": "POST", "outcome": "answered",
     "status": 200, "delivered": True, "waited_sec": 900.0,
     "waited_for_owner_ids": ["something-else"],
     "wait_timeout_sec": 900.0, "deployment_fence_held": False},
    # A render route, refused, but NOT by the queue's own timeout: the
    # refusal's structured pair is what identifies the queue wait.
    {"seq": 0, "route": "/script", "method": "POST",
     "outcome": gate_admission.NOT_ADMITTED, "status": 409, "delivered": True,
     "waited_sec": 900.0, "waited_for_owner_ids": ["something-else"],
     "owner_kind": "active", "owner_id": "owner-x"},
], ids=["other-route", "not-a-refusal", "not-the-queue-timeout"])
def test_an_unrelated_waited_sec_is_never_read_as_render_contention(record):
    """`waited_sec` is a general name for "somebody waited". It is read only
    from the record this module can prove is the render queue's own refusal,
    so a different wait in a different answer cannot become contention."""
    admission = gate_admission.admission_summary([record])
    assert admission["queued_behind"] == []
    assert admission["render_owner_wait_sec"] == 0.0
    assert rt._render_contention(admission) == {
        "owners": [], "wait_sec": 0.0, "state": "none"}


def test_one_record_is_read_under_one_spelling_only():
    """A record carrying BOTH spellings is read under the success path's, once:
    the fold never sums one wait twice or invents a second owner list."""
    record = {"seq": 0, "route": "/script", "method": "POST",
              "outcome": gate_admission.NOT_ADMITTED, "status": 409,
              "delivered": True, "render_owner_wait_sec": 12.0,
              "render_owner_waited_for_owner_ids": ["owner-A"],
              "waited_sec": 99.0, "waited_for_owner_ids": ["owner-B"],
              "wait_timeout_sec": WAIT_TIMEOUT_SEC, "deployment_fence_held": False}
    admission = gate_admission.admission_summary([record])
    assert admission["queued_behind"] == ["owner-A"]
    assert admission["render_owner_wait_sec"] == 12.0
