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
import inspect
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Callable, Iterable, Literal, Mapping, Sequence

from core.gate_deferral import DeferralLedger

Outcome = Literal["pass", "fail", "absent"]

#: What each argument NAME must hold, checked when a plan is created so that a
#: broken binding (`argv` given as a string) is refused instead of measuring as
#: an absence weeks later. Argument names mean the same thing in every predicate.
_STR_LISTS = {"argv", "scope", "require_keys", "paths"}
_STRINGS = {"path", "pattern", "contains", "base"}


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
    # The commit the work started from. `evaluate` hands it to a predicate that
    # declares a `base_sha` parameter (`changes_within`); a binding cannot set it.
    base_sha: str = ""

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

    def check(self, binding: Binding) -> None:
        """Refuse a binding the library cannot evaluate, before any work starts.

        An unknown name raises KeyError; an argument the predicate does not take
        (or a required one that is missing) raises ValueError.
        """
        fn = self._definitions.get(binding.predicate)
        if fn is None:
            raise KeyError(f"unknown predicate {binding.predicate!r}; library has {self.names()}")
        params = {name: p for name, p in inspect.signature(fn).parameters.items()
                  if name not in ("workspace", "pre_state", "base_sha")}
        unknown = sorted(set(binding.args) - set(params))
        missing = sorted(n for n, p in params.items() if p.default is p.empty and n not in binding.args)
        if unknown or missing:
            raise ValueError(f"{binding.predicate}: unknown argument(s) {unknown}, missing argument(s) {missing}")
        for name, value in binding.args.items():
            ok = True
            if name == "require_keys":
                ok = isinstance(value, list) and all(isinstance(v, str) for v in value)
            elif name in _STR_LISTS:
                ok = isinstance(value, list) and bool(value) and all(isinstance(v, str) and v for v in value)
            elif name in _STRINGS:
                ok = isinstance(value, str) and bool(value)
            elif name == "expect":
                ok = type(value) is int
            elif name == "timeout":
                ok = value is None or (type(value) in (int, float) and value > 0)
            if not ok:
                raise ValueError(f"{binding.predicate}.{name} has the wrong type or is empty: {value!r}")

    def evaluate(self, binding: Binding, *, workspace: Path, pre_state: Mapping[str, Any],
                 base_sha: str = "") -> PredicateResult:
        fn = self._definitions.get(binding.predicate)
        if fn is None:
            # A plan referring to a predicate nobody defined is a broken binding,
            # not an absent measurement: failing loudly here is the whole point
            # of keeping definitions out of plans.
            raise KeyError(f"unknown predicate {binding.predicate!r}; library has {self.names()}")
        extra = {"base_sha": base_sha} if "base_sha" in inspect.signature(fn).parameters else {}
        return fn(workspace=workspace, pre_state=pre_state, **extra, **dict(binding.args))


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
            rels = [str(binding.args["path"])]
        elif binding.predicate == "files_changed":
            rels = [str(p) for p in binding.args["paths"]]
        else:
            continue
        for rel in rels:
            captured[rel] = _digest(Path(workspace) / rel)
    return captured


def _safe_rev(rev: Any) -> str:
    """A revision that cannot be read by git as an option."""
    text = str(rev)
    if not text or text.startswith("-"):
        raise ValueError(f"not a usable revision: {text!r}")
    return text


def _git_status(workspace: Path, *args: str) -> tuple[int, str, str]:
    """Run git in the workspace: (exit code, stdout, "") or (-1, "", why it could not run)."""
    if not Path(workspace).is_dir():
        return -1, "", f"workspace {workspace} does not exist"
    try:
        done = subprocess.run(["git", "-C", str(workspace), *args], capture_output=True,
                              text=True, timeout=60)
    except FileNotFoundError:
        return -1, "", "'git' is not installed here"
    except subprocess.TimeoutExpired:
        return -1, "", f"git {args[0]} timed out after 60s"
    return done.returncode, done.stdout, (done.stderr or done.stdout or "").strip()


def _git(workspace: Path, *args: str) -> tuple[str, str]:
    """(stdout, "") or ("", why it could not be measured).

    Git being missing, slow, or unable to read the repository is an absence, not
    a verdict on the work.
    """
    code, out, err = _git_status(workspace, *args)
    if code == -1:
        return "", err
    if code:
        return "", f"git {args[0]} exited {code}: {(err.splitlines() or [''])[-1][:200]}"
    return out, ""


