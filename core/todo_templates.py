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
  the real workspace while the plan is still being frozen: a path whose change the
  predicate cannot see as the plan is written (an existing directory, an existing
  non-regular file, a path under an existing non-directory, one outside the
  workspace, a repeat) is refused there, each refusal naming what to declare instead,
  and the ones that do not exist yet - legal, since creating a file is a change -
  are announced in the result the director reads. It ALSO holds the `report` a
  binding names to the plan's workspace-relative rule, because a plan that freezes a
  report path the run can never clear has frozen an item that cannot pass as written.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any, Mapping

# `_relative` is reused, not restated: a declared path that escapes the workspace must be
# refused by the SAME rule whether the plan or this audit looks at it. A report's PARENT is judged
# by `core.security_jail.verify_path_safe`, the call the run makes (`core.todo_runtime._owned_entry`),
# so the audit and the run cannot disagree about which parent escapes through a symlink.
# `core.security_jail` imports nothing from `core`, so this import creates no cycle.
from core.security_jail import SecurityException, verify_path_safe
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
#: pre-state for exactly these two, so a path bound here is one the item REQUIRES to change: a
#: path the predicate cannot see change (see `check_declared_paths`) leaves the item unable to
#: pass as written, however good the work is.
DELTA_PATHS = {"file_changed": "path", "files_changed": "paths"}

#: The argument that names the file a command CLEARS before it runs and reads afterwards
#: (`core.todo_runtime._clear_report` / `_report_absence`; `_STRINGS` is what types it as a string).
#: Only `command_exit` takes it today, and this audit names it by ARGUMENT, not by predicate, so a
#: second predicate that clears a report cannot escape the containment rule below. The name is the
#: honest source available here: a `TodoPlan` carries bindings, not the library's definitions, and
#: `check_declared_paths` is handed nothing but the frozen plan - so there is no library to ask which
#: predicates clear a report, and the runtime's own account of that is this argument name.
REPORT_ARG = "report"


# Who reads what `check_declared_paths` returns, as of this commit. Checked against the tree:
#
# * `PathAudit.as_json` (below) emits `workspace`, `workspace_is_a_directory`, `declared`, and the
#   three buckets `existing`, `not_yet_existing` and `notes`.
# * The only in-repo consumer is `core/todo_cli.py`: `_plan_from_args` calls `check_declared_paths`
#   (line 174) and `cmd_create` forwards `paths.as_json()` unchanged under the key `paths` (line 193).
#   No other module calls `check_declared_paths` or reads `PathAudit`.
# * The refusal payload is `Refusal.as_json` (`core/todo_plan.py`), and `todo_cli.main` prints it with
#   exit status 2. Both keep their fields and their exit status unchanged in this commit.
# * The DSH tool wrapper is `TodoCli` in `integrations/dsh-driver/runtime.mjs`, which is in this repo
#   (`TodoCli.call`, lines 512-518). On success it passes the buckets through unchanged: `create`
#   already carries a `brief`, so the wrapper returns that result as it is. A refusal carries NO
#   buckets (`cmd_create` raises before `paths` is built), and for a refusal the wrapper ADDS a
#   `brief` field (`{ ...value, brief: ... }`). So a refusal payload is not byte-verbatim from the
#   CLI. Documented in `docs/dsh-driver.md`.
# * The plan-store rehydrate path (`core/todo_plan.py` `new_document`, `from_stored` and `load_run`;
#   `_Open` in `core/todo_cli.py`) never calls `check_declared_paths`. The buckets are not persisted
#   in the plan file, so nothing re-reads them after `create` returns.
# * Nothing under `web/` or `docs/` interprets `existing`, `not_yet_existing`, `notes` or
#   `workspace_is_a_directory`.
#
# BOUNDARY, against main `fa390d8f27`: this change refuses NO declared path that main accepted.
# The directory, file-parent, FIFO and socket refusals were already on main at `c2f2a51c2b`, so
# they are not new here. The only text that changes is the workspace-absent note. A missing parent
# directory is still allowed, so an ordinary `write` or `mkdir` step is never refused.
#
# THE REPORT PATH, added after `3202466603`: this audit now also refuses a `report` binding that
# names a path outside the workspace (absolute, or holding `..`), by the same `todo_plan._relative`
# call it already uses for declared delta paths. It also refuses a report whose PARENT resolves
# outside the workspace through a symlink, judged by `core.security_jail.verify_path_safe` - the call
# the run makes - so a symlinked parent cannot freeze a report the run can never clear. A link the
# run creates after the freeze is not visible here; that boundary is stated in `check_declared_paths`.
# Main accepted all of those and discovered the escape only at run time, where `_clear_report`
# refuses to touch anything outside the workspace and the report is therefore never this run's - a
# plan frozen with an item that cannot pass as written.
# The refusal is containment ONLY: a report path still never enters `declared`, is never checked for
# duplicates, and is never required to exist, so every in-workspace report path - a not-yet-existing
# parent included, and one identical to a declared delta path - freezes exactly as it did on main.
# `PathAudit` and its JSON gained no field, `command_exit` and every other run-time path are
# untouched, and no template gained a `report` parameter. Nothing above changed meaning.
#
# SAFEGUARDS: no acceptance criterion, evidence format, non-author review control or privacy
# boundary is weakened by the path audit. The report rule above joined the delta refusals in
# `d7e2b30bb6`; `PathAudit` gained no field, no bucket and no template parameter with it.
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


