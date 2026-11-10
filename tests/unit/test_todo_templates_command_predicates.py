"""`COMMAND_PREDICATES` is MEASURED here, against the library's real registration table.

The freeze audit (`core.todo_templates.check_declared_paths`, through `_judge_the_pair`) withholds a
refusal when an item runs a program that could revert a declared delta: an argv that NAMES the
declared path makes the pair unjudgeable (`PAIR_DECLINED`, announced), and for a delta git excludes
it keeps the refusal (`git add -f` could expose it). That judgement rests on one fact - WHICH
predicates spawn a process whose argv words came out of the plan - and the fact is a hand-kept
literal, `core.todo_templates.COMMAND_PREDICATES = ("command_exit",)`. Nothing measured the literal,
so the next program-running predicate would have joined the library silently: its argv would name
nothing to `_programs_naming`, and the audit would REFUSE (`invalid_plan`) a plan the plan's own
program makes satisfiable - the over-refusal the audit exists to end - with every other test green
(iss-f7d7793371a34bec).

`test_every_program_running_predicate_is_registered_as_one` is what measures it. The test walks the
REAL registration table (`builtin_library()`, name -> implementation, so a predicate nobody told this
file about is still driven), hands every implementation type-directed sentinel arguments, and lets the
library's own `evaluate` path run it with the program-spawning entry points replaced by recorders - so
nothing executes. What it then requires is that the predicates whose spawned program is built from
binding words are EXACTLY `COMMAND_PREDICATES`, naming whatever disagrees in either direction, and
that their words arrive in `argv` - the argument `_argv_of` reads.

The freeze arms at the bottom pin what the set governs through the real CLI (`create --template`):
the shapes that must keep freezing, and the two that must keep being refused.
"""
from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Callable

import pytest

from core.todo_runtime import Binding, Item, PredicateLibrary, builtin_library
from core.todo_templates import COMMAND_PREDICATES, _argv_of
# The CLI harness is shared with the free-plan tests: `create --template` is driven exactly as the
# director's tool drives it, against a real git repository, in a fresh process per call.
from tests.unit.test_todo_plan_cli import Cli, base, cli, commit_all, repo  # noqa: F401

#: Filled by `PredicateLibrary.evaluate`, never by a plan: a probe must not hand these in.
_INFRASTRUCTURE = ("workspace", "pre_state", "base_sha")
#: Prefix of every probe value, so a recorded argv word can be traced back to the argument it came from.
_PROBE = "aitelier-probe-"
#: The binding argument `core.todo_runtime._STR_LISTS` fixes for a program's argv, and what `_argv_of` reads.
_ARGV = "argv"


def _sentinel(arg: str, index: int = 0) -> str:
    return f"{_PROBE}{arg}-{index}"


def _registrations(library: PredicateLibrary) -> dict[str, Callable[..., Any]]:
    """The table the library dispatches through: registered name -> implementation.

    Read through the attribute `check`/`evaluate` themselves read. It is private, and that is on
    purpose: this test exists to walk whatever IS registered, so it must fail loudly if the library
    ever keeps its definitions somewhere else - rather than silently measure an empty table and pass.
    """
    table = getattr(library, "_definitions", None)
    assert isinstance(table, dict) and table, (
        f"PredicateLibrary no longer holds its definitions in `_definitions` (found {table!r}): this test "
        f"measures THAT table, so it cannot be allowed to pass while walking nothing")
    return table


def _probe_args(fn: Callable[..., Any]) -> dict[str, Any]:
    """One binding argument per parameter, each carrying a SENTINEL a spawn can be traced through.

    Type-directed, from what each predicate already declares: a `Sequence[str]` parameter gets a LIST
    of sentinels (a program the plan names is a list of words - the rule `_STR_LISTS` fixes for `argv`),
    an int a 0, a float a 1.0, and anything else a sentinel string. Sentinels are unique per argument
    name, so a recorded argv says WHICH binding argument its words came out of.
    """
    args: dict[str, Any] = {}
    for name, param in inspect.signature(fn).parameters.items():
        if name in _INFRASTRUCTURE:
            continue
        kind = str(param.annotation)
        if "Sequence" in kind or "list" in kind:
            args[name] = [_sentinel(name, 0), _sentinel(name, 1)]
        elif "int" in kind:
            args[name] = 0
        elif "float" in kind:
            args[name] = 1.0
        else:
            args[name] = _sentinel(name, 0)
    return args


