"""Physical model-request admission for RSI evaluation cells.

The evaluator coordinator owns one ``PhysicalBudget`` per evaluation. Each cell
run gets a ``CellScope``; the host binds it to the run (``bind_run``) and the
ordinary runner -> PipelineEngine -> AgentFactory -> AIGateway path picks it up
by run id. The gateway hands litellm an OpenAI client whose httpx transport is
``AdmissionTransport``: EVERY HTTP request that actually leaves the process
crosses ``handle_request`` -- the first send of a turn, the openai SDK's own
internal retries, litellm's re-sends, gateway fallbacks and engine repairs
alike -- so no retry layer above it can send without a reservation. Guessing a
``max_retries`` keyword is not relied on; the SDK's retries stay enabled and
each one is admitted.

Admission happens after the effective request bytes exist: the observed URL,
model and generation parameters are compared with the frozen profile, and a
conservative upper bound (request bytes + declared overhead input tokens, the
request's own ``max_tokens`` output, frozen prices, no cached discount) is
reserved atomically against cell, round and campaign envelopes and elapsed
deadlines. Denial raises ``AdmissionDenied`` -- an ``openai.OpenAIError``, which
the openai SDK propagates without retrying -- and the scope keeps the typed
denial so the gateway and engine re-raise it through every repair/failover
handler (litellm re-maps exception classes on the way up).

Settlement is read from the wire: a completed stream's usage settles known; an
error status with a complete body settles by the frozen pricing rule; a
connection that never opened is positive no-send; anything else -- missing
usage, early close, an abandoned call still blocked in the transport -- keeps
its full reservation, closes the budget to new dispatch and is reported
unknown, never zero. There is no ledger beyond this in-process object: facts go
to the run's own SkillFlow trace and into the immutable evaluation receipts.
"""
from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx
import openai

from contracts.rsi.evaluator.contract import Amount, PhysicalAttempt, Runtime, digest
from contracts.rsi.evaluator.providers import PhysicalRequest, Reservation

ZERO = Amount(requests=0, input_tokens=0, output_tokens=0, usd_micros=0)
_FIELDS = tuple(Amount.model_fields)
# Body keys the gateway's native/JSON request may carry. Anything else is a
# generation parameter the frozen profile does not name: a hidden repair.
_KNOWN_BODY_KEYS = frozenset({
    "messages", "model", "max_tokens", "stream", "stream_options", "tools",
    "tool_choice", "temperature", "thinking", "reasoning_effort",
})
_MAX_OBSERVED_BODY = 64 * 1024 * 1024


class AdmissionDenied(openai.OpenAIError):
    """Typed, nonretryable refusal of one physical request.

    Subclassing ``openai.OpenAIError`` is what stops the openai SDK's retry
    loop from re-sending (it re-raises OpenAIErrors as-is); every other layer
    re-raises the scope's recorded denial instead of trusting the class that
    reached it."""

    retryable = False

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


def is_admission_denial(error: BaseException | None) -> bool:
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, AdmissionDenied):
            return True
        error = error.__cause__ or error.__context__
    return False


def cell_scope(cell) -> str:
    """Budget scope name of one planned cell."""
    return f"cell.{cell.case_id}.{cell.arm}.{cell.repetition}"


def _add(a: Amount, b: Amount, sign: int = 1) -> Amount:
    return Amount(**{k: getattr(a, k) + sign * getattr(b, k) for k in _FIELDS})


def _fits(used: Amount, extra: Amount, limit: Amount) -> bool:
    return all(getattr(used, k) + getattr(extra, k) <= getattr(limit, k) for k in _FIELDS)


@dataclass
class _Scope:
    upper: Amount
    elapsed_ms: int
    used: Amount = ZERO


