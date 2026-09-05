"""`debugctl await` must reconcile the stream against the run's own status.

Smoke D, 2026-09-05: a `coding_impl` run completed at 14:09:19.861Z and this
watcher subscribed at 14:09:19.878Z — 17ms late. The push had already gone to
the consumers that were connected, `StreamManager.push_log` only buffers for
replay when a channel has NO consumer, so there was nothing to replay for the
late arrival. `await` waited out its full 180s and printed TIMEOUT for a run
that had SUCCEEDED. A watcher whose "still running" and whose "I arrived late"
look identical is worse than none, because a driver acts on the difference.

These tests drive `cmd_await` against a scripted SSE body and a scripted
`GET /api/runs/{id}`, and assert the exit code — the thing callers branch on.
"""
import argparse
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import debugctl  # noqa: E402

RUN = "97cf57f4-ecbb-4fac-b4aa-ec70020970ee"
OTHER_RUN = "11111111-2222-3333-4444-555555555555"
PID = "coding-impl-9322cd6a"


def _args(**kw):
    base = dict(project_id=PID, url="http://localhost:4444", timeout=1,
                run=RUN, follow=False)
    base.update(kw)
    return argparse.Namespace(**base)


def _sse(event: dict) -> bytes:
    """One SSE data line exactly as StreamManager.event_generator writes it."""
    return ("data: " + json.dumps({"log": json.dumps(event)}) + "\n").encode()


# Registering the queue pushes this to __global__, so it is the first thing a
# subscriber receives — and it is what proves the subscription is live.
PRESENCE = _sse({"type": "presence", "total": 2, "authenticated": 1, "anonymous": 1})


def _completed(run_id=RUN):
    return _sse({"type": "run_completed", "run_id": run_id, "project_id": PID,
                 "reason": "Node 'done' reached"})


def _checkpoint():
    return _sse({"type": "checkpoint_paused", "run_id": RUN, "project_id": PID,
                 "step_id": "plan", "label": "approve the plan", "next_node": "implement"})


class _Stream:
    """Scripted SSE response body.

    After the scripted lines it keeps yielding heartbeat comments instead of
    ending, because that is what the real server does — a test that ended the
    body would measure the reconnect path while claiming to measure a timeout.
    """

    def __init__(self, lines, order, keep_open=True):
        self._lines, self._order, self._keep = list(lines), order, keep_open

    def __iter__(self):
        for line in self._lines:
            if isinstance(line, Exception):
                raise line
            self._order.append("stream-line")
            yield line
        while self._keep:
            yield b": ping\n"


class _Json:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self._payload).encode()


def _install(monkeypatch, *, lines=None, run_row=None, keep_open=True,
             stream_error=None, streams=None, run_rows=None):
    """Patch urlopen for both URLs and return the ordered call record.

    `streams` / `run_rows` script SUCCESSIVE connections and status reads (the
    last entry repeats), which is what a reconnect boundary needs.
    """
    order = []
    scripted = list(streams) if streams is not None else [lines]
    rows = list(run_rows) if run_rows is not None else [run_row]

    def fake_urlopen(url, timeout=None):
        if "/api/events/stream" in url:
            order.append("stream-open")
            nth = sum(1 for o in order if o == "stream-open")
            if stream_error is not None and nth > len(scripted):
                raise stream_error
            body = scripted[min(nth, len(scripted)) - 1]
            return _Stream(body, order, keep_open=keep_open)
        if "/api/runs/" in url:
            order.append("status-read")
            nth = sum(1 for o in order if o == "status-read")
            row = rows[min(nth, len(rows)) - 1]
            if isinstance(row, Exception):
                raise row
            return _Json(row)
        raise AssertionError(f"unexpected URL {url}")

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(debugctl.time, "sleep", lambda *a, **k: None)
    return order


def _row(status="running", run_id=RUN, **extra):
    row = {"id": run_id, "status": status, "project_id": PID,
           "error_reason": None, "cancel_requested_at": None}
    row.update(extra)
    return row