def _existing_non_directory_parent(workspace: Path, rel: str) -> tuple[str, Path] | None:
    """The first existing component of `rel`'s parent chain that is NOT a directory, if any.

    A declared path is created by walking its directories; a component that already exists as a
    file, a FIFO or a socket cannot be walked through while it stands, so the path below it has no
    digest until the step replaces that component with a directory. The walk stops at the first
    component that does not exist: nothing deeper exists either, and creating below it is an
    ordinary change.
    """
    parts = PurePosixPath(rel).parts
    for i in range(1, len(parts)):
        component = str(PurePosixPath(*parts[:i]))
        path = workspace / PurePosixPath(component)
        if not path.exists():
            return None
        if not path.is_dir():
            return component, path
    return None


def _what_it_is(path: Path) -> str:
    """`a FIFO`, `a socket`, `a regular file`, ... for a refusal that names what it found.

    `stat` follows symlinks on purpose: the component the walk would descend into is the target's
    kind, and a symlink to a regular file must keep being judged by the file it points at.
    """
    try:
        mode = path.stat().st_mode
    except OSError:                      # gone between the check and this look: a bare fact, not a claim
        return "not a directory"
    if stat.S_ISREG(mode):
        return "a regular file"
    if stat.S_ISFIFO(mode):
        return "a FIFO"
    if stat.S_ISSOCK(mode):
        return "a socket"
    if stat.S_ISCHR(mode):
        return "a character device"
    if stat.S_ISBLK(mode):
        return "a block device"
    return "not a directory"


def _report_parent_contained(plan: TodoPlan, report: str, where: str) -> None:
    """Refuse a report whose PARENT the run would not clear, judged the way the run judges it.

    The run builds the report's path from the RESOLVED workspace and contains its parent with
    `verify_path_safe` (`core.todo_runtime._report_path`, `_owned_entry`). This does the same
    join and the same call, so a symlinked parent that leaves the workspace is refused here
    rather than discovered when the item runs. A parent that cannot be resolved at all (a loop,
    an OSError) is refused too: the run treats that as a reason it cannot clear the report, so
    the report can never be this run's either.

    This reads the disk as it stands at freeze time. A link the run itself creates after the
    plan froze is not seen here (see `check_declared_paths`).
    """
    try:
        root = Path(plan.workspace).resolve()
        parent = (root / str(report)).parent
        verify_path_safe(root, parent)
    except SecurityException:
        raise Refusal(
            "invalid_plan",
            f"{where} must be a relative path inside the workspace, got {report!r}: its parent resolves to a "
            f"place outside the workspace {plan.workspace}, and the run clears a report only under a parent "
            f"that resolves inside it, so this report could never be established as the run's own",
        ) from None
    except (RuntimeError, OSError) as exc:
        raise Refusal(
            "invalid_plan",
            f"{where} names {report!r}, whose parent cannot be resolved inside the workspace {plan.workspace} "
            f"({exc.__class__.__name__}: {exc}), so the run could not clear this report and it could never be "
            f"established as the run's own",
        ) from None