class PhysicalBudget:
    """Atomic conservative reservations across named scopes (``PhysicalAdmission``).

    One lock covers check-and-apply over every scope a request names, so two
    contenders at an exact boundary cannot both be admitted and a refused
    request changes nothing. Unknown settlement retains its reservation and
    closes the whole budget: no further dispatch until a new evaluation."""

    def __init__(self, scopes: dict[str, tuple[Amount, int]], *, clock=time.monotonic):
        self._lock = threading.Lock()
        self._scopes = {name: _Scope(upper, ms) for name, (upper, ms) in scopes.items()}
        self._clock = clock
        self._closed: str | None = None
        self._live: dict[str, Reservation] = {}
        self._facts: list[dict] = []
        self._seq = 0

    # -- scope management (coordinator only) ------------------------------
    def add_scope(self, name: str, upper: Amount, elapsed_ms: int) -> None:
        with self._lock:
            if name in self._scopes:
                raise ValueError(f"budget scope {name!r} already exists")
            self._scopes[name] = _Scope(upper, elapsed_ms)

    @property
    def closed(self) -> str | None:
        return self._closed

    def close(self, reason: str) -> None:
        with self._lock:
            self._closed = self._closed or reason

    def used(self, name: str) -> Amount:
        with self._lock:
            return self._scopes[name].used

    def live(self) -> list[Reservation]:
        with self._lock:
            return list(self._live.values())

    def facts(self) -> list[dict]:
        with self._lock:
            return [dict(f) for f in self._facts]

    def _fact(self, kind: str, **payload) -> dict:
        self._seq += 1
        fact = {"seq": self._seq, "kind": kind, **payload}
        self._facts.append(fact)
        return fact

    # -- PhysicalAdmission ------------------------------------------------
    def reserve(self, request: PhysicalRequest, scopes: tuple[str, ...] | None = None,
                *, cell_key: str | None = None) -> Reservation:
        """Reserve ``request.upper`` on every scope at once, or nothing.

        ``scopes`` defaults to the request's own cell, round and campaign."""
        names = tuple(scopes) if scopes is not None else (
            cell_scope(request.cell), "round", "campaign")
        elapsed = {"cell": request.elapsed_cell_ms, "round": request.elapsed_round_ms,
                   "campaign": request.elapsed_campaign_ms}
        with self._lock:
            if self._closed:
                self._fact("denied", descriptor=request.descriptor_id, reason=self._closed)
                raise AdmissionDenied(self._closed, "budget closed to new dispatch")
            if cell_key is not None and any(r.descriptor.cell == request.cell
                                            and r.scopes and r.scopes[0] == cell_key
                                            for r in self._live.values()):
                # A previous physical attempt of this cell has not settled: its
                # call was abandoned while the transport still holds it.
                self._closed = "UNKNOWN_EXECUTION"
                self._fact("denied", descriptor=request.descriptor_id,
                           reason="UNKNOWN_EXECUTION", detail="unsettled prior attempt")
                raise AdmissionDenied("UNKNOWN_EXECUTION",
                                      "a prior physical attempt of this cell is unsettled")
            missing = [n for n in names if n not in self._scopes]
            if missing or not names:
                self._fact("denied", descriptor=request.descriptor_id, reason="BUDGET_EXHAUSTED",
                           detail=f"no envelope {missing}")
                raise AdmissionDenied("BUDGET_EXHAUSTED", f"no authorized envelope {missing}")
            for n in names:
                scope = self._scopes[n]
                kind = n.split(".", 1)[0]
                ms = elapsed.get(kind, elapsed["cell"])
                if ms >= scope.elapsed_ms or not _fits(scope.used, request.upper, scope.upper):
                    why = "deadline" if ms >= scope.elapsed_ms else "envelope"
                    self._fact("denied", descriptor=request.descriptor_id,
                               reason="BUDGET_EXHAUSTED", scope=n, detail=why)
                    raise AdmissionDenied("BUDGET_EXHAUSTED", f"{why} of scope {n}")
            for n in names:
                self._scopes[n].used = _add(self._scopes[n].used, request.upper)
            reservation = Reservation(reservation_id=request.descriptor_id,
                                      descriptor=request, scopes=names)
            self._live[reservation.reservation_id] = reservation
            self._fact("reserved", descriptor=request.descriptor_id, scopes=list(names),
                       upper=request.upper.model_dump(), cause=request.cause)
            return reservation

    def settle(self, reservation: Reservation, observation: PhysicalAttempt) -> None:
        with self._lock:
            if self._live.pop(reservation.reservation_id, None) is None:
                # Late facts about an attempt already settled (or retained as
                # unknown) are appended, never applied a second time.
                self._fact("late", descriptor=reservation.reservation_id,
                           settlement=observation.settlement)
                return
            if observation.settlement == "unknown":
                self._closed = self._closed or (
                    "UNKNOWN_ACCOUNTING" if observation.observed_send else "UNKNOWN_EXECUTION")
                self._fact("unknown", descriptor=reservation.reservation_id,
                           retained=reservation.descriptor.upper.model_dump())
                return
            measured = observation.measured or ZERO
            for n in reservation.scopes:
                scope = self._scopes[n]
                scope.used = _add(_add(scope.used, reservation.descriptor.upper, -1), measured)
            self._fact("settled", descriptor=reservation.reservation_id,
                       settlement=observation.settlement, measured=measured.model_dump())