class _FakeProc:
    """A started process that is ALREADY finished, so nothing is waited on and nothing is signalled.

    `poll()` answers 0 for exactly that reason: `todo_runtime._run_in_own_group` kills the process
    GROUP in its `finally` when `poll()` is None, and a fake pid must never reach `os.killpg`. There
    is deliberately no `pid` attribute - an accidental call raises instead of signalling a real group.
    """

    def __init__(self, argv: list[str]):
        self.args = list(argv)
        self.returncode = 0
        self.stdout = ""
        self.stderr = ""

    def communicate(self, timeout=None, input=None):  # noqa: A002 - the signature subprocess calls with
        return self.stdout, self.stderr

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


class _Spawns:
    """Every process a predicate tries to START, recorded instead of run."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def _record(self, argv) -> list[str]:
        words = [str(w) for w in (argv if isinstance(argv, (list, tuple)) else [argv])]
        self.calls.append(words)
        return words

    @staticmethod
    def _first(args, kwargs):
        return args[0] if args else kwargs.get("args", kwargs.get("argv", kwargs.get("command", [])))

    def popen(self, *args, **kwargs):
        return _FakeProc(self._record(self._first(args, kwargs)))

    def run(self, *args, **kwargs):
        return subprocess.CompletedProcess(self._record(self._first(args, kwargs)), 0, "", "")

    def call(self, *args, **kwargs):
        self._record(self._first(args, kwargs))
        return 0

    def check_output(self, *args, **kwargs):
        self._record(self._first(args, kwargs))
        return ""

    def system(self, command):
        self._record(command)
        return 0

    def execv(self, program, argv, *args, **kwargs):
        self._record([program, *argv])
        return 0

    def execvp(self, program, argv, *args, **kwargs):
        self._record([program, *argv])
        return 0

    def spawnv(self, mode, program, argv, *args, **kwargs):
        self._record([program, *argv])
        return 0

    def spawnvp(self, mode, program, argv, *args, **kwargs):
        self._record([program, *argv])
        return 0


@pytest.fixture
def spawns(monkeypatch) -> _Spawns:
    """Replace every program-spawning entry point with a recorder, for the length of one test."""
    recorder = _Spawns()
    monkeypatch.setattr(subprocess, "Popen", recorder.popen)
    monkeypatch.setattr(subprocess, "run", recorder.run)
    monkeypatch.setattr(subprocess, "call", recorder.call)
    monkeypatch.setattr(subprocess, "check_output", recorder.check_output)
    for name in ("system", "execv", "execvp", "spawnv", "spawnvp"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, getattr(recorder, name))
    return recorder


def _program_arg(argv: list[str], sentinels: dict[str, Any]) -> str | None:
    """The binding argument whose words ARE this argv, or None.

    "Are", element for element: a predicate that runs a program the plan names hands the plan's words
    to the process as its argv (`command_exit` does `_run_in_own_group(list(argv), ...)`), and a
    single shell word - `os.system(script)`, `subprocess.run(script, shell=True)` - is that same thing
    with one word. A word merely APPEARING inside a longer argv is NOT this: `git rev-parse
    <base>^{commit}` puts a binding OBJECT ID into a program the plan does not name, `_argv_of` cannot
    read a program out of it either, and counting it would put `git_delivered` in the audit's set for
    a reason the audit does not act on.
    """
    words = [str(w) for w in argv]
    if not words:
        return None
    for arg, value in sentinels.items():
        if isinstance(value, list):
            if words == [str(v) for v in value]:
                return arg
        elif words == [str(value)]:
            return arg
    return None


def measure_program_predicates(library: PredicateLibrary, workspace: Path,
                               spawns: _Spawns) -> dict[str, str]:
    """Drive every REGISTERED predicate; report which ran a program the binding named, and through what.

    The registration table is the source of truth (`_registrations`), so a predicate added to
    `builtin_library()` is driven without this file knowing it exists - which is the whole point of
    measuring instead of listing. Every exception is swallowed: the probe hands each predicate
    nonsense on purpose, and a predicate that refuses its arguments says nothing about whether its
    implementation can start a process.
    """
    measured: dict[str, str] = {}
    for name, fn in _registrations(library).items():
        args = _probe_args(fn)
        start = len(spawns.calls)
        try:
            library.evaluate(Binding(name, args), workspace=workspace, pre_state={}, base_sha="")
        except Exception:                       # a probe drives nonsense; a raise is not a verdict
            pass
        for argv in spawns.calls[start:]:
            arg = _program_arg(argv, args)
            if arg is not None:
                measured[name] = arg
                break
    return measured


# -- the invariant -------------------------------------------------------------------------------

def test_every_program_running_predicate_is_registered_as_one(tmp_path, spawns):
    """The declared set and the MEASURED set are the same set, in both directions.

    Measured by driving the registration table `builtin_library()` actually holds, so a predicate that
    spawns a program built from binding words and is not in `COMMAND_PREDICATES` is a failure NAMING
    IT - the literal this pins cannot fail that way on its own - and a listed name whose
    implementation spawns nothing is a failure too, because the audit acts on the list.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    measured = measure_program_predicates(builtin_library(), workspace, spawns)
    declared = set(COMMAND_PREDICATES)

    unregistered = {name: measured[name] for name in sorted(set(measured) - declared)}
    assert not unregistered, (
        f"predicate(s) {unregistered} run a program the BINDING names, and are not in "
        f"`core.todo_templates.COMMAND_PREDICATES` {tuple(COMMAND_PREDICATES)}. The freeze audit reads "
        f"that list to decide whether an item can revert a declared delta: with a program-running "
        f"predicate missing from it, the audit reads its argv as naming nothing and REFUSES "
        f"(`invalid_plan`) a plan the program itself makes satisfiable - the over-refusal of "
        f"iss-f7d7793371a34bec - and for a delta git excludes it stops keeping the refusal. Add the "
        f"name to COMMAND_PREDICATES together with the measurement showing its words arriving in `argv`")

    stale = sorted(declared - set(measured))
    assert not stale, (
        f"predicate(s) {stale} are listed as running a program the binding names, and driving them "
        f"never turned binding words into a program's argv. The list is what the audit acts on, so a "
        f"stale entry withholds a refusal for a program that is not there: drop it from "
        f"COMMAND_PREDICATES, or measure the argument its words really arrive in before believing it")

    for name, arg in measured.items():
        assert arg == _ARGV, (
            f"{name} runs a program the binding names, and the words reach it through the binding "
            f"argument {arg!r}, not {_ARGV!r}: `_argv_of` reads {_ARGV!r} (`core.todo_runtime._STR_LISTS` "
            f"fixes that name for a program's argv), so it would read NOTHING for this predicate and "
            f"the audit would withhold no refusal for the item that can revert the delta")


