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
  non-regular file, a path under an existing non-directory, a repeat) is refused
  there, each refusal naming what to declare instead, and the ones that do not exist
  yet - legal, since creating a file is a change - are announced in the result the
  director reads. It ALSO judges every declared change against the `changes_within`
  scope it is judged under - the item's own, the scope of every item that NEEDS it
  (which includes the appended `deliver` item: it needs every item, and binds the
  plan's own `scope`), and the scope of every item the runtime PRESENTS AFTER it -
  with the run's own matcher (`core.todo_runtime._scope_regex`): a path no
  scope glob matches is refused, because then no tree can satisfy both predicates -
  changing the file fails `changes_within`, leaving it alone fails `files_changed` -
  and the plan would freeze an item that can never pass. The presentation half exists
  because `TodoRuntime.next_item` hands items out in PLAN order, so an item declared
  later measures `changes_within` with every earlier item's change already in the tree.
  The remaining containment
  refusal, a path outside the workspace
  (absolute, or holding a `..` component), is NOT this audit's on the production
  route: the plan's own binding validation (`TodoPlan._binding`, inside
  `TodoPlan.from_json`) applies `todo_plan._relative` to every `path`/`paths`/
  `pattern` argument while the plan is CONSTRUCTED, so it refuses first and this
  audit is never entered. The audit's own `_relative` call for delta paths is a
  backstop, reachable only for a plan built without `from_json` - kept, so the same
  rule holds on both routes. It ALSO holds the `report` a binding names to the plan's
  workspace-relative rule, because a plan that freezes a report path the run can never
  clear has frozen an item that cannot pass as written; a `report` argument is not one
  of the three `_binding` checks, so THAT refusal is this audit's on both routes.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
from typing import Any, Mapping, NamedTuple

# `_relative` is reused, not restated: a declared path that escapes the workspace must be
# refused by the SAME rule whichever site answers. On the production route the plan's own binding
# validation (`TodoPlan._binding`, inside `TodoPlan.from_json`) answers first, while the plan is
# constructed; this audit's call for delta paths is a backstop for a plan built without `from_json`.
# (For a report the audit IS the answering site, since `report` is not one of `_binding`'s keys.)
# A report's PARENT is judged by `core.security_jail.verify_path_safe`, the call the run makes
# (`core.todo_runtime._owned_entry`), so the audit and the run cannot disagree about which parent
# escapes through a symlink. `core.security_jail` imports nothing from `core`, so no cycle.
from core.security_jail import SecurityException, verify_path_safe
from core.todo_plan import Refusal, TodoPlan, _relative
# `_scope_regex` is reused, not restated: at run time `changes_within` compiles each scope glob with
# this function and matches every changed path with `rx.match(p)`. A second, hand-rolled glob rule
# here could refuse a plan the run would accept, or accept one the run can never satisfy - exactly
# the disagreement this audit exists to end. The audit's scope check below calls THIS function.
from core.todo_runtime import PredicateLibrary, _scope_regex

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
# call it also applies to declared delta paths - a backstop there (the plan's own binding validation,
# inside `TodoPlan.from_json`, is what refuses a delta path on the production route), but the
# answering site here: `report` is not one of the keys `_binding` contains, so no earlier check exists.
# It also refuses a report whose PARENT resolves outside the workspace through a symlink, judged by
# `core.security_jail.verify_path_safe` - the call the run makes - so a symlinked parent cannot freeze
# a report the run can never clear. A link the run creates after the freeze is not visible here; that
# boundary is stated in `check_declared_paths`.
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


def _judged_before(plan: TodoPlan, item_id: str) -> list[str]:
    """The items whose results are in place when `item_id` is judged: itself and its transitive `needs`.

    `needs` names only items declared EARLIER (`TodoPlan._item` refuses anything else) and `begin`
    refuses an item whose `needs` have not passed, so this closure is exactly what `changes_within`
    can already see changed in the tree. Returned in PLAN order so a refusal names the first
    offending pair in the order a director reads the plan. A name no item carries, and a cycle, are
    both guarded rather than assumed away: a `TodoPlan` built without `from_json` validates nothing.
    """
    by_id = {i.id: i for i in plan.items}
    keep: set[str] = set()
    pending = [item_id]
    while pending:
        current = pending.pop()
        if current in keep or current not in by_id:
            continue
        keep.add(current)
        pending.extend(by_id[current].needs)
    return [i.id for i in plan.items if i.id in keep]