# ── Frozen pricing / profile projection ────────────────────────────────────

@dataclass(frozen=True)
class Pricing:
    """Frozen conservative price schedule (Source configuration, not live prices)."""
    document: dict

    @property
    def identity(self) -> str:
        return digest(self.document)

    def rates(self, endpoint_key: str) -> tuple[int, int]:
        rate = (self.document.get("endpoints") or {}).get(endpoint_key)
        if not isinstance(rate, dict):
            raise AdmissionDenied("BUDGET_EXHAUSTED", f"no frozen price for {endpoint_key}")
        return int(rate["input"]), int(rate["output"])

    def upper(self, endpoint_key: str, request_bytes: int, max_tokens: int) -> Amount:
        price_in, price_out = self.rates(endpoint_key)
        tokens_in = request_bytes + int(self.document["input_overhead_tokens"])
        usd = math.ceil((tokens_in * price_in + max_tokens * price_out) / 1_000_000)
        return Amount(requests=1, input_tokens=tokens_in, output_tokens=max_tokens, usd_micros=usd)

    def measured(self, endpoint_key: str, prompt: int, completion: int) -> Amount:
        price_in, price_out = self.rates(endpoint_key)
        usd = math.ceil((prompt * price_in + completion * price_out) / 1_000_000)
        return Amount(requests=1, input_tokens=prompt, output_tokens=completion, usd_micros=usd)

    @property
    def error_status_billable(self) -> bool:
        return self.document.get("error_status_billable") is not False


def expected_wire(runtime: Runtime, base_url: str) -> dict:
    """The exact request projection the frozen profile permits.

    With thinking enabled the gateway sends no temperature (it is popped), so
    the projection says so instead of normalising the observed request."""
    thinking = runtime.effort != "disabled"
    return {
        "url": base_url.rstrip("/") + "/chat/completions",
        "model": runtime.endpoint.model,
        "max_tokens": runtime.max_output_tokens,
        "stream": True,
        "thinking": {"type": "enabled"} if thinking else {"type": "disabled"},
        "reasoning_effort": runtime.effort if thinking else None,
        "temperature": None if thinking else runtime.temperature_micros / 1_000_000,
        "unknown_keys": [],
    }


def observe_wire(request: httpx.Request) -> tuple[dict, bytes]:
    """Effective identity of one outgoing request, from its actual bytes.

    Header values (credentials) are never read or recorded."""
    content = request.content
    try:
        body = json.loads(content.decode("utf-8")) if content else {}
    except (UnicodeDecodeError, ValueError):
        body = {}
    if not isinstance(body, dict):
        body = {}
    url = request.url
    return ({
        "url": f"{url.scheme}://{url.host}{':' + str(url.port) if url.port else ''}{url.path}",
        "model": body.get("model"),
        "max_tokens": body.get("max_tokens"),
        "stream": body.get("stream"),
        "thinking": body.get("thinking"),
        "reasoning_effort": body.get("reasoning_effort"),
        "temperature": body.get("temperature"),
        "unknown_keys": sorted(set(body) - _KNOWN_BODY_KEYS),
        "tools": len(body.get("tools") or []),
        "messages": len(body.get("messages") or []),
    }, content)


# ── Per-cell scope bound to one evaluator-owned run ────────────────────────

