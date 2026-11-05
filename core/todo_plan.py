"""A todo plan that is frozen when it is written, and the file that holds its state.

The director writes the plan once, at dispatch. This module is the half that
makes "once" mean something:

* `TodoPlan.from_json` validates the plan against the predicate library and
  refuses the WHOLE plan on any defect - an unknown predicate is a broken
  binding, never an absent measurement.
* `require_library_version` refuses a stored document that records no predicate
  library version, or one this build does not run, and it does so before a
  `TodoRun` exists to evaluate anything: the library was frozen with the plan.
* `plan_hash` is the sha256 of the canonical JSON of the frozen part (the plan,
  including the `deliver` item the harness appends). Re-creating the same attempt
  with the same plan is idempotent; with a different one it is refused.
* `PlanStore` keeps `<store>/<attempt_id>.json`, replaced atomically under a
  per-plan `flock`. Next to the frozen plan the file holds only OBSERVATIONS:
  per-item pre-state and latest results, notes, the session binding, and the
  `DeferralLedger` episodes - enough for a fresh process to rehydrate a
  `TodoRun` whose episodes keep their original `started_at`.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any, Callable, Iterator, Mapping

from core.gate_deferral import DeferralLedger
from core.todo_runtime import (Binding, Item, ItemReport, Plan, PredicateLibrary,
                               PredicateResult, TodoRun)

MAX_ITEMS = 12
MAX_PREDICATES = 4
DELIVER = "deliver"
MAX_NOTES = 20
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_TOP = {"attempt_id", "node_key", "context_hash", "base_sha", "workspace", "scope", "goal", "items"}
_ITEM = {"id", "title", "needs", "predicates"}


class Refusal(Exception):
    """A request the harness will not carry out. `code` is stable; `reason` is for humans."""

    def __init__(self, code: str, reason: str, **extra: Any):
        super().__init__(f"{code}: {reason}")
        self.code, self.reason, self.extra = code, reason, extra

    def as_json(self) -> dict:
        return {"refused": self.code, "reason": self.reason, **self.extra}


def _text(data: Mapping[str, Any], key: str, where: str, *, empty: bool = False) -> str:
    value = data.get(key)
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise Refusal("invalid_plan", f"{where}{key} must be a non-empty string")
    return value


def _relative(value: Any, where: str) -> None:
    path = PurePosixPath(str(value))
    if path.is_absolute() or ".." in path.parts:
        raise Refusal("invalid_plan", f"{where} must be a relative path inside the workspace, got {value!r}")


def _stored_parts(document: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """The two objects a load needs from a stored document, or a refusal - a file that is not a plan
    at all is damaged, which is a different finding from a plan whose library was never recorded."""
    plan, items = document.get("plan"), document.get("items")
    if not isinstance(plan, Mapping) or not isinstance(items, Mapping):
        raise Refusal("corrupt_plan", "the stored plan is damaged: 'plan' and 'items' must both be objects "
                                      f"(got plan={type(plan).__name__}, items={type(items).__name__})")
    return plan, items


def require_library_version(document: Mapping[str, Any], library: PredicateLibrary) -> str:
    """The predicate library a stored document was frozen under, or a refusal naming both.

    A plan binds predicates by NAME, so the library is part of what the plan means:
    the same document judged by another library is a different plan judged by other
    predicates. `new_document` records `library_version` for exactly this, and every
    load path asks here - BEFORE it builds a TodoRun, hence before any predicate can
    evaluate - because a silently re-versioned verdict is worse than no verdict.

    No `library_version` is NOT "probably the running one": nothing recorded which
    library froze the document, so there is nothing to compare and no honest default.
    """
    plan, _items = _stored_parts(document)
    attempt_id, node_key = plan.get("attempt_id"), plan.get("node_key")
    where = f"plan {attempt_id!r} at node {node_key!r}"
    recorded, running = document.get("library_version"), library.version
    identity = {"attempt_id": attempt_id, "node_key": node_key,
                "plan_library_version": recorded, "running_library_version": running}
    if not isinstance(recorded, str) or not recorded.strip():
        raise Refusal(
            "library_unmeasured",
            f"{where} records no library_version ({recorded!r}): which predicate library froze it was never "
            f"recorded, and this build runs v{running} - refused as unmeasured, not judged by an assumed library",
            **identity)
    if recorded != running:
        raise Refusal(
            "library_mismatch",
            f"{where} was frozen under predicate library v{recorded}; this build runs v{running}, whose "
            f"predicates are not the ones the plan bound - refused rather than re-judged",
            **identity)
    return recorded


@dataclass(frozen=True)
class TodoPlan:
    attempt_id: str
    node_key: str
    context_hash: str
    base_sha: str
    workspace: str
    scope: tuple[str, ...]
    goal: str
    items: tuple[Item, ...]  # the director's items followed by the appended `deliver`

    @classmethod
    def from_json(cls, data: Any, library: PredicateLibrary) -> "TodoPlan":
        if not isinstance(data, dict):
            raise Refusal("invalid_plan", "a plan is a JSON object")
        stray = sorted(set(data) - _TOP)
        missing = sorted(_TOP - set(data))
        if stray or missing:
            raise Refusal("invalid_plan", f"plan keys: unexpected {stray}, missing {missing}")
        attempt_id = _text(data, "attempt_id", "")
        if not _ID.match(attempt_id):
            raise Refusal("invalid_plan", "attempt_id must be 1-128 chars of [A-Za-z0-9._-], starting alphanumeric")
        node_key, context_hash, goal = (_text(data, k, "") for k in ("node_key", "context_hash", "goal"))
        base_sha = _text(data, "base_sha", "")
        if not _SHA.match(base_sha):
            raise Refusal("invalid_plan", "base_sha must be a full lowercase commit hash")
        workspace = _text(data, "workspace", "")
        if not os.path.isabs(workspace):
            raise Refusal("invalid_plan", "workspace must be an absolute path")
        scope = data["scope"]
        if not isinstance(scope, list) or not scope or not all(isinstance(g, str) and g for g in scope):
            raise Refusal("invalid_plan", "scope must be a non-empty list of glob strings")
        for glob in scope:
            _relative(glob, "scope entry")
        raw_items = data["items"]
        if not isinstance(raw_items, list):
            raise Refusal("invalid_plan", "items must be a list")
        if len(raw_items) > MAX_ITEMS:
            raise Refusal("too_many_items", f"{len(raw_items)} items; the limit is {MAX_ITEMS} (plus the appended deliver)")
        items: list[Item] = []
        for raw in raw_items:
            items.append(cls._item(raw, [i.id for i in items], library))
        items.append(Item(
            id=DELIVER, title="Deliver: commit the work, inside the declared scope",
            needs=tuple(i.id for i in items),
            predicates=(Binding("git_delivered", {"base": base_sha}),
                        Binding("changes_within", {"scope": list(scope)}))))
        return cls(attempt_id, node_key, context_hash, base_sha, workspace, tuple(scope), goal, tuple(items))

    @classmethod
    def from_stored(cls, data: Mapping[str, Any], library: PredicateLibrary) -> "TodoPlan":
        """Rebuild a plan from `as_json()`: the appended `deliver` is regenerated, not trusted."""
        items = [i for i in data.get("items", []) if isinstance(i, dict) and i.get("id") != DELIVER]
        return cls.from_json({**data, "items": items}, library)

    @staticmethod
    def _item(raw: Any, earlier: list[str], library: PredicateLibrary) -> Item:
        if not isinstance(raw, dict) or set(raw) - _ITEM or {"id", "title", "predicates"} - set(raw):
            raise Refusal("invalid_plan", f"an item is an object with keys {sorted(_ITEM)}: {str(raw)[:120]}")
        item_id = _text(raw, "id", "item ")
        if not _ID.match(item_id) or item_id == DELIVER or item_id in earlier:
            raise Refusal("invalid_plan", f"item id {item_id!r} must be unique, plain, and not {DELIVER!r}")
        title = _text(raw, "title", f"item {item_id}: ", empty=True)
        needs = raw.get("needs", [])
        if not isinstance(needs, list) or any(n not in earlier for n in needs):
            raise Refusal("invalid_plan", f"item {item_id}: needs must name items declared EARLIER; got {needs!r}")
        preds = raw["predicates"]
        if not isinstance(preds, list) or not preds:
            raise Refusal("invalid_plan", f"item {item_id}: an item needs at least one predicate; completion is observed")
        if len(preds) > MAX_PREDICATES:
            raise Refusal("too_many_predicates", f"item {item_id}: {len(preds)} predicates; the limit is {MAX_PREDICATES}")
        bindings = []
        for entry in preds:
            if not isinstance(entry, dict) or set(entry) - {"predicate", "args"} or "predicate" not in entry:
                raise Refusal("invalid_plan", f"item {item_id}: a predicate is {{predicate, args}}: {str(entry)[:120]}")
            args = entry.get("args", {})
            if not isinstance(args, dict):
                raise Refusal("invalid_plan", f"item {item_id}: args must be an object")
            binding = Binding(str(entry["predicate"]), args)
            try:
                library.check(binding)
            except KeyError as exc:
                raise Refusal("unknown_predicate",
                              f"item {item_id}: {binding.predicate!r} is not in the predicate library "
                              f"v{library.version} {library.names()}; a plan binds predicates, it cannot define one") from exc
            except ValueError as exc:
                raise Refusal("invalid_plan", f"item {item_id}: {exc}") from exc
            for key in ("path", "pattern", "paths"):
                if key in args:
                    for value in (args[key] if isinstance(args[key], list) else [args[key]]):
                        _relative(value, f"item {item_id}: {binding.predicate}.{key}")
            bindings.append(binding)
        return Item(item_id, title, tuple(bindings), tuple(needs))

    def as_json(self) -> dict:
        return {
            "attempt_id": self.attempt_id, "node_key": self.node_key, "context_hash": self.context_hash,
            "base_sha": self.base_sha, "workspace": self.workspace, "scope": list(self.scope), "goal": self.goal,
            "items": [{"id": i.id, "title": i.title, "needs": list(i.needs),
                       "predicates": [{"predicate": b.predicate, "args": dict(b.args)} for b in i.predicates]}
                      for i in self.items],
        }

    @property
    def plan_hash(self) -> str:
        canonical = json.dumps(self.as_json(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def runtime_plan(self) -> Plan:
        return Plan(key=self.attempt_id, goal=self.goal, items=self.items, base_sha=self.base_sha)


# -- the file ---------------------------------------------------------------

class PlanStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def path(self, attempt_id: str) -> Path:
        if not isinstance(attempt_id, str) or not _ID.match(attempt_id):
            raise Refusal("invalid_attempt", f"not a usable attempt id: {attempt_id!r}")
        return self.root / f"{attempt_id}.json"

    @contextmanager
    def lock(self, attempt_id: str) -> Iterator[None]:
        """Exclusive per-plan lock. A sidecar file: the plan file is replaced, its inode is not stable."""
        path = self.path(attempt_id)
        self.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)  # closing releases the flock

    def read(self, attempt_id: str) -> dict | None:
        path = self.path(attempt_id)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise Refusal("corrupt_plan", f"{path} cannot be read as a plan file: {str(exc)[:200]}") from exc
        if not isinstance(document, dict):
            raise Refusal("corrupt_plan", f"{path} is not a plan file")
        return document

    def write(self, attempt_id: str, document: Mapping[str, Any]) -> None:
        path = self.path(attempt_id)
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(document, handle, sort_keys=True, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        directory = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def attempts(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.json") if _ID.match(p.stem)) if self.root.is_dir() else []


def new_document(plan: TodoPlan, library: PredicateLibrary, now: float,
                 template: Mapping[str, Any] | None = None, params: Mapping[str, Any] | None = None) -> dict:
    """The file's initial content: the frozen plan, where it came from, and an empty observation record per item."""
    return {
        "schema": 1, "library_version": library.version, "plan": plan.as_json(), "plan_hash": plan.plan_hash,
        "template": dict(template) if template else None, "params": dict(params) if params is not None else None,
        "created_at": now, "child_session": None, "episodes": {},
        "items": {i.id: {"pre_state": None, "begun_at": None, "outcome": None, "unmeasurable": False,
                         "results": [], "observed_at": None, "notes": []} for i in plan.items},
    }


