"""The repository gate reaches the Godot engine through an admission relay.

One run of a repository gate has three outcomes, and they stay three: it
passed, it ran and went red, or it did not run. The gate's exit code carries
the first two (0 and 1). The third is decided from what the ENGINE answered,
never from the gate's prose: every request the gate sends to the engine goes
through a relay on 127.0.0.1 that forwards it to the real builder and records
the engine's own answer -- the HTTP status and, for a refusal, the harness's
structured payload (`owner_kind`, `needs_reconciliation`, `owner_id`).

Render requests are admitted through the harness's durable render queue
(`docker/godot/godot_harness.py:acquire_render_owner_waiting`): a request that
finds a live render owner waits for it to release, and the answer carries
`render_owner_wait_sec` and `render_owner_waited_for_owner_ids`. The relay
gives each render request a bounded `render_wait_timeout_sec` when the gate
sent none, so a request that is not admitted inside that window comes back as
the harness's structured refusal while the gate is still listening. A gate
that sends its own value keeps it.

While a render request waits in that queue the relay keeps the gate's socket
alive: every `keepalive_sec` it sends the gate an interim
`HTTP/1.1 100 Continue`, which an HTTP/1.1 client reads and skips, and which
restarts the client's per-read socket timeout. The relay watches the
harness's own owner table (`GET /lifecycle`) for the request's
`operation_id`; once a new owner row carries it the request is admitted, the
relay sends one last interim response and then nothing until the answer. So
the gate's own timeout measures the render, counted from admission, and not
the queue in front of it. A request whose admission cannot be observed (no
`/lifecycle`, or the gate speaks HTTP/1.0) gets no interim responses at all.

The relay adds no retries. A refusal is recorded and handed to the gate
unchanged; what happens next is the scheduler's business
(`core/gate_deferral.py`), and it never charges the implementer for it.
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The harness routes that take the globally exclusive render lock
# (docker/godot/godot_harness.py:_Handler._RENDER_ROUTES).
RENDER_ROUTES = ("/playtest", "/script", "/x11_input_smoke")

DEFAULT_BUILDER_URL = "http://godot-builder:8080"

# How long one render request may queue for the render lock before the
# harness refuses it with a structured 409.
#
# The gate's own client timeout does not bound this: the relay keeps a queued
# request's socket alive (KEEPALIVE_SECONDS) and stops once the request is
# admitted, so that timeout counts from admission. This bounds how long one
# step holds the scheduler waiting in the queue; a request refused here is not
# run, and the scheduler re-acquires the verdict later.
RENDER_WAIT_SECONDS = 1500.0

# How often the relay tells a queued gate it is still waiting (see the module
# docstring). Below every client timeout the gate sets on a render route
# (/script 1800 s, /playtest 3600 s in tools/godot_gate.py).
KEEPALIVE_SECONDS = 20.0

# The engine answered the request (2xx).
ANSWERED = "answered"
# The engine refused admission (409: render owner in the way, a holder that
# needs reconciliation, or the request's own queue wait ran out).
NOT_ADMITTED = "not_admitted"
# The relay could not reach the engine at all.
UNREACHABLE = "unreachable"
# The engine answered with another HTTP error.
ENGINE_ERROR = "engine_error"
# The gate exited while the request was still in flight.
ABANDONED = "abandoned"

# A gate whose LAST engine request ended in one of these produced no verdict.
NOT_RUN_STATES = frozenset({NOT_ADMITTED, UNREACHABLE, ABANDONED, "undelivered"})

_ANSWER_FIELDS = ("owner_kind", "needs_reconciliation", "owner_id",
                  "render_owner_wait_sec", "render_owner_waited_for_owner_ids",
                  "waited_sec", "wait_timeout_sec", "deployment_fence_held")


def render_wait_seconds() -> float:
    """The queue window, read at call time; a bad value keeps the default."""
    raw = os.environ.get("AITELIER_REPO_GATE_RENDER_WAIT_SECONDS")
    if raw:
        try:
            value = float(raw)
            if value >= 0:
                return value
        except ValueError:
            pass
    return RENDER_WAIT_SECONDS


def keepalive_seconds() -> float:
    """The interim-response interval, read at call time; must be positive."""
    raw = os.environ.get("AITELIER_REPO_GATE_KEEPALIVE_SECONDS")
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return KEEPALIVE_SECONDS


class AdmissionRelay:
    """Forward the gate's engine requests and record how each was answered.

    `records` holds one dict per request, appended when the request ARRIVES
    and completed when it ends, so a request the gate abandoned mid-flight is
    still listed (with no `outcome`) rather than missing.
    """

    def __init__(self, upstream: str, *, render_wait_sec: float,
                 upstream_timeout: float, keepalive_sec: float | None = None):
        parts = urllib.parse.urlsplit(upstream)
        if parts.scheme != "http" or not parts.hostname:
            raise ValueError(f"engine URL must be http://host[:port]: {upstream!r}")
        self.upstream = upstream
        self._host = parts.hostname
        self._port = parts.port or 80
        self._prefix = parts.path.rstrip("/")
        self._render_wait = float(render_wait_sec)
        self._timeout = float(upstream_timeout)
        self._keepalive = float(keepalive_seconds() if keepalive_sec is None
                                else keepalive_sec)
        self._lock = threading.Lock()
        self._records: list[dict] = []
        self._in_flight: set = set()
        self._stopping = False
        self._server = None

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> str:
        relay = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_POST(self):
                relay._relay(self, "POST")

            def do_GET(self):
                relay._relay(self, "GET")

            def log_message(self, *_args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever,
                         name="gate-admission-relay", daemon=True).start()
        return f"http://127.0.0.1:{self._server.server_port}"

    def stop(self) -> None:
        """Stop serving and cut any request the gate left in flight.

        Cutting the upstream socket is what lets the harness see that nobody
        is waiting any more, so a queued request is dropped from the queue
        instead of rendering for a caller that is gone.
        """
        with self._lock:
            self._stopping = True
            conns = list(self._in_flight)
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        for conn in conns:
            sock = getattr(conn, "sock", None)
            try:
                if sock is not None:
                    sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._records]

    def _owner_ids(self, operation_id: str) -> set | None:
        """Owner rows the harness holds for `operation_id`, or None if unreadable."""
        conn = http.client.HTTPConnection(self._host, self._port, timeout=10)
        try:
            conn.request("GET", self._prefix + "/lifecycle")
            resp = conn.getresponse()
            data = resp.read()
            if resp.status != 200:
                return None
            owners = json.loads(data or b"{}").get("owners")
        except (OSError, http.client.HTTPException, ValueError, AttributeError):
            return None
        finally:
            conn.close()
        if not isinstance(owners, list):
            return None
        return {str(o.get("owner_id")) for o in owners
                if isinstance(o, dict)
                and str(o.get("operation_id")) == operation_id}

    # ── one request ────────────────────────────────────────────────────────

    def _relay(self, handler, method: str) -> None:
        started = time.monotonic()
        length = int(handler.headers.get("Content-Length") or 0)
        body = handler.rfile.read(length) if length else b""
        route = handler.path.split("?", 1)[0]
        record: dict = {"route": route, "method": method}
        with self._lock:
            record["seq"] = len(self._records)
            self._records.append(record)

        operation = handler.headers.get("X-AItelier-Operation")
        watched_operation = None
        if method == "POST" and route in RENDER_ROUTES:
            try:
                payload = json.loads(body or b"{}")
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                if "render_wait_timeout_sec" not in payload:
                    payload["render_wait_timeout_sec"] = self._render_wait
                    record["render_wait_injected"] = True
                record["render_wait_timeout_sec"] = payload["render_wait_timeout_sec"]
                # The harness names the owner row after this id
                # (`req["operation_id"] or X-AItelier-Operation`); a request
                # that carries neither gets one, so its admission can be seen.
                if not payload.get("operation_id") and not operation:
                    payload["operation_id"] = "relay-" + uuid.uuid4().hex
                    record["operation_id_injected"] = True
                watched_operation = str(payload.get("operation_id") or operation)
                body = json.dumps(payload).encode()

        # Interim responses go only to an HTTP/1.1 client, and only when the
        # harness's owner table can be read: without it admission cannot be
        # seen, and a keepalive that never stopped would take the gate's own
        # timeout away altogether.
        watch = None
        if (watched_operation is not None
                and handler.request_version == "HTTP/1.1"):
            before = self._owner_ids(watched_operation)
            record["admission_observed"] = before is not None
            if before is not None:
                watch = _AdmissionWatch(self, handler, watched_operation,
                                        before, started, record)
        conn = http.client.HTTPConnection(self._host, self._port,
                                          timeout=self._timeout)
        with self._lock:
            self._in_flight.add(conn)
        done: dict = {}
        try:
            headers = {"Content-Type": handler.headers.get("Content-Type")
                       or "application/json"}
            if operation:
                headers["X-AItelier-Operation"] = operation
            conn.request(method, self._prefix + handler.path,
                         body=body if method == "POST" else None,
                         headers=headers)
            if watch is not None:
                watch.start()
            resp = conn.getresponse()
            status, data = resp.status, resp.read()
        except (OSError, http.client.HTTPException) as exc:
            error = f"{type(exc).__name__}: {exc}"[:500]
            done.update(outcome=ABANDONED if self._stopping else UNREACHABLE,
                        error=error)
            status = 502
            data = json.dumps({"error": "engine not reached through the "
                                        "admission relay: " + error}).encode()
        else:
            done["status"] = status
            done["outcome"] = (ANSWERED if 200 <= status < 300
                               else NOT_ADMITTED if status == 409
                               else ENGINE_ERROR)
            try:
                answer = json.loads(data or b"{}")
            except ValueError:
                answer = None
            if isinstance(answer, dict):
                for key in _ANSWER_FIELDS:
                    if key in answer:
                        done[key] = answer[key]
                if done["outcome"] != ANSWERED and "error" in answer:
                    done["error"] = str(answer["error"])[:500]
        finally:
            with self._lock:
                self._in_flight.discard(conn)
            conn.close()
        done["elapsed_sec"] = round(time.monotonic() - started, 3)
        if watch is not None:
            # From here only the final answer is written to the gate.
            watch.stop()
            with self._lock:
                done["keepalives"] = watch.sent

        try:
            handler.send_response(status)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(data)))
            handler.end_headers()
            handler.wfile.write(data)
            handler.wfile.flush()
            done["delivered"] = True
        except OSError as exc:
            done["delivered"] = False
            done["delivery_error"] = f"{type(exc).__name__}: {exc}"[:300]
        with self._lock:
            record.update(done)


class _AdmissionWatch:
    """Keeps a queued gate's socket alive until the harness admits its request.

    Polls the harness's owner table; a NEW owner row for the request's
    `operation_id` (one that was not there before the request was sent) is its
    admission. Until then an interim `100 Continue` goes to the gate every
    `keepalive_sec`; at admission one more goes, and then none. Writes to the
    gate happen under `write_lock`, and none after `stop()`, so an interim
    response can never interleave with the final one.
    """

    def __init__(self, relay: AdmissionRelay, handler, operation_id: str,
                 before: set, started: float, record: dict):
        self._relay = relay
        self._handler = handler
        self._operation = operation_id
        self._before = before
        self._started = started
        self._record = record
        self._stopped = threading.Event()
        self._write_lock = threading.Lock()
        self.sent = 0
        interval = relay._keepalive
        self._interval = interval
        self._poll = min(2.0, interval / 2.0)

    def start(self) -> None:
        threading.Thread(target=self._run, name="gate-admission-watch",
                         daemon=True).start()

    def stop(self) -> None:
        with self._write_lock:
            self._stopped.set()

    def _send(self) -> bool:
        with self._write_lock:
            if self._stopped.is_set():
                return False
            try:
                self._handler.wfile.write(b"HTTP/1.1 100 Continue\r\n\r\n")
                self._handler.wfile.flush()
            except OSError:
                self._stopped.set()
                return False
            self.sent += 1
            return True

    def _run(self) -> None:
        last = time.monotonic()
        while not self._stopped.wait(self._poll):
            owners = self._relay._owner_ids(self._operation)
            if owners is None:
                # Admission can no longer be seen: stop, so the gate's own
                # timeout is never suspended past it.
                with self._relay._lock:
                    self._record["admission_observed"] = False
                return
            if owners - self._before:
                with self._relay._lock:
                    self._record["admitted_after_sec"] = round(
                        time.monotonic() - self._started, 3)
                self._send()
                return
            if time.monotonic() - last >= self._interval:
                if not self._send():
                    return
                last = time.monotonic()


def admission_summary(records: list[dict]) -> dict:
    """What the engine said to this gate run, as one structured reading.

    `state` is the fate of the LAST engine request the gate made: a gate stops
    at the first engine answer it cannot use, so that request is the one its
    exit code is about. `queued_behind` lists every render owner a request of
    this run waited for in the queue.
    """
    ordered = sorted(records, key=lambda r: r.get("seq", 0))
    queued_behind: list = []
    waited = 0.0
    for r in ordered:
        for owner in r.get("render_owner_waited_for_owner_ids") or []:
            if owner not in queued_behind:
                queued_behind.append(owner)
        try:
            waited += float(r.get("render_owner_wait_sec") or 0.0)
        except (TypeError, ValueError):
            pass
    summary = {"requests": ordered, "queued_behind": queued_behind,
               "render_owner_wait_sec": round(waited, 3)}
    if not ordered:
        summary["state"] = "no_engine_request"
        return summary
    last = ordered[-1]
    outcome = last.get("outcome")
    if outcome is None:
        state = ABANDONED
    elif outcome in (NOT_ADMITTED, UNREACHABLE, ABANDONED):
        state = outcome
    elif last.get("delivered") is False:
        state = "undelivered"
    else:
        state = outcome
    summary["state"] = state
    summary["last_route"] = last.get("route")
    return summary
