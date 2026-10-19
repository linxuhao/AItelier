"""Private helper for the repeated write-scope-refusal progress contract.

Pure data helpers plus one synchronous per-request closure. Nothing here keeps
a request/batch journal: the only durable objects are the pending cause and the
latest successful-response witness, both stored by the engine in the existing
``write_scope_receipt.json``.
"""
from __future__ import annotations

import hashlib
import json

CARRY_OPEN = "[Host Scope Refusal - mutation NOT performed; correct it]"
CARRY_CLOSE = "[/Host Scope Refusal]"
_CAUSE_KEYS = {"index", "tool", "denied_targets", "authority", "error", "origin", "hash"}
_WITNESS_KEYS = {"cause", "origin", "invocation_id", "presented_sha256",
                 "carry_match", "response_digest", "positions"}


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), default=str)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def canonical_authority(scope) -> dict:
    """Only trusted resolved WriteScope fields; order/duplicates irrelevant."""
    return {
        "policy": scope.policy,
        "task": scope.task,
        "owns": sorted(set(scope.owns)),
        "shared_hotspots": sorted(set(scope.shared_hotspots)),
    }


def denied_targets(paths, scope) -> list[str]:
    from core.write_scope import normalize_path
    found = set()
    for path in paths:
        if scope.authorizes(path):
            continue
        try:
            found.add(normalize_path(path))
        except ValueError:
            found.add(str(path))
    return sorted(found)


def cause_hash(body: dict) -> str:
    return sha256_text(canonical_json(body))


def make_cause(index: int, tool: str, targets: list[str], authority: dict,
               error: dict, origin: dict) -> dict:
    body = {"index": index, "tool": tool, "denied_targets": list(targets),
            "authority": authority, "error": error, "origin": origin}
    return {**body, "hash": cause_hash(body)}


def validate_cause(cause) -> dict:
    if (not isinstance(cause, dict) or set(cause) != _CAUSE_KEYS
            or not isinstance(cause["denied_targets"], list)
            or not isinstance(cause["authority"], dict)
            or not isinstance(cause["error"], dict)
            or not isinstance(cause["origin"], dict)
            or not isinstance(cause["tool"], str)):
        raise ValueError("scope cause is malformed")
    body = {k: v for k, v in cause.items() if k != "hash"}
    if cause_hash(body) != cause["hash"]:
        raise ValueError("scope cause hash mismatch")
    return cause


def validate_witness(witness) -> dict:
    if not isinstance(witness, dict) or set(witness) != _WITNESS_KEYS:
        raise ValueError("scope response witness is malformed")
    if witness["cause"] is not None:
        validate_cause(witness["cause"])
    if not isinstance(witness["origin"], dict):
        raise ValueError("scope response witness origin is malformed")
    return witness




def validate_positions(mode: str, positions) -> None:
    """Validate the actual producer syntax, not model/tool semantic validity.

    JSON action tool values are opaque model JSON (including None); file paths
    are stringified by json_positions and may be empty. Skipped non-dict model
    entries legitimately leave gaps in JSON indices.
    """
    if not isinstance(positions, list) or not positions:
        raise ValueError("scope response witness positions are missing")
    native_batch = None
    last_json_index = {"actions": -1, "files": -1}
    files_started = False
    for i, position in enumerate(positions):
        if not isinstance(position, dict):
            raise ValueError("scope response witness position is malformed")
        if mode == "native":
            if (set(position) != {"segment", "assistant_index", "call_index", "tool"}
                    or any(type(position[k]) is not int or position[k] < 0
                           for k in ("segment", "assistant_index", "call_index"))
                    or position["call_index"] != i
                    or (position["tool"] is not None and not isinstance(position["tool"], str))):
                raise ValueError("native scope witness position is malformed")
            batch = (position["segment"], position["assistant_index"])
            if native_batch is not None and batch != native_batch:
                raise ValueError("native scope witness spans different assistant batches")
            native_batch = batch
        else:
            kind = position.get("kind")
            if (kind not in ("actions", "files")
                    or set(position) != {"kind", "index", "tool" if kind == "actions" else "path"}
                    or type(position["index"]) is not int or position["index"] < 0
                    or position["index"] <= last_json_index[kind]
                    or (kind == "files" and not isinstance(position["path"], str))
                    or (kind == "actions" and files_started)):
                raise ValueError("JSON scope witness position is malformed")
            last_json_index[kind] = position["index"]
            files_started |= kind == "files"


