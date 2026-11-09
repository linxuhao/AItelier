"""The todo runtime's whole point: completion is observed, not claimed.

Each test below is a rule from the design, written so that breaking the rule
makes it fail - not so that it describes the implementation back to itself.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from core.gate_deferral import ABSENCE_TERMINAL, absent_terminal_names_no_failure
from core.todo_runtime import (Binding, Item, Plan, TodoRun, builtin_library)


def process_is_stopped(pid: int) -> bool:
    """True when the process is gone OR is a zombie (state Z: killed, not yet reaped).

    Whether a killed grandchild is ever reaped depends on the container having an init;
    "no longer running" is what the kill guarantees, so a zombie counts as stopped and a
    live state (R/S/D/T) does not.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return True
    return stat.rpartition(")")[2].split()[0] in ("Z", "X")


def _plan(*items: Item) -> Plan:
    return Plan(key="fixture-plan", goal="prove the item is done by observation", items=tuple(items))


def _run(tmp_path: Path, *items: Item, clock=None) -> TodoRun:
    return TodoRun(_plan(*items), builtin_library(), tmp_path, clock=clock)


def test_an_item_is_not_required_to_be_red_before_it_runs(tmp_path):
    """Over-gating check: a predicate already true must not fail the step.

    Requiring redness first would make deleting the file the only way to satisfy
    the contract, which is how a gate teaches a worker to cheat.
    """
    (tmp_path / "app.py").write_text("x = 1\n")
    item = Item(id="touch", title="edit app.py", predicates=(Binding("files_present", {"pattern": "*.py"}),))
    run = _run(tmp_path, item)
    run.begin("touch")
    assert run.observe("touch").outcome == "pass"


def test_a_claim_without_a_change_is_a_failure(tmp_path):
    """The worker saying it is done is not an input to the verdict."""
    target = tmp_path / "app.py"
    target.write_text("x = 1\n")
    item = Item(id="edit", title="edit", predicates=(Binding("file_changed", {"path": "app.py", "contains": "y = 2"}),))
    run = _run(tmp_path, item)
    run.begin("edit")
    assert run.observe("edit").outcome == "fail", "an untouched file must not pass a change predicate"

    target.write_text("x = 1  # touched\n")          # changed, but not the intended change
    assert run.observe("edit").outcome == "fail", "a spurious edit must not satisfy a bare delta"

    target.write_text("x = 1\ny = 2\n")
    assert run.observe("edit").outcome == "pass"


def test_a_missing_tool_is_an_absence_not_a_failure_and_ends_by_naming_it(tmp_path):
    """Could-not-run is neither pass nor fail, and reusing the ledger is the point."""
    now = [0.0]
    item = Item(id="lint", title="lint",
                predicates=(Binding("command_exit", {"argv": ["definitely-not-a-real-binary-xyz"], "timeout": 5}),))
    run = _run(tmp_path, item, clock=lambda: now[0])
    run.begin("lint")

    first = run.observe("lint")
    assert first.outcome == "absent", "a missing binary must not be reported as a failing step"
    key = "fixture-plan/lint"
    assert key in run.ledger.episodes, "the absence must be recorded as an episode"
    assert run.ledger.episodes[key].gate == "command_exit"

    now[0] = 10_000_000.0                              # outlive any ceiling
    assert run.observe("lint").outcome == "absent"
    reason = run.terminal_reason("lint")
    assert "command_exit" in reason, f"an expired absence must name what it waited for: {reason!r}"


def test_the_library_is_portable_and_plans_only_bind(tmp_path):
    library = builtin_library()
    with pytest.raises(ValueError):
        @library.define("files_present")
        def _duplicate(**kwargs): ...  # a plan may not redefine a shared predicate

    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.txt").write_text("")
    one = Item(id="py", title="", predicates=(Binding("files_present", {"pattern": "*.py"}),))
    two = Item(id="txt", title="", predicates=(Binding("files_present", {"pattern": "*.txt"}),))
    # One definition, two bindings: this is what keeps the workflow config portable.
    run = _run(tmp_path, one, two)
    for item in (one, two):
        run.begin(item.id)
        assert run.observe(item.id).outcome == "pass"

    broken = Item(id="bad", title="", predicates=(Binding("not_a_predicate", {}),))
    run2 = _run(tmp_path, broken)
    run2.begin("bad")
    with pytest.raises(KeyError):
        run2.observe("bad")


def test_resume_is_by_reference_and_bounded(tmp_path):
    first = Item(id="one", title="", predicates=(Binding("files_present", {"pattern": "*.py"}),))
    second = Item(id="two", title="", predicates=(Binding("files_present", {"pattern": "*.py"}),), needs=("one",))
    run = _run(tmp_path, first, second)
    assert run.next_item().id == "one", "the dependency order must be respected"

    (tmp_path / "a.py").write_text("")
    run.begin("one")
    assert run.observe("one").complete
    assert run.next_item().id == "two"

    brief = run.brief(max_chars=200)
    assert len(brief) <= 200, "the brief must stay bounded; it is re-injected after every compact"
    assert "two" in brief and "one" in run.report()["done"]
    assert "definitely-not-a-real-binary-xyz" not in brief, "the brief carries ids, not the whole catalogue"


# -- predicate library v2 ----------------------------------------------------------

_GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          env={**os.environ, **_GIT_ENV})
    assert done.returncode == 0, f"git {args}: {done.stderr}"
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    (path / "src").mkdir()
    (path / "src" / "a.py").write_text("a = 1\n")
    (path / "README.md").write_text("hi\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "base")
    return path


def _eval(repo: Path, predicate: str, plan_base: str | None = None, **args):
    return builtin_library().evaluate(Binding(predicate, args), workspace=repo, pre_state={},
                                      base_sha=plan_base or "")


def test_the_library_is_versioned_and_offers_the_contracted_predicates():
    library = builtin_library()
    assert library.version == "2"
    assert {"files_present", "file_changed", "files_changed", "command_exit",
            "changes_within", "git_delivered", "report_json"} <= set(library.names())


def test_the_library_checks_a_binding_before_any_work_starts():
    library = builtin_library()
    library.check(Binding("file_changed", {"path": "a.py"}))
    library.check(Binding("command_exit", {"argv": ["true"], "expect": 1, "timeout": 2.5}))
    library.check(Binding("command_exit", {"argv": ["true"], "report": "out/test_report.json"}))
    library.check(Binding("report_json", {"path": "r.json", "require_keys": []}))
    with pytest.raises(ValueError):
        library.check(Binding("changes_within", {"scope": ["**"], "base_sha": "f" * 40}))   # the plan's base, not a binding's
    with pytest.raises(KeyError):
        library.check(Binding("worker_says_done", {}))
    with pytest.raises(ValueError):
        library.check(Binding("file_changed", {}))                       # path is required
    with pytest.raises(ValueError):
        library.check(Binding("file_changed", {"path": "a.py", "claimed": True}))


def test_changes_within_judges_committed_uncommitted_and_untracked_paths(repo):
    base = _git(repo, "rev-parse", "HEAD")
    scope = ["src/**", "docs/"]
    assert _eval(repo, "changes_within", base, scope=scope).outcome == "pass", "no change at all is inside any scope"

    (repo / "src" / "deep" / "x").parent.mkdir()
    (repo / "src" / "deep" / "x").write_text("1")
    (repo / "docs").mkdir()
    (repo / "docs" / "d.md").write_text("1")
    assert _eval(repo, "changes_within", base, scope=scope).outcome == "pass", "** crosses segments, a trailing / takes the tree"

    (repo / "README.md").write_text("changed\n")                         # tracked, uncommitted, outside
    outcome = _eval(repo, "changes_within", base, scope=scope)
    assert outcome.outcome == "fail" and "README.md" in outcome.detail
    _git(repo, "checkout", "--", "README.md")

    (repo / "stray.txt").write_text("1")                                  # untracked, outside
    assert "stray.txt" in _eval(repo, "changes_within", base, scope=scope).detail
    (repo / "stray.txt").unlink()

    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "inside only")
    assert _eval(repo, "changes_within", base, scope=scope).outcome == "pass"
    (repo / "README.md").write_text("committed outside\n")
    _git(repo, "commit", "-qam", "outside")
    assert _eval(repo, "changes_within", base, scope=scope).outcome == "fail", "a committed change counts"
    assert _eval(repo, "changes_within", base, scope=["src/*.py"]).outcome == "fail", "* stays inside one segment"


def test_changes_within_cannot_be_measured_without_a_readable_base(repo):
    assert _eval(repo, "changes_within", "0" * 40, scope=["**"]).outcome == "absent"
    with pytest.raises(ValueError):
        _eval(repo, "changes_within", None, scope=["**"])


@pytest.mark.parametrize("predicate,args", [
    ("command_exit", {"argv": "true"}),                     # a string would be split into letters
    ("command_exit", {"argv": []}),
    ("command_exit", {"argv": ["true", 1]}),
    ("command_exit", {"argv": ["true"], "timeout": True}),
    ("command_exit", {"argv": ["true"], "expect": "0"}),
    ("command_exit", {"argv": ["true"], "report": 5}),
    ("command_exit", {"argv": ["true"], "report": ""}),
    ("changes_within", {"scope": "src/**"}),
    ("changes_within", {"scope": []}),
    ("report_json", {"path": "r.json", "require_keys": "passed"}),
    ("report_json", {"path": 5, "require_keys": []}),
    ("files_present", {"pattern": ["*.py"]}),
    ("git_delivered", {"base": 12}),
])
def test_a_binding_with_a_wrongly_typed_argument_is_refused_up_front(predicate, args):
    with pytest.raises(ValueError):
        builtin_library().check(Binding(predicate, args))


def test_a_missing_workspace_is_named_as_such_not_as_a_missing_binary(tmp_path):
    gone = tmp_path / "gone"
    for name, args in (("command_exit", {"argv": [sys.executable, "-V"]}),
                       ("git_delivered", {"base": "0" * 40}),
                       ("changes_within", {"scope": ["**"]})):
        result = builtin_library().evaluate(Binding(name, args), workspace=gone, pre_state={}, base_sha="0" * 40)
        assert result.outcome == "absent" and str(gone) in result.detail, result
        assert "not installed" not in result.detail


def test_git_delivered_needs_head_past_base_and_a_clean_tree(repo):
    base = _git(repo, "rev-parse", "HEAD")
    assert _eval(repo, "git_delivered", base=base).outcome == "fail", "HEAD == base is not a delivery"
    (repo / "src" / "a.py").write_text("a = 2\n")
    _git(repo, "commit", "-qam", "work")
    (repo / "wip.txt").write_text("untracked")
    dirty = _eval(repo, "git_delivered", base=base)
    assert dirty.outcome == "fail" and "not clean" in dirty.detail
    (repo / "wip.txt").unlink()
    assert _eval(repo, "git_delivered", base=base).outcome == "pass"
    assert _eval(repo, "git_delivered", base="f" * 40).outcome == "absent", "an unreadable base is unmeasured, not failed"


