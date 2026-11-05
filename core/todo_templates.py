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
* A template binds paths into predicates that require them to CHANGE from their
  state at the start of the step. `check_declared_paths` judges those paths against
  the real workspace while the plan is still being frozen: a path that can never
  differ (an existing directory, one outside the workspace, a repeat) is refused
  there, and the ones that do not exist yet - legal, since creating a file is a
  change - are announced in the result the director reads.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any, Mapping

# `_relative` is reused, not restated: a declared path that escapes the workspace must be
# refused by the SAME rule whether the plan or this audit looks at it.
from core.todo_plan import Refusal, TodoPlan, _relative
from core.todo_runtime import PredicateLibrary

TYPES = ("str", "int", "list[str]")
_NAME = re.compile(r"[a-z][a-z0-9_]*\Z")
_REF = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)\Z")
_TITLE_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_TOP = {"name", "version", "description", "params", "items"}
_PARAM = {"type", "required", "default", "description"}
_SAMPLE = {"str": "x", "int": 1, "list[str]": ["x"]}


def default_dir() -> Path:
    """AITELIER_TODO_TEMPLATES, like `create --plan-json`, is not gated: both rely on the DSH tool
    that calls `create` being root-only (a child must never choose the template directory)."""
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
    if not params["scope"].required and params["scope"].default is None:
        raise _bad(name, "param `scope` must be required or have a default: deliver cannot run without one")
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
        for arg, value in (binding.get("args") or {}).items():
            where = f"item {item.get('id')}: {binding.get('predicate')}.{arg}"
            # A reference is the whole arg value, or a whole element of a list value.
            for element in (value if isinstance(value, list) else [value]):
                m = _REF.match(element) if isinstance(element, str) else None
                if m and m.group(1) not in params:
                    raise _bad(name, f"{where} references undeclared param ${m.group(1)}")
                if not m and _embeds_ref(element, params):
                    raise _bad(name, f"{where} embeds a reference; a $param must be the whole value of an arg "
                                     "(or a whole element of a list)")


def _embeds_ref(value: Any, params: Mapping[str, Param]) -> bool:
    """A `$name` / `${name}` for a declared param inside a longer string, or any `${...}` at all."""
    if isinstance(value, str):
        return "${" in value or any(re.search(rf"\${re.escape(n)}(?!\w)", value) for n in params)
    if isinstance(value, list):
        return any(_embeds_ref(v, params) or (isinstance(v, str) and _REF.match(v)) for v in value)
    if isinstance(value, dict):
        return any(_embeds_ref(v, params) or (isinstance(v, str) and _REF.match(v)) for v in value.values())
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


def _expand_list(value: list, resolved: Mapping[str, Any]) -> list:
    out = []
    for element in value:
        m = _REF.match(element) if isinstance(element, str) else None
        if not m:
            out.append(element)
        elif m.group(1) in resolved:          # an optional param nobody set drops its element
            out.append(resolved[m.group(1)])
    return out


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
                if isinstance(value, list):       # whole elements may be refs: [$report_path]
                    args[arg] = _expand_list(value, resolved)
                    continue
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


# -- the paths a frozen plan requires to change ----------------------------------------------

#: The predicates whose verdict is "this path differs from its state at the start of the step",
#: with the argument that carries those paths. `core/todo_runtime.capture_pre_state` captures a
#: pre-state for exactly these two, so a path bound here is one the item REQUIRES to change: if
#: it can never change, the item can never pass, however good the work is.
DELTA_PATHS = {"file_changed": "path", "files_changed": "paths"}


@dataclass(frozen=True)
class PathAudit:
    """What a plan's declared paths are, judged against the real workspace at freeze time."""

    workspace: str
    measured: bool          # False: the workspace is not a directory, so nothing was looked up
    declared: tuple[str, ...]
    existing: tuple[str, ...]
    not_yet_existing: tuple[str, ...]
    notes: tuple[str, ...]

    def as_json(self) -> dict:
        return {"workspace": self.workspace, "workspace_is_a_directory": self.measured,
                "declared": list(self.declared), "existing": list(self.existing),
                "not_yet_existing": list(self.not_yet_existing), "notes": list(self.notes)}