def _run_await(args):
    with pytest.raises(SystemExit) as exc:
        debugctl.cmd_await(args)
    return exc.value.code


# ── the defect ────────────────────────────────────────────────────────────

def test_a_late_attach_reports_the_real_ending_not_a_timeout(monkeypatch, capsys):
    """Smoke D exactly: nothing to replay, run already completed."""
    _install(monkeypatch, lines=[PRESENCE], run_row=_row("completed"))
    code = _run_await(_args())
    out = capsys.readouterr().out
    assert code == 0, f"late attach still reports non-completion: {out!r}"
    assert "COMPLETED" in out
    assert "TIMEOUT" not in out
    # The reader must be able to tell a reconciled ending from a witnessed one.
    assert "reconciled" in out


def test_the_status_is_read_only_after_the_subscription_is_live(monkeypatch):
    """Opening the connection is not subscribing.

    `urlopen` returns on the response HEADERS; Starlette registers the queue
    inside the body generator, which runs after. Reading the status on urlopen
    alone would re-open the same race one connection-setup wide.
    """
    order = _install(monkeypatch, lines=[PRESENCE], run_row=_row("completed"))
    _run_await(_args())
    assert order[:3] == ["stream-open", "stream-line", "status-read"], order


def test_a_pushed_terminal_event_still_ends_the_watch(monkeypatch, capsys):
    """The stream remains the primary path; reconciliation is the safety net."""
    _install(monkeypatch, lines=[PRESENCE, _completed()], run_row=_row("running"))
    code = _run_await(_args())
    out = capsys.readouterr().out
    assert code == 0
    assert "COMPLETED" in out
    assert "reconciled" not in out          # witnessed, not inferred


# ── it must not be fooled ─────────────────────────────────────────────────

def test_another_runs_status_cannot_complete_this_watch(monkeypatch, capsys):
    """`/api/runs/{id}` resolves a PROJECT id too, and a project resolves to its
    newest run — so an inexact handle must never authorise an ending."""
    _install(monkeypatch, lines=[PRESENCE],
             run_row=_row("completed", run_id=OTHER_RUN))
    code = _run_await(_args())
    out = capsys.readouterr().out
    assert code == 2, f"a foreign run's status ended the watch: {out!r}"
    assert "STATUS IGNORED" in out
    assert "COMPLETED" not in out


def test_another_runs_terminal_event_cannot_complete_this_watch(monkeypatch, capsys):
    _install(monkeypatch, lines=[PRESENCE, _completed(run_id=OTHER_RUN)],
             run_row=_row("running"))
    code = _run_await(_args())
    assert code == 2
    assert "COMPLETED" not in capsys.readouterr().out


def test_without_an_exact_run_no_status_is_read_at_all(monkeypatch, capsys):
    """A project can have had several runs; the newest one having finished says
    nothing about the run the caller meant."""
    order = _install(monkeypatch, lines=[PRESENCE], run_row=_row("completed"))
    code = _run_await(_args(run=""))
    assert code == 2
    assert "status-read" not in order, order
    assert "COMPLETED" not in capsys.readouterr().out


def test_an_unreadable_status_is_not_an_outcome(monkeypatch, capsys):
    """"I could not read it" and "it is still going" are different facts."""
    _install(monkeypatch, lines=[PRESENCE], run_row=OSError("connection refused"))
    code = _run_await(_args())
    out = capsys.readouterr().out
    assert code == 2
    assert "STATUS UNKNOWN" in out
    assert "COMPLETED" not in out and "FAILED" not in out


# ── the other terminal states ─────────────────────────────────────────────

@pytest.mark.parametrize("row,word", [
    (_row("failed", error_reason="green agent exhausted retries"), "FAILED"),
    (_row("failed", error_reason="stopped by operator",
          cancel_requested_at="2026-09-05 14:20:00"), "CANCELLED"),
])
def test_a_failed_or_cancelled_run_reconciles_to_a_failure(monkeypatch, capsys, row, word):
    _install(monkeypatch, lines=[PRESENCE], run_row=row)
    code = _run_await(_args())
    out = capsys.readouterr().out
    assert code == 1, f"a dead run reported as {code}: {out!r}"
    assert word in out
    assert "TIMEOUT" not in out


