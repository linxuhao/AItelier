"""Two render requests that carry the same operation_id are not one admission.

The relay decides "this request was admitted" from the harness's render-owner
table (`aitelier/gate_admission.py:_AdmissionWatch`): a row for the request's
`operation_id` that was not there before the request was sent. `acquire_render_owner`
mints `owner_id` itself, so a row names an operation, not a request. Two render
requests carrying the SAME operation_id therefore alias: a row that appears
while both are in flight is attributable to neither, and either one reading it
as its own admission sends the admission `100 Continue` that ends its queue
keepalive although its own render has not started. The gate then counts its own
client timeout from an admission it never got and reports a render it never
began as not run (iss-b859d7478c524da1 item 4).

Every test here drives the REAL `AdmissionRelay` over HTTP against a SCRIPTED
sidecar (`Sidecar`): its `/lifecycle` answers with exactly the rows the test
added, its render route holds each answer until the test releases it, and one
`/lifecycle` answer can be held open so the test decides when a watch learns
about a row. So the three facts the relay's witness depends on -- which request
arrived first, when an owner row appears, and when the relay stops -- are
events the TEST sets, never a race the test sleeps through. The client speaks
raw HTTP/1.1 so the interim `100 Continue` responses are visible too; an
HTTP/1.0 client is how one test carries a second request with the same
`operation_id` without opening a second watch. Assertions read the relay's own
per-request records through its public `snapshot()`; no private attribute is
touched, and nothing here assumes a duration.

Boundary this file does NOT fix and this change leaves unchanged: a request
that is never witnessed as admitted is not bounded by the gate client's own
timeout. Its bound is `upstream_timeout` in the relay and the harness's own
render wait. The reviewer measured it on this commit's parent (8eb1d8b18d) --
with a 1 s client timeout against a 3 s render, the admitted request timed out
at 1 s while the never-witnessed duplicate returned 200 with 32 keepalive
interim responses. That measurement is quoted, not re-made here: the gate path
that produces it is `@pytest.mark.docker`, deselected in the disposable
container this suite runs in. Bounding it needs a per-request nonce from the
harness (`docker/godot/godot_harness.py` mints `owner_id`), which this change
does not touch.
"""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from aitelier import gate_admission as admission

OP = "op-shared"


