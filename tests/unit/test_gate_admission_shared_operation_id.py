"""Two render requests that carry the same operation_id are not one admission.

The relay decides "this request was admitted" from the harness's render-owner
row (`aitelier/gate_admission.py:_AdmissionWatch`): a row whose
`operation_id` was not there before the request was sent. `acquire_render_owner`
mints `owner_id` itself, so the row names no request. Two requests carrying the
SAME operation_id therefore alias: when the first one's row appears after the
second one's snapshot, the second recorded `admitted_after_sec` from the first
one's row and sent the final admission `100 Continue` although its own render
had not started. The gate then counted its own client timeout from an admission
it never got, timed out while still queued, and reported a render it never began
as not run (iss-b859d7478c524da1 item 4).

Every test here serves the REAL harness admission code (`tests/gate_fixture.py`)
at a scale where the queue and the render are both measurable, and reads the
relay's own per-request records. The client speaks raw HTTP/1.1 so the interim
`100 Continue` responses are visible too: the admission one ends the queue
keepalive once, and a request whose keepalive never ended keeps receiving them
at the ordinary cadence.
"""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.parse

from aitelier import gate_admission as admission
from tests.gate_fixture import HOLDER, HarnessRig


def _answer(url, operation_id, *, path="/script", timeout=180):
    """POST one render request over raw HTTP/1.1 and read the whole answer.

    Returns the final status, its body, the arrival time of every interim
    `100 Continue`, and when the final answer arrived.
    """
    parts = urllib.parse.urlsplit(url)
    payload = json.dumps({"project_dir": "/tmp/shared-operation",
                          "project_id": "shared-operation",
                          "run_id": "shared-operation",
                          "operation_id": operation_id,
                          "scripts": ["res://tests/a.gd"]}).encode()
    head = (f"POST {path} HTTP/1.1\r\nHost: {parts.hostname}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\n"
            f"X-AItelier-Operation: {operation_id}\r\n\r\n").encode()
    sock = socket.create_connection((parts.hostname, parts.port), timeout=timeout)
    started = time.monotonic()
    interim: list[float] = []
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
                    interim.append(time.monotonic() - started)
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