def _scope_regex(pattern: str) -> "re.Pattern[str]":
    if pattern in (".", "./"):
        pattern = "**"                      # the whole workspace
    if pattern.endswith("/"):
        pattern += "**"
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def _file_delta(target: Path, rel: str, before: str | None, contains: str | None) -> str:
    """Why `rel` is not changed as intended, or "" when it is. Never asks for a bad starting point."""
    after = _digest(target)
    if after is None:
        return f"{rel} does not exist after the step"
    if after == before:
        return f"{rel} is byte-identical to the state captured at step start"
    if contains is not None and contains not in target.read_text(errors="replace"):
        # A change that is not the INTENDED change is still a failure: any
        # spurious edit would otherwise satisfy a bare delta.
        return f"{rel} changed but does not contain {contains!r}"
    return ""


def builtin_library() -> PredicateLibrary:
    library = PredicateLibrary(version="2")

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
        problem = _file_delta(Path(workspace) / rel, rel, pre_state[rel], contains)
        return PredicateResult("file_changed", "fail", problem) if problem else \
            PredicateResult("file_changed", "pass", f"{rel} changed as intended")

    @library.define("files_changed")
    def files_changed(*, workspace: Path, pre_state: Mapping[str, Any], paths: Sequence[str],
                      contains: str | None = None) -> PredicateResult:
        """file_changed for N files in one binding: EVERY path must differ from its state at the
        start of the step (and carry `contains`, when given). Names the first path that does not."""
        for rel in map(str, paths):
            if rel not in pre_state:
                raise ValueError(f"files_changed({rel!r}) needs a pre-state; capture it when the item begins")
            problem = _file_delta(Path(workspace) / rel, rel, pre_state[rel], contains)
            if problem:
                return PredicateResult("files_changed", "fail", problem)
        return PredicateResult("files_changed", "pass", f"all {len(paths)} file(s) changed as intended")

    @library.define("command_exit")
    def command_exit(*, workspace: Path, pre_state: Mapping[str, Any], argv: Sequence[str],
                     expect: int = 0, timeout: float | None = None) -> PredicateResult:
        if not Path(workspace).is_dir():
            return PredicateResult("command_exit", "absent", f"workspace {workspace} does not exist")
        try:
            done = subprocess.run(list(argv), cwd=str(workspace), capture_output=True,
                                  text=True, timeout=timeout)
        except FileNotFoundError:
            return PredicateResult("command_exit", "absent", f"{argv[0]!r} is not installed here")
        except subprocess.TimeoutExpired:
            return PredicateResult("command_exit", "absent", f"{' '.join(argv)} timed out after {timeout}s")
        except OSError as exc:
            return PredicateResult("command_exit", "absent", f"{argv[0]!r} could not be run: {exc}")
        if done.returncode == expect:
            return PredicateResult("command_exit", "pass", f"exit {done.returncode} as expected")
        tail = (done.stderr or done.stdout or "").strip().splitlines()[-1:] or [""]
        return PredicateResult("command_exit", "fail", f"exit {done.returncode}, expected {expect}: {tail[0][:200]}")

    @library.define("changes_within")
    def changes_within(*, workspace: Path, pre_state: Mapping[str, Any], base_sha: str,
                       scope: Sequence[str]) -> PredicateResult:
        """Every path changed since the plan's base commit matches a scope glob.

        The base commit is the plan's `base_sha`, handed in by `evaluate`; a binding
        cannot set it. "Changed" is the union of `git diff base..HEAD`, uncommitted
        changes to tracked files, and untracked files. Globs: `*` stays inside one
        path segment, `**` crosses segments, a trailing `/` means everything under.
        """
        base = base_sha
        if not base:
            raise ValueError("changes_within needs the plan's base_sha")
        changed: set[str] = set()
        for args in (("diff", "--name-only", "--no-renames", "-z", f"{_safe_rev(base)}..HEAD"),
                     ("diff", "--name-only", "--no-renames", "-z", "HEAD"),
                     ("ls-files", "--others", "--exclude-standard", "-z")):
            out, absence = _git(workspace, *args)
            if absence:
                return PredicateResult("changes_within", "absent", absence)
            changed.update(p for p in out.split("\0") if p)
        patterns = [_scope_regex(str(g)) for g in scope]
        outside = sorted(p for p in changed if not any(rx.match(p) for rx in patterns))
        if outside:
            more = f" (+{len(outside) - 5} more)" if len(outside) > 5 else ""
            return PredicateResult("changes_within", "fail",
                                   f"{len(outside)} changed path(s) outside scope {list(scope)}: "
                                   f"{', '.join(outside[:5])}{more}")
        return PredicateResult("changes_within", "pass", f"{len(changed)} changed path(s), all inside scope")

    @library.define("git_delivered")
    def git_delivered(*, workspace: Path, pre_state: Mapping[str, Any], base: str) -> PredicateResult:
        """HEAD is a descendant of `base`, is not `base`, and the working tree is clean.

        A branch reset behind the base, or one that diverged from it, is not a delivery.
        """
        base_commit, absence = _git(workspace, "rev-parse", "--verify", "--quiet", f"{_safe_rev(base)}^{{commit}}")
        if absence:
            return PredicateResult("git_delivered", "absent", absence)
        head, absence = _git(workspace, "rev-parse", "HEAD")
        if absence:
            return PredicateResult("git_delivered", "absent", absence)
        dirty, absence = _git(workspace, "status", "--porcelain")
        if absence:
            return PredicateResult("git_delivered", "absent", absence)
        code, _, err = _git_status(workspace, "merge-base", "--is-ancestor", base_commit.strip(), "HEAD")
        if code not in (0, 1):
            return PredicateResult("git_delivered", "absent", f"git merge-base exited {code}: {err[:200]}")
        problems = []
        if head.strip() == base_commit.strip():
            problems.append(f"HEAD is still the base commit {base_commit.strip()[:12]}")
        elif code == 1:
            problems.append(f"HEAD {head.strip()[:12]} is not a descendant of the base commit {base_commit.strip()[:12]}")
        if dirty.strip():
            problems.append(f"working tree is not clean: {len(dirty.splitlines())} uncommitted path(s)")
        if problems:
            return PredicateResult("git_delivered", "fail", "; ".join(problems))
        return PredicateResult("git_delivered", "pass", f"HEAD {head.strip()[:12]} is past the base and the tree is clean")

    @library.define("report_json")
    def report_json(*, workspace: Path, pre_state: Mapping[str, Any], path: str,
                    require_keys: Sequence[str]) -> PredicateResult:
        """The file is a JSON object with the keys: structure present, not a verdict on its content."""
        target = Path(workspace) / str(path)
        if not target.is_file():
            return PredicateResult("report_json", "fail", f"{path} does not exist")
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            return PredicateResult("report_json", "fail", f"{path} is not valid JSON: {str(exc)[:120]}")
        if not isinstance(data, dict):
            return PredicateResult("report_json", "fail", f"{path} is not a JSON object")
        missing = [k for k in require_keys if k not in data]
        if missing:
            return PredicateResult("report_json", "fail", f"{path} lacks key(s) {missing}")
        return PredicateResult("report_json", "pass", f"{path} carries every required key")

    return library


