"""`debugctl await` HTTP reads must carry the CLI's own credential headers.

Before this fix both the exact-run snapshot (`_run_snapshot`) and the SSE
subscribe (`cmd_await` -> `_reopen`, initial and on every reconnect) called
`urlopen(url)` on the bare URL: an anonymous request. Against a server whose
write-gate requires identity the watch reported a 403 as an unreachable
server, and the driver acted on a watcher that could not read anything.

These tests drive the real code paths with a synthetic token configured
through the existing `cli.client._auth_headers` helper (core/driver_credentials
resolves it — no new token file is invented here) and inspect the ACTUAL
`urllib.request.Request` objects `urlopen` receives, so a future regression to
a headerless call fails here rather than live.

UNRUN: source-candidate delivery; the director's independent reviewer runs
these on CPU with no live effects.
"""
import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import debugctl  # noqa: E402
import cli.client  # noqa: E402

RUN = "97cf57f4-ecbb-4fac-b4aa-ec70020970ee"
PID = "coding-impl-9322cd6a"
TOKEN_HEADER = {"Authorization": "Bearer synthetic-test-token"}


@pytest.fixture
def synthetic_credentials(monkeypatch):
    """Make the EXISTING helper hand back a synthetic token header."""
    monkeypatch.setattr(cli.client, "_auth_headers", lambda: dict(TOKEN_HEADER))