#: Predicates that RUN a program. What such a step does to the tree is not in the plan, so whether it
#: can touch a declared delta is decided by its ARGV and by nothing else.
COMMAND_PREDICATES = ("command_exit",)

#: Files whose CONTENT decides whether git excludes a path. An item that declares a change to one of
#: them can change the answer `_invisible_to_changes_within` reads, and that content is not in the plan.
EXCLUDE_RULE_FILES = (".gitignore", (".git", "info", "exclude"))

#: What `_judge_the_pair` answers for one (declared delta, judging scope) pair the scope does not match.
PAIR_REFUSE = "refuse"                  # provably unsatisfiable: refuse at freeze
PAIR_SATISFIABLE = "satisfiable"        # the delta cannot make that scope fail: freeze, silently
PAIR_DECLINED = "declined"              # the audit cannot tell: freeze, and ANNOUNCE the doubt


class _PairVerdict(NamedTuple):
    """One pair's answer, plus the items that made it what it is (empty when they did not matter)."""

    action: str
    programs: tuple[str, ...] = ()          # items in the window whose argv NAMES the declared path
    exclude_writers: tuple[str, ...] = ()   # items in the window that change an exclude-rule file


def _path_components(value: object) -> tuple[str, ...]:
    """`value` as non-empty, separator-normalised path components: the unit `_names_path` compares."""
    return tuple(part for part in str(value).replace("\\", "/").split("/") if part not in ("", "."))