# ── what must NOT change ──────────────────────────────────────────────────

def test_follow_keeps_watching_past_a_checkpoint_and_paused_is_not_terminal(
        monkeypatch, capsys):
    """`paused` is a settled status for `wait_for_run` but NOT an ending here:
    --follow promises to witness the run to its terminal state."""
    _install(monkeypatch, lines=[PRESENCE, _checkpoint(), _completed()],
             run_row=_row("paused"))
    code = _run_await(_args(follow=True))
    out = capsys.readouterr().out
    assert code == 0
    assert "CHECKPOINT" in out
    assert "COMPLETED" in out
    assert out.index("CHECKPOINT") < out.index("COMPLETED")


def test_without_follow_a_checkpoint_still_ends_the_watch(monkeypatch, capsys):
    _install(monkeypatch, lines=[PRESENCE, _checkpoint()], run_row=_row("running"))
    code = _run_await(_args(follow=False))
    assert code == 0
    assert "CHECKPOINT" in capsys.readouterr().out


def test_a_genuinely_running_run_still_times_out_without_polling(monkeypatch, capsys):
    """The fix must not become a poll loop: exactly two reads, at boundaries."""
    order = _install(monkeypatch, lines=[PRESENCE], run_row=_row("running"))
    started = time.time()
    code = _run_await(_args(timeout=1))
    out = capsys.readouterr().out
    assert code == 2
    assert "TIMEOUT" in out
    assert time.time() - started >= 1.0
    assert order.count("status-read") == 2, order      # connect + before timeout


def test_exhausted_reconnects_stay_distinct_from_a_timeout(monkeypatch, capsys):
    """Transport failure is exit 4. It was 2 once, and a watcher that had
    silently stopped watching looked exactly like one that waited its deadline."""
    _install(monkeypatch, lines=[PRESENCE], run_row=_row("running"),
             keep_open=False, stream_error=OSError("connection reset"))
    code = _run_await(_args(timeout=30))
    out = capsys.readouterr().out
    assert code == 4, out
    assert "STREAM LOST" in out
    assert "TIMEOUT" not in out


# ── transport errors are not run failures ─────────────────────────────────

def test_a_socket_read_error_is_never_reported_as_a_run_failure(monkeypatch, capsys):
    """`urlopen` carries a socket timeout, so a quiet stream raises out of the
    iteration. Unhandled that escaped as a traceback and exit 1 — the code that
    means THE RUN FAILED. Observed live on 2026-09-05 with `--timeout 12`,
    below the server's 15s heartbeat."""
    _install(monkeypatch, streams=[[PRESENCE, TimeoutError("timed out")]],
             run_row=_row("running"), keep_open=False,
             stream_error=OSError("connection reset"))
    code = _run_await(_args(timeout=30))
    out = capsys.readouterr().out
    assert code != 1, f"a transport error was reported as a run failure: {out!r}"
    assert code == 4, out
    assert "STREAM READ ERROR" in out
    assert "TimeoutError" in out


def test_a_run_that_ends_while_disconnected_is_caught_on_reconnect(monkeypatch, capsys):
    """The reconnect boundary is a new subscription, so it earns a new
    reconciliation: the gap a reconnect leaves is exactly long enough for the
    terminal event to be pushed to nobody."""
    order = _install(
        monkeypatch,
        streams=[[PRESENCE, TimeoutError("timed out")], [PRESENCE]],
        run_rows=[_row("running"), _row("completed")],
        keep_open=True)
    code = _run_await(_args(timeout=30))
    out = capsys.readouterr().out
    assert code == 0, out
    assert "COMPLETED" in out
    assert "reconciled at connect" in out
    assert order.count("stream-open") == 2, order
    assert order.count("status-read") == 2, order