def test_git_delivered_refuses_a_head_that_is_not_a_descendant_of_the_base(repo):
    first = _git(repo, "rev-parse", "HEAD")
    (repo / "src" / "a.py").write_text("a = 2\n")
    _git(repo, "commit", "-qam", "second")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "reset", "-q", "--hard", first)                            # HEAD now BEHIND the base, tree clean
    behind = _eval(repo, "git_delivered", base=base)
    assert behind.outcome == "fail" and "not a descendant" in behind.detail

    _git(repo, "switch", "-q", "-c", "side")                              # a sibling of the base
    (repo / "side.txt").write_text("1")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "side")
    diverged = _eval(repo, "git_delivered", base=base)
    assert diverged.outcome == "fail" and "not a descendant" in diverged.detail

    _git(repo, "merge", "-q", "--no-edit", base)                          # now it contains the base
    assert _eval(repo, "git_delivered", base=base).outcome == "pass"


def test_report_json_wants_an_object_with_the_required_keys(repo):
    def check(**kw):
        return _eval(repo, "report_json", path="out/report.json", **kw)
    assert check(require_keys=["passed"]).outcome == "fail", "no file yet"
    (repo / "out").mkdir()
    (repo / "out" / "report.json").write_text("{not json")
    assert check(require_keys=["passed"]).outcome == "fail"
    (repo / "out" / "report.json").write_text("[1]")
    assert check(require_keys=[]).outcome == "fail"
    (repo / "out" / "report.json").write_text(json.dumps({"passed": 3}))
    assert check(require_keys=["passed"]).outcome == "pass"
    missing = check(require_keys=["passed", "failed"])
    assert missing.outcome == "fail" and "failed" in missing.detail


def test_files_changed_wants_every_path_changed_and_names_the_first_that_is_not(tmp_path):
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / name).write_text("v1\n")
    item = Item(id="all", title="", predicates=(Binding("files_changed", {"paths": ["a.py", "b.py", "c.py"], "contains": "v2"}),))
    run = _run(tmp_path, item)
    run.begin("all")
    assert run.pre_states["all"].keys() == {"a.py", "b.py", "c.py"}, "every listed path is captured at begin"
    (tmp_path / "a.py").write_text("v2\n")
    (tmp_path / "c.py").write_text("v2\n")
    report = run.observe("all")
    assert report.outcome == "fail" and "b.py" in report.results[0].detail and "a.py" not in report.results[0].detail
    (tmp_path / "b.py").write_text("v1 edited\n")
    assert "does not contain 'v2'" in run.observe("all").results[0].detail
    (tmp_path / "b.py").write_text("v2\n")
    assert run.observe("all").outcome == "pass"
    (tmp_path / "c.py").unlink()
    assert "c.py does not exist" in run.observe("all").results[0].detail


def test_a_delivery_whose_tree_equals_the_base_is_not_a_delivery(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "empty")
    empty = _eval(repo, "git_delivered", base=base)
    assert empty.outcome == "fail" and "nothing was delivered" in empty.detail
    (repo / "src" / "a.py").write_text("a = 2\n")
    _git(repo, "commit", "-qam", "real")
    assert _eval(repo, "git_delivered", base=base).outcome == "pass"
    (repo / "src" / "a.py").write_text("a = 1\n")                        # changed and changed back
    _git(repo, "commit", "-qam", "revert")
    assert _eval(repo, "git_delivered", base=base).outcome == "fail", "net change is what counts"


def test_scope_entries_are_normalised_before_matching(repo):
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "src" / "a.py").write_text("a = 2\n")
    for scope in (["./src/"], ["./src/**"], [".//src/a.py".replace(".//", "./")], ["././src/"]):
        assert _eval(repo, "changes_within", base, scope=scope).outcome == "pass", scope
    assert _eval(repo, "changes_within", base, scope=["./docs/"]).outcome == "fail"


def test_a_dot_scope_means_the_whole_workspace(repo):
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "anywhere").mkdir()
    (repo / "anywhere" / "x.txt").write_text("1")
    assert _eval(repo, "changes_within", base, scope=["src/**"]).outcome == "fail"
    assert _eval(repo, "changes_within", base, scope=["."]).outcome == "pass"