def test_the_audit_reads_the_words_the_measurement_found():
    """`_argv_of` returns the argv of every predicate the measurement says runs a plan-named program.

    The invariant above says the words arrive in `argv`; this says the audit actually reads them out
    of a binding and hands them to `_names_path`. A set entry whose argument the audit could not read
    would make `_programs_naming` empty for that item - the same over-refusal from the other end.
    """
    assert COMMAND_PREDICATES, ("`COMMAND_PREDICATES` is empty: `command_exit` runs a program the plan "
                                "names, so an empty set means the audit withholds nothing at all")
    for name in COMMAND_PREDICATES:
        words = ["/bin/sh", "-c", "git restore app.py"]
        assert _argv_of(Item(id="run", title="run", predicates=(Binding(name, {_ARGV: list(words)}),))) == words


# -- the freeze shapes the set governs, through the real CLI --------------------------------------

def _write_template(directory: Path, name: str, body: str) -> None:
    directory.mkdir(exist_ok=True)
    (directory / f"{name}.yaml").write_text(body)


def _create(cli: Cli, workspace: Path, base_sha: str, template: str, params: dict):
    return cli.run("create", "--template", template, "--params", json.dumps(params),
                   "--attempt", "att-1", "--node-key", "execution.demo", "--context-hash", "c" * 64,
                   "--base-sha", base_sha, "--workspace", str(workspace), "--goal", "make the change")


def _declined(out: dict) -> list[str]:
    return [note for note in (out.get("paths") or {}).get("notes") or () if "NOT refused" in note]


IGNORED_DELTA_THEN_A_NOOP = """\
name: t
version: 1
description: a delta git excludes, a program in the window that names nothing, and a scope without it
params:
  scope:
    type: list[str]
    required: true
items:
  - id: change
    title: "write the ignored secret"
    predicates:
      - predicate: files_changed
        args: {paths: ["secret.txt"]}
  - id: noop
    title: "run a program that names nothing"
    needs: [change]
    predicates:
      - predicate: command_exit
        args: {argv: ["/bin/sh", "-c", "true"], expect: 0}
  - id: judge
    title: "judge other.py against a scope of its own"
    predicates:
      - predicate: changes_within
        args: {scope: ["other.py"]}
"""

IGNORED_DELTA_NAMED_AND_FORCE_ADDED = """\
name: t
version: 1
description: a delta git excludes, and a program in the window that NAMES it and force-adds it
params:
  scope:
    type: list[str]
    required: true
items:
  - id: change
    title: "write the ignored secret"
    predicates:
      - predicate: files_changed
        args: {paths: ["secret.txt"]}
  - id: track
    title: "make the ignored delta visible to every changed set"
    needs: [change]
    predicates:
      - predicate: command_exit
        args: {argv: ["/bin/sh", "-c", "git add -f secret.txt"], expect: 0}
  - id: judge
    title: "judge other.py against a scope of its own"
    predicates:
      - predicate: changes_within
        args: {scope: ["other.py"]}
"""