def check_declared_paths(plan: TodoPlan) -> PathAudit:
    """Judge the plan's declared paths against the real workspace, before the plan file exists.

    The item's own predicate is "every listed path differs from its state at the start of the
    step". A path for which that is impossible is refused HERE, while the plan is being frozen,
    because a frozen plan whose item can never pass keeps its attempt running until someone
    abandons it and the director only finds out at delivery:

    * an existing DIRECTORY - `_digest` digests files only, so a directory has no state to
      differ from: `files_changed` would fail it forever with "does not exist after the step";
    * a path outside the workspace (absolute, or holding a `..` component) - refused by the
      plan's own rule, `todo_plan._relative`, called here so there are never two rules;
    * the same path twice in one list, compared in normalised form so `./a` and `a` collide -
      a list that repeats one file names fewer files than it looks like it does.

    A path that does not exist yet stays LEGAL: creating a file is a normal change and
    `code_change` relies on it. It is ANNOUNCED instead ("the step will have to create it"), in
    the result that carries the frozen plan, so a director who will never create one sees it now.

    A workspace that is not a directory is said out loud rather than skipped, and nothing is
    assumed about it: the paths cannot be looked up, so they are not judged by a guess.
    """
    workspace = Path(plan.workspace)
    measured = workspace.is_dir()
    declared: list[str] = []
    existing: list[str] = []
    created: list[str] = []
    for item in plan.items:
        for binding in item.predicates:
            arg = DELTA_PATHS.get(binding.predicate)
            if arg is None or arg not in binding.args:
                continue
            where = f"item {item.id}: {binding.predicate}.{arg}"
            raw = binding.args[arg]
            seen: dict[str, str] = {}
            for value in (raw if isinstance(raw, list) else [raw]):
                _relative(value, where)
                rel = str(PurePosixPath(str(value)))
                if rel in seen:
                    raise Refusal(
                        "impossible_path",
                        f"{where} declares {rel!r} twice ({seen[rel]!r} and {value!r} are the same path once "
                        f"normalised): a list that repeats one file names fewer files than the director reads it "
                        f"as - the other file that was meant is not declared at all, and this item would judge a "
                        f"shorter list than intended",
                        item=item.id, predicate=binding.predicate, path=rel)
                seen[rel] = str(value)
                if rel in declared:
                    continue
                declared.append(rel)
                if not measured:
                    continue
                target = workspace / rel
                if target.is_dir():
                    raise Refusal(
                        "impossible_path",
                        f"{where} declares {rel!r}, an existing DIRECTORY under workspace {plan.workspace}: the "
                        f"item requires every declared path to differ from its state at the start of the step, and a "
                        f"directory has no state to differ from - {binding.predicate} compares a file's digest, so "
                        f"this item could never pass and the attempt would run until someone abandoned it; declare "
                        f"the files inside it",
                        item=item.id, predicate=binding.predicate, path=rel)
                (existing if target.exists() else created).append(rel)
    notes: list[str] = []
    if not measured:
        notes.append(
            f"workspace {plan.workspace} is not a directory, so the {len(declared)} declared path(s) {list(declared)} "
            f"could not be looked up: no directory, duplicate-spelling or existence check was made, and nothing was "
            f"assumed about them")
    elif created:
        notes.append(
            f"declared path(s) {list(created)} do not exist in workspace {plan.workspace} yet: the item requires "
            f"EVERY declared path to differ from its state at the start of the step, so this step will have to "
            f"CREATE them - a director who will never create one has frozen an item that cannot pass")
    return PathAudit(plan.workspace, measured, tuple(declared), tuple(existing), tuple(created), tuple(notes))
