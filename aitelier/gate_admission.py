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
sent none -- a missing key and an explicit JSON null both mean none, because
the harness reads a null wait as an unbounded queue -- so a request that is not
admitted inside that window comes back as the harness's structured refusal
while the gate is still listening. A gate that sends its own number keeps it.

That refusal is the most contended answer a gate can get -- the request queued
behind a live owner for its whole window and was then not run -- and the
harness reports the SAME wait under the QUEUE's names:
`waited_sec` / `waited_for_owner_ids` (`godot_harness.py:_wait_timed_out`),
not the success path's `render_owner_*` ones. Both spellings are read here, so
a refused request keeps its owners and its seconds in `admission_summary` and
the render-contention block reads `waited` instead of a measured absence.
`waited_sec` is a general name, so it is read ONLY from a record this module
can point at as that refusal (`_render_queue_refusal`): a render route, an
engine refusal, and the refusal's own structured pair -- never from an
unrelated wait in another answer.

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

That witness is one owner row for one `operation_id`, and the row carries no
per-request nonce (the harness mints `owner_id` itself), so it identifies a
REQUEST only while one request carries that id. A render request that arrives
while another request on this relay is still carrying the same `operation_id`
is a duplicate: the row it will eventually get is indistinguishable from the
one already in flight, so its admission is NOT witnessed. It is still queued
and answered by the harness like any other request -- it is not refused,
cancelled, or told to wait differently -- and it keeps receiving the ordinary
interim response while it waits, but it never sends the admission interim
response that ends that keepalive and its record carries
`admission_witnessed: false` instead of `admitted_after_sec`. The aliasing
lasts only while both requests are in flight: an operation id is counted from
arrival to answer, so a later sequential request with the same id is admitted
and witnessed normally.

The relay adds no retries. A refusal is recorded and handed to the gate
unchanged; what happens next is the scheduler's business
(`core/gate_deferral.py`), and it never charges the implementer for it.
A request that was joined while it waited also claims no new row.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import socket
import stat
from pathlib import Path
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import ThreadingMixIn, UnixStreamServer

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

# _ANSWER_FIELDS is the whitelist the relay copies out of an engine answer.
# It carries BOTH spellings of the render queue's wait: the success path's
# `render_owner_*` names and the queue refusal's own `waited_*` names
# (`godot_harness.py:_wait_timed_out`). Without `waited_for_owner_ids` here a
# refusal would lose the owners it queued behind before `admission_summary`
# ever saw them.
_ANSWER_FIELDS = ("owner_kind", "needs_reconciliation", "owner_id",
                  "render_owner_wait_sec", "render_owner_waited_for_owner_ids",
                  "waited_sec", "waited_for_owner_ids", "wait_timeout_sec",
                  "deployment_fence_held")

# The owners key and the wait key of each spelling of a render-queue answer: the
# success path names them `render_owner_*`, the queue's refusal names them
# `waited_*`. ONE record is read under exactly one spelling, so a wait is never
# summed twice. `owner_id` is not a substitute for the owners list: a refusal
# that never queued (a holder needing reconciliation) names an owner and is no
# contention at all.
SUCCESS_OWNERS_FIELD, SUCCESS_WAIT_FIELD = ("render_owner_waited_for_owner_ids",
                                            "render_owner_wait_sec")
QUEUE_OWNERS_FIELD, QUEUE_WAIT_FIELD = ("waited_for_owner_ids", "waited_sec")


def _render_queue_refusal(record: dict) -> bool:
    """True iff this record is the render queue's own wait-timeout refusal.

    `waited_sec` is a general name for "somebody waited", so it is only ever
    read as render-queue contention from a record this can be proven to be
    that refusal: the engine refused admission (`not_admitted`, i.e. the 409)
    on a render route, and the answer carried the refusal's own structured
    pair `wait_timeout_sec` + `deployment_fence_held`. Those two keys are
    emitted together by `godot_harness.py:_wait_timed_out` -- and by no other
    answer the harness gives: the "render owner exists" 409 and the
    `RuntimeError` 409 carry neither, and the queue's success row's
    `waited_sec` is renamed to `render_owner_wait_sec` before the answer is
    built (`_Handler.do_POST`), so it never reaches an answer at all.
    """
    return (record.get("route") in RENDER_ROUTES
            and record.get("outcome") == NOT_ADMITTED
            and "wait_timeout_sec" in record
            and "deployment_fence_held" in record)