def _wait_in_flight(relay, operation, count, timeout=30.0):
    """Wait until the relay is carrying `count` requests for `operation`.

    The relay counts an operation id from arrival to answer; that counter is
    the only observable that a request has reached the relay and taken its
    snapshot, so the test can order the two requests without sleeping on the
    implementation's own timings.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with relay._lock:
            if relay._in_flight_operations.get(operation, 0) >= count:
                return
        time.sleep(0.01)
    raise AssertionError(f"{operation} never reached {count} request(s) in flight")


def test_a_second_same_operation_id_borrows_no_admission_from_the_first(
        tmp_path, monkeypatch):
    """Queue 0.3 s, then first renders 1.5 s, then the duplicate renders 1.5 s."""
    rig = HarnessRig(tmp_path / "harness", monkeypatch, render_seconds=1.5)
    relay = admission.AdmissionRelay(rig.base, render_wait_sec=60,
                                     upstream_timeout=120, keepalive_sec=0.2)
    url = relay.start()
    first_box: dict = {}
    second_box: dict = {}
    try:
        rig.hold()
        first, first_box = _in_thread(_answer, url, "op-shared")
        _wait_in_flight(relay, "op-shared", 1)
        second, second_box = _in_thread(_answer, url, "op-shared")
        _wait_in_flight(relay, "op-shared", 2)
        # Both requests are inside the relay and neither owns a row yet.
        # Releasing the holder is what makes the FIRST request's row appear --
        # the only new row either request can see -- and its admission now
        # happens strictly after the duplicate was already in flight.
        time.sleep(0.3)
        rig.release.set()
        first.join(120)
        second.join(120)
        assert not first.is_alive() and not second.is_alive()
    finally:
        relay.stop()
        rig.close()

    assert "error" not in first_box, first_box.get("error")
    assert "error" not in second_box, second_box.get("error")
    first_answer, second_answer = first_box["result"], second_box["result"]
    first_record, second_record = relay.snapshot()
    assert (first_record["seq"], second_record["seq"]) == (0, 1)

    # The first request owns the operation id, so it IS witnessed, from the row
    # that appears when the holder lets go.
    assert first_answer["status"] == 200
    assert first_record["outcome"] == admission.ANSWERED
    assert first_record["admission_observed"] is True
    assert first_record["admitted_after_sec"] > 0.2
    assert "admission_witnessed" not in first_record

    # The duplicate ran and was answered by the harness, and it reports no
    # admission of its own: no admitted_after_sec from the first one's row.
    assert second_answer["status"] == 200
    assert second_record["outcome"] == admission.ANSWERED
    assert "admitted_after_sec" not in second_record
    assert second_record["admission_witnessed"] is False

    # Both renders really happened, in the harness's own order: the duplicate
    # was neither refused nor cancelled, and the serialisation is untouched.
    assert rig.rendered == [HOLDER, "op-shared", "op-shared"]

    # Its interim responses are the ordinary keepalive cadence for the whole
    # wait, so none of them ended the keepalive: the admission send happens
    # once and is then followed by nothing but the answer.
    interim = second_answer["interim"]
    assert len(interim) >= 5, second_answer
    assert max(later - earlier for earlier, later in zip(interim, interim[1:])) < 1.0
    assert second_answer["answered_after"] - interim[-1] < 1.0


def test_a_later_request_with_the_same_operation_id_is_still_admitted(
        tmp_path, monkeypatch):
    """Sequential retry: the first answer released the operation id."""
    rig = HarnessRig(tmp_path / "harness", monkeypatch, render_seconds=0.4)
    relay = admission.AdmissionRelay(rig.base, render_wait_sec=60,
                                     upstream_timeout=60, keepalive_sec=0.1)
    url = relay.start()
    try:
        first_answer = _answer(url, "op-repeat")
        second_answer = _answer(url, "op-repeat")
    finally:
        relay.stop()
        rig.close()

    first_record, second_record = relay.snapshot()
    for answer, record in ((first_answer, first_record),
                           (second_answer, second_record)):
        assert answer["status"] == 200
        assert record["outcome"] == admission.ANSWERED
        assert "admission_witnessed" not in record
        assert record["admission_observed"] is True
        # Its own row, seen while it renders: a request that starts after the
        # earlier one was answered is not its duplicate.
        assert record["admitted_after_sec"] >= 0
    assert rig.rendered == ["op-repeat", "op-repeat"]


def test_two_requests_with_different_operation_ids_are_each_admitted(
        tmp_path, monkeypatch):
    """Different ids: the concurrent case, unchanged in both records."""
    rig = HarnessRig(tmp_path / "harness", monkeypatch, render_seconds=0.5)
    relay = admission.AdmissionRelay(rig.base, render_wait_sec=60,
                                     upstream_timeout=120, keepalive_sec=0.2)
    url = relay.start()
    first_box: dict = {}
    second_box: dict = {}
    try:
        rig.hold()
        first, first_box = _in_thread(_answer, url, "op-a")
        _wait_in_flight(relay, "op-a", 1)
        second, second_box = _in_thread(_answer, url, "op-b")
        _wait_in_flight(relay, "op-b", 1)
        time.sleep(0.3)
        rig.release.set()
        first.join(120)
        second.join(120)
        assert not first.is_alive() and not second.is_alive()
    finally:
        relay.stop()
        rig.close()

    for box in (first_box, second_box):
        assert "error" not in box, box.get("error")
    first_record, second_record = relay.snapshot()
    for answer, record in ((first_box["result"], first_record),
                           (second_box["result"], second_record)):
        assert answer["status"] == 200
        assert record["outcome"] == admission.ANSWERED
        # Each waited in the queue and each was admitted by its own row: no
        # operation id is shared, so nothing here is a duplicate.
        assert record["admission_observed"] is True
        assert record["admitted_after_sec"] > 0.2
        assert "admission_witnessed" not in record
    assert sorted(rig.rendered) == sorted([HOLDER, "op-a", "op-b"])