TRACKED_DELTA_THEN_A_NOOP = """\
name: t
version: 1
description: a TRACKED delta, a program in the window that names nothing, and a scope without it
params:
  scope:
    type: list[str]
    required: true
items:
  - id: change
    title: "change app.py"
    predicates:
      - predicate: files_changed
        args: {paths: ["app.py"]}
  - id: noop
    title: "run a program that names nothing"
    needs: [change]
    predicates:
      - predicate: command_exit
        args: {argv: ["/bin/sh", "-c", "true"], expect: 0}
  - id: judge
    title: "judge other.py against a scope of its own"
    predicates:
      - predicate: changes_within
        args: {scope: ["other.py"]}
"""


@pytest.fixture
def custom(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "tpl"
    directory.mkdir()
    monkeypatch.setenv("AITELIER_TODO_TEMPLATES", str(directory))
    return directory


def test_a_delta_git_cannot_see_freezes_even_with_a_program_in_the_window(cli, repo, custom):
    """The narrow shape: the delta is on a path git excludes, so `changes_within` cannot fail on it.

    A program in the window (`/bin/sh -c true`) names nothing, so it neither reverts anything the
    audit can see nor makes the pair unjudgeable: the plan freezes, silently, as before - refusing it
    would be the over-refusal a program-running predicate must not re-open.
    """
    (repo / ".gitignore").write_text("secret.txt\n")
    start = commit_all(repo, "ignore the secret")
    assert subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", "--", "secret.txt"]).returncode == 0, \
        "the premise: git excludes the untracked path, so no tree can make changes_within fail on it"

    _write_template(custom, "t", IGNORED_DELTA_THEN_A_NOOP)
    code, out = _create(cli, repo, start, "t", {"scope": ["secret.txt", "other.py"]})
    assert code == 0, out
    assert out["items"] == ["change", "noop", "judge", "deliver"], out
    assert _declined(out) == [], out["paths"]


def test_a_program_that_names_an_excluded_delta_keeps_the_refusal(cli, repo, custom):
    """The other direction, and what makes the set non-vacuous: `git add -f` can make it visible.

    `_programs_naming` reads the declared argv, so the program in the window is SEEN to name
    `secret.txt`; a tracked path is in all three of `changes_within`'s changed sets, so the refusal
    stands. A set that had lost this predicate, or stopped reading its argv, would freeze this plan
    instead - a plan that measurably dies at `judge`.
    """
    (repo / ".gitignore").write_text("secret.txt\n")
    start = commit_all(repo, "ignore the secret")
    assert subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", "--", "secret.txt"]).returncode == 0, \
        "the premise: git excludes the untracked path at freeze time"

    _write_template(custom, "t", IGNORED_DELTA_NAMED_AND_FORCE_ADDED)
    code, out = _create(cli, repo, start, "t", {"scope": ["secret.txt", "other.py"]})
    assert code == 2 and out["refused"] == "invalid_plan", out
    assert out["item"] == "judge" and out["declared_by"] == "change", out
    assert out["path"] == "secret.txt" and out["scope"] == ["other.py"], out
    assert "['track']" in out["reason"] and "git add -f" in out["reason"], out["reason"]
    assert not list(cli.store.glob("*.json")), "a refused create leaves no plan behind"


def test_a_program_that_names_nothing_is_not_an_opt_out(cli, repo, base, custom):
    """`/bin/sh -c true` between the declaring and the judging item must NOT withhold the refusal.

    The delta is on a tracked path, so `changes_within` sees it and no tree satisfies the plan: it is
    refused (`invalid_plan`) rather than frozen into an attempt that can only hang. The program IS in
    the registered set (it is a `command_exit`) and its ARGV is what decides - which is why a set that
    had lost the predicate, or an audit that failed to read its argv, would freeze it.
    """
    _write_template(custom, "t", TRACKED_DELTA_THEN_A_NOOP)
    code, out = _create(cli, repo, base, "t", {"scope": ["app.py", "other.py"]})
    assert code == 2 and out["refused"] == "invalid_plan", out
    assert out["item"] == "judge" and out["declared_by"] == "change", out
    assert out["path"] == "app.py" and out["scope"] == ["other.py"], out
    assert not list(cli.store.glob("*.json")), "a refused create leaves no plan behind"