def _recorded_render_wait(record: dict) -> tuple[list, float]:
    """The owners and the seconds ONE record attributes to the render queue.

    A record naming neither spelling contributed no wait and no owner: that is
    the honest clean pole, not a default. The queue's own spelling is used
    only under `_render_queue_refusal`'s identity, so a `waited_sec` belonging
    to a different wait in a different answer can never become contention.
    """
    if "render_owner_waited_for_owner_ids" in record or "render_owner_wait_sec" in record:
        owner_key, wait_key = SUCCESS_OWNERS_FIELD, SUCCESS_WAIT_FIELD
    elif _render_queue_refusal(record):
        owner_key, wait_key = QUEUE_OWNERS_FIELD, QUEUE_WAIT_FIELD
    else:
        return [], 0.0
    owners = record.get(owner_key)
    try:
        seconds = float(record.get(wait_key) or 0.0)
    except (TypeError, ValueError):
        seconds = 0.0
    return (owners if isinstance(owners, list) else []), seconds


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


def relay_socket_path(home: Path, run_id: str, ticket: str) -> Path:
    """A separate compact socket identifier over the COMPLETE owner tuple.

    Report paths retain their full SHA256 run namespace. This BLAKE2s-128
    digest is its own full identifier, never a clipped report/run hash.
    Both backend and host compute it; no registry or caller-selected mount.
    """
    owner = json.dumps([run_id, ticket], ensure_ascii=False, separators=(",", ":")).encode()
    name = hashlib.blake2s(owner, digest_size=16).hexdigest() + ".s"
    path = Path(home).resolve() / "r" / name
    if len(os.fsencode(path)) >= 108:
        raise ValueError("owned relay path exceeds Linux AF_UNIX's 107-byte filesystem budget")
    return path


def relay_socket_namespace(home: Path, *, create: bool = False) -> Path:
    """Require the shared private namespace, without following an alias."""
    root = Path(home).resolve() / "r"
    if create:
        root.mkdir(mode=0o700, exist_ok=True)
    info = root.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise PermissionError("relay namespace must be an owned private directory, not an alias")
    return root