def load_progress(payload, violations, claims):
    """Legacy raw ledgers may migrate; incomplete versioned state may not."""
    fields = {"scope_progress_version", "scope_cause", "response_witness"}
    present = fields & payload.keys()
    if not present:
        if any("scope_position" in error or "repeated_after_feedback" in error
               for error in violations):
            raise ValueError("versioned scope refusal lost its required progress metadata")
        cause = None
        if violations:
            # Preserve the original raw pointer on a trustworthy legacy ledger.
            error = violations[0]
            allowed = error.get("allowed_scope") or {}
            authority = {"policy": error.get("policy", payload.get("policy")),
                         "task": error.get("task"),
                         "owns": sorted(set(allowed.get("owns", []))),
                         "shared_hotspots": sorted(set(allowed.get("shared_hotspots", [])))}
            if not (error.get("scope_violation") is True and isinstance(error.get("tool"), str)
                    and isinstance(error.get("requested_path"), str)):
                raise ValueError("legacy refusal authority is unavailable")
            from core.write_scope import normalize_path
            cause = make_cause(0, error["tool"], [normalize_path(error["requested_path"])],
                               authority, dict(error),
                               {"mode": "legacy", "step_instance_id": error.get("step_instance_id"),
                                "claim_epoch": error.get("claim_epoch")})
        return cause, None
    if present != fields or payload["scope_progress_version"] != 1:
        raise ValueError("required versioned scope progress fields are missing or unsupported")
    cause, witness = payload["scope_cause"], payload["response_witness"]
    def linked(value):
        validate_cause(value)
        i = value["index"]
        if (type(i) is not int or not 0 <= i < len(violations)
                or value["error"] != violations[i]
                or value["authority"].get("task") != payload.get("task")):
            raise ValueError("scope cause does not link to its original item refusal")
    if cause is not None:
        linked(cause)
    elif violations:
        raise ValueError("versioned scope refusal history has no pending cause")
    if witness is not None:
        validate_witness(witness)
        if witness["cause"] is None:
            raise ValueError("scope response witness has no original cause")
        linked(witness["cause"])
        origin = witness["origin"]
        if (set(origin) != {"mode", "step_instance_id", "claim_epoch"}
                or origin["mode"] not in {"native", "json-content", "json-tools"}
                or type(origin["step_instance_id"]) is not int
                or type(origin["claim_epoch"]) is not int
                or {k: origin[k] for k in ("step_instance_id", "claim_epoch")} not in claims
                or witness["carry_match"] is not True
                or not isinstance(witness["invocation_id"], str) or not witness["invocation_id"]
                or not all(isinstance(witness[k], str) and len(witness[k]) == 64
                           and all(c in "0123456789abcdef" for c in witness[k])
                           for k in ("presented_sha256", "response_digest"))):
            raise ValueError("scope response witness provenance is malformed")
        validate_positions(origin["mode"], witness["positions"])
    return cause, witness


def carry_text(cause: dict) -> str:
    """Bounded host-authored block: exact raw error plus compared authority."""
    return "\n".join([
        CARRY_OPEN,
        f"cause: index={cause['index']} hash={cause['hash']}",
        f"error: {cause['error'].get('error', '')}",
        f"denied_tool: {cause['tool']}",
        f"denied_targets: {canonical_json(cause['denied_targets'])}",
        f"authority: {canonical_json(cause['authority'])}",
        "This operation was refused by the host and was not performed. Use a "
        "path inside the allowed scope; repeating the same operation under the "
        "same authority ends the step.",
        CARRY_CLOSE,
    ])