@dataclass
class CellScope:
    """Trusted per-cell admission binding. Never built from request JSON."""
    budget: PhysicalBudget
    cell: Any                      # contract Cell
    cell_key: str                  # budget scope name of this cell
    scopes: tuple[str, ...]        # (cell_key, round, campaign)
    runtime: Runtime
    endpoints: dict                # nonsecret endpoint label -> base_url
    pricing: Pricing
    evaluation_id: str
    inner: Callable[[], httpx.BaseTransport]
    started: dict                  # {"cell": t, "round": t, "campaign": t} monotonic
    clock: Callable[[], float] = time.monotonic
    trace: Callable[[str, dict], None] | None = None
    attempts: list = field(default_factory=list)       # settled PhysicalAttempt
    pending: dict = field(default_factory=dict)        # reservation_id -> Reservation
    denials: list = field(default_factory=list)
    providers_path: str | None = None  # frozen provider registry for gateways
    routes_path: str | None = None     # frozen route table for gateways
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _clients: dict = field(default_factory=dict)
    _call_cause: str = "initial"
    _sends_in_call: int = 0
    _seq: int = 0

    # Cause of the next physical send: set by the gateway at each provider call.
    def begin_call(self, cause: str) -> None:
        with self._lock:
            self._call_cause = cause
            self._sends_in_call = 0

    def _next_cause(self) -> str:
        with self._lock:
            cause = self._call_cause if self._sends_in_call == 0 else "transport_retry"
            self._sends_in_call += 1
            return cause

    def denial(self) -> AdmissionDenied | None:
        with self._lock:
            return self.denials[-1] if self.denials else None

    def _record(self, event: str, payload: dict) -> None:
        if self.trace is not None:
            try:
                self.trace(event, payload)
            except Exception:                                    # noqa: BLE001
                pass

    def _endpoint(self, url: str) -> str | None:
        for label, base in self.endpoints.items():
            if url == base.rstrip("/") + "/chat/completions":
                return label
        return None

    def admit(self, request: httpx.Request) -> tuple[Reservation, dict]:
        observed, content = observe_wire(request)
        cause = self._next_cause()
        with self._lock:
            self._seq += 1
            attempt_id = (f"p-{self.cell.case_id}-{self.cell.arm}-"
                          f"{self.cell.repetition}-{self._seq}")
        base = self.endpoints.get(self.runtime.endpoint.endpoint)
        expected = expected_wire(self.runtime, base or "")
        drift = sorted(k for k in expected if observed.get(k) != expected[k])
        if base is None:
            drift = ["endpoint"]
        label = self._endpoint(observed["url"])
        if drift:
            denied = AdmissionDenied("RUNTIME_DRIFT", f"effective request differs in {drift}")
            with self._lock:
                self.denials.append(denied)
            self._record("rsi_physical_denied", {
                "attempt_id": attempt_id, "cause": cause, "reason": "RUNTIME_DRIFT",
                "fields": drift, "observed_endpoint": label,
                "observed": {k: observed.get(k) for k in drift}})
            raise denied
        now = self.clock()
        key = f"{self.runtime.endpoint.provider}/{self.runtime.endpoint.model}@{self.runtime.endpoint.endpoint}"
        try:
            upper = self.pricing.upper(key, len(content), int(observed["max_tokens"]))
            descriptor = PhysicalRequest(
                descriptor_id=attempt_id, evaluation_id=self.evaluation_id, cell=self.cell,
                runtime=self.runtime, cause=cause, upper=upper,
                elapsed_cell_ms=int((now - self.started["cell"]) * 1000),
                elapsed_round_ms=int((now - self.started["round"]) * 1000),
                elapsed_campaign_ms=int((now - self.started["campaign"]) * 1000))
            reservation = self.budget.reserve(descriptor, self.scopes, cell_key=self.cell_key)
        except AdmissionDenied as denied:
            with self._lock:
                self.denials.append(denied)
            self._record("rsi_physical_denied", {"attempt_id": attempt_id, "cause": cause,
                                                 "reason": denied.reason, "detail": denied.detail})
            raise
        with self._lock:
            self.pending[reservation.reservation_id] = reservation
        self._record("rsi_physical_send", {
            "attempt_id": attempt_id, "cause": cause, "endpoint": label,
            "upper": upper.model_dump(),
            # The effective identity this send actually carried (nonsecret).
            "observed": {k: observed.get(k) for k in expected}})
        return reservation, {"key": key, "observed": observed}

    def settle(self, reservation: Reservation, settlement: str, measured: Amount | None,
               observed_send: bool | None, detail: str = "") -> PhysicalAttempt:
        with self._lock:
            if self.pending.pop(reservation.reservation_id, None) is None:
                late = True
            else:
                late = False
        if late:
            # A fact about an attempt already sealed (e.g. an abandoned call
            # that finally returned): appended to the trace, never applied.
            self._record("rsi_physical_late", {"attempt_id": reservation.reservation_id,
                                               "settlement": settlement, "detail": detail})
            return None
        attempt = PhysicalAttempt(
            attempt_id=reservation.reservation_id, cause=reservation.descriptor.cause,
            effective_runtime=self.runtime, reservation=reservation.descriptor.upper,
            settlement=settlement, measured=measured, observed_send=observed_send)
        self.budget.settle(reservation, attempt)
        with self._lock:
            self.attempts.append(attempt)
        self._record("rsi_physical_settled", {
            "attempt_id": attempt.attempt_id, "settlement": settlement, "detail": detail,
            "measured": measured.model_dump() if measured else None})
        return attempt

    def abandon_pending(self, why: str) -> list[PhysicalAttempt]:
        """Seal every still-unsettled attempt as unknown (live/abandoned)."""
        with self._lock:
            live = list(self.pending.values())
        return [a for a in (self.settle(r, "unknown", None, True, why) for r in live) if a]

    def require_admitted_route(self, gateway, kwargs: dict) -> None:
        """Refuse, before litellm sends anything, a call this scope cannot admit.

        Only litellm's OpenAI-compatible path (``openai/<model>``) sends through
        the injected OpenAI client and therefore through ``AdmissionTransport``.
        litellm's native provider handlers (``deepseek/``, ``anthropic/``, ...)
        use their own HTTP clients and would bypass admission entirely: such a
        binding -- typically a failover target -- is refused as runtime drift."""
        with self._lock:
            live = bool(self.pending)
        if live:
            # A previous physical call of this cell has not settled (abandoned
            # by the gateway's wall clock, still held by the transport): no new
            # dispatch of any kind, whatever endpoint the retry would use.
            self.budget.close("UNKNOWN_EXECUTION")
            denied = AdmissionDenied("UNKNOWN_EXECUTION",
                                     "a prior physical attempt of this cell is unsettled")
            with self._lock:
                self.denials.append(denied)
            self._record("rsi_physical_denied", {"attempt_id": None, "cause": self._call_cause,
                                                 "reason": "UNKNOWN_EXECUTION",
                                                 "detail": denied.detail})
            raise denied
        client = kwargs.get("client")
        with self._lock:
            ours = any(client is c for c in self._clients.values())
        model = str(kwargs.get("model") or "")
        if not ours or not model.startswith("openai/"):
            denied = AdmissionDenied(
                "RUNTIME_DRIFT", f"{gateway.active_model} would send through litellm's "
                f"{model.split('/', 1)[0] or 'unknown'} handler, outside physical admission")
            with self._lock:
                self.denials.append(denied)
            self._record("rsi_physical_denied", {
                "attempt_id": None, "cause": self._call_cause, "reason": "RUNTIME_DRIFT",
                "detail": denied.detail, "observed_endpoint": gateway.active_model})
            raise denied

    def openai_client(self, gateway) -> openai.OpenAI:
        """One admitted client per (gateway, bound endpoint): connection reuse
        is kept, and every request it sends still crosses AdmissionTransport."""
        key = (id(gateway), gateway.api_base, bool(gateway.api_key))
        with self._lock:
            client = self._clients.get(key)
            if client is None:
                client = openai.OpenAI(
                    api_key=gateway.api_key or "unset", base_url=gateway.api_base,
                    http_client=httpx.Client(transport=AdmissionTransport(self.inner(), self)))
                self._clients[key] = client
            return client


