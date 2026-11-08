"""A builder that ANSWERED with an error is a failed gate, not a skipped one.

``post_playtest`` used to fold every exception from its POST into the
"godot-builder unreachable" branch. ``urllib.error.HTTPError`` is a URLError,
so a 500 the sidecar really sent (a malformed spec: ``actions: 5``, measured
2026-11-08 in evidence/playtest-two-reading-20261108, key crash_consequence)
came back as ``passed: true, gate_skipped: true``. A malformed contract then
passed the gate.

Each case drives the real client helper against a real HTTP server bound to
127.0.0.1 (no request leaves the machine). ``log_gate_skip`` is recorded rather
than written, so nothing lands in ~/.AItelier or in the tree.
"""
from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from aitelier.tools.godot_playtest import impl


class _Builder:
    """A finite stand-in for the sidecar's POST /playtest, on 127.0.0.1."""

    def __init__(self):
        self.status = 200
        self.body = b"{}"
        self.posts: list[dict] = []
        self.server = None

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d" % self.server.server_port

    def answer(self, status: int, body) -> None:
        self.status = status
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()

    def start(self) -> None:
        builder = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                builder.posts.append(json.loads(self.rfile.read(n) or b"{}"))
                self.send_response(builder.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(builder.body)))
                self.end_headers()
                self.wfile.write(builder.body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()


@pytest.fixture
def builder(monkeypatch):
    b = _Builder()
    b.start()
    monkeypatch.setattr(impl, "_BUILDER_URL", b.url)
    yield b
    b.stop()


@pytest.fixture
def skips(monkeypatch):
    """Record log_gate_skip calls instead of writing ~/.AItelier/logs."""
    calls: list[tuple] = []
    monkeypatch.setattr(impl, "log_gate_skip",
                        lambda gate, reason, **detail: calls.append((gate, reason, detail)))
    return calls


def _payload(**extra) -> dict:
    return {"project_dir": "/nonexistent/proj", "spec": {"scenarios": [{"name": "a"}]},
            **extra}


def test_builder_500_fails_the_gate_and_records_the_answer(builder, skips):
    crash = {"error": "'int' object is not iterable"}
    builder.answer(500, crash)

    out = impl.post_playtest(_payload())

    assert out["passed"] is False
    assert out.get("gate_skipped") is not True
    assert out["builder_error"] == {"status": 500, "response": crash}
    assert "answered" in out["summary"] and "error" in out["summary"]
    assert "unreachable" not in out["summary"]
    assert skips == [], "an answered error must not be logged as an unreachable skip"
    assert len(builder.posts) == 1, "the request must have reached the builder"


def test_closed_port_keeps_todays_unreachable_shape(monkeypatch, skips):
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()                       # nothing listens on this port now
    monkeypatch.setattr(impl, "_BUILDER_URL", "http://127.0.0.1:%d" % port)

    out = impl.post_playtest(_payload())

    assert out["passed"] is True
    assert out["gate_skipped"] is True
    assert out["spec_used"] is False
    assert out["frames"] == 0
    assert "did not answer" in out["summary"]
    assert skips == [("godot_playtest", "godot-builder unreachable",
                      {"url": "http://127.0.0.1:%d" % port,
                       "error": "URLError"})]


def test_render_owner_409_is_still_a_skip_naming_the_owner(builder, skips):
    refusal = {"error": "render owner exists", "owner_kind": "owner_lost",
               "owner_id": "owner-A", "needs_reconciliation": True,
               "detail": "lost"}
    builder.answer(409, refusal)

    out = impl.post_playtest(_payload())

    assert out["passed"] is True
    assert out["gate_skipped"] is True
    assert out["skipped_because"] == "render_owner_owner_lost"
    assert out["render_owner_conflict"]["owner_id"] == "owner-A"
    assert "owner-A" in out["summary"]
    assert "gate_refused" not in out and "builder_error" not in out


def test_ui_text_400_is_still_a_refusal_with_the_builder_body(builder, skips):
    refusal = {"error": "ui_text_snapshot not supported", "detail": "no"}
    builder.answer(400, refusal)

    out = impl.post_playtest(_payload(ui_text_snapshot={"version": 1}))

    assert out["passed"] is False
    assert out["gate_refused"] is True
    assert out["builder_refusal"] == {"status": 400, "response": refusal}
    assert not out.get("gate_skipped")
    assert "builder_error" not in out