def check_declared_paths(plan: TodoPlan) -> PathAudit:
    """Judge the plan's declared paths against the real workspace, before the plan file exists.

    Two things are judged here, and only one of them is a delta:

    * The paths a predicate requires to CHANGE. The item's own predicate is "every listed path
      differs from its state at the start of the step". A path whose change that predicate cannot
      see, as the plan is written, is refused HERE, while the plan is being frozen, because a frozen
      plan whose item cannot pass as written keeps its attempt running until someone abandons it and
      the director only finds out at delivery. "Cannot be seen to change" describes the declared
      delta, not an impossibility: each refusal names what the director should declare instead:

      - an existing DIRECTORY - `_digest` digests regular files only, and a directory has no digest,
        so a step that edits inside it cannot be seen to change the path itself. The one change the
        predicate can register for this path is the directory being replaced by a regular file (the
        digest goes from None to a hash). A symlink to a directory is a directory here: `_digest`
        follows the link;
      - a path whose existing PARENT is not a directory (`app.py/x/y.txt`, where `app.py` is a
        file) - the path has no digest while that parent stands, so the step cannot be seen to
        change it unless it first replaces the parent with a directory;
      - an existing path that is neither a directory nor a regular file (a FIFO, a socket, a
        device) - `_digest` returns None for it, so a step cannot be seen to change it unless it
        replaces it with a regular file;
      - a path outside the workspace (absolute, or holding a `..` component) - refused by the
        plan's own rule, `todo_plan._relative`, called here so there are never two rules;
      - the same path twice in one list, compared in normalised form so `./a` and `a` collide -
        a list that repeats one file names fewer files than it looks like it does. This one reads
        the plan, not the workspace, so it is refused whether or not the workspace exists.

    * The REPORT a binding names (`REPORT_ARG`; `command_exit`'s `report`, today the only one).
      It is judged by containment only, in two steps. First the plan's own `_relative` call: a
      report that is absolute or holds a `..` component is refused. Second, the report's PARENT is
      judged the way the run judges it: the parent is joined onto the RESOLVED workspace and
      contained with `core.security_jail.verify_path_safe`, which resolves symlinks. So a parent
      that is a symlink pointing outside the workspace is refused here, as is a parent whose
      resolution fails (a symlink loop, an OSError), because the run cannot clear such a report
      either. Why: at run time the report's PARENT must resolve inside the workspace before the
      entry is unlinked (`todo_runtime._clear_report`), so an escaping parent is a reason, never a
      deletion - the command's report is neither removed nor read, ownership of it is never
      established, and the exit code alone decides. That is safe, but if it is discovered only when
      the item runs then the plan froze an item that cannot pass as written: the step's own declared
      evidence was never this run's. So the escape is refused at freeze time instead.
      The parent is resolved on disk AS IT STANDS AT FREEZE TIME, and that is a boundary this audit
      cannot cross. A parent that does not exist yet is accepted, because `verify_path_safe` resolves
      a missing path lexically and nothing is yet linked. A link the RUN itself creates after the
      plan froze - the command makes `d` a symlink to a directory outside the workspace - cannot be
      seen here, and is caught only at run time: `_clear_report` refuses to clear, no report is read,
      and the verdict is the exit code plus a reason. The test
      `test_a_parent_the_run_links_out_after_freeze_is_only_caught_at_run_time` pins that boundary.
      The report is NOT a delta claim: it never joins `declared`, is never compared for duplicates,
      and is not required to exist. A report path inside the workspace, under a parent that does not
      exist yet, or identical to a declared delta path, is accepted exactly as before (refusing the
      last of those would be a false refusal: one file, written once, may be both the delta the item
      requires and the report its command writes). A report whose FINAL component is a symlink
      pointing outside is also accepted: the run removes the link and never follows it, so that
      shape is safe and must stay accepted.

    A path that does not exist yet stays LEGAL: creating a file is a normal change and
    `code_change` relies on it. It is ANNOUNCED instead ("the step will have to create it"), in
    the result that carries the frozen plan, so a director who will never create one sees it now.

    A workspace that is not a directory is said out loud rather than skipped, and nothing is
    assumed about it: the paths cannot be looked up, so they are not judged by a guess. The note
    says which checks that cost - and which ones it did not: BOTH refusals that read the plan
    rather than the workspace (a duplicate spelling in one list, and a path outside the workspace
    via `todo_plan._relative`) still run there and still refuse, so neither is one of the checks a
    missing directory cost. The report rule's PLAN half - the `_relative` call - is one of those
    plan-reading refusals and runs there too. Its PARENT half reads the disk: a symlinked parent can
    only be resolved in a workspace that exists, so with no workspace there is nothing for it to find,
    and the audit does not claim it checked one.
    """
    workspace = Path(plan.workspace)
    measured = workspace.is_dir()
    declared: list[str] = []
    existing: list[str] = []
    created: list[str] = []
    for item in plan.items:
        for binding in item.predicates:
            # The report a command declares, judged by the PLAN's rule and before the delta logic,
            # which only ever looks at DELTA_PATHS predicates and would skip `command_exit` outright.
            # A report outside the workspace is not removed and not read at run time
            # (`todo_runtime._clear_report` returns a reason instead of deleting anything outside the
            # workspace), so the exit code alone would decide and the frozen plan would carry a report
            # the run can never establish as its own. Refused here, while the plan is being frozen.
            # Containment only: a report path is NOT a delta claim, so it never enters `declared` and
            # never collides with a declared delta path (see the docstring).
            if REPORT_ARG in binding.args:
                where = f"item {item.id}: {binding.predicate}.{REPORT_ARG}"
                # ONE value, ONE check. The plan's own binding validation requires `report` to be a
                # non-empty STRING (`core/todo_runtime` `_STRINGS`), and it runs inside
                # `TodoPlan.from_json` - the only way `core/todo_cli._plan_from_args`, this audit's
                # only production caller, ever gets a plan. So exactly one string arrives here, and an
                # element-wise loop would be a branch no frozen plan can reach.
                # Keep these two together: `_relative` judges `PurePosixPath(str(value))`, so a list
                # would reach it as its own repr and `['../x']` has no `..` PART - the type check,
                # not this call, is what keeps a list from passing. A test in
                # `tests/unit/test_todo_plan_judges_the_report_it_will_clear.py` pins that refusal to
                # `from_json`, so loosening the type check cannot happen unnoticed.
                _relative(binding.args[REPORT_ARG], where)
                _report_parent_contained(plan, binding.args[REPORT_ARG], where)
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
                        f"item requires every declared path to differ from its state at the start of the step, and "
                        f"{binding.predicate} compares a file's digest, which a directory does not have. The step "
                        f"cannot be seen to change this path unless it first replaces the directory with a regular "
                        f"file: declare that regular file, or declare the files inside the directory instead",
                        item=item.id, predicate=binding.predicate, path=rel)
                parent = _existing_non_directory_parent(workspace, rel)
                if parent is not None:
                    component, found = parent
                    raise Refusal(
                        "impossible_path",
                        f"{where} declares {rel!r}, whose parent {component!r} is {_what_it_is(found)}, where a "
                        f"directory is required, under workspace {plan.workspace}: the item requires every declared "
                        f"path to differ from its state at the start of the step, and {binding.predicate} compares a "
                        f"file's digest, which this path does not have while its parent is not a directory. The step "
                        f"cannot be seen to change it unless it first replaces {component!r} with a directory and "
                        f"creates the file under that: declare a path whose existing parents are all directories",
                        item=item.id, predicate=binding.predicate, path=rel)
                # What the directory refusal above does not own: a path that exists and is neither a
                # directory nor a regular file has no digest. The `is_dir` term keeps the two refusals
                # disjoint - anything that resolves to a directory is the directory refusal's case,
                # including a symlink to one.
                if target.exists() and not target.is_file() and not target.is_dir():
                    raise Refusal(
                        "impossible_path",
                        f"{where} declares {rel!r}, which exists and is {_what_it_is(target)}, under workspace "
                        f"{plan.workspace}: the item requires every declared path to differ from its state at the "
                        f"start of the step, and {binding.predicate} compares a file's digest, which this path does "
                        f"not have. The step cannot be seen to change it unless it first replaces it with a regular "
                        f"file: declare that regular file instead",
                        item=item.id, predicate=binding.predicate, path=rel)
                (existing if target.exists() else created).append(rel)
    notes: list[str] = []
    if not measured:
        notes.append(
            f"workspace {plan.workspace} is not a directory, so the {len(declared)} declared path(s) {list(declared)} "
            f"could not be looked up: no directory, parent, non-regular-file or existence check was made about them, "
            f"and nothing was assumed about them (the refusals that read the PLAN still ran: a duplicate spelling of "
            f"one path in one list is still refused, and so is a path outside the workspace - neither needs a "
            f"directory to decide)")
    elif created:
        notes.append(
            f"declared path(s) {list(created)} do not exist in workspace {plan.workspace} yet: the item requires "
            f"EVERY declared path to differ from its state at the start of the step, so this step will have to "
            f"CREATE them - a director who will never create one has frozen an item that cannot pass")
    return PathAudit(plan.workspace, measured, tuple(declared), tuple(existing), tuple(created), tuple(notes))