class AdmissionRelay:
    """Forward the gate's engine requests and record how each was answered.

    `records` holds one dict per request, appended when the request ARRIVES
    and completed when it ends, so a request the gate abandoned mid-flight is
    still listed (with no `outcome`) rather than missing. `admitted_after_sec`
    is only ever written from an owner row this request is alone in claiming;
    a request whose `operation_id` another request is still carrying records
    `admission_witnessed: false` and no admission time (module docstring).
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
        # operation_id -> the records of the requests this relay is carrying
        # with it. A record leaves when its request ends; while two are listed
        # the id is shared, and the record keeps that request's own no-claim
        # decision even if the mark behind it goes away.
        self._in_flight_operations: dict = {}
        # operation_ids that two requests on this relay carried at once: while
        # any of those overlaps is in flight, its owner rows name no request.
        self._shared_operations: set = set()
        self._stopping = False
        self._server = None
        self._unix_socket_identity = None

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self, unix_socket: str = "") -> str:
        relay = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_POST(self):
                relay._relay(self, "POST")

            def do_GET(self):
                relay._relay(self, "GET")

            def log_message(self, *_args):
                pass

        if unix_socket:
            path = Path(unix_socket)
            if len(os.fsencode(path)) >= 108:
                raise ValueError("admission relay exceeds Linux AF_UNIX's 107-byte path budget")
            if os.path.lexists(path):
                raise ValueError("admission relay socket already exists; never overwrite a listener")
            info = path.parent.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) & 0o077):
                raise PermissionError("admission relay requires an owned private parent")
            class UnixHTTPServer(ThreadingMixIn, UnixStreamServer):
                pass
            self._server = UnixHTTPServer(unix_socket, _Handler)
            os.chmod(path, 0o600)
            info = path.lstat()
            self._unix_socket_identity = (path, info.st_dev, info.st_ino, info.st_ctime_ns)
        else:
            self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever,
                         name="gate-admission-relay", daemon=True).start()
        return "" if unix_socket else f"http://127.0.0.1:{self._server.server_port}"

    def stop(self) -> None:
        """Stop serving and cut any request the gate left in flight.

        Cutting the upstream socket is what lets the harness see that nobody
        is waiting any more, so a queued request is dropped from the queue
        instead of rendering for a caller that is gone.

        Nothing still in flight when this returns can witness an admission
        afterwards: the mark and the record of every request carrying an
        operation_id are fenced here, so a row that appears while the relay is
        going away is credited to nobody.
        """
        with self._lock:
            self._stopping = True
            # A request that is still in flight here must never be able to claim
            # an owner row afterwards, so nothing it depends on is taken away:
            # every id still in flight keeps its shared mark (clearing it would
            # hand the next row for that id to the first request, which is
            # exactly the request that was joined while it waited), and each of
            # those requests is told now that its admission was not witnessed.
            # The record is a second authority, read by `_claim_admission` too.
            self._shared_operations.update(self._in_flight_operations)
            for carried in self._in_flight_operations.values():
                for record in carried:
                    if "admitted_after_sec" not in record:
                        record["admission_witnessed"] = False
            self._in_flight_operations.clear()
            conns = list(self._in_flight)
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._unix_socket_identity is not None:
            path, device, inode, created = self._unix_socket_identity
            try:
                info = path.lstat()
                if (info.st_dev, info.st_ino, info.st_ctime_ns) == (device, inode, created) and stat.S_ISSOCK(info.st_mode):
                    path.unlink()
            except FileNotFoundError:
                pass
            self._unix_socket_identity = None
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

    def _enter_operation(self, operation: str, record: dict) -> bool:
        """Count this request in for `operation`; True if another one is in flight.

        From here until this request's `_leave_operation` no owner row for
        `operation` can be attributed to a single request, so the flag stays
        set for the whole overlap: a request that joined, or that was joined
        while it waited, never claims a row as its own admission. The record is
        listed so `stop()` can still name every request carrying the id.
        """
        with self._lock:
            carried = self._in_flight_operations.setdefault(operation, [])
            joined = bool(carried)
            carried.append(record)
            if joined:
                self._shared_operations.add(operation)
            return joined

    def _leave_operation(self, operation: str, record: dict) -> None:
        """Drop this request from the id, so a later one is not its duplicate."""
        with self._lock:
            carried = self._in_flight_operations.get(operation) or []
            if record in carried:
                carried.remove(record)
            if carried:
                self._in_flight_operations[operation] = carried
                return
            self._in_flight_operations.pop(operation, None)
            if not self._stopping:
                # `stop()` cleared the counts and kept the marks on purpose: a
                # request leaving afterwards must not clear what it may not claim.
                self._shared_operations.discard(operation)

    def _operation_shared(self, operation: str) -> bool:
        """True while two requests carrying `operation` are both in flight."""
        with self._lock:
            return operation in self._shared_operations

    def _claim_admission(self, operation: str, record: dict, started: float) -> bool:
        """Write the admission time iff this request is alone in claiming it.

        An owner row carries no per-request nonce, so a row that appears while
        another request carries the same `operation_id` is not evidence about
        THIS request: the claim is refused and the record says
        `admission_witnessed: false`. Both answers are written under one lock
        hold, against the requests in flight at that instant, so a row and a
        duplicate arriving together cannot slip between the check and the
        write. True means the caller sends the one admission interim response
        and stops watching; False means it keeps the ordinary keepalive.
        """
        with self._lock:
            if (self._stopping
                    or operation in self._shared_operations
                    or record.get("admission_witnessed") is False):
                # Three authorities, and any one of them refuses the claim. A
                # stopped relay witnesses nothing: a request still in flight when
                # `stop()` ran can never credit itself with a row. The record is
                # the second authority -- `stop()` writes the refusal into it, so
                # a request already named as never-witnessed cannot claim
                # afterwards in any case -- and the shared mark itself is kept by
                # `stop()` for every id still in flight.
                record["admission_witnessed"] = False
                return False
            record["admitted_after_sec"] = round(time.monotonic() - started, 3)
            return True

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
                # A missing key and an explicit JSON null both mean the gate
                # sent no bound of its own. The harness reads a null wait as
                # "queue forever", so forwarding one would drop the bound this
                # relay exists to impose; a number (0 included) is kept as sent.
                if payload.get("render_wait_timeout_sec") is None:
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
        #
        # An operation_id is counted from arrival to answer, and an owner row
        # names no request (the harness mints `owner_id` itself). A request
        # that arrives while another one carries the same id -- or that is
        # still waiting when another one arrives -- therefore cannot claim a
        # new row for it: it keeps the ordinary keepalive and its record says
        # so (module docstring).
        shared = (watched_operation is not None
                  and self._enter_operation(watched_operation, record))
        if shared:
            record["admission_witnessed"] = False
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
            if watch is not None:
                # From here only the final answer is written to the gate. The
                # watch is stopped while this request still counts as in
                # flight, so a claim it has not made by now is never made, and
                # the record it was given is never contradicted afterwards.
                watch.stop()
            with self._lock:
                self._in_flight.discard(conn)
            if watched_operation is not None:
                # Read the overlap BEFORE leaving it: a request that was joined
                # while it waited still has to name what it could not claim,
                # even when its answer lands between two polls.
                shared = shared or self._operation_shared(watched_operation)
                self._leave_operation(watched_operation, record)
            conn.close()
        done["elapsed_sec"] = round(time.monotonic() - started, 3)
        if watch is not None:
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
            if shared and "admitted_after_sec" not in record:
                # The id was shared while this request waited and no admission
                # was ever attributable to it: the record says so, so the
                # gate's own timeout is never counted from another request's
                # row (module docstring).
                done["admission_witnessed"] = False
            record.update(done)


class _AdmissionWatch:
    """Keeps a queued gate's socket alive until the harness admits its request.

    Polls the harness's owner table; a NEW owner row for the request's
    `operation_id` (one that was not there before the request was sent) is its
    admission, but only while this request is the ONLY one carrying that id.
    While another request on this relay carries it too, the row names no
    request (the harness mints `owner_id` itself), so it is attributed to
    neither: the watch keeps the ordinary keepalive running instead of ending
    it, and the record says `admission_witnessed: false` rather than claiming
    someone else's row as this request's admission. Until an admission is
    witnessed an interim `100 Continue` goes to the gate every `keepalive_sec`;
    at admission one more goes, and then none. Writes to the gate happen under
    `write_lock`, and none after `stop()`, so an interim response can never
    interleave with the final one.
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
        decided = False
        while not self._stopped.wait(self._poll):
            owners = self._relay._owner_ids(self._operation)
            if owners is None:
                # Admission can no longer be seen: stop, so the gate's own
                # timeout is never suspended past it.
                with self._relay._lock:
                    self._record["admission_observed"] = False
                return
            if owners - self._before and not decided:
                # A row appeared; whether it may be read as THIS request's
                # admission is decided under the relay's lock, against the
                # requests it is carrying right now.
                decided = True
                if self._relay._claim_admission(self._operation, self._record,
                                                self._started):
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
    this run waited for in the queue, and `render_owner_wait_sec` is the wait
    they cost -- read from the success path's `render_owner_*` names and from
    the queue refusal's own `waited_*` names, so a request REFUSED after
    queueing contributes what it waited instead of nothing.
    """
    ordered = sorted(records, key=lambda r: r.get("seq", 0))
    queued_behind: list = []
    waited = 0.0
    for r in ordered:
        owners, seconds = _recorded_render_wait(r)
        for owner in owners:
            if owner not in queued_behind:
                queued_behind.append(owner)
        waited += seconds
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
