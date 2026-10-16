"""Standalone loud fake of the pipeline-versioning contract (statement phase).

Pure stdlib, no YAML, no filesystem, no registry, no model service. It states
the contract in executable form; it does NOT establish real host behaviour and
is not a substitute for the real provider. See README.md for the semantics.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
from typing import Any, Dict, List, Mapping, Optional

SCHEMA = "rsi.pipeline_version.v1"
_VERSION_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_UNSET = object()


class PipelineVersionError(Exception):
    """Base of every loud refusal. `code` is the stable, vector-visible name."""

    code = "PIPELINE_VERSION_ERROR"


def _err(code: str) -> type:
    return type(code.title().replace("_", ""), (PipelineVersionError,), {"code": code})


MalformedBundle = _err("MALFORMED_BUNDLE")
MalformedVersionId = _err("MALFORMED_VERSION_ID")
UnknownVersion = _err("UNKNOWN_VERSION")
TamperedVersion = _err("TAMPERED_VERSION")
ToolUnavailable = _err("TOOL_UNAVAILABLE")
ToolIdentityMismatch = _err("TOOL_IDENTITY_MISMATCH")
Conflict = _err("CONFLICT")
NoCurrentVersion = _err("NO_CURRENT_VERSION")
UnknownRun = _err("UNKNOWN_RUN")
DuplicateRun = _err("DUPLICATE_RUN")

ERROR_CODES = (
    "MALFORMED_BUNDLE", "MALFORMED_VERSION_ID", "UNKNOWN_VERSION",
    "TAMPERED_VERSION", "TOOL_UNAVAILABLE", "TOOL_IDENTITY_MISMATCH",
    "CONFLICT", "NO_CURRENT_VERSION", "UNKNOWN_RUN", "DUPLICATE_RUN",
)


def _text_bytes(value: Any, path: str) -> bytes:
    if not isinstance(value, str):
        raise MalformedBundle(f"{path}: expected string")
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise MalformedBundle(f"{path}: not valid UTF-8 text ({exc})")


def _check_json(value: Any, path: str) -> None:
    """Only null/bool/int/str/list/str-keyed dict. Floats and tuples refused."""
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, str):
        _text_bytes(value, path)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _check_json(item, f"{path}[{i}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            _text_bytes(key, f"{path}.<key>")
            _check_json(item, f"{path}.{key}")
    else:
        raise MalformedBundle(f"{path}: unsupported value type {type(value).__name__}")


def _b64(value: Any, path: str) -> str:
    return base64.b64encode(_text_bytes(value, path)).decode("ascii")


def _exact_keys(obj: Any, keys: set, path: str) -> None:
    if not isinstance(obj, dict):
        raise MalformedBundle(f"{path}: expected object")
    if set(obj) != keys:
        raise MalformedBundle(
            f"{path}: keys must be exactly {sorted(keys)}, got {sorted(map(str, obj))}")


def _payload(bundle: Any) -> Dict[str, Any]:
    """Validate a bundle and build the canonical hashing payload."""
    _exact_keys(bundle, {"graph", "roles", "templates"}, "bundle")
    _check_json(bundle["graph"], "graph")
    if not isinstance(bundle["graph"], dict):
        raise MalformedBundle("graph: expected object")
    roles_in, roles = bundle["roles"], {}
    if not isinstance(roles_in, dict):
        raise MalformedBundle("roles: expected object")
    for name, role in roles_in.items():
        path = f"roles.{name}"
        _text_bytes(name, path)
        _exact_keys(role, {"settings", "tools", "prompt"}, path)
        _check_json(role["settings"], f"{path}.settings")
        if not isinstance(role["settings"], dict):
            raise MalformedBundle(f"{path}.settings: expected object")
        if not isinstance(role["tools"], list):
            raise MalformedBundle(f"{path}.tools: expected list")
        tools: Dict[str, str] = {}
        for i, tool in enumerate(role["tools"]):
            _exact_keys(tool, {"name", "identity"}, f"{path}.tools[{i}]")
            for field in ("name", "identity"):
                _text_bytes(tool[field], f"{path}.tools[{i}].{field}")
                if not tool[field]:
                    raise MalformedBundle(f"{path}.tools[{i}].{field}: empty")
            if tool["name"] in tools:
                raise MalformedBundle(f"{path}.tools: duplicate tool {tool['name']!r}")
            tools[tool["name"]] = tool["identity"]
        roles[name] = {
            "prompt_b64": _b64(role["prompt"], f"{path}.prompt"),
            "settings": role["settings"],
            "tools": [{"identity": tools[n], "name": n} for n in sorted(tools)],
        }
    templates_in = bundle["templates"]
    if not isinstance(templates_in, dict):
        raise MalformedBundle("templates: expected object")
    templates = {}
    for name, text in templates_in.items():
        _text_bytes(name, "templates.<key>")
        templates[name] = _b64(text, f"templates.{name}")
    return {"graph": bundle["graph"], "roles": roles, "schema": SCHEMA,
            "templates": templates}


def canonical_bytes(bundle: Any) -> bytes:
    """Canonical JSON: sorted keys, no whitespace, ASCII-escaped, UTF-8 bytes."""
    text = json.dumps(_payload(bundle), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)
    return text.encode("ascii")


def version_id_of(bundle: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(bundle)).hexdigest()


def _check_id(version_id: Any) -> str:
    if not isinstance(version_id, str) or not _VERSION_ID_RE.match(version_id):
        raise MalformedVersionId(f"not a version id: {version_id!r}")
    return version_id


class FakePipelineVersionStore:
    """In-memory history + current selection + run pins.

    The store owns history; callers own every object they pass in (copied on
    entry) and every object returned (copied on exit). There is no delete or
    overwrite API. Every refusal happens before any state change.
    """

    capabilities = frozenset({"tamper_hook"})

    def __init__(self, available_tools: Mapping[str, str]):
        self._tools: Dict[str, str] = {}
        self.set_available_tools(available_tools)
        self._versions: Dict[str, Dict[str, Any]] = {}
        self._order: List[str] = []
        self._current: Optional[str] = None
        self._pins: Dict[str, Dict[str, str]] = {}

    # -- pure helpers (exposed so conformance can compare them) -----------
    def canonical_form(self, bundle: Any) -> bytes:
        return canonical_bytes(bundle)

    def version_id_of(self, bundle: Any) -> str:
        return version_id_of(bundle)

    # -- tool identity registry (stand-in for the host's tool inventory) --
    def set_available_tools(self, tools: Mapping[str, str]) -> None:
        if not isinstance(tools, Mapping) or not all(
                isinstance(k, str) and k and isinstance(v, str) and v
                for k, v in tools.items()):
            raise MalformedBundle("available tools: expected {name: identity}")
        self._tools = dict(tools)

    def _require_tools(self, bundle: Mapping[str, Any]) -> None:
        for role_name in sorted(bundle["roles"]):
            for tool in sorted(bundle["roles"][role_name]["tools"],
                               key=lambda t: t["name"]):
                have = self._tools.get(tool["name"])
                if have is None:
                    raise ToolUnavailable(
                        f"role {role_name!r}: tool {tool['name']!r} is not available")
                if have != tool["identity"]:
                    raise ToolIdentityMismatch(
                        f"role {role_name!r}: tool {tool['name']!r} identity "
                        f"{tool['identity']!r} != available {have!r}")

    def _check_expected(self, expected: Any) -> None:
        if expected is _UNSET:
            return
        if expected is not None:
            _check_id(expected)
        if expected != self._current:
            raise Conflict(f"expected current {expected!r}, actual {self._current!r}")

    def _load(self, version_id: Any) -> Dict[str, Any]:
        _check_id(version_id)
        entry = self._versions.get(version_id)
        if entry is None:
            raise UnknownVersion(f"unknown version {version_id}")
        try:
            actual = version_id_of(entry["bundle"])
        except PipelineVersionError:
            actual = None
        if actual != version_id:
            raise TamperedVersion(f"stored content of {version_id} no longer hashes to it")
        return entry

    # -- version API -------------------------------------------------------
    def capture(self, bundle: Any, *, expected_current: Any = _UNSET) -> Dict[str, Any]:
        """Order: validate bundle, expected_current, tools, tamper; then commit."""
        vid = version_id_of(bundle)
        self._check_expected(expected_current)
        self._require_tools(bundle)
        created = vid not in self._versions
        if not created:
            self._load(vid)
        previous = self._current
        if created:
            self._versions[vid] = {"seq": len(self._order) + 1,
                                   "bundle": copy.deepcopy(bundle)}
            self._order.append(vid)
        self._current = vid
        return {"version_id": vid, "created": created,
                "previous_version_id": previous, "current_version_id": vid}

    def list_versions(self) -> List[Dict[str, Any]]:
        return [{"version_id": v, "seq": self._versions[v]["seq"],
                 "is_current": v == self._current} for v in self._order]

    def read(self, version_id: Any) -> Dict[str, Any]:
        entry = self._load(version_id)
        return {"version_id": version_id, "bundle": copy.deepcopy(entry["bundle"])}

    def current_version(self) -> Optional[str]:
        return self._current

    def restore(self, version_id: Any, *, expected_current: Any = _UNSET) -> Dict[str, Any]:
        """Order: id form, unknown, tamper, expected_current, tools; then select."""
        entry = self._load(version_id)
        self._check_expected(expected_current)
        self._require_tools(entry["bundle"])
        previous = self._current
        self._current = version_id  # selection only; history and pins untouched
        return {"version_id": version_id, "previous_version_id": previous,
                "changed": previous != version_id}

    # -- run pins ------------------------------------------------------------
    def pin_run(self, run_id: Any) -> str:
        if not isinstance(run_id, str) or not run_id:
            raise MalformedBundle("run id: expected non-empty string")
        if run_id in self._pins:
            raise DuplicateRun(f"run {run_id!r} already pinned")
        if self._current is None:
            raise NoCurrentVersion("no version selected to pin")
        self._pins[run_id] = {"version_id": self._current, "state": "running"}
        return self._current

    def finish_run(self, run_id: Any) -> None:
        if run_id not in self._pins:
            raise UnknownRun(f"unknown run {run_id!r}")
        self._pins[run_id]["state"] = "finished"  # pin is retained, never removed

    def run_pin(self, run_id: Any) -> str:
        if run_id not in self._pins:
            raise UnknownRun(f"unknown run {run_id!r}")
        return self._pins[run_id]["version_id"]

    def list_pins(self) -> Dict[str, Dict[str, str]]:
        return copy.deepcopy(self._pins)

    # -- optional conformance capability "tamper_hook" ----------------------
    def _tamper_for_test(self, version_id: str, bundle: Any) -> None:
        """Replace stored content without re-keying (simulates corruption)."""
        if version_id not in self._versions:
            raise UnknownVersion(f"unknown version {version_id}")
        self._versions[version_id]["bundle"] = copy.deepcopy(bundle)