def item_state(entry: Mapping[str, Any]) -> str:
    """open | pass | fail | absent | unmeasurable - what the latest observation says."""
    if entry["outcome"] is None:
        return "open"
    return "unmeasurable" if entry["unmeasurable"] else entry["outcome"]


def load_run(document: Mapping[str, Any], plan: TodoPlan, library: PredicateLibrary,
             clock: Callable[[], float]) -> TodoRun:
    """Rehydrate a TodoRun, ledger episodes included, from the file's observations."""
    require_library_version(document, library)   # the library boundary: no run, so no predicate, before this
    entries = document["items"]
    reports = {
        item_id: ItemReport(item_id, e["outcome"], tuple(PredicateResult(**r) for r in e["results"]), e["unmeasurable"])
        for item_id, e in entries.items() if e["outcome"] is not None
    }
    pre_state = {item_id: e["pre_state"] for item_id, e in entries.items() if e["pre_state"] is not None}
    run = TodoRun(plan.runtime_plan(), library, Path(plan.workspace), ledger=DeferralLedger(),
                  clock=clock, pre_state=pre_state, reports=reports)
    for item_id, ep in document["episodes"].items():
        # note_absence keeps started_at as given; last_absence_at and the counters
        # are then restored over what a first sighting would have set.
        restored = run.ledger.note_absence(
            run.episode_key(item_id), now=ep["started_at"], gate=ep["gate"],
            report_key=tuple(ep["report_key"]) if ep["report_key"] is not None else None,
            gate_runs=ep["gate_runs"])
        restored.last_absence_at, restored.absences = ep["last_absence_at"], ep["absences"]
    return run


def save_observations(document: dict, plan: TodoPlan, run: TodoRun, now: float) -> None:
    """Copy the run's observations back into the document (the plan part is never touched)."""
    pre_states, reports = run.pre_states, run.reports
    for item in plan.items:
        entry = document["items"][item.id]
        if item.id in pre_states:
            if entry["pre_state"] is None:
                entry["begun_at"] = now
            entry["pre_state"] = pre_states[item.id]
        report = reports.get(item.id)
        if report is not None:
            entry.update(outcome=report.outcome, unmeasurable=report.unmeasurable,
                         results=[r.as_json() for r in report.results])
    document["episodes"] = {
        item.id: asdict(run.ledger.episodes[run.episode_key(item.id)])
        for item in plan.items if run.episode_key(item.id) in run.ledger.episodes
    }
