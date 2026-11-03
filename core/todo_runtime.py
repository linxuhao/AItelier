"""A todo list whose completion is observed rather than claimed.

The step pipeline spent its rounds on orchestration: a step graph with cycle
edges, a workflow config generated per run, and rework edges that turned a gate
that never ran into four rewrites. This module is the cheap half of its
replacement - the part that decides whether a step is DONE.

Two rules give it its point:

* An item is complete when a PREDICATE says so, evaluated against the workspace.
  The worker's own report is not an input to that decision.
* No item is ever required to be red before it runs. A step that modifies a file
  asserts the post-state, or a delta against the state captured when the step
  began; demanding redness first would make deleting the file the only way to
  satisfy the contract, and would push the worker towards cheating.

Predicates live in a versioned, project-independent library. A plan BINDS them -
it may not define one - so the portable workflow config survives: what varies per
task is the binding, never the predicate.

When a predicate cannot be measured at all (missing binary, busy gate, timeout),
that is not a failure. It is an absence, and it is handed to the existing
`DeferralLedger`, which already knows that an absence is a bounded EPISODE that
is retried cheaply and ends by naming itself. Nothing here invents a second
state machine for it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from glob import glob as _glob
import hashlib
import os
from pathlib import Path
import subprocess
import time
from typing import Any, Callable, Iterable, Literal, Mapping, Sequence

from core.gate_deferral import DeferralLedger

Outcome = Literal["pass", "fail", "absent"]


@dataclass(frozen=True)
class PredicateResult:
    """One predicate's verdict, with the evidence that produced it."""

    predicate: str
    outcome: Outcome
    detail: str

    def as_json(self) -> dict:
        return {"predicate": self.predicate, "outcome": self.outcome, "detail": self.detail}


@dataclass(frozen=True)
class Binding:
    """A plan's use of a library predicate. Never a definition."""

    predicate: str
    args: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Item:
    """One step. `needs` names items whose output this one reads."""

    id: str
    title: str
    predicates: tuple[Binding, ...]
    needs: tuple[str, ...] = ()
    scope: tuple[str, ...] = ()


@dataclass(frozen=True)
class Plan:
    key: str
    goal: str
    items: tuple[Item, ...]

    def item(self, item_id: str) -> Item:
        for entry in self.items:
            if entry.id == item_id:
                return entry
        raise KeyError(f"no item {item_id!r} in plan {self.key!r}")


class PredicateLibrary:
    """Versioned, project-independent predicates. Plans bind; they do not add."""

    def __init__(self, *, version: str = "1"):
        self.version = version
        self._definitions: dict[str, Callable[..., PredicateResult]] = {}

    def define(self, name: str) -> Callable[[Callable[..., PredicateResult]], Callable[..., PredicateResult]]:
        def register(fn: Callable[..., PredicateResult]) -> Callable[..., PredicateResult]:
            if name in self._definitions:
                raise ValueError(f"predicate {name!r} is already defined; the library is versioned, not per-task")
            self._definitions[name] = fn
            return fn

        return register

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._definitions))

    def evaluate(self, binding: Binding, *, workspace: Path, pre_state: Mapping[str, Any]) -> PredicateResult:
        fn = self._definitions.get(binding.predicate)
        if fn is None:
            # A plan referring to a predicate nobody defined is a broken binding,
            # not an absent measurement: failing loudly here is the whole point
            # of keeping definitions out of plans.
            raise KeyError(f"unknown predicate {binding.predicate!r}; library has {self.names()}")
        return fn(workspace=workspace, pre_state=pre_state, **dict(binding.args))