@dataclass
class ItemReport:
    item_id: str
    outcome: Outcome
    results: tuple[PredicateResult, ...] = ()
    # Terminal for the dispatch loop: the absence outlived the ledger's ceiling, so
    # the item is handed back by name instead of being retried forever. A later
    # measurement that does reach a verdict still replaces it.
    unmeasurable: bool = False

    @property
    def complete(self) -> bool:
        return self.outcome == "pass"


class TodoRun:
    """Executes the plan's OBSERVATION half. The worker still does the work."""

    def __init__(self, plan: Plan, library: PredicateLibrary, workspace: Path, *,
                 ledger: DeferralLedger | None = None, clock: Callable[[], float] | None = None,
                 pre_state: Mapping[str, Mapping[str, Any]] | None = None,
                 reports: Mapping[str, ItemReport] | None = None):
        self.plan = plan
        self.library = library
        self.workspace = Path(workspace)
        self.ledger = ledger if ledger is not None else DeferralLedger()
        self._clock = clock or time.time
        # `pre_state` and `reports` let a fresh process resume a run it did not start.
        self._pre_state: dict[str, dict[str, Any]] = {k: dict(v) for k, v in (pre_state or {}).items()}
        self._reports: dict[str, ItemReport] = dict(reports or {})

    @property
    def pre_states(self) -> dict[str, dict[str, Any]]:
        return {k: dict(v) for k, v in self._pre_state.items()}

    @property
    def reports(self) -> dict[str, ItemReport]:
        return dict(self._reports)

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
        key = self.episode_key(item_id)
        now = self._clock()
        stored = self._reports.get(item_id)
        episode = self.ledger.episodes.get(key)
        if (stored is not None and stored.outcome == "absent" and not stored.unmeasurable
                and episode is not None and self.ledger.deferring(key, now=now)):
            # Inside the wait the gate is not asked again: the stored absence is read
            # a second time. Same report, so the wait does not restart and no gate
            # run is counted - exactly what the ledger does for a re-read report.
            self.ledger.note_absence(key, now=now, gate=episode.gate, report_key=episode.report_key)
            return stored
        results = tuple(
            self.library.evaluate(binding, workspace=self.workspace, pre_state=pre_state,
                                  base_sha=self.plan.base_sha)
            for binding in item.predicates
        )
        outcomes = {r.outcome for r in results}
        if "fail" not in outcomes and "absent" in outcomes:
            # Not a failure: hand the episode to the ledger that already models
            # absence as bounded, cheaply retried, and self-naming. One execution is
            # one report, so a re-measurement after the wait is a new report: it
            # restarts the wait and counts one gate run.
            absent = [r for r in results if r.outcome == "absent"]
            self.ledger.note_absence(
                key, now=now, gate=absent[0].predicate,
                report_key=(*(r.predicate for r in absent), f"measured@{now!r}"))
            report = ItemReport(item_id, "absent", results, unmeasurable=self.ledger.expired(key, now=now))
            self._reports[item_id] = report
            return report
        # A measured failure goes back to the worker, whatever else could not be
        # measured: it is a verdict, which an absence is not.
        self.ledger.clear(key)
        report = ItemReport(item_id, "fail" if "fail" in outcomes else "pass", results)
        self._reports[item_id] = report
        return report

    # -- resume ----------------------------------------------------------
    def next_item(self) -> Item | None:
        """First item whose needs are met, which is not observed complete and not unmeasurable."""
        done = {iid for iid, r in self._reports.items() if r.complete}
        for item in self.plan.items:
            if item.id in done or self._is_unmeasurable(item.id):
                continue
            if all(need in done for need in item.needs):
                return item
        return None

    def finished(self) -> bool:
        return all(self._is_complete(i.id) for i in self.plan.items)

    def terminal_reason(self, item_id: str) -> str:
        """The sentence an expired absence ends with - the ledger owns the words."""
        return self.ledger.terminal_reason(self.episode_key(item_id))

    def report(self) -> dict:
        """Resume view: what is done, what is next, what is still unmeasured."""
        nxt = self.next_item()
        return {
            "plan": self.plan.key,
            "done": [i.id for i in self.plan.items if self._is_complete(i.id)],
            "next": nxt.id if nxt else None,
            "deferred": [i.id for i in self.plan.items
                         if self.episode_key(i.id) in self.ledger.episodes and not self._is_unmeasurable(i.id)],
            "unmeasurable": [{"id": i.id, "reason": self.terminal_reason(i.id)}
                             for i in self.plan.items if self._is_unmeasurable(i.id)],
        }

    def brief(self, *, max_chars: int = 600) -> str:
        """The compact-surviving summary: ids and one line each, never the catalogue."""
        remaining = [i for i in self.plan.items if not self._is_complete(i.id)]
        nxt = self.next_item()
        lines = [f"{self.plan.goal[:160]}\nNext: {nxt.id if nxt else '(none)'}"]
        unmeasurable = self.report()["unmeasurable"]
        if unmeasurable:
            lines.append("Unmeasurable: " + "; ".join(f"{u['id']} ({u['reason']})" for u in unmeasurable))
        lines.append(f"Remaining {len(remaining)} item(s): " + ", ".join(i.id for i in remaining))
        text = "\n".join(lines)
        return text[:max_chars]

    def episode_key(self, item_id: str) -> str:
        return f"{self.plan.key}/{item_id}"

    def _is_complete(self, item_id: str) -> bool:
        report = self._reports.get(item_id)
        return bool(report and report.complete)

    def _is_unmeasurable(self, item_id: str) -> bool:
        report = self._reports.get(item_id)
        return bool(report and report.unmeasurable)
