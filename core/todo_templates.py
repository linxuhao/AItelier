"""Plans are expanded from reusable repository templates, never written per task.

A template is a versioned asset in `todo_templates/<name>.yaml` (override the
directory with AITELIER_TODO_TEMPLATES), reviewed like the predicate library it
binds. It declares typed params and items whose predicate args may reference a param
as a WHOLE value (`paths: $paths`) and whose titles may interpolate `${param}`.

* An arg whose whole value is `$param` takes the param's value. If that param is
  optional, has no default, and was not supplied, the arg is left out.
* Titles: `${param}` becomes the value (a list is comma-joined, an unset one is empty).
* A template that is wrong is refused when it is LOADED, not when a plan first
  trips over it: undeclared references, unknown predicates, args the library would
  refuse. Loading proves this by expanding the template with sample values.
* Every template declares a `scope` list[str] param: it is the scope of the
  appended `deliver` item.
* `sha256` is the digest of the template file's bytes; the plan file records it, so
  a plan can always be traced to the exact template it came from.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
from typing import Any, Mapping

from core.todo_plan import Refusal, TodoPlan
from core.todo_runtime import PredicateLibrary

TYPES = ("str", "int", "list[str]")
_NAME = re.compile(r"[a-z][a-z0-9_]*\Z")
_REF = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)\Z")
_TITLE_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_TOP = {"name", "version", "description", "params", "items"}
_PARAM = {"type", "required", "default", "description"}
_SAMPLE = {"str": "x", "int": 1, "list[str]": ["x"]}


def default_dir() -> Path:
    return Path(os.environ.get("AITELIER_TODO_TEMPLATES") or Path(__file__).resolve().parents[1] / "todo_templates")


@dataclass(frozen=True)
class Param:
    type: str
    required: bool
    default: Any = None
    description: str = ""


@dataclass(frozen=True)
class Template:
    name: str
    version: int
    description: str
    params: Mapping[str, Param]
    items: tuple[dict, ...]
    sha256: str

    def as_json(self) -> dict:
        return {"name": self.name, "version": self.version, "description": self.description,
                "sha256": self.sha256, "items": [i["id"] for i in self.items],
                "params": {n: {"type": p.type, "required": p.required, "default": p.default,
                               "description": p.description} for n, p in self.params.items()}}


def _bad(name: str, reason: str) -> Refusal:
    return Refusal("bad_template", f"template {name}: {reason}")


def type_ok(kind: str, value: Any) -> bool:
    if kind == "str":
        return isinstance(value, str)
    if kind == "int":
        return type(value) is int
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def load_template(name: str, library: PredicateLibrary, directory: Path | None = None) -> Template:
    directory = directory or default_dir()
    if not isinstance(name, str) or not _NAME.match(name):
        raise Refusal("unknown_template", f"not a template name: {name!r}")
    path = directory / f"{name}.yaml"
    try:
        raw = path.read_bytes()
    except OSError:
        raise Refusal("unknown_template", f"no template {name!r} in {directory}; "
                                          f"available: {available(directory)}") from None
    import yaml
    try:
        doc = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise _bad(name, f"not valid YAML: {str(exc)[:200]}") from exc
    template = _validate(name, doc, hashlib.sha256(raw).hexdigest())
    # Prove it expands, with sample values, with and without its optional params.
    for supplied in ({n: _SAMPLE[p.type] for n, p in template.params.items()},
                     {n: _SAMPLE[p.type] for n, p in template.params.items() if p.required}):
        resolved = resolve_params(template, supplied)
        try:
            TodoPlan.from_json(plan_data(template, resolved, attempt_id="probe", node_key="probe", context_hash="probe",
                                         base_sha="0" * 40, workspace="/probe", goal="probe"), library)
        except Refusal as refusal:
            if refusal.code == "unknown_predicate":
                raise
            raise _bad(name, f"does not expand to a valid plan: {refusal.reason}") from refusal
    return template


def available(directory: Path | None = None) -> list[str]:
    directory = directory or default_dir()
    return sorted(p.stem for p in directory.glob("*.yaml")) if directory.is_dir() else []


def _validate(name: str, doc: Any, sha256: str) -> Template:
    if not isinstance(doc, dict) or set(doc) != _TOP:
        raise _bad(name, f"keys must be exactly {sorted(_TOP)}")
    if doc["name"] != name:
        raise _bad(name, f"declares name {doc['name']!r}; the file name is the template name")
    if type(doc["version"]) is not int or doc["version"] < 1:
        raise _bad(name, "version must be a positive integer")
    if not isinstance(doc["description"], str) or not doc["description"].strip():
        raise _bad(name, "description must be a non-empty string")
    if not isinstance(doc["params"], dict):
        raise _bad(name, "params must be a mapping")
    params: dict[str, Param] = {}
    for pname, spec in doc["params"].items():
        if not isinstance(pname, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", pname):
            raise _bad(name, f"param name {pname!r} is not an identifier")
        if not isinstance(spec, dict) or set(spec) - _PARAM or spec.get("type") not in TYPES:
            raise _bad(name, f"param {pname}: a mapping with type in {TYPES} (and required/default/description)")
        required = spec.get("required", False)
        if type(required) is not bool:
            raise _bad(name, f"param {pname}: required must be true or false")
        if "default" in spec and (required or not type_ok(spec["type"], spec["default"])):
            raise _bad(name, f"param {pname}: a default must match its type, and a required param has none")
        params[pname] = Param(spec["type"], required, spec.get("default"), str(spec.get("description", "")))
    if "scope" not in params or params["scope"].type != "list[str]":
        raise _bad(name, "must declare a list[str] param `scope`: it is the scope of the appended deliver item")
    items = doc["items"]
    if not isinstance(items, list) or not items or not all(isinstance(i, dict) for i in items):
        raise _bad(name, "items must be a non-empty list of mappings")
    for item in items:
        bindings = item.get("predicates")
        if not isinstance(bindings, list) or not all(
                isinstance(b, dict) and isinstance(b.get("args") or {}, dict) for b in bindings):
            raise _bad(name, f"item {item.get('id')}: predicates must be a list of {{predicate, args}} mappings")
        _check_refs(name, item, params)
    return Template(name, doc["version"], doc["description"], params, tuple(items), sha256)


def _check_refs(name: str, item: dict, params: Mapping[str, Param]) -> None:
    for ref in _TITLE_REF.findall(str(item.get("title", ""))):
        if ref not in params:
            raise _bad(name, f"item {item.get('id')}: title references undeclared param ${{{ref}}}")
    for binding in item.get("predicates") or []:
        for arg, value in (binding.get("args") or {}).items() if isinstance(binding, dict) else []:
            m = _REF.match(value) if isinstance(value, str) else None
            if m and m.group(1) not in params:
                raise _bad(name, f"item {item.get('id')}: {binding.get('predicate')}.{arg} references undeclared param ${m.group(1)}")
            if not m and _contains_ref(value):
                raise _bad(name, f"item {item.get('id')}: {binding.get('predicate')}.{arg} embeds a reference; "
                                 "a $param must be the whole value of an arg")


def _contains_ref(value: Any) -> bool:
    if isinstance(value, str):
        return "${" in value or bool(_REF.match(value))
    if isinstance(value, list):
        return any(_contains_ref(v) for v in value)
    if isinstance(value, dict):
        return any(_contains_ref(v) for v in value.values())
    return False


def resolve_params(template: Template, supplied: Any) -> dict:
    """Typed, complete parameters: defaults filled in, anything missing, extra or mistyped refused."""
    if not isinstance(supplied, dict):
        raise Refusal("bad_param", "params must be a JSON object")
    unknown = sorted(set(supplied) - set(template.params))
    if unknown:
        raise Refusal("unknown_param", f"template {template.name} has no param(s) {unknown}; it declares {sorted(template.params)}")
    resolved: dict[str, Any] = {}
    for pname, param in template.params.items():
        if pname in supplied:
            if not type_ok(param.type, supplied[pname]):
                raise Refusal("bad_param", f"param {pname} must be {param.type}, got {supplied[pname]!r}")
            resolved[pname] = supplied[pname]
        elif param.required:
            raise Refusal("missing_param", f"template {template.name} requires param {pname} ({param.type})")
        elif param.default is not None:
            resolved[pname] = param.default
    return resolved


def plan_data(template: Template, resolved: Mapping[str, Any], *, attempt_id: str, node_key: str,
              context_hash: str, base_sha: str, workspace: str, goal: str) -> dict:
    """The free-form plan this template and these params stand for."""
    def title(text: str) -> str:
        def fill(m: re.Match) -> str:
            value = resolved.get(m.group(1))
            return ", ".join(value) if isinstance(value, list) else "" if value is None else str(value)
        return _TITLE_REF.sub(fill, text)

    items = []
    for raw in template.items:
        preds = []
        for binding in raw.get("predicates") or []:
            args = {}
            for arg, value in (binding.get("args") or {}).items():
                m = _REF.match(value) if isinstance(value, str) else None
                if m and m.group(1) not in resolved:
                    continue                      # an optional param nobody set: the arg is left out
                args[arg] = resolved[m.group(1)] if m else value
            preds.append({"predicate": binding.get("predicate"), "args": args})
        item = {"id": raw.get("id"), "title": title(str(raw.get("title", ""))), "predicates": preds}
        if raw.get("needs"):
            item["needs"] = raw["needs"]
        items.append(item)
    return {"attempt_id": attempt_id, "node_key": node_key, "context_hash": context_hash, "base_sha": base_sha,
            "workspace": workspace, "scope": list(resolved["scope"]), "goal": goal, "items": items}