def _digest(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def capture_pre_state(workspace: Path, bindings: Iterable[Binding]) -> dict[str, Any]:
    """State the write predicates will be compared against.

    Captured when the item BEGINS, so `file_changed` can be a delta without ever
    demanding that the file be absent or wrong beforehand.
    """
    captured: dict[str, Any] = {}
    for binding in bindings:
        if binding.predicate == "file_changed":
            rel = str(binding.args["path"])
            captured[rel] = _digest(Path(workspace) / rel)
    return captured


def builtin_library() -> PredicateLibrary:
    library = PredicateLibrary(version="1")

    @library.define("files_present")
    def files_present(*, workspace: Path, pre_state: Mapping[str, Any], pattern: str) -> PredicateResult:
        hits = [p for p in _glob(str(Path(workspace) / pattern), recursive=True) if os.path.isfile(p)]
        return PredicateResult(
            "files_present", "pass" if hits else "fail",
            f"{len(hits)} file(s) match {pattern!r}" + ("" if hits else " - nothing on disk"),
        )

    @library.define("file_changed")
    def file_changed(*, workspace: Path, pre_state: Mapping[str, Any], path: str,
                     contains: str | None = None) -> PredicateResult:
        rel = str(path)
        if rel not in pre_state:
            raise ValueError(f"file_changed({rel!r}) needs a pre-state; capture it when the item begins")
        target = Path(workspace) / rel
        after = _digest(target)
        before = pre_state[rel]
        if after is None:
            return PredicateResult("file_changed", "fail", f"{rel} does not exist after the step")
        if after == before:
            return PredicateResult("file_changed", "fail", f"{rel} is byte-identical to the state captured at step start")
        if contains is not None and contains not in target.read_text(errors="replace"):
            # A change that is not the INTENDED change is still a failure: any
            # spurious edit would otherwise satisfy a bare delta.
            return PredicateResult("file_changed", "fail", f"{rel} changed but does not contain {contains!r}")
        return PredicateResult("file_changed", "pass", f"{rel} changed as intended")

    @library.define("command_exit")
    def command_exit(*, workspace: Path, pre_state: Mapping[str, Any], argv: Sequence[str],
                     expect: int = 0, timeout: float | None = None) -> PredicateResult:
        try:
            done = subprocess.run(list(argv), cwd=str(workspace), capture_output=True,
                                  text=True, timeout=timeout)
        except FileNotFoundError:
            return PredicateResult("command_exit", "absent", f"{argv[0]!r} is not installed here")
        except subprocess.TimeoutExpired:
            return PredicateResult("command_exit", "absent", f"{' '.join(argv)} timed out after {timeout}s")
        if done.returncode == expect:
            return PredicateResult("command_exit", "pass", f"exit {done.returncode} as expected")
        tail = (done.stderr or done.stdout or "").strip().splitlines()[-1:] or [""]
        return PredicateResult("command_exit", "fail", f"exit {done.returncode}, expected {expect}: {tail[0][:200]}")

    return library


@dataclass
class ItemReport:
    item_id: str
    outcome: Outcome
    results: tuple[PredicateResult, ...] = ()

    @property
    def complete(self) -> bool:
        return self.outcome == "pass"


class TodoRun:
    """Executes the plan's OBSERVATION half. The worker still does the work."""

    def __init__(self, plan: Plan, library: PredicateLibrary, workspace: Path, *,
                 ledger: DeferralLedger | None = None, clock: Callable[[], float] | None = None):
        self.plan = plan
        self.library = library
        self.workspace = Path(workspace)
        self.ledger = ledger if ledger is not None else DeferralLedger()
        self._clock = clock or time.time
        self._pre_state: dict[str, dict[str, Any]] = {}
        self._reports: dict[str, ItemReport] = {}

    # -- lifecycle -------------------------------------------------------
    def begin(self, item_id: str) -> Item:
        """Mark the item as started and capture what its predicates compare to."""
        item = self.plan.item(item_id)
        self._pre_state.setdefault(item_id, capture_pre_state(self.workspace, item.predicates))
        return item

    def observe(self, item_id: str) -> ItemReport:
        """Evaluate the item's predicates against the workspace, right now."""
        item = self.plan.item(item_id)
        pre_state = self._pre_state.get(item_id)
        if pre_state is None:
            raise RuntimeError(f"observe({item_id!r}) before begin(): no pre-state to compare against")
        results = tuple(
            self.library.evaluate(binding, workspace=self.workspace, pre_state=pre_state)
            for binding in item.predicates
        )
        outcomes = {r.outcome for r in results}
        key = self._key(item_id)
        if "absent" in outcomes:
            # Not a failure: hand the episode to the ledger that already models
            # absence as bounded, cheaply retried, and self-naming.
            self.ledger.note_absence(key, now=self._clock(),
                                     gate=next(r.predicate for r in results if r.outcome == "absent"))
            if self.ledger.expired(key, now=self._clock()):
                report = ItemReport(item_id, "absent", results)
                self._reports[item_id] = report
                return report
            return ItemReport(item_id, "absent", results)
        self.ledger.clear(key)
        report = ItemReport(item_id, "fail" if "fail" in outcomes else "pass", results)
        self._reports[item_id] = report
        return report

    # -- resume ----------------------------------------------------------
    def next_item(self) -> Item | None:
        """First item whose needs are met and which is not yet observed complete."""
        done = {iid for iid, r in self._reports.items() if r.complete}
        for item in self.plan.items:
            if item.id in done:
                continue
            if all(need in done for need in item.needs):
                return item
        return None

    def finished(self) -> bool:
        return self.next_item() is None

    def terminal_reason(self, item_id: str) -> str:
        """The sentence an expired absence ends with - the ledger owns the words."""
        return self.ledger.terminal_reason(self._key(item_id))

    def report(self) -> dict:
        """Resume view: what is done, what is next, what is still unmeasured."""
        return {
            "plan": self.plan.key,
            "done": [i.id for i in self.plan.items if self._reports.get(i.id) and self._reports[i.id].complete],
            "next": (self.next_item().id if self.next_item() else None),
            "deferred": [i.id for i in self.plan.items if self._key(i.id) in self.ledger.episodes],
        }

    def brief(self, *, max_chars: int = 600) -> str:
        """The compact-surviving summary: ids and one line each, never the catalogue."""
        remaining = [i for i in self.plan.items if not (self._reports.get(i.id) and self._reports[i.id].complete)]
        head = f"{self.plan.goal}\nNext: {self.next_item().id if self.next_item() else '(none)'}" 
        lines = [head, f"Remaining {len(remaining)} item(s): " + ", ".join(i.id for i in remaining)]
        text = "\n".join(lines)
        return text[:max_chars]

    def _key(self, item_id: str) -> str:
        return f"{self.plan.key}/{item_id}"
