"""Pure r2 codec, input domain and refusal codes of the pipeline-versioning contract.

Stdlib only: shared by the standalone fake and the native binding, so the
canonical projection has one definition. It parses no YAML, touches no
filesystem/registry and owns no state. See README.md.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

SCHEMA = "rsi.pipeline_version.v2"
SELF = "$self"
VERSION_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")
UNIT_IDENTITY_RE = re.compile(r"[0-9a-f]{64}")
TARGET_RE = re.compile(r"gen_[a-z0-9][a-z0-9_]{0,59}")
SEAL_PREFIX = "gen_rsi_"
PHYSICAL_RE = re.compile(r"gen_rsi_v([0-9a-f]{31})_([0-9a-f]{16})")
ROLE_KEYS = frozenset({"model", "tools", "settings", "prompt"})
CONTENT_KEYS = frozenset({"graph", "roles", "tools"})


class VersioningError(Exception):
    """Every loud refusal. ``code`` is the stable, vector-visible name."""

    code = "VERSIONING_ERROR"


def _err(code: str) -> type:
    return type("".join(w.title() for w in code.split("_")), (VersioningError,),
                {"code": code})


MalformedContent = _err("MALFORMED_CONTENT")
MalformedVersionId = _err("MALFORMED_VERSION_ID")
MalformedTarget = _err("MALFORMED_TARGET")
MalformedExpectedCurrent = _err("MALFORMED_EXPECTED_CURRENT")
UnknownTarget = _err("UNKNOWN_TARGET")
UnknownVersion = _err("UNKNOWN_VERSION")
TamperedVersion = _err("TAMPERED_VERSION")
ProviderUnavailable = _err("PROVIDER_UNAVAILABLE")
ProviderChanged = _err("PROVIDER_CHANGED")
Conflict = _err("CONFLICT")
UnsupportedContent = _err("UNSUPPORTED_CONTENT")
NativeRefused = _err("NATIVE_REFUSED")

ERROR_CODES = (
    "MALFORMED_CONTENT", "MALFORMED_VERSION_ID", "MALFORMED_TARGET",
    "MALFORMED_EXPECTED_CURRENT", "UNKNOWN_TARGET", "UNKNOWN_VERSION",
    "TAMPERED_VERSION", "PROVIDER_UNAVAILABLE", "PROVIDER_CHANGED", "CONFLICT",
    "UNSUPPORTED_CONTENT", "NATIVE_REFUSED",
)


def _text(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise MalformedContent(f"{path}: expected string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise MalformedContent(f"{path}: not valid UTF-8 text ({exc})") from exc
    return value


def check_finite_json(value: Any, path: str = "content") -> None:
    """null, bool, int, FINITE float, UTF-8 str, list, str-keyed dict; nothing else.

    No coercion: 1 and 1.0 and True stay distinct values; NaN/Infinity, tuples,
    sets, bytes and any other object refuse."""
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise MalformedContent(f"{path}: non-finite number {value!r}")
        return
    if isinstance(value, str):
        _text(value, path)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            check_finite_json(item, f"{path}[{i}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            _text(key, f"{path}.<key>")
            check_finite_json(item, f"{path}.{key}")
    else:
        raise MalformedContent(f"{path}: unsupported value type {type(value).__name__}")


def agent_refs(node: Any):
    """Every string ``agent_config`` in a graph, nested loop bodies included."""
    if isinstance(node, dict):
        if isinstance(node.get("agent_config"), str) and node["agent_config"]:
            yield node["agent_config"]
        for value in node.values():
            yield from agent_refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from agent_refs(value)


def check_content(content: Any) -> None:
    """Validate one logical version content (see README 'Logical content')."""
    if not isinstance(content, dict) or set(content) != CONTENT_KEYS:
        raise MalformedContent(f"content: keys must be exactly {sorted(CONTENT_KEYS)}")
    check_finite_json(content, "content")
    graph, roles, tools = content["graph"], content["roles"], content["tools"]
    if not isinstance(graph, dict):
        raise MalformedContent("graph: expected object")
    if not isinstance(roles, dict) or not isinstance(tools, dict):
        raise MalformedContent("roles/tools: expected objects")
    for name, ident in tools.items():
        if not name or not isinstance(ident, dict):
            raise MalformedContent(f"tools.{name}: expected nonempty name -> object identity")
    for name, role in roles.items():
        path = f"roles.{name}"
        if not name or not isinstance(role, dict) or set(role) != ROLE_KEYS:
            raise MalformedContent(f"{path}: keys must be exactly {sorted(ROLE_KEYS)}")
        _text(role["model"], f"{path}.model")
        if not isinstance(role["settings"], dict):
            raise MalformedContent(f"{path}.settings: expected object")
        if role["prompt"] is not None:
            _text(role["prompt"], f"{path}.prompt")
        if not isinstance(role["tools"], list):
            raise MalformedContent(f"{path}.tools: expected list")
        for tool in role["tools"]:
            if not isinstance(tool, str) or tool not in tools:
                raise MalformedContent(f"{path}.tools: {tool!r} has no resolved identity")
    referenced = set(agent_refs(graph))
    if referenced != set(roles):
        raise MalformedContent(
            f"roles {sorted(roles)} are not exactly the graph's agent roles {sorted(referenced)}")


def canonical_bytes(content: Any) -> bytes:
    """The exact canonical projection hashed into a version id."""
    check_content(content)
    payload = {"schema": SCHEMA, **content}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def version_id_of(content: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(content)).hexdigest()


def check_version_id(value: Any) -> str:
    if not isinstance(value, str) or not VERSION_ID_RE.fullmatch(value):
        raise MalformedVersionId(f"not a logical version id: {value!r}")
    return value


def check_target(value: Any) -> str:
    if (not isinstance(value, str) or not TARGET_RE.fullmatch(value)
            or value.startswith(SEAL_PREFIX)):
        raise MalformedTarget(f"not an owned generated target name: {value!r}")
    return value


def check_unit_identity(value: Any) -> str:
    if not isinstance(value, str) or not UNIT_IDENTITY_RE.fullmatch(value):
        raise MalformedExpectedCurrent(f"not a whole-unit identity: {value!r}")
    return value


def seal_slug(version_id: str) -> str:
    """Native seal target slug naming a logical version (``v`` + 31 hex)."""
    return "v" + version_id[len("sha256:"):][:31]


def physical_matches(physical_name: Any, version_id: str) -> bool:
    m = PHYSICAL_RE.fullmatch(physical_name) if isinstance(physical_name, str) else None
    return bool(m) and "v" + m.group(1) == seal_slug(version_id)


def detached(value: Any) -> Any:
    """A deep copy through the JSON domain (results never alias provider state)."""
    if isinstance(value, dict):
        return {k: detached(v) for k, v in value.items()}
    if isinstance(value, list):
        return [detached(v) for v in value]
    return value