# ── The physical-send boundary ─────────────────────────────────────────────

def _usage_from_sse(data: bytes) -> tuple[int, int] | None:
    found = None
    for line in data.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            continue
        try:
            chunk = json.loads(payload)
        except ValueError:
            continue
        usage = chunk.get("usage") if isinstance(chunk, dict) else None
        if isinstance(usage, dict) and isinstance(usage.get("prompt_tokens"), int) \
                and isinstance(usage.get("completion_tokens"), int):
            found = (usage["prompt_tokens"], usage["completion_tokens"])
    return found


def _sse_terminated(buf: bytearray) -> bool:
    """True once an event stream carries its terminal ``data: [DONE]`` event."""
    tail = bytes(buf[-64:]).replace(b"\r\n", b"\n")
    return tail.rstrip(b"\n").endswith(b"data: [DONE]") and tail.endswith(b"\n\n")


class _SettlingStream(httpx.SyncByteStream):
    """Pass response bytes through; settle when the body ends or is dropped."""

    def __init__(self, inner, scope: CellScope, reservation: Reservation, key: str,
                 status: int):
        self._inner = inner
        self._scope = scope
        self._reservation = reservation
        self._key = key
        self._status = status
        self._buf = bytearray()
        self._done = False
        self._settled = False

    def __iter__(self):
        try:
            for chunk in self._inner:
                if len(self._buf) < _MAX_OBSERVED_BODY:
                    self._buf.extend(chunk)
                if self._status < 400 and _sse_terminated(self._buf):
                    # The provider declared the event stream complete. A client
                    # stops reading here (the openai SDK neither exhausts nor
                    # closes the body after [DONE]), so settle BEFORE handing the
                    # final bytes up rather than waiting for an EOF that no
                    # consumer will ask for.
                    self._done = True
                    self._settle(True, "")
                yield chunk
        except GeneratorExit:
            # Consumer abandoned the body: complete only if it had already ended.
            self._settle(self._done, "body abandoned before it ended")
            raise
        except BaseException as exc:
            self._settle(False, f"body read failed: {type(exc).__name__}")
            raise
        self._done = True
        self._settle(True, "")

    def close(self) -> None:
        try:
            if hasattr(self._inner, "close"):
                self._inner.close()
        finally:
            if not self._settled:
                self._settle(self._done, "body closed before it ended")

    def _settle(self, complete: bool, why: str) -> None:
        if self._settled:
            return
        self._settled = True
        scope, res = self._scope, self._reservation
        if not complete:
            scope.settle(res, "unknown", None, True, why)
            return
        if self._status >= 400:
            measured = res.descriptor.upper if scope.pricing.error_status_billable else \
                Amount(requests=1, input_tokens=0, output_tokens=0, usd_micros=0)
            scope.settle(res, "known", measured, True, f"error status {self._status}")
            return
        usage = _usage_from_sse(bytes(self._buf))
        if usage is None:
            try:
                body = json.loads(bytes(self._buf).decode("utf-8"))
                u = body.get("usage") if isinstance(body, dict) else None
                if isinstance(u, dict):
                    usage = (int(u["prompt_tokens"]), int(u["completion_tokens"]))
            except (ValueError, KeyError, TypeError):
                usage = None
        if usage is None:
            scope.settle(res, "unknown", None, True, "response carried no usage")
            return
        measured = scope.pricing.measured(self._key, *usage)
        if not all(getattr(measured, k) <= getattr(res.descriptor.upper, k) for k in _FIELDS):
            # The provider reported more than the conservative bound: the bound
            # was wrong, so the accounting is not known. Keep everything.
            scope.settle(res, "unknown", None, True,
                         f"reported usage {usage} exceeds reservation")
            return
        scope.settle(res, "known", measured, True, "")