def _content_text(message) -> str:
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(part.get("text", "")) for part in content
                         if isinstance(part, dict))
    return ""


def carry_in_messages(messages, carry: str) -> bool:
    return any(carry in _content_text(m) for m in (messages or []))


def native_response_digest(text, tool_calls) -> str:
    calls = []
    for tc in tool_calls or []:
        fn = (tc or {}).get("function") or {}
        calls.append({"id": (tc or {}).get("id"), "name": fn.get("name"),
                      "arguments": fn.get("arguments")})
    return sha256_text(canonical_json({"text": text or "", "tool_calls": calls}))


def json_positions(payload) -> list[dict]:
    """Deterministic original action/file indices of a parsed JSON response."""
    if isinstance(payload, list):
        payload = {"actions": payload}
    if not isinstance(payload, dict):
        return []
    out: list[dict] = []
    actions = payload.get("actions")
    if isinstance(actions, list):
        for i, action in enumerate(actions):
            if isinstance(action, dict):
                out.append({"kind": "actions", "index": i, "tool": action.get("tool")})
    files = payload.get("files")
    if isinstance(files, dict):
        for i, name in enumerate(files):
            out.append({"kind": "files", "index": i, "path": str(name)})
    elif isinstance(files, list):
        for i, entry in enumerate(files):
            if isinstance(entry, dict):
                out.append({"kind": "files", "index": i,
                            "path": str(entry.get("path") or entry.get("file") or "")})
    return out


def json_response_digest(response: str, positions: list[dict]) -> str:
    return sha256_text(canonical_json({"text": response, "positions": positions}))


class RequestClosure:
    """Per-call synchronous observer pair composed with existing observers.

    It records only successful completed invocations and the final sanitized
    messages the gateway reports after a successful completion. A failure in
    a previously installed observer still propagates as before.
    """

    def __init__(self, gateway, cause: dict):
        self.gateway = gateway
        self.cause = cause
        self.carry = carry_text(cause)
        self.completed: list[str] = []
        self.presented: dict | None = None
        self._prev_present = None
        self._prev_complete = None

    def __enter__(self):
        g = self.gateway
        self._prev_present = getattr(g, "on_messages_presented", None)
        self._prev_complete = getattr(g, "on_completion", None)
        g.on_messages_presented = self._present
        g.on_completion = self._complete
        return self

    def __exit__(self, *exc):
        self.gateway.on_messages_presented = self._prev_present
        self.gateway.on_completion = self._prev_complete
        return False

    def _present(self, messages):
        try:
            self.presented = {
                "sha256": sha256_text(canonical_json(messages)),
                "carry_match": carry_in_messages(messages, self.carry),
                "completed_count": len(self.completed),
            }
        except Exception:
            self.presented = None
        if callable(self._prev_present):
            self._prev_present(messages)

    def _complete(self, event, payload):
        if (event == "completion_finished" and isinstance(payload, dict)
                and payload.get("outcome") == "completed"
                and isinstance(payload.get("invocation_id"), str)):
            self.completed.append(payload["invocation_id"])
        if callable(self._prev_complete):
            self._prev_complete(event, payload)

    def evidence(self) -> dict:
        """Return the paired success observation or raise ValueError(detail)."""
        if not self.completed:
            raise ValueError("no completed model invocation was observed")
        if self.presented is None:
            raise ValueError("completed invocation without a presented-request "
                             "observation; presentation is never inferred")
        if self.presented["completed_count"] != len(self.completed):
            raise ValueError("presented request does not pair with the last "
                             "completed invocation")
        if not self.presented["carry_match"]:
            raise ValueError("final presented request omitted or truncated the "
                             "complete scope-refusal carry block")
        return {"invocation_id": self.completed[-1],
                "presented_sha256": self.presented["sha256"],
                "carry_match": True}