class _Recorder:
    """Stands in for urlopen; records the Request objects it is handed."""

    def __init__(self, responses, *, snapshots=None):
        self.requests = []
        self._responses = list(responses)
        self._snapshots = list(snapshots) if snapshots is not None else None

    def __call__(self, req, timeout=None):
        assert isinstance(req, urllib.request.Request), \
            f"expected a Request, got {type(req).__name__}"
        self.requests.append(req)
        responses = self._responses
        if self._snapshots is not None:
            if req.full_url == f"http://localhost:4444/api/runs/{RUN}":
                responses = self._snapshots
            else:
                assert req.full_url == "http://localhost:4444/api/events/stream"
        outcome = responses.pop(0) if len(responses) > 1 else responses[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    @property
    def headers(self):
        return [{k.lower(): v for k, v in r.header_items()} for r in self.requests]


class _Body:
    """Minimal read()-able context manager, like an HTTPResponse."""

    def __init__(self, data: bytes = b""):
        self._data = data

    def read(self, n=-1):
        return self._data

    def __iter__(self):
        return iter(self._data.splitlines(keepends=True))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _args(**kw):
    base = dict(project_id=PID, url="http://localhost:4444", timeout=1,
                run=RUN, follow=True)
    base.update(kw)
    return argparse.Namespace(**base)


def _sse(event: dict) -> bytes:
    return ("data: " + json.dumps({"log": json.dumps(event)}) + "\n").encode()


def _completed():
    return _sse({"type": "run_completed", "run_id": RUN, "project_id": PID,
                 "reason": "Node 'done' reached"})


def test_snapshot_request_carries_the_existing_helper_headers(
        monkeypatch, synthetic_credentials):
    rec = _Recorder([_Body(json.dumps({"id": RUN, "status": "running"}).encode())])
    monkeypatch.setattr(urllib.request, "urlopen", rec)
    run = debugctl._run_snapshot("http://localhost:4444", RUN)
    assert run == {"id": RUN, "status": "running"}
    assert rec.requests[0].full_url == f"http://localhost:4444/api/runs/{RUN}"
    assert rec.headers[0]["authorization"] == TOKEN_HEADER["Authorization"]


def test_snapshot_exact_404_verdict_unchanged(monkeypatch, synthetic_credentials):
    rec = _Recorder([urllib.error.HTTPError(
        f"http://localhost:4444/api/runs/{RUN}", 404, "no", {}, b"")])
    monkeypatch.setattr(urllib.request, "urlopen", rec)
    assert debugctl._run_snapshot("http://localhost:4444", RUN) is debugctl._RUN_NOT_FOUND
    assert rec.headers[0]["authorization"] == TOKEN_HEADER["Authorization"]


def test_snapshot_403_is_unknown_not_missing(monkeypatch, synthetic_credentials):
    rec = _Recorder([urllib.error.HTTPError(
        f"http://localhost:4444/api/runs/{RUN}", 403, "forbidden", {}, b"")])
    monkeypatch.setattr(urllib.request, "urlopen", rec)
    assert debugctl._run_snapshot("http://localhost:4444", RUN) is None


def test_sse_initial_and_reconnected_connects_both_authenticated(
        monkeypatch, synthetic_credentials, capsys):
    # First connect yields only a heartbeat (no terminal event) and then
    # falls out of the iterator — a closed stream; the reconnect yields the
    # terminal event. Both connects must have carried the credential header.
    first = _Body(_sse({"type": "heartbeat"}))
    second = _Body(_completed())
    rec = _Recorder([first, Exception("connection reset"), second],
                    snapshots=[_Body(json.dumps({"id": RUN, "project_id": PID, "status": "running"}).encode())])
    monkeypatch.setattr(urllib.request, "urlopen", rec)
    monkeypatch.setattr(debugctl.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(debugctl.time, "time",
                        lambda: 0.0)  # never past the 1s deadline
    with pytest.raises(SystemExit) as exc:
        debugctl.cmd_await(_args())
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "RECONNECTED" in out
    assert "COMPLETED" in out
    assert [r.full_url for r in rec.requests] == [
        "http://localhost:4444/api/events/stream",
        f"http://localhost:4444/api/runs/{RUN}",
        "http://localhost:4444/api/events/stream",
        "http://localhost:4444/api/events/stream",
        f"http://localhost:4444/api/runs/{RUN}",
    ]
    for hdrs in rec.headers:
        assert hdrs.get("authorization") == TOKEN_HEADER["Authorization"]


def test_reconnect_loop_keeps_sending_credentials_after_failures(
        monkeypatch, synthetic_credentials):
    # The reconnect while-loop is exactly where a bare URL could creep back
    # in, so prove the credentials survive repeated transport failures.
    err = Exception("connection reset")
    rec = _Recorder([err, _Body(_completed())],
                    snapshots=[_Body(json.dumps({"id": RUN, "project_id": PID, "status": "running"}).encode())])
    monkeypatch.setattr(urllib.request, "urlopen", rec)
    monkeypatch.setattr(debugctl.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(debugctl.time, "time", lambda: 0.0)
    with pytest.raises(SystemExit) as exc:
        debugctl.cmd_await(_args())
    assert exc.value.code == 0
    assert [r.full_url for r in rec.requests] == [
        "http://localhost:4444/api/events/stream",
        "http://localhost:4444/api/events/stream",
        f"http://localhost:4444/api/runs/{RUN}",
    ]
    assert all(h.get("authorization") == TOKEN_HEADER["Authorization"]
               for h in rec.headers)


def test_late_checkpoint_read_uses_existing_caller_credentials(
        monkeypatch, synthetic_credentials, capsys):
    paused = {"id": RUN, "project_id": PID, "status": "paused",
              "current_node": "implement",
              "steps": [{"id": 42, "step_id": "plan", "status": "completed"}]}
    pending = {"project_id": PID, "checkpoint": "plan", "step": "plan",
               "checkpoint_instance": 42, "label": "approve plan"}
    rec = _Recorder([_Body(_sse({"type": "presence"}) + _completed()),
                     _Body(json.dumps(paused).encode()),
                     _Body(json.dumps(pending).encode())])
    monkeypatch.setattr(urllib.request, "urlopen", rec)
    with pytest.raises(SystemExit) as exc:
        debugctl.cmd_await(_args())
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert out.index("CHECKPOINT") < out.index("COMPLETED")
    assert [r.full_url for r in rec.requests] == [
        "http://localhost:4444/api/events/stream",
        f"http://localhost:4444/api/runs/{RUN}",
        f"http://localhost:4444/api/runs/{RUN}/checkpoint",
    ]
    assert all(h.get("authorization") == TOKEN_HEADER["Authorization"]
               for h in rec.headers)
