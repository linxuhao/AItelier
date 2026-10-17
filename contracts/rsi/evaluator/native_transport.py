"""Deterministic transport FIXTURE standing in for the model provider.

Source evidence only -- never a model, provider, billing or availability
result. It is the inner httpx transport under the production
``core.rsi_admission.AdmissionTransport``: every request it sees was built by
the real gateway, litellm and openai SDK and admitted by the real admission
boundary. It answers with OpenAI-compatible SSE scripted per scenario, deriving
its "answer" from the PUBLIC package only (it is never given private material),
and appends every received request to a durable JSONL log so observations read
what actually reached the provider side.
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

import httpx

_CASE = re.compile(r"PUBLIC_(TRAIN|HOLDOUT)_(\d\d)")


class _Body(httpx.SyncByteStream):
    """A response body delivered part by part, like a real streamed transport."""

    def __init__(self, parts):
        self._parts = list(parts)

    def __iter__(self):
        yield from self._parts


def _sse(chunks) -> _Body:
    return _Body([b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks]
                 + [b"data: [DONE]\n\n"])


def _response(status, headers, body) -> httpx.Response:
    stream = body if isinstance(body, _Body) else _Body([body])
    return httpx.Response(status, headers=headers, stream=stream)


class DeterministicProvider(httpx.BaseTransport):
    evidence = "deterministic-transport-fixture"

    def __init__(self, log_path: Path, public: dict):
        self.log_path = Path(log_path)
        self.public = public
        self.script = {}            # fault name -> behaviour, see respond()
        self.fault = "none"
        self.hooks = {}             # fault -> callable(request_record) side effect at first send
        self.first_cell = None      # (case_id, arm) of plan cell 0: where faults apply
        self.tenacity_failures = 1
        self.tenacity_status = 429
        self.release = threading.Event()
        self.blocked = threading.Event()
        self._lock = threading.Lock()
        self._per_cell: dict = {}
        self._inflight = 0
        self._idle = threading.Condition(self._lock)

    # -- durable provider-side facts --------------------------------------
    def _log(self, record: dict) -> int:
        with self._lock:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, sort_keys=True) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            return record["seq"]

    def received(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    # -- request parsing (public facts only) ------------------------------
    @staticmethod
    def _text(body: dict) -> tuple[str, str]:
        system = "".join(str(m.get("content") or "") for m in body.get("messages") or []
                         if m.get("role") == "system")
        rest = "".join(str(m.get("content") or "") for m in body.get("messages") or []
                       if m.get("role") != "system")
        return system, rest

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self._inflight += 1
        try:
            return self._handle(request)
        finally:
            with self._lock:
                self._inflight -= 1
                self._idle.notify_all()

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        system, rest = self._text(body)
        match = _CASE.search(rest)
        case = f"{match.group(1).lower()}-{match.group(2)}" if match else None
        arm = "candidate" if "CANDIDATE fixture instruction variant." in system else "reference"
        with self._lock:
            seq = len(self.received()) + 1
            n = self._per_cell.get((case, arm), 0) + 1
            self._per_cell[(case, arm)] = n
        record = {"seq": seq, "url": str(request.url), "case": case, "arm": arm,
                  "n_for_case": n, "fault": self.fault, "body": body,
                  "authorization_present": "authorization" in request.headers}
        self._log(record)
        return self.respond(record, body)

    # -- responses ----------------------------------------------------------
    def _answer(self, case: str, arm: str) -> str:
        files = (self.public.get(case) or {}).get("files") or {}
        name, text = next(iter(sorted(files.items())))
        value = int(text.split("=")[-1].strip())
        if arm == "candidate":
            return (f"# Findings\n\n{name} exports `{value}`: the line "
                    f"`{text.strip()}` defines it.\n")
        return f"# Findings\n\nThe constant is {value}.\n"

    def _usage(self, body, completion=40):
        return {"prompt_tokens": max(1, len(json.dumps(body)) // 4),
                "completion_tokens": completion, "total_tokens": 0}

    def _stream(self, body, *, tool_calls=None, content=None, usage=True, completion=40,
                finish="tool_calls", empty_choices=False):
        base = {"id": "chatcmpl-fixture", "object": "chat.completion.chunk", "created": 0,
                "model": body.get("model")}
        chunks = []
        if empty_choices:
            chunks.append({**base, "choices": [], "usage": self._usage(body, completion)})
            return _response(200, {"content-type": "text/event-stream"}, _sse(chunks))
        delta = {"role": "assistant"}
        if content is not None:
            delta["content"] = content
        if tool_calls:
            delta["tool_calls"] = [{"index": i, "id": f"call_{i}", "type": "function",
                                    "function": {"name": name, "arguments": json.dumps(args)}}
                                   for i, (name, args) in enumerate(tool_calls)]
        chunks.append({**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]})
        chunks.append({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
        if usage:
            chunks.append({**base, "choices": [], "usage": self._usage(body, completion)})
        return _response(200, {"content-type": "text/event-stream"}, _sse(chunks))

    def _deliver(self, body, case, arm, *, usage=True):
        return self._stream(body, tool_calls=[("write_findings", {"content": self._answer(case, arm)}),
                                              ("finish_step", {"summary": "findings written"})],
                            usage=usage)

    def respond(self, record: dict, body: dict) -> httpx.Response:
        fault, case, arm, n = self.fault, record["case"], record["arm"], record["n_for_case"]
        target = fault != "none" and self.first_cell is not None and (case, arm) == self.first_cell
        if fault in self.hooks and target and n == 1:
            self.hooks[fault](record)
        if not target or case is None:
            return self._deliver(body, case, arm)
        if fault == "task_failure" and n == 1:
            return self._stream(body, tool_calls=[("finish_step", {"summary": "no findings"})])
        if fault == "unknown_usage" and n == 1:
            return self._deliver(body, case, arm, usage=False)
        if fault == "unknown_execution" and n == 1:
            self.blocked.set()
            self.release.wait(timeout=180)
            return self._deliver(body, case, arm)
        if fault == "envelope" and n == 1:
            return self._stream(body, content="Thinking about the repository first.",
                                finish="stop", completion=8000)
        if fault == "runtime_midcell" and n == 1:
            return self._stream(body, content="Let me plan before writing.", finish="stop")
        if fault in ("fallback", "endpoint_fallback") and n == 1:
            return _response(503, {"content-type": "application/json", "x-should-retry": "false"},
                             json.dumps({"error": {"message": "fixture endpoint unavailable",
                                                   "type": "server_error"}}).encode())
        if fault == "repair" and n <= 2:
            return self._stream(body, content=f"Draft {n}: the answer is in sample.txt.",
                                finish="stop")
        if fault == "retry" and n <= 2:
            # A request-shaped provider rejection: no failover and no gateway
            # retry; the engine re-asks the model in its next attempt.
            return _response(400, {"content-type": "application/json"},
                             json.dumps({"error": {"message": "fixture request rejected",
                                                   "type": "invalid_request_error"}}).encode())
        if fault == "tenacity" and n <= self.tenacity_failures:
            # Endpoint failure the SDK does not retry: the gateway's own retry
            # decorator (tenacity on generate_native) re-sends the turn.
            status, kind = (429, "rate_limit_error") if self.tenacity_status == 429 else (503, "server_error")
            return _response(status, {"content-type": "application/json", "x-should-retry": "false"},
                             json.dumps({"error": {"message": "fixture endpoint busy",
                                                   "type": kind}}).encode())
        if fault == "transport_retry" and n <= 2:
            return _response(500, {"content-type": "application/json"},
                             json.dumps({"error": {"message": "fixture transient failure",
                                                   "type": "server_error"}}).encode())
        return self._deliver(body, case, arm)

    def close(self, timeout: float = 60.0) -> bool:
        """Release any held call and wait until no request is inside the
        provider, so a world is never torn down under a late reply."""
        self.release.set()
        with self._lock:
            return self._idle.wait_for(lambda: self._inflight == 0, timeout=timeout)