class Sidecar:
    """The harness's two surfaces, scripted: its owner table and a render route.

    `/lifecycle` answers with exactly the rows the test has added, so "a new
    owner row appears" is something the test decides. The render route records
    each POST's arrival and holds its answer until the test releases it, so a
    request is never answered before the test says so. `arrived` and `answer`
    are indexed by arrival order and pre-created, so the test can wait on a
    request that has not arrived yet.
    """

    def __init__(self, capacity: int = 3):
        self.owners: list[dict] = []
        self.posts: list[dict] = []
        self.arrived = [threading.Event() for _ in range(capacity)]
        self.answer = [threading.Event() for _ in range(capacity)]
        # One `/lifecycle` answer can be held open: the test raises
        # `hold_lifecycle`, `lifecycle_held` says a read is now inside the
        # hold, and `release_lifecycle` lets it answer -- with the owner rows
        # that exist at THAT moment. So "a watch read the table after the row
        # appeared" is an event the test sets, not an interval it waits out.
        self.hold_lifecycle = False
        self.lifecycle_held = threading.Event()
        self.release_lifecycle = threading.Event()
        self._lock = threading.Lock()
        sidecar = self

        class Handler(BaseHTTPRequestHandler):
            # The harness itself speaks HTTP/1.1 for its own links; the relay
            # puts one connection per read through it.
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                if sidecar.hold_lifecycle:
                    sidecar.lifecycle_held.set()
                    sidecar.release_lifecycle.wait(120)
                with sidecar._lock:
                    owners = [dict(row) for row in sidecar.owners]
                body = json.dumps({"owners": owners}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                with sidecar._lock:
                    index = len(sidecar.posts)
                    sidecar.posts.append(payload)
                sidecar.arrived[index].set()
                sidecar.answer[index].wait(120)
                body = json.dumps({"passed": True, "results": [],
                                   "summary": "2 passed"}).encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def add_owner(self, owner_id: str, operation_id: str = OP) -> None:
        """Add one row to the harness's table, as `acquire_render_owner` does."""
        with self._lock:
            self.owners.append({"owner_id": owner_id,
                                "operation_id": operation_id,
                                "status": "active"})

    def close(self) -> None:
        self.release_lifecycle.set()
        for event in self.answer:
            event.set()
        self.server.shutdown()
        self.server.server_close()


def _answer(url, operation_id, progress, *, path="/script", timeout=180,
            version="HTTP/1.1"):
    """POST one render request over raw HTTP and read the whole answer.

    `progress["interim"]` is the live list of interim arrival times (monotonic,
    so the test can compare them with its own markers while the request is
    still in flight). The return value carries the final status, its body, the
    interim times and when the answer arrived. `version` is the client's
    protocol line: an HTTP/1.0 client gets no interim responses at all
    (module docstring), so it is the way to carry an `operation_id` without
    opening a second admission watch.
    """
    parts = urllib.parse.urlsplit(url)
    payload = json.dumps({"project_dir": "/tmp/shared-operation",
                          "project_id": "shared-operation",
                          "run_id": "shared-operation",
                          "operation_id": operation_id,
                          "scripts": ["res://tests/a.gd"]}).encode()
    head = (f"POST {path} {version}\r\nHost: {parts.hostname}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\n"
            f"X-AItelier-Operation: {operation_id}\r\n\r\n").encode()
    sock = socket.create_connection((parts.hostname, parts.port), timeout=timeout)
    started = time.monotonic()
    interim: list[float] = []
    progress["interim"] = interim
    buffer = b""
    try:
        sock.sendall(head + payload)
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                raise AssertionError("the relay closed without an answer")
            buffer += chunk
            head_end = buffer.find(b"\r\n\r\n")
            while head_end != -1:
                block, buffer = buffer[:head_end], buffer[head_end + 4:]
                status = int(block.split(b" ", 2)[1])
                if status == 100:
                    interim.append(time.monotonic())
                else:
                    length = 0
                    for line in block.split(b"\r\n")[1:]:
                        name, _, value = line.partition(b":")
                        if name.strip().lower() == b"content-length":
                            length = int(value.strip())
                    while len(buffer) < length:
                        buffer += sock.recv(65536)
                    return {"status": status,
                            "body": json.loads(buffer[:length] or b"{}"),
                            "interim": interim,
                            "answered_after": time.monotonic() - started}
                head_end = buffer.find(b"\r\n\r\n")
    finally:
        sock.close()


def _in_thread(target, *args, **kwargs):
    """Run one request in its own thread; the test surfaces any exception."""
    box: dict = {}

    def go():
        try:
            box["result"] = target(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - re-raised by the assertions
            box["error"] = exc

    thread = threading.Thread(target=go, daemon=True)
    thread.start()
    return thread, box


def _until(predicate, timeout=5.0) -> bool:
    """True as soon as the predicate holds; False if it never does.

    This only waits for a fact the code under test produces (a record names an
    admission decision, more interim responses arrive). It never assumes that
    some interval has elapsed, which is what makes the ordering in these tests
    the test's own.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _await(predicate, what, timeout=30.0) -> None:
    if not _until(predicate, timeout):
        raise AssertionError(f"never observed: {what}")


def _records(relay) -> dict:
    """The relay's own per-request records, keyed by arrival sequence."""
    return {record["seq"]: record for record in relay.snapshot()}


def _claims(record) -> bool:
    return "admitted_after_sec" in record


def _decided(record) -> bool:
    """True once this record says what its admission was: a claim or a refusal."""
    return _claims(record) or record.get("admission_witnessed") is False


def _interims_since(progress, marker: float) -> int:
    return sum(1 for at in progress.get("interim") or [] if at > marker)


def _recorded(relay, count: int) -> None:
    """Wait until the relay has finished recording `count` requests.

    The relay writes the last of a record AFTER it has written the answer, so a
    client can return before its own record is complete; the test waits for the
    fact instead of assuming the handler thread got there first.
    """
    _await(lambda: sum(1 for record in _records(relay).values()
                       if "outcome" in record) == count,
           f"{count} answers recorded by the relay")


def _start(relay, url, operation_id, progress, **kwargs):
    thread, box = _in_thread(_answer, url, operation_id, progress, **kwargs)
    return thread, box


def _watch_running() -> bool:
    """True while an admission watch is still running.

    The relay starts a watch as a daemon thread named `gate-admission-watch`
    and names nothing else that way, so "the watch that could still write a
    claim has finished" is a fact about the process the test can wait for.
    Without it, an assertion that a record does NOT carry a claim could pass
    before the claim was written. No relay attribute is read here.
    """
    return any(thread.name == "gate-admission-watch" and thread.is_alive()
               for thread in threading.enumerate())


@pytest.fixture
def rig():
    """A sidecar and a relay over it, torn down even when an assertion fires."""
    side = Sidecar()
    relay = admission.AdmissionRelay(side.base, render_wait_sec=60,
                                     upstream_timeout=120, keepalive_sec=0.1)
    url = relay.start()
    try:
        yield side, relay, url
    finally:
        for event in side.answer:
            event.set()
        relay.stop()
        side.close()


@pytest.mark.parametrize("arrival", ["a-then-b", "b-then-a"],
                         ids=["a-arrives-first", "b-arrives-first"])
@pytest.mark.parametrize("owner", ["first-to-arrive", "second-to-arrive"],
                         ids=["rows-of-first-arrival", "rows-of-second-arrival"])
def test_neither_request_claims_a_row_that_appeared_while_they_shared_an_id(
        rig, owner, arrival):
    """BOTH arrival orders: the one row that appears belongs to neither request.

    Request A reaches the engine and takes its `before` snapshot (the relay
    reads the owner table before it forwards the POST, so the sidecar's arrival
    event bounds that read). Request B then arrives with the same
    operation_id and takes its own snapshot. A row appearing now is strictly
    after BOTH snapshots, and it is labelled as each arrival in turn: the old
    `owners - before` witness read it as A's admission when it was B's, and as
    B's when it was A's. Which of them actually arrived first is a parameter
    here, not a constant -- the first version of this test started request 0
    first in both cases and only relabelled the row, so the net effect of
    swapping who claims was never exercised. The label and the order change
    nothing on purpose: neither request can attribute the row.
    """
    side, relay, url = rig
    # Which request starts first is the parameter; the sidecar's arrival slots
    # are filled in that order, so `side.arrived[slot]` is the slot-th arrival.
    order = (0, 1) if arrival == "a-then-b" else (1, 0)
    progress = [dict(), dict()]
    threads: list[threading.Thread] = []
    boxes: list[dict] = [dict(), dict()]
    try:
        for slot, index in enumerate(order):
            thread, boxes[index] = _start(relay, url, OP, progress[index])
            threads.append(thread)
            assert side.arrived[slot].wait(30), (
                f"request {index + 1} never reached the engine")
        side.add_owner(f"row-of-the-{owner}")
        marker = time.monotonic()
        _await(lambda: all(_decided(record) for record in _records(relay).values()),
               "both requests to say what the new owner row was")
        # The admission interim is what ENDS the queue keepalive, so a request
        # that may not claim has to keep receiving the ordinary interim until
        # its answer. Two more of them after the decision is that fact; it is
        # best effort -- on the old witness the claim ends the keepalive and
        # this expires -- because the record is the assertion, not the cadence.
        kept_alive = [_until(lambda i=index: _interims_since(progress[i], marker) >= 2,
                             timeout=3.0)
                      for index in (0, 1)]
    finally:
        for event in side.answer:
            event.set()
        for thread in threads:
            thread.join(60)

    _recorded(relay, 2)
    for index, box in enumerate(boxes):
        assert "error" not in box, box.get("error")
    records = _records(relay)

    # Each request's record is identified by the ORDER it arrived in, so the
    # reversed case asserts on the reverse request's record and not on a label.
    for slot, index in enumerate(order):
        label = f"the request that arrived {'first' if slot == 0 else 'second'}"
        answer, record, kept = boxes[index]["result"], records[slot], kept_alive[index]
        assert answer["status"] == 200, answer
        assert record["outcome"] == admission.ANSWERED
        # The owner table WAS readable: this is the shared-id rule, not the
        # "admission cannot be observed" one.
        assert record["admission_observed"] is True
        assert not _claims(record), (
            f"{label} claimed an admission from an owner row it cannot "
            "attribute to itself (the id was carried by two requests): "
            f"its record carries admitted_after_sec={record.get('admitted_after_sec')}")
        assert record.get("admission_witnessed") is False, record
        assert kept, (
            f"{label} stopped receiving interim responses at a row it cannot "
            "call its own: that is the admission interim, which must not be "
            "sent by a request that was never witnessed as admitted")
        # Every interim the relay counted reached the gate: one stream, and the
        # admission interim is in it or is not.
        assert record["keepalives"] == len(answer["interim"])

    # Both requests were queued and answered by the harness: neither was
    # refused, cancelled, or told to wait differently, and the harness still
    # serialises them (the scripted sidecar saw exactly two POSTs).
    assert [post.get("operation_id") for post in side.posts] == [OP, OP]


def test_a_row_this_request_alone_can_claim_is_still_admitted(rig):
    """An admission witnessed while the id was UNIQUE is not retracted later.

    Request 1's own row appears while it is the only request carrying the id,
    so it is admitted and says so. Request 2 arrives afterwards: the row
    predates its snapshot, so it can borrow nothing -- and request 1's admission
    stands, because the row was attributable when it was read.
    """
    side, relay, url = rig
    progress = [dict(), dict()]
    boxes: list[dict] = [dict(), dict()]
    threads: list[threading.Thread] = []
    try:
        thread, boxes[0] = _start(relay, url, OP, progress[0])
        threads.append(thread)
        assert side.arrived[0].wait(30), "request 1 never reached the engine"
        side.add_owner("row-of-request-1")
        _await(lambda: _claims(_records(relay).get(0, {})),
               "request 1's own admission")
        thread, boxes[1] = _start(relay, url, OP, progress[1])
        threads.append(thread)
        assert side.arrived[1].wait(30), "request 2 never reached the engine"
    finally:
        for event in side.answer:
            event.set()
        for thread in threads:
            thread.join(60)

    _recorded(relay, 2)
    for box in boxes:
        assert "error" not in box, box.get("error")
    first_record, second_record = _records(relay)[0], _records(relay)[1]

    assert boxes[0]["result"]["status"] == 200
    assert first_record["outcome"] == admission.ANSWERED
    assert _claims(first_record)
    assert "admission_witnessed" not in first_record

    assert boxes[1]["result"]["status"] == 200
    assert not _claims(second_record), second_record
    assert [post.get("operation_id") for post in side.posts] == [OP, OP]


def test_a_later_request_with_the_same_id_is_admitted_after_the_first_answered(rig):
    """Sequential retry: the id is counted from arrival to ANSWER.

    The first request is answered and only then does the second one arrive with
    the same operation_id. Nothing is in flight for that id any more, so the
    retry owns its row like any other request.
    """
    side, relay, url = rig
    progress = [dict(), dict()]
    boxes: list[dict] = [dict(), dict()]
    threads: list[threading.Thread] = []
    try:
        thread, boxes[0] = _start(relay, url, OP, progress[0])
        threads.append(thread)
        assert side.arrived[0].wait(30), "the first request never reached the engine"
        side.add_owner("row-of-request-1")
        _await(lambda: _claims(_records(relay).get(0, {})),
               "the first request's own admission")
        side.answer[0].set()
        thread.join(60)
        assert not thread.is_alive()

        thread, boxes[1] = _start(relay, url, OP, progress[1])
        threads.append(thread)
        assert side.arrived[1].wait(30), "the retry never reached the engine"
        side.add_owner("row-of-request-2")
        _await(lambda: _claims(_records(relay).get(1, {})),
               "the retry's own admission")
    finally:
        for event in side.answer:
            event.set()
        for thread in threads:
            thread.join(60)

    _recorded(relay, 2)
    for box in boxes:
        assert "error" not in box, box.get("error")
    for index, box in enumerate(boxes):
        assert box["result"]["status"] == 200
        record = _records(relay)[index]
        assert record["outcome"] == admission.ANSWERED
        assert _claims(record)
        assert "admission_witnessed" not in record
    assert [post.get("operation_id") for post in side.posts] == [OP, OP]


def test_another_requests_shared_id_does_not_suppress_a_unique_one(rig):
    """The rule is per operation_id: a third request with its own id is admitted.

    Two requests share `op-shared` and a third carries `op-solo`. The shared id's
    rows are unclaimable, and the unique one's row is still this request's own
    admission -- the relay must not stop witnessing everything because one id
    is aliased.
    """
    side, relay, url = rig
    progress = [dict(), dict(), dict()]
    boxes: list[dict] = [dict(), dict(), dict()]
    threads: list[threading.Thread] = []
    try:
        for index, operation in enumerate((OP, OP, "op-solo")):
            thread, boxes[index] = _start(relay, url, operation, progress[index])
            threads.append(thread)
            assert side.arrived[index].wait(30), (
                f"request {index + 1} never reached the engine")
        side.add_owner("row-of-request-1", OP)
        side.add_owner("row-of-request-3", "op-solo")
        side.add_owner("row-of-request-2", OP)
        _await(lambda: all(_decided(record) for record in _records(relay).values()),
               "all three requests to say what the new owner rows were")
    finally:
        for event in side.answer:
            event.set()
        for thread in threads:
            thread.join(60)

    _recorded(relay, 3)
    for box in boxes:
        assert "error" not in box, box.get("error")
    records = _records(relay)
    for index in (0, 1):
        assert boxes[index]["result"]["status"] == 200
        assert not _claims(records[index]), records[index]
        assert records[index].get("admission_witnessed") is False
        assert records[index]["admission_observed"] is True
    assert boxes[2]["result"]["status"] == 200
    assert _claims(records[2]), records[2]
    assert "admission_witnessed" not in records[2]
    assert [post.get("operation_id") for post in side.posts] == [OP, OP, "op-solo"]


def test_a_request_in_flight_when_the_relay_stops_claims_no_row(rig):
    """A row that appears while the relay stops is credited to nobody.

    753d1c03b6 cleared the shared marks inside `stop()`, so the next owner row
    for an id was handed to the first request -- the one that was never refused
    -- while it was still queued: measured on that commit as
    `admitted_after_sec` on its record, twice, and never without a `stop()`.
    The interleaving is the test's own, in three events: request 1 reached the
    engine, request 1's watch is inside a `/lifecycle` read, and the relay has
    stopped. Only then does the row appear, so the row is read AFTER `stop()`
    returned by a request that was still in flight when it ran. Request 2
    carries the same id over HTTP/1.0 -- it joins the id but opens no watch --
    so the one watch here is unambiguously request 1's, and request 1 is the
    first arrival, which is the request the leak was measured on.
    """
    side, relay, url = rig
    progress = [dict(), dict()]
    boxes: list[dict] = [dict(), dict()]
    threads: list[threading.Thread] = []
    try:
        thread, boxes[0] = _start(relay, url, OP, progress[0])
        threads.append(thread)
        assert side.arrived[0].wait(30), "request 1 never reached the engine"
        thread, boxes[1] = _start(relay, url, OP, progress[1], version="HTTP/1.0")
        threads.append(thread)
        assert side.arrived[1].wait(30), "request 2 never reached the engine"
        _await(lambda: _records(relay)[0].get("admission_observed") is True,
               "request 1's watch to be running")
        # The watch's next read of the owner table is held open, so the watch is
        # parked inside the read the row has to be seen through.
        side.hold_lifecycle = True
        assert side.lifecycle_held.wait(30), "no watch read the owner table"
        relay.stop()
        # The row exists only from here: it is the row the stop-path leak was
        # measured from, and the request it is offered to was already stopped.
        side.add_owner("row-while-the-relay-stopped")
        side.hold_lifecycle = False
        side.release_lifecycle.set()
        # The claim is written by the watch thread, so the assertion below is
        # only meaningful once that thread has finished.
        _await(lambda: not _watch_running(), "request 1's watch to finish")
    finally:
        side.release_lifecycle.set()
        for event in side.answer:
            event.set()
        for thread in threads:
            thread.join(60)

    for index, box in enumerate(boxes):
        assert "error" not in box, box.get("error")
    records = _records(relay)
    first, second = records[0], records[1]

    # The relay stopped under both of them: this is the stop path, not an
    # ordinary answer.
    assert first["outcome"] == admission.ABANDONED, first
    assert first["admission_observed"] is True, first
    assert "admitted_after_sec" not in first, (
        "a request still in flight when the relay stopped claimed an owner row "
        f"as its admission: admitted_after_sec={first.get('admitted_after_sec')}")
    assert first.get("admission_witnessed") is False, first
    # Request 2 shared the id, so it claims nothing either.
    assert second["outcome"] == admission.ABANDONED, second
    assert "admitted_after_sec" not in second, second
    assert second.get("admission_witnessed") is False, second
    # The row the leak was measured from existed, and the harness saw exactly
    # one request per client: nothing was retried or refused along the way.
    assert [row["owner_id"] for row in side.owners] == ["row-while-the-relay-stopped"]
    assert [post.get("operation_id") for post in side.posts] == [OP, OP]