class AdmissionTransport(httpx.BaseTransport):
    """httpx transport that admits each actual request before sending it."""

    def __init__(self, inner: httpx.BaseTransport, scope: CellScope):
        self._inner = inner
        self._scope = scope

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        reservation, info = self._scope.admit(request)
        try:
            response = self._inner.handle_request(request)
        except httpx.ConnectError:
            # The connection never opened: no request bytes left the process.
            self._scope.settle(reservation, "positive_no_send",
                               Amount(requests=0, input_tokens=0, output_tokens=0, usd_micros=0),
                               False, "connect failed")
            raise
        except BaseException as exc:
            self._scope.settle(reservation, "unknown", None, True,
                               f"transport failed after send: {type(exc).__name__}")
            raise
        stream = _SettlingStream(response.stream, self._scope, reservation,
                                 info["key"], response.status_code)
        if response.status_code >= 400:
            # An error status is the server's complete answer for accounting;
            # the openai SDK closes a retryable error body without reading it.
            stream._done = True
            stream._settle(True, "")
            return response
        try:
            loaded = response.content          # an inner transport that read the body
        except httpx.ResponseNotRead:
            response.stream = stream           # streamed: settle as it is consumed
        else:
            stream._buf.extend(loaded)
            stream._done = True
            stream._settle(True, "")
        return response

    def close(self) -> None:
        self._inner.close()


# ── Run binding (in-process, owned by the coordinator) ─────────────────────

_BINDINGS: dict[str, Any] = {}
_BINDINGS_LOCK = threading.Lock()


def bind_run(run_id: str, binding) -> None:
    with _BINDINGS_LOCK:
        if run_id in _BINDINGS and _BINDINGS[run_id] is not binding:
            raise ValueError(f"run {run_id} already has an evaluator binding")
        _BINDINGS[run_id] = binding


def unbind_run(run_id: str, binding) -> None:
    with _BINDINGS_LOCK:
        if _BINDINGS.get(run_id) is binding:
            del _BINDINGS[run_id]


def binding_for_run(run_id: str):
    with _BINDINGS_LOCK:
        return _BINDINGS.get(run_id)


def scope_for_run(run_id: str) -> CellScope | None:
    binding = binding_for_run(run_id) if run_id else None
    return getattr(binding, "scope", None) if binding is not None else None
