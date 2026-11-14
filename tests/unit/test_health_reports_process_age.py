"""/health reports how long the serving process has been up.

The process's own age is read from the kernel on every request, so a probe can
tell "up 31 hours" from "restarted 40 seconds ago" without Docker metadata. The
two requests here are a real interval apart: a value frozen at import time would
report the same uptime twice and fail the second test.
"""
import time
from datetime import datetime


def _probe(client):
    """Return the `process` object of one /health answer, with the wall clock around it."""
    before = time.time()
    body = client.get("/health").json()
    after = time.time()
    return body["process"], before, after


def test_started_at_plus_uptime_is_the_request_time(client):
    process, before, after = _probe(client)

    assert process["reason"] is None
    started = datetime.fromisoformat(process["started_at"]).timestamp()
    assert before - 0.1 <= started + process["uptime_seconds"] <= after + 0.1


def test_a_later_request_reports_a_larger_uptime_for_the_same_start(client):
    first, _, _ = _probe(client)
    time.sleep(1.1)
    second, _, _ = _probe(client)

    assert second["uptime_seconds"] - first["uptime_seconds"] >= 1.0
    first_start = datetime.fromisoformat(first["started_at"]).timestamp()
    second_start = datetime.fromisoformat(second["started_at"]).timestamp()
    assert abs(second_start - first_start) < 0.05