def _invisible_to_changes_within(workspace: Path, rel: str) -> bool:
    """True when git itself excludes `rel` from the changed set `changes_within` measures.

    `changes_within` (``core.todo_runtime.changes_within``) builds its set from `git diff
    base..HEAD`, `git diff HEAD` and `git ls-files --others --exclude-standard`, so an ignored
    UNTRACKED path is in none of the three and can never make that predicate fail. git answers the
    exclusion question itself, which is the only answer worth having: tracked files are not subject
    to exclude rules, so a force-added ignored path answers "not ignored" here and stays refused.
    When git cannot answer (no repository, no git, a timeout) this returns False and the audit keeps
    its refusal.

    The answer is read AS THE WORKSPACE STANDS AT FREEZE TIME, and a program in the plan can change
    it later: `git add -f` on the excluded path makes it visible to all three sets. That is why a
    program whose argv NAMES the path turns this answer into a refusal instead of a skip
    (`_judge_the_pair`) - measured 2026-11-10, see the BOUNDARY in `check_declared_paths`.
    """
    try:
        done = subprocess.run(["git", "-C", str(workspace), "check-ignore", "-q", "--", rel],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def _runs_a_command(item: Any) -> bool:
    """Whether this item runs a program whose effect on the tree the plan does not declare."""
    return any(binding.predicate in COMMAND_PREDICATES for binding in item.predicates)


def _argv_of(item: Any) -> list[str]:
    """Every argv word the item's program bindings carry, in binding order."""
    words: list[str] = []
    for binding in item.predicates:
        if binding.predicate not in COMMAND_PREDICATES:
            continue
        raw = binding.args.get("argv")
        for value in (raw if isinstance(raw, list) else [] if raw is None else [raw]):
            words.append(str(value))
    return words


#: Characters that separate one shell word from the next INSIDE one argv element. A `command_exit`
#: binding normally carries `["/bin/sh", "-c", "<program>"]`, so the program's own paths sit inside a
#: single argv element; splitting it on shell metacharacters is what lets the audit read them at all.
_SHELL_WORD_SEPARATORS = re.compile(r"[^A-Za-z0-9_./@+=:~^-]+")


def _names_path(words: list[str], rel: str) -> bool:
    """Whether one of these argv words NAMES `rel`, as a whole path SUFFIX compared by components.

    Component-wise, not by substring: `git restore app.py` and `git checkout -- ./app.py` name
    `app.py`; `myapp.py` and `app.py.bak` do not. One argv element may be a WHOLE program
    (`/bin/sh -c "<program>"`), so each element is split into shell words first - that is the only
    reason `["/bin/sh", "-c", "git restore app.py"]` names `app.py` at all. A bare `.` or `/` names no
    declared path, so `git checkout -- .` restores everything and is NOT this audit's undo shape - a
    boundary, not an oversight (see the BOUNDARY in `check_declared_paths`).
    """
    parts = _path_components(rel)
    if not parts:
        return False
    for word in words:
        for token in [str(word), *_SHELL_WORD_SEPARATORS.split(str(word))]:
            token_parts = _path_components(token)
            if len(token_parts) >= len(parts) and token_parts[-len(parts):] == parts:
                return True
    return False


def _window_items(plan: TodoPlan, declared_by: str, judged_by: str) -> list[Any]:
    """Items from `declared_by` through `judged_by` in PLAN order, both INCLUDED.

    Both ends are included because what either end does to the tree happens before the scope
    predicate is measured: the declaring item's work runs in the step that declares the delta, and
    the judging item's own predicates are evaluated in its own step.
    """
    ids = [i.id for i in plan.items]
    if declared_by not in ids or judged_by not in ids:
        return []
    start, stop = ids.index(declared_by), ids.index(judged_by)
    if start > stop:
        return []
    return list(plan.items[start:stop + 1])


def _programs_naming(plan: TodoPlan, declared_by: str, judged_by: str, rel: str) -> tuple[str, ...]:
    """Ids of the items in that window that run a program whose argv NAMES `rel`."""
    return tuple(item.id for item in _window_items(plan, declared_by, judged_by)
                 if _names_path(_argv_of(item), rel))


def _declares_exclude_rule(binding: Any) -> bool:
    """Whether this binding requires a file that can carry git EXCLUDE RULES to change."""
    arg = DELTA_PATHS.get(binding.predicate)
    if arg is None or arg not in binding.args:
        return False
    raw = binding.args[arg]
    for value in (raw if isinstance(raw, list) else [raw]):
        parts = _path_components(value)
        if not parts:
            continue
        if parts[-1] == EXCLUDE_RULE_FILES[0] or parts[-3:] == EXCLUDE_RULE_FILES[1]:
            return True
    return False


def _exclude_rule_writers(plan: TodoPlan, declared_by: str, judged_by: str) -> tuple[str, ...]:
    """Ids of the items in that window that declare a change to a file carrying exclude rules."""
    return tuple(item.id for item in _window_items(plan, declared_by, judged_by)
                 if any(_declares_exclude_rule(binding) for binding in item.predicates))


def _judge_the_pair(plan: TodoPlan, workspace: Path, declared_by: str, judged_by: str,
                    rel: str) -> _PairVerdict:
    """Answer one (declared delta, judging scope) pair that no glob of the scope matches.

    The refusals rest on two premises: the delta is still in the tree when the scope is measured, and
    git can see it there. The refusal fires only when the plan can defeat neither, and each way it
    can is a shape measured in `tests/unit/test_todo_templates.py` (2026-11-10):

    * a program in the window NAMES the path (`_programs_naming`) - the undo shape a non-author
      reviewer found refused, and its residual: the same naming with a program that reverts nothing;
    * the path is excluded by git and no program names it - `changes_within` cannot see the delta at
      all, so the scope cannot fail (silently satisfiable). If a program DOES name it, `git add -f`
      can make it visible, so the refusal stands;
    * an item in the window declares a change to a file that carries exclude rules
      (`_exclude_rule_writers`): whether the delta is visible is then decided by content the plan does
      not carry, so the plan freezes - the shape a plan that creates its own ignore rule takes.
    """
    invisible = _invisible_to_changes_within(workspace, rel)
    naming = _programs_naming(plan, declared_by, judged_by, rel)
    if invisible:
        if naming:
            return _PairVerdict(PAIR_REFUSE, programs=naming)
        return _PairVerdict(PAIR_SATISFIABLE)
    if naming:
        return _PairVerdict(PAIR_DECLINED, programs=naming)
    writers = _exclude_rule_writers(plan, declared_by, judged_by)
    if writers:
        return _PairVerdict(PAIR_DECLINED, exclude_writers=writers)
    return _PairVerdict(PAIR_REFUSE)


def _named_message(where: str, rel: str, scope_where: str, scope: Any, programs: tuple[str, ...]) -> str:
    """The refusal for a delta on a path git EXCLUDES that a program in the window also NAMES."""
    if not programs:
        return ""
    return (
        f"{where} declares {rel!r}, which matches no glob in the {scope_where} it must also satisfy: the "
        f"declared scope is {list(scope)}. Item(s) {list(programs)} in that window run a program whose argv "
        f"NAMES {rel!r}, and git excludes {rel!r} from every changed set changes_within reads at freeze time: "
        f"`git add -f` on it makes it visible to all three, which fails that scope, while a program that leaves "
        f"it alone does not. The plan does not say which program runs, so the audit cannot promise this item "
        f"can pass and refuses rather than freeze it. Declare {rel!r} inside the scope, or drop the program "
        f"that names it")


def _declined_note(where: str, rel: str, scope_where: str, scope: Any, verdict: _PairVerdict) -> str:
    """The announcement for a pair the audit declines to judge, naming what made it undecidable."""
    if verdict.programs:
        because = (f"item(s) {list(verdict.programs)} in that window run a program whose argv NAMES {rel!r}, so "
                   f"they can revert the delta and commit the revert - a program that reverts it and one that "
                   f"leaves it look identical in the plan")
    else:
        because = (f"item(s) {list(verdict.exclude_writers)} in that window declare a change to a file that "
                   f"carries git exclude rules (.gitignore / .git/info/exclude), so whether git can see {rel!r} "
                   f"when the scope is measured depends on content the plan does not carry")
    return (f"{where} declares {rel!r}, which matches no glob in the {scope_where} (declared scope "
            f"{list(scope)}): NOT refused, because {because}. The audit cannot prove this pair "
            f"unsatisfiable, so the plan freezes and the run answers at todo_done - if the delta is still "
            f"visible there, item {scope_where.split(':')[0].replace('item ', '')} fails with the changed path "
            f"named")


def check_declared_paths(plan: TodoPlan) -> PathAudit:
    """Judge the plan's declared paths against the real workspace, before the plan file exists.

    Three things are judged here, and two of them are about a delta:

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
      - a path outside the workspace (absolute, or holding a `..` component). On the production route
        this one is NOT refused here: `TodoPlan.from_json` applies `todo_plan._relative` to every
        `path`/`paths`/`pattern` argument while the plan is CONSTRUCTED (`TodoPlan._binding`), and
        `core.todo_cli._plan_from_args` - this audit's only production caller - always hands the audit
        a plan built there, so the plan refuses and this audit is never entered. The `_relative` call
        below is a backstop, reachable only for a plan built without `from_json`, kept so the same
        rule answers on both routes (never two rules). A test measures the route rather than the
        outcome: `test_the_delta_refusal_arrives_with_the_audit_never_entered`;
      - the same path twice in one list, compared in normalised form so `./a` and `a` collide -
        a list that repeats one file names fewer files than it looks like it does. This one reads
        the plan, not the workspace, so it is refused whether or not the workspace exists.

    * The PAIR every declared delta is judged under: a path a predicate requires to CHANGE
      (`files_changed`, or `file_changed`) must match at least one glob of EVERY `changes_within`
      scope that path is already in place for. There are THREE such scopes. The first is the
      declaring item's own - the `code_change`/`docs_change` shape, where the item carries both
      predicates. The second is the scope of every item that transitively NEEDS the declaring item:
      `begin` refuses an item whose `needs` have not passed, so those items are judged only after
      this change is in the tree, and `changes_within` measures the WHOLE tree since the base. The
      appended `deliver` item needs every item and binds the plan's own `scope`, which makes that
      scope one of them for every plan - so a delta no plan-level scope glob matches (an
      `investigate` report path, say) is refused here rather than frozen into a `deliver` item that
      can never pass. At run time `files_changed` requires each declared path to differ from its
      pre-state, while `changes_within` fails on any path changed since the base that no scope glob
      matches. A declared path matching NONE of a scope it is judged under is therefore
      unsatisfiable both ways - change it and `changes_within` fails, leave it and `files_changed`
      fails - so it is refused at freeze time (`invalid_plan`), naming the path and that scope's
      declared list. That sentence is exact for the delta the audit can still SEE, and the BOUNDARY
      below says which deltas it cannot. The third is the scope of every item the runtime PRESENTS
      AFTER the declaring one: `TodoRuntime.next_item` hands out the first item, in PLAN order,
      whose needs have passed, and `needs` may only name items declared earlier, so the
      presentation order IS the plan order
      - an item declared later is measured with the earlier item's change already in the tree
      whether or not it needs it. That is the scope this audit used to spare ("it does not need the
      declaring item, so it may run first"), and the plan it spared was measurably dead: `change`
      was presented first, and `judge` then failed with `1 changed path(s) outside scope
      ['other.py']: app.py`. So the two cases are answered by two refusals, never one twice: an item
      that NEEDS the declaring item gets the dependency sentence, and an item declared after it
      WITHOUT needing it gets the presentation sentence. What stays unjudged - and must - is a
      delta declared LATER than the scope that judges it: the scope item is presented first, while
      the tree is still clean, so such a plan freezes and runs (a test drives it by following
      `status.next`). Only an item carrying BOTH halves itself is judged immediately, inside its own
      turn, and by the same rule. The match is made by `core.todo_runtime._scope_regex` -
      the function `changes_within` itself compiles its globs with at run time - imported rather
      than restated, so the freeze and the run cannot apply two different glob rules. Like the
      duplicate rule, this reads the plan and not the workspace, so it runs whether or not the
      workspace exists. This is a refusal, never an announcement: a path that does not exist yet is
      still legal when the scope can match it.
      BOUNDARY: the presentation order holds for every item the run can present. An earlier item
      skipped as UNMEASURABLE (an expired absence) is never presented, but an attempt with one
      cannot finish at all (`TodoRun.finished` requires every item complete). That is NOT the whole
      premise the three refusals above rest on: a declared delta can make a scope fail only if it is
      still in the tree when the scope is measured AND git can see it there, and the refusal fires
      only when the plan can defeat NEITHER. `_judge_the_pair` answers each pair with one of three
      verdicts, and every shape below is measured (2026-11-09, 2026-11-10) and pinned by a test:
        - `PAIR_SATISFIABLE`, silently: a path `changes_within` cannot observe - one git excludes
          from all three of its changed sets (`_invisible_to_changes_within`) - cannot fail that
          predicate however it changed. A path git DOES track answers "not excluded" even when a
          pattern names it (tracked files are not subject to exclude rules), so for it the refusal
          stands unless one of the two cases below holds;
        - `PAIR_DECLINED`, announced with the frozen plan: item(s) in the window run a program whose
          argv NAMES the declared path (`_programs_naming`), or declare a change to a file that
          carries exclude rules (`_exclude_rule_writers`; `.gitignore`, `.git/info/exclude`). A
          program that reverts the delta and commits the revert, and one that leaves it, look
          identical in the plan; and whether an exclude rule hides the delta is decided by content
          the plan does not carry. So the audit declines and the run answers at `todo_done` - a
          `fail` carrying the predicate's own detail. What it trades away is written into the
          announcement: `["/bin/sh", "-c", "true app.py"]` NAMES `app.py` and disables the refusal
          for a plan that will then die, and a plan that writes its own `.gitignore` may write a
          rule that does not exclude the delta. Naming a path is compared by COMPONENTS
          (`git restore app.py` names it, `myapp.py` does not), and `git checkout -- .` names no
          declared path, so that undo shape is still refused. This is the audit's residual escape,
          measured rather than hidden: a freeze cannot read a program;
        - `PAIR_REFUSE`, the three sentences above, otherwise - and also when the path is EXCLUDED by
          git and a program in the window names it, because `git add -f` can make such a path
          visible to all three of `changes_within`'s sets and the plan does not say which program
          runs. That refusal says so rather than claiming no tree can satisfy the plan.
           An undo program that names NO declared path is refused even when it genuinely reverts the
           delta: `git checkout HEAD~1 -- . && echo o = 2 > other.py && git commit -qam undo`, against
           `files_changed ['app.py']` and `changes_within ['other.py']`, is a deliberate false refusal,
           and its price is that a freeze cannot read a program. Measured 2026-11-10: the freeze returns
           rc 2 (`invalid_plan`), while the identical argv replayed by hand - starting from a clean
           tree - leaves `changes_within`'s changed set at `['other.py']` and HEAD != base, so the judging
           item's `changes_within` would pass on that tree. That replayed tree is NOT clean: the undo
           commits only tracked files, so `git status --porcelain` prints `?? other.py` and the
           `git_delivered` predicate would refuse it. The false refusal is thus real for the judging
           item, and nothing here claims a delivery verdict. Relaxing this shape is a conscious change
           to the test that pins it.
      What the narrowings cost is measured on the shipped templates: `code_change` refuses `paths`
      outside `scope` in the item that binds BOTH halves (`implement`), and no program or
      exclude-rule write names a declared path in that window, so that refusal is untouched;
      `investigate` declares its report path in an item and binds the judging scope in `deliver`,
      with no program and no exclude-rule writer in the window, so its refusal is untouched too. A
      template that hangs a program naming the declared path off the declaring or judging item gets
      its verdict from the run.

    * The REPORT a binding names (`REPORT_ARG`; `command_exit`'s `report`, today the only one).
      It is judged by containment only, in two steps. First `_relative` - the plan's rule, and here
      the answering site, because `report` is not one of the keys `_binding` validates: a
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
    says which checks that cost - and which ones it did not: the THREE refusals that read the plan
    rather than the workspace (a duplicate spelling in one list, a path outside the workspace
    via `todo_plan._relative`, and a declared path no glob of a `changes_within` scope it is judged
    under matches - its own item's, one that item needs, or one declared before it and presented
    first) still run there and still refuse, so
    none is one of the checks a
    missing directory cost. The outside-workspace one is the backstop again: on the production route
    `TodoPlan.from_json` has already refused such a path before this audit is entered - workspace or
    not - and the audit's own `_relative` call is what answers only for a plan built without
    `from_json`. The report rule's PLAN half - the `_relative` call - is one of those
    plan-reading refusals and runs there too, and for a report the audit is the answering site on
    both routes. Its PARENT half reads the disk: a symlinked parent can
    only be resolved in a workspace that exists, so with no workspace there is nothing for it to find,
    and the audit does not claim it checked one.
    """
    workspace = Path(plan.workspace)
    measured = workspace.is_dir()
    declared: list[str] = []
    existing: list[str] = []
    created: list[str] = []
    # Pairs the audit declines to judge, announced with the frozen plan instead of refused: the plan's
    # own program or its own exclude-rule write can defeat a premise, and a freeze cannot read either.
    declined: list[str] = []
    # Every declared delta and every declared scope, kept per item and judged AFTER every item has
    # been read: a delta is constrained by the scopes it is judged under, and those are not all its
    # own item's (see the dependency pass below, which needs the whole plan in hand).
    deltas: dict[str, list[tuple[str, str, str]]] = {}       # item id -> (where, normalised path, predicate)
    scopes: dict[str, list[tuple[str, tuple[str, ...]]]] = {}   # item id -> (where, declared scope list)
    for item in plan.items:
        # The paths this ITEM requires to change, and the scopes it requires the whole change to fall
        # inside (the `code_change`/`docs_change` shape: `files_changed.paths` + `changes_within.scope`).
        # They constrain each other whenever the same item carries both, which is judged at the end of
        # this turn; both are ALSO collected per item id, because an item's delta is constrained by the
        # scopes of the items that need it as well (the dependency pass after this loop).
        item_delta: list[tuple[str, str, str]] = []      # (where, normalised path, delta predicate)
        item_scopes: list[tuple[str, tuple[str, ...]]] = []   # (where, declared scope list)
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
            # The scope this binding demands the whole change fall inside. `changes_within` is the
            # only predicate that takes one; its `scope` is a required non-empty list[str] (the
            # library types it in `_STR_LISTS`), so a frozen plan always arrives here with one.
            # `str(g)` per element is how the run builds its own patterns, so this reads the scope
            # exactly as the run will.
            if binding.predicate == "changes_within" and isinstance(binding.args.get("scope"), list):
                scope = tuple(str(g) for g in binding.args["scope"])
                if scope:
                    item_scopes.append((f"item {item.id}: changes_within.scope", scope))
            arg = DELTA_PATHS.get(binding.predicate)
            if arg is None or arg not in binding.args:
                continue
            where = f"item {item.id}: {binding.predicate}.{arg}"
            raw = binding.args[arg]
            seen: dict[str, str] = {}
            for value in (raw if isinstance(raw, list) else [raw]):
                # backstop, not the production answering site: `TodoPlan._binding`, inside
                # `TodoPlan.from_json`, applied this same rule to `path`/`paths` while the plan was
                # constructed, so on the route `core.todo_cli._plan_from_args` drives this is only
                # reached by a plan built without `from_json`. Kept so no plan escapes the one rule.
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
                item_delta.append((where, rel, binding.predicate))
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
        deltas.setdefault(item.id, []).extend(item_delta)
        scopes.setdefault(item.id, []).extend(item_scopes)
        # The pair, judged only when the SAME item declares both halves. A declared path no scope glob
        # matches is unsatisfiable in both directions at run time: `files_changed` requires it to differ
        # from its pre-state, `changes_within` fails on any changed path outside the scope. Every scope
        # the item binds must admit the path, since the item passes only when all its predicates pass.
        # SAME MATCHER AS THE RUN: `changes_within` builds its patterns with
        # `core.todo_runtime._scope_regex` and tests `rx.match(p)` against each changed path, so this
        # calls that imported function on the normalised declared path. Nothing about globbing is
        # restated here, so the freeze cannot disagree with the run about what a scope matches.
        if item_delta:
            for scope_where, scope in item_scopes:
                patterns = [_scope_regex(glob) for glob in scope]
                for where, rel, predicate in item_delta:
                    if any(pattern.match(rel) for pattern in patterns):
                        continue
                    verdict = _judge_the_pair(plan, workspace, item.id, item.id, rel)
                    if verdict.action == PAIR_SATISFIABLE:
                        continue    # git never lists it and nothing here can make it visible
                    if verdict.action == PAIR_DECLINED:
                        declined.append(_declined_note(where, rel, scope_where, scope, verdict))
                        continue
                    raise Refusal(
                        "invalid_plan",
                        _named_message(where, rel, scope_where, scope, verdict.programs) or
                        f"{where} declares {rel!r}, which matches no glob in the {scope_where} it must also "
                        f"satisfy: the declared scope is {list(scope)}. The same item requires every declared path "
                        f"to differ from its state at the start of the step ({predicate}) AND every path changed "
                        f"since the base to fall inside that scope (changes_within). A path no scope glob matches "
                        f"cannot satisfy both: changing it fails changes_within, and leaving it unchanged fails "
                        f"{predicate}. No tree can make this item pass as written, so the plan is refused here "
                        f"rather than frozen into an attempt that can only hang. Declare {rel!r} inside the scope, "
                        f"or add a scope glob that matches it",
                        item=item.id, predicate=predicate, path=rel, scope=list(scope))
    # The scope each item is judged under, against every declared delta that is ALREADY IN PLACE when
    # it is judged. Two things put a delta there, and each gets its own refusal so the message can say
    # which: the item's transitive `needs` (`begin` refuses an item whose `needs` have not passed), and
    # PLAN ORDER, which is the order the runtime presents items in.
    #
    # The harness-appended `deliver` item needs EVERY item and binds the plan's own `scope`, so every
    # plan is judged under that scope here: a declared path it cannot match (an `investigate` report
    # path outside `scope`) is refused at freeze instead of freezing a `deliver` item that can never
    # pass.
    #
    # PRESENTATION ORDER is the second half, and it is not decoration: `TodoRuntime.next_item` hands
    # out the first item, IN PLAN ORDER, whose needs have passed, and `needs` may only name items
    # declared EARLIER (`TodoPlan._item`). Induction on the plan makes the presentation order exactly
    # the plan order - the first item has nothing earlier to need, and every item's needs sit before
    # it, so each is presented (and completed) in turn - which is what the retained probe shows
    # (`change` is presented first even though `judge` does not need it). So an item declared later is
    # measured with every earlier item's declared change already in the tree, and a scope that
    # excludes one is unsatisfiable however the child behaves. Refusing it here needs the ORDER, not
    # the declaration: the same two items in the other order (narrower scope first, measured while the
    # tree is still clean) freeze and run, which is why this rule fires only on earlier declarations
    # and never on a later one.
    #
    # An item that NEEDS the declaring item is answered by the dependency pass below, with its own
    # sentence (`Item judge needs item change...`); the presentation pass skips exactly those owners
    # so one contradiction is never reported twice. Everything else declared earlier is the
    # presentation pass's.
    for position, item in enumerate(plan.items):
        item_scopes = scopes.get(item.id) or ()
        if not item_scopes:
            continue
        judged = _judged_before(plan, item.id)      # itself + transitive needs, in plan order
        # The owners the dependency pass does not answer: declared earlier, not needed.
        presented_before = [i.id for i in plan.items[:position] if i.id not in judged]
        for scope_where, scope in item_scopes:
            patterns = [_scope_regex(glob) for glob in scope]
            for owner in judged:
                if owner == item.id:
                    continue    # the item's own pair, refused inside its own turn above
                for where, rel, predicate in deltas.get(owner) or ():
                    if any(pattern.match(rel) for pattern in patterns):
                        continue
                    verdict = _judge_the_pair(plan, workspace, owner, item.id, rel)
                    if verdict.action == PAIR_SATISFIABLE:
                        continue    # invisible to changes_within: it cannot make this scope fail
                    if verdict.action == PAIR_DECLINED:
                        declined.append(_declined_note(where, rel, scope_where, scope, verdict))
                        continue
                    raise Refusal(
                        "invalid_plan",
                        _named_message(where, rel, scope_where, scope, verdict.programs) or
                        f"{where} declares {rel!r}, which matches no glob in the {scope_where} it must also "
                        f"satisfy: the declared scope is {list(scope)}. Item {item.id} needs item {owner}, so it is "
                        f"judged only after {owner} has passed - by which point {rel!r} is the change item {owner} "
                        f"requires ({predicate}) - and it then requires every path changed since the base to fall "
                        f"inside that scope (changes_within). The two cannot both hold of the delivered tree: with "
                        f"the change in place, changes_within fails on {rel!r}; with it undone, item {owner}'s "
                        f"{predicate} describes work that was never delivered. No tree can satisfy this plan as "
                        f"written, so it is refused here rather than frozen into an attempt that can only hang. "
                        f"Declare {rel!r} inside the scope, or add a scope glob that matches it",
                        item=item.id, declared_by=owner, predicate=predicate, path=rel, scope=list(scope))
            for owner in presented_before:
                for where, rel, predicate in deltas.get(owner) or ():
                    if any(pattern.match(rel) for pattern in patterns):
                        continue
                    verdict = _judge_the_pair(plan, workspace, owner, item.id, rel)
                    if verdict.action == PAIR_SATISFIABLE:
                        continue    # invisible to changes_within: it cannot make this scope fail
                    if verdict.action == PAIR_DECLINED:
                        # A program (or an exclude-rule write) in between can take the delta out of
                        # view; the plan does not say it does not, so the audit cannot call this plan
                        # unsatisfiable - it freezes and ANNOUNCES that (see the BOUNDARY).
                        declined.append(_declined_note(where, rel, scope_where, scope, verdict))
                        continue
                    raise Refusal(
                        "invalid_plan",
                        _named_message(where, rel, scope_where, scope, verdict.programs) or
                        f"{where} declares {rel!r}, which matches no glob in the {scope_where} it must also "
                        f"satisfy: the declared scope is {list(scope)}. Item {item.id} does not need item {owner}, "
                        f"but {owner} is declared EARLIER in the plan and the runtime presents a plan in that order "
                        f"- `next_item` hands out the first item, in plan order, whose needs have passed - so "
                        f"{owner} is completed, and {rel!r} is in the tree as the change it requires ({predicate}), "
                        f"before {item.id} is ever presented. {item.id} then requires every path changed since the "
                        f"base to fall inside its scope (changes_within), which no delivered tree satisfies: with "
                        f"{rel!r} changed, changes_within fails on it; with it undone, item {owner}'s {predicate} "
                        f"describes work that was never delivered. The plan is refused here rather than frozen into "
                        f"one whose later item can never pass. Declare {rel!r} inside the scope, add a scope glob "
                        f"that matches it, or declare {item.id} BEFORE {owner}, so {item.id} is measured while the "
                        f"tree is still clean",
                        item=item.id, declared_by=owner, predicate=predicate, path=rel, scope=list(scope))
    notes: list[str] = list(declined)
    if not measured:
        notes.append(
            f"workspace {plan.workspace} is not a directory, so the {len(declared)} declared path(s) {list(declared)} "
            f"could not be looked up: no directory, parent, non-regular-file or existence check was made about them, "
            f"and nothing was assumed about them (the refusals that read the PLAN still ran: a duplicate spelling of "
            f"one path in one list is still refused, and so is a path outside the workspace - neither needs a "
            f"directory to decide. So is a declared path that no glob of a changes_within scope it is judged "
            f"under matches, whether that scope belongs to its own item, to an item that needs it (deliver "
            f"included), or to an item declared after it - which the runtime presents after it, so it measures "
            f"with the change already in the tree: that rule reads the plan too)")
    elif created:
        notes.append(
            f"declared path(s) {list(created)} do not exist in workspace {plan.workspace} yet: the item requires "
            f"EVERY declared path to differ from its state at the start of the step, so this step will have to "
            f"CREATE them - a director who will never create one has frozen an item that cannot pass")
    return PathAudit(plan.workspace, measured, tuple(declared), tuple(existing), tuple(created), tuple(notes))