def test_file_changed_is_a_delta_that_never_demands_a_bad_starting_point(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("y = 2\n")                    # already carries the marker before the step begins
    item = Item(id="edit", title="", predicates=(Binding("file_changed", {"path": "app.py", "contains": "y = 2"}),))
    run = _run(tmp_path, item)
    run.begin("edit")
    assert run.observe("edit").outcome == "fail", "no delta yet, whatever the file already says"
    target.write_text("y = 2\nz = 3\n")
    assert run.observe("edit").outcome == "pass"


# -- TodoRun: the two ledger fixes ---------------------------------------------------------

SLOW = [sys.executable, "-c", "import time; time.sleep(30)"]


def test_an_expired_absence_is_a_terminal_state_that_next_item_skips_and_the_brief_names(tmp_path):
    now = [0.0]
    gate = Item(id="gate", title="", predicates=(Binding("command_exit", {"argv": SLOW, "timeout": 0.3}),))
    docs = Item(id="docs", title="", predicates=(Binding("files_present", {"pattern": "*.md"}),))
    run = _run(tmp_path, gate, docs, clock=lambda: now[0])
    run.begin("gate")
    assert run.observe("gate").unmeasurable is False
    assert run.next_item().id == "gate", "still inside the ceiling: keep looking"

    now[0] = 10_000_000.0
    report = run.observe("gate")
    assert report.outcome == "absent" and report.unmeasurable is True
    assert run.next_item().id == "docs", "an unmeasurable item is skipped, not retried forever"
    reason = run.terminal_reason("gate")
    assert ABSENCE_TERMINAL in reason and absent_terminal_names_no_failure(reason)
    assert run.report()["unmeasurable"] == [{"id": "gate", "reason": reason}]
    assert "gate" not in run.report()["deferred"] and "gate" not in run.report()["done"]
    assert "gate" in run.brief() and reason in run.brief() and len(run.brief()) <= 600
    assert not run.finished()

    (tmp_path / "x.md").write_text("")
    run.begin("docs")
    assert run.observe("docs").complete and run.next_item() is None
    assert not run.finished(), "an unmeasurable item is not a completed one"


def test_a_measured_failure_beside_an_absence_is_a_failure_never_unmeasurable(tmp_path):
    now = [0.0]
    (tmp_path / "app.py").write_text("x = 1\n")
    mixed = Item(id="mixed", title="", predicates=(
        Binding("file_changed", {"path": "app.py"}),
        Binding("command_exit", {"argv": SLOW, "timeout": 0.3})))
    run = _run(tmp_path, mixed, clock=lambda: now[0])
    run.begin("mixed")
    for moment in (0.0, 10_000_000.0):
        now[0] = moment
        report = run.observe("mixed")
        assert report.outcome == "fail" and report.unmeasurable is False
        assert run.next_item().id == "mixed", "a failure goes back to the worker"
        assert run.ledger.episodes == {}, "an absence beside a failure opens no episode"
    assert run.report()["unmeasurable"] == []

    (tmp_path / "app.py").write_text("x = 2\n")                          # the failing half is fixed ...
    assert run.observe("mixed").outcome == "absent", "... and only then does the absence speak"


def test_a_run_can_be_resumed_from_its_observations(tmp_path):
    (tmp_path / "a.py").write_text("")
    one = Item(id="one", title="", predicates=(Binding("files_present", {"pattern": "*.py"}),))
    two = Item(id="two", title="", predicates=(Binding("files_present", {"pattern": "*.py"}),), needs=("one",))
    first = _run(tmp_path, one, two)
    first.begin("one")
    first.observe("one")
    resumed = TodoRun(_plan(one, two), builtin_library(), tmp_path,
                      pre_state=first.pre_states, reports=first.reports)
    assert resumed.report()["done"] == ["one"] and resumed.next_item().id == "two"


def test_a_command_that_times_out_takes_its_whole_process_group_with_it(tmp_path):
    import signal
    import time
    pidfile = tmp_path / "child.pid"
    code = ("import subprocess, sys, time; "
            "p = subprocess.Popen(['sleep', '60']); open(sys.argv[1], 'w').write(str(p.pid)); time.sleep(60)")
    started = time.monotonic()
    result = builtin_library().evaluate(
        Binding("command_exit", {"argv": [sys.executable, "-c", code, str(pidfile)], "timeout": 1.5}),
        workspace=tmp_path, pre_state={})
    elapsed = time.monotonic() - started
    # The command's own write of its child's pid can be cut short by the 1.5 s
    # kill: open(...,'w') truncates before it writes, so the witness is read for
    # a COMPLETE pid (bounded) rather than assumed readable just because the
    # command returned -- the pre-kill window must not surface as int('').
    read_deadline = time.monotonic() + 5
    while True:
        try:
            grandchild = int(pidfile.read_text())
            break
        except (OSError, ValueError):
            if time.monotonic() > read_deadline:
                pytest.fail(f"the command never published its child's pid in {pidfile}")
            time.sleep(0.05)
    # A survivor holds the command's pipes open, so the call blocks until it exits on its own (~60 s)
    # and would look stopped by the time it is checked: bound the call itself, not only the survivor.
    if elapsed > 20:
        if not process_is_stopped(grandchild):
            os.kill(grandchild, signal.SIGKILL)
        pytest.fail(f"a 1.5 s command timeout took {elapsed:.0f} s to return: its child outlived the kill")
    assert result.outcome == "absent" and "timed out" in result.detail
    for _ in range(100):
        if process_is_stopped(grandchild):
            return
        time.sleep(0.05)
    os.kill(grandchild, signal.SIGKILL)
    pytest.fail("the timed-out command's child is still running")


# -- the wider absence channel: a run whose own report declares it measured nothing ---------

DECLARED_ABSENCE = {"repo_gate_absent": True, "repo_gate": {"script": "scripts/repo_gate.sh"}}
MEASURED_FAILURE = {"repo_gate_absent": False, "repo_gate": {"script": "scripts/repo_gate.sh"}, "passed": 0}


def _writes(path: str, payload: dict | None, *, code: int) -> list[str]:
    """A command that writes `path` (JSON, or a deliberately unreadable string) and exits `code`."""
    text = json.dumps(payload) if payload is not None else "{not json"
    script = ("import pathlib, sys\n"
              "target = pathlib.Path(sys.argv[1])\n"
              "target.parent.mkdir(parents=True, exist_ok=True)\n"
              f"target.write_text({text!r})\n"
              f"sys.exit({code})\n")
    return [sys.executable, "-c", script, path]


def _exits(code: int) -> list[str]:
    return [sys.executable, "-c", f"import sys; sys.exit({code})"]


def test_a_gate_that_declares_it_measured_nothing_is_an_absence_not_its_exit_code(tmp_path):
    """A run that RAN and said so in its own report is the missing-gate absence.

    The failure such a gate prints is the one it could not measure, so charging it
    to the implementer is exactly what the ledger exists to prevent - and the
    episode it opens must be the ledger's, not a second mechanism.
    """
    item = Item(id="gate", title="", predicates=(
        Binding("command_exit", {"argv": _writes("out/test_report.json", DECLARED_ABSENCE, code=2),
                                 "report": "out/test_report.json", "timeout": 120}),))
    run = _run(tmp_path, item, clock=lambda: 0.0)
    run.begin("gate")
    report = run.observe("gate")
    assert report.outcome == "absent", "a non-zero exit that declares an absence is not the implementer's failure"
    assert report.unmeasurable is False, "the first look is inside the ledger's wait"
    episode = run.ledger.episodes["fixture-plan/gate"]
    assert episode.gate == "command_exit", "the absence must reach the existing DeferralLedger episode"
    assert run.report()["deferred"] == ["gate"] and run.report()["done"] == []
    detail = report.results[0].detail
    assert "repo_gate.sh" in detail and "out/test_report.json" in detail, \
        f"the detail must name the gate AND the report it was read from: {detail!r}"


def test_a_declared_absence_is_never_a_release_even_at_the_expected_exit(tmp_path):
    """An exit that happens to match `expect` cannot turn "nothing was measured" into a pass."""
    item = Item(id="gate", title="", predicates=(
        Binding("command_exit", {"argv": _writes("test_report.json", DECLARED_ABSENCE, code=0),
                                 "report": "test_report.json", "timeout": 120}),))
    run = _run(tmp_path, item)
    run.begin("gate")
    report = run.observe("gate")
    assert report.outcome == "absent", "an exit 0 that declares an absence is not a release"
    assert not report.complete and run.report()["done"] == [] and run.next_item().id == "gate"


def test_the_report_is_read_exactly_as_the_gate_pipeline_reads_it(tmp_path):
    """`repo_gate_unmeasured` counts only from a report that has no `repo_gate_absent` key."""
    def verdict(payload):
        return builtin_library().evaluate(
            Binding("command_exit", {"argv": _writes("test_report.json", payload, code=1),
                                     "report": "test_report.json", "timeout": 120}),
            workspace=tmp_path, pre_state={})

    assert verdict({"repo_gate_unmeasured": True}).outcome == "absent", "the fallback flag is an absence"
    assert verdict({"repo_gate_unmeasured": True, "repo_gate_absent": False}).outcome == "fail", \
        "with the primary key present it is the only one read: a measured red goes back to the worker"
    assert verdict({"repo_gate_absent": False, "passed": 0}).outcome == "fail"
    assert verdict({"repo_gate_absent": True, "repo_gate": {"script": "g.sh"}}).outcome == "absent"
    assert verdict({"repo_gate": {"script": "g.sh"}}).outcome == "fail", "a report stating nothing is no absence"


@pytest.mark.parametrize("case", ["no report named", "named but never written",
                                  "flag false", "older than the run"])
def test_a_non_zero_exit_decides_the_verdict_when_no_absence_is_declared(tmp_path, case):
    """Only a report THIS run wrote and declared an absence in may widen the channel."""
    named = "out/test_report.json"
    args: dict
    if case == "no report named":                      # the old binding, untouched
        (tmp_path / "out").mkdir()
        (tmp_path / "out" / "test_report.json").write_text(json.dumps(DECLARED_ABSENCE))  # nobody named it
        argv, args = _exits(2), {}
    elif case == "named but never written":
        argv, args = _exits(2), {"report": named}
    elif case == "flag false":
        argv, args = _writes(named, MEASURED_FAILURE, code=2), {"report": named}
    else:
        (tmp_path / "out").mkdir()
        stale = tmp_path / "out" / "test_report.json"
        stale.write_text(json.dumps(DECLARED_ABSENCE))
        os.utime(stale, (time.time() - 3600, time.time() - 3600))   # last run's declaration
        # Not a clock comparison any more: the bound path is CLEARED before the command
        # runs, so the leftover is simply gone and the exit code decides. Same verdict,
        # a different reason - ownership is constructed rather than inferred from mtime.
        argv, args = _exits(2), {"report": named}

    result = builtin_library().evaluate(Binding("command_exit", {"argv": argv, "timeout": 120, **args}),
                                        workspace=tmp_path, pre_state={})
    assert result.outcome == "fail", f"{case}: the exit code decides when no absence is declared"
    assert "exit 2, expected 0" in result.detail, result.detail


def test_a_binding_with_no_report_is_judged_by_its_exit_code_exactly_as_before(tmp_path):
    library = builtin_library()

    def verdict(code, expect=0):
        return library.evaluate(Binding("command_exit", {"argv": _exits(code), "expect": expect,
                                                         "timeout": 120}),
                                workspace=tmp_path, pre_state={})

    assert verdict(0).outcome == "pass"
    assert verdict(3, expect=3).outcome == "pass"
    failed = verdict(3)
    assert failed.outcome == "fail" and "exit 3, expected 0" in failed.detail


def test_the_report_may_be_named_by_an_absolute_path(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    target = elsewhere / "test_report.json"
    result = builtin_library().evaluate(
        Binding("command_exit", {"argv": _writes(str(target), DECLARED_ABSENCE, code=1),
                                 "report": str(target), "timeout": 120}),
        workspace=tmp_path, pre_state={})
    assert result.outcome == "absent" and str(target) in result.detail



def test_a_leftover_report_with_a_future_mtime_cannot_outvote_a_measured_failure(tmp_path):
    """An untouched OLD declaration must not speak for a run that measured a red gate.

    The clock rule (`written < started` -> not this run's report) is not an identity
    test: a leftover whose mtime sits in the FUTURE passed it, so this case came back
    `absent` - the worker charged nothing for a gate that never ran. Ownership is now
    established by construction: the bound path is cleared before the command starts,
    so the exit 2 the command really produced is the verdict.
    """
    named = "out/test_report.json"
    (tmp_path / "out").mkdir()
    leftover = tmp_path / "out" / "test_report.json"
    leftover.write_text(json.dumps(DECLARED_ABSENCE))
    now = time.time()
    os.utime(leftover, (now + 3600, now + 3600))        # the clock rule was defeated by this
    result = builtin_library().evaluate(
        Binding("command_exit", {"argv": _exits(2), "report": named, "timeout": 120}),
        workspace=tmp_path, pre_state={})
    assert result.outcome == "fail", \
        f"a leftover declaring an absence is not this run's report: {result.detail}"
    assert "exit 2, expected 0" in result.detail, result.detail
    assert not leftover.exists(), \
        "the bound path is cleared before the command runs: a report present after it is this run's"


def test_a_report_this_run_wrote_is_its_own_even_with_a_backdated_mtime(tmp_path):
    """A FRESH declaration must not be discarded because its mtime fell before the start.

    Integer-second rounding can do that by itself; here the command forces it, which is
    the same falsifier made deterministic. The report exists because THIS run wrote it -
    it was absent when the run began - so the absence it declares still speaks. The old
    clock rule (`written < started` -> not this run's report) answered `pass` here: an
    absence becoming a release.
    """
    named = "test_report.json"
    script = ("import json, os, pathlib, sys, time\n"
              f"target = pathlib.Path({named!r})\n"
              f"target.write_text({json.dumps(DECLARED_ABSENCE)!r})\n"
              "now = time.time()\n"
              "os.utime(target, (now - 3600, now - 3600))\n"
              "sys.exit(0)\n")
    began = time.time()
    result = builtin_library().evaluate(
        Binding("command_exit", {"argv": [sys.executable, "-c", script],
                                 "report": named, "timeout": 120}),
        workspace=tmp_path, pre_state={})
    assert result.outcome == "absent", \
        f"an exit 0 whose own report declares an absence is not a release: {result.detail}"
    assert "repo_gate.sh" in result.detail and named in result.detail, \
        f"the detail must still name the gate and the report: {result.detail!r}"
    written = (tmp_path / named).stat().st_mtime
    assert written < began - 1800, \
        "the fixture must actually put the report's mtime behind the run, or it proves nothing"


@pytest.mark.parametrize("code,outcome", [(2, "fail"), (0, "pass")])
def test_a_report_path_that_cannot_be_cleared_lets_the_exit_code_decide(tmp_path, code, outcome):
    """Falling back to an absence here would be the same laundering in the other direction.

    A directory sits at the bound report path, so nothing can establish ownership of it.
    No report may be read from a path this run does not own; the exit code decides, and
    the detail says why the report could not be cleared - on the pass path too.
    """
    named = "blocked/test_report.json"
    (tmp_path / "blocked").mkdir()
    (tmp_path / "blocked" / "test_report.json").mkdir()      # unlink() cannot remove this
    result = builtin_library().evaluate(
        Binding("command_exit", {"argv": _exits(code), "report": named, "timeout": 120}),
        workspace=tmp_path, pre_state={})
    assert result.outcome == outcome, f"exit {code} must decide, not an absence: {result.detail}"
    assert "could not be cleared" in result.detail and named in result.detail, \
        f"the {outcome} detail must say why no report was read: {result.detail!r}"
    if outcome == "fail":
        assert "exit 2, expected 0" in result.detail, result.detail


# -- the clearing is bounded: a binding cannot name a file outside the workspace -----------

def _escapes(tmp_path: Path, kind: str) -> tuple[Path, str]:
    """A report binding that names a file OUTSIDE the workspace, and the file it names.

    `kind` is one of the three ways a path leaves: an absolute one, a `..`
    traversal, or a parent that is a symlink to somewhere else.
    """
    outside = tmp_path.parent / f"{tmp_path.name}-{kind}-victim.json"
    outside.write_text(json.dumps(DECLARED_ABSENCE))
    if kind == "absolute":
        return outside, str(outside)
    if kind == "traversal":
        return outside, f"../{outside.name}"
    elsewhere = tmp_path.parent / f"{tmp_path.name}-{kind}-elsewhere"
    elsewhere.mkdir()
    linked = elsewhere / "victim.json"
    linked.write_text(json.dumps(DECLARED_ABSENCE))
    (tmp_path / "link-out").symlink_to(elsewhere, target_is_directory=True)
    return linked, "link-out/victim.json"


@pytest.mark.parametrize("kind", ["absolute", "traversal", "symlinked-parent"])
@pytest.mark.parametrize("code,outcome", [(2, "fail"), (0, "pass")])
def test_a_report_outside_the_workspace_is_neither_removed_nor_read(tmp_path, kind, code, outcome):
    """A binding names a path; the workspace is the only place this run may clear one.

    Clearing what an absolute path, a `..` traversal or a symlinked parent points at
    would make the harness delete a file it does not own - and then read the absence in
    it, laundering a stranger's declaration as this run's. Containment is the bound:
    nothing outside the resolved workspace is removed, the exit code decides, and the
    detail says why on the pass path as well as the fail path.
    """
    outside, report = _escapes(tmp_path, kind)
    result = builtin_library().evaluate(
        Binding("command_exit", {"argv": _exits(code), "report": report, "timeout": 120}),
        workspace=tmp_path, pre_state={})
    assert outside.exists(), f"{kind}: a file outside the workspace must not be removed: {outside}"
    assert result.outcome == outcome, \
        f"{kind}: the exit code decides when the bound path is not this run's to clear: {result.detail}"
    assert "outside the workspace" in result.detail and "could not be cleared" in result.detail, \
        f"{kind}: the detail must say why no report was read: {result.detail!r}"
    if outcome == "fail":
        assert "exit 2, expected 0" in result.detail, result.detail


# -- the bound path is a directory ENTRY: contain the parent, never follow the entry ---------

def test_a_symlink_at_the_report_path_is_removed_as_a_link_not_followed(tmp_path):
    """The file the link names is the run's own source, and it is not the harness's to delete.

    An in-workspace link at the bound path (`out/test_report.json -> ../src/app.py`)
    RESOLVES inside the workspace, so containing the whole path let it through and
    the resolved-unlink deleted `src/app.py` - a file the run never named - leaving
    a dangling link. What is contained is the parent, and what is removed is the
    entry: `unlink` does not follow a symlink, so the link goes and the source stays.
    """
    named = "out/test_report.json"
    (tmp_path / "src").mkdir()
    source = tmp_path / "src" / "app.py"
    source.write_text("SENTINEL: this file belongs to the run's own source tree\n")
    (tmp_path / "out").mkdir()
    link = tmp_path / "out" / "test_report.json"
    link.symlink_to("../src/app.py")

    result = builtin_library().evaluate(
        Binding("command_exit", {"argv": _exits(0), "report": named, "timeout": 120}),
        workspace=tmp_path, pre_state={})

    assert result.outcome == "pass", result.detail
    assert source.exists() and source.read_text().startswith("SENTINEL"), \
        f"the file the link pointed at is not the harness's to remove: {source}"
    assert not link.is_symlink(), \
        "the link IS the directory entry at the bound path: it is removed as a link, not followed"


def test_a_symlink_loop_at_the_report_path_is_a_link_to_remove_not_a_crash(tmp_path):
    """`out/report.json -> report.json`: resolving the whole path raised out of `evaluate`.

    `Path.resolve()` reports a loop as `RuntimeError: Symlink loop`, and the old
    clear resolved the whole target, so a pathological binding crashed the
    measurement instead of returning a verdict. The entry is a link like any other,
    so it is removed as one and the exit code decides - nothing is raised.
    """
    named = "out/report.json"
    (tmp_path / "out").mkdir()
    loop = tmp_path / "out" / "report.json"
    loop.symlink_to("report.json")               # points at itself

    result = builtin_library().evaluate(
        Binding("command_exit", {"argv": _exits(0), "report": named, "timeout": 120}),
        workspace=tmp_path, pre_state={})

    assert result.outcome == "pass", f"the exit code decides; nothing may be raised: {result.detail}"
    assert not loop.is_symlink(), "a loop at the bound path is an entry to remove, not a report to read"


@pytest.mark.parametrize("code,outcome", [(2, "fail"), (0, "pass")])
def test_a_symlink_loop_in_the_report_parent_is_a_reason_not_a_crash(tmp_path, code, outcome):
    """A loop that `resolve()` cannot get through is a reason, on BOTH details.

    `out -> out`: there is no directory to contain, so the path is not this run's
    to clear and no report may be read from it. The `RuntimeError` pathlib raises
    for the loop is caught and becomes a reason; the exit code decides, the detail
    names the report and the reason, and nothing escapes `evaluate`.
    """
    named = "out/report.json"
    (tmp_path / "out").symlink_to("out")        # a directory that is only itself

    result = builtin_library().evaluate(
        Binding("command_exit", {"argv": _exits(code), "report": named, "timeout": 120}),
        workspace=tmp_path, pre_state={})

    assert result.outcome == outcome, f"exit {code} must decide, not a crash: {result.detail}"
    assert named in result.detail and "could not be cleared" in result.detail, \
        f"the {outcome} detail must say why no report was read: {result.detail!r}"
    assert "loop" in result.detail.lower(), \
        f"the reason must name the loop it could not resolve: {result.detail!r}"
    if outcome == "fail":
        assert "exit 2, expected 0" in result.detail, result.detail


@pytest.mark.parametrize("code,outcome", [(2, "fail"), (0, "pass")])
def test_a_report_written_as_a_symlink_is_not_read_even_when_it_loops(tmp_path, code, outcome):
    """The read after the run is contained too: a link there is not a report this run wrote.

    The command creates `out/report.json -> report.json` - the probe's D2 shape, but
    arriving AFTER the clear, so the bound path is occupied when the read happens.
    Following it either reads a file the run never named or raises on the loop;
    neither is a verdict. Nothing is read, the exit code decides, and the reason is
    in the detail on that path's pass and its fail alike.
    """
    named = "out/report.json"
    script = ("import os, pathlib, sys\n"
              "out = pathlib.Path('out')\n"
              "out.mkdir()\n"
              "os.symlink('report.json', out / 'report.json')\n"
              f"sys.exit({code})\n")
    result = builtin_library().evaluate(
        Binding("command_exit", {"argv": [sys.executable, "-c", script], "report": named,
                                 "timeout": 120}),
        workspace=tmp_path, pre_state={})

    assert result.outcome == outcome, f"exit {code} must decide, not an absent report: {result.detail}"
    assert "symlink" in result.detail and named in result.detail, \
        f"the {outcome} detail must say why no report was read: {result.detail!r}"
    assert "no report was read" in result.detail, result.detail
    if outcome == "fail":
        assert "exit 2, expected 0" in result.detail, result.detail


@pytest.mark.parametrize("code,outcome", [(2, "fail"), (0, "pass")])
@pytest.mark.parametrize("age", ["stale", "fresh"])
def test_a_parent_symlink_created_by_the_command_gets_no_absence_read(tmp_path, age, code, outcome):
    """The command has its own turn, and it can move the report's parent out of the run.

    At clear time `out/` did not exist, so there was nothing to remove. The command
    then creates `out` as a symlink to a directory OUTSIDE the workspace that holds
    a declaring report (stale in one case, fresh in the other - the mtime is not the
    question). Reading it would launder a stranger's declaration as this run's and
    turn a measured exit into an absence, so the parent is contained AGAIN: nothing
    is read, the exit code decides, and the detail says why.
    """
    outside = tmp_path.parent / f"{tmp_path.name}-outside-{age}"
    outside.mkdir()
    declared = outside / "test_report.json"
    declared.write_text(json.dumps(DECLARED_ABSENCE))
    if age == "stale":
        old = time.time() - 3600
        os.utime(declared, (old, old))           # last run's declaration, out there
    named = "out/test_report.json"
    creates_parent = [sys.executable, "-c",
                      "import pathlib, sys\n"
                      "pathlib.Path('out').symlink_to(sys.argv[1])\n"
                      f"sys.exit({code})\n",
                      str(outside)]

    result = builtin_library().evaluate(
        Binding("command_exit", {"argv": creates_parent, "report": named, "expect": 0,
                                 "timeout": 120}),
        workspace=tmp_path, pre_state={})

    assert declared.exists(), f"{age}: a report outside the workspace must not be removed"
    assert result.outcome == outcome, \
        f"{age}: the exit code decides when the bound path left the workspace: {result.detail}"
    assert "outside the workspace" in result.detail and named in result.detail, \
        f"{age}: the {outcome} detail must say why no report was read: {result.detail!r}"
    assert "no report was read" in result.detail, result.detail
    if outcome == "fail":
        assert f"exit {code}, expected 0" in result.detail, result.detail


# -- a RELATIVE workspace: the bound report must still be the file the gate wrote -----

@pytest.mark.parametrize("cwd_name", ["first-process-cwd", "second-process-cwd"])
def test_a_relative_workspace_reads_the_report_the_gate_actually_wrote(monkeypatch, tmp_path, cwd_name):
    """One workspace, one report path - even when the plan names it relatively.

    `_owned_entry` contains the report's PARENT, and `verify_path_safe` binds a relative
    path onto the ROOT, not onto the process working directory. A report path built as
    `<ws>/out/x.json` without resolving `ws` is therefore bound a second time:
    `<cwd>/ws/out/x.json` becomes `<cwd>/ws/ws/out/x.json`. That doubled path is still
    inside the workspace, so the containment does not refuse it - the clear finds nothing
    there, a missing entry reads as "nothing was there", and the read then stats the same
    empty path. The gate's own declaration is never seen, the detail is empty, and the
    exit code decides, which is the silent form of "you did not measure this" - the one
    thing the absence channel exists to make impossible. Driven from two different process
    working directories to make the reading variable explicit.
    """
    here = tmp_path / cwd_name
    here.mkdir()
    monkeypatch.chdir(here)                 # a relative workspace means THIS directory
    (here / "ws").mkdir()
    written = here / "ws" / "out" / "test_report.json"
    argv = _writes(str(written), DECLARED_ABSENCE, code=2)

    result = builtin_library().evaluate(
        Binding("command_exit", {"argv": argv, "report": "out/test_report.json",
                                 "expect": 0, "timeout": 120}),
        workspace=Path("ws"), pre_state={})

    assert written.exists(), "the gate must really have written the report the binding names"
    assert result.outcome == "absent", (
        "a non-zero exit whose own report declares it measured nothing is an absence, and a "
        f"relative workspace must not hide it: {result.detail}")
    assert "repo_gate.sh" in result.detail and "out/test_report.json" in result.detail, \
        f"the absence detail must name the gate and the report it was read from: {result.detail!r}"


def test_a_relative_workspace_still_refuses_a_report_that_escapes_it(monkeypatch, tmp_path):
    """Resolving the join must not become a way out of the containment.

    The sibling of the test above: a binding that names `../outside/...` is still refused
    BEFORE resolution, so nothing outside the workspace is removed or read on this run's
    behalf and the exit code decides. The bound path is planted with a declaration BEFORE
    the run and the command writes its own report somewhere else, so this measures the
    removal and the read rather than a file the command would have recreated afterwards: a
    fix that made the relative case work by loosening `_owned_entry` would show up here as a
    removed or rewritten sentinel, and one that read the escaped file would turn the pass
    into an absence.
    """
    here = tmp_path / "cwd"
    here.mkdir()
    monkeypatch.chdir(here)
    (here / "ws").mkdir()
    outside = here / "outside"
    outside.mkdir()
    escaped = outside / "test_report.json"
    escaped.write_text(json.dumps(DECLARED_ABSENCE))     # a declaration a reader would believe
    written = outside / "gate_report.json"
    argv = _writes(str(written), DECLARED_ABSENCE, code=0)

    result = builtin_library().evaluate(
        Binding("command_exit", {"argv": argv, "report": "../outside/test_report.json",
                                 "expect": 0, "timeout": 120}),
        workspace=Path("ws"), pre_state={})

    assert written.exists(), "the command must really have run, and written outside the workspace"
    assert escaped.exists() and json.loads(escaped.read_text()) == DECLARED_ABSENCE, \
        "a report outside the workspace must never be removed or rewritten on this run's behalf"
    assert result.outcome == "pass", (
        f"the exit code decides when the bound path leaves the workspace: {result.detail}")
    assert "could not be cleared" in result.detail and "../outside/test_report.json" in result.detail, \
        f"the detail must say the report was not established, and name it: {result.detail!r}"
