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
    grandchild = int(pidfile.read_text())
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
    the fail detail says why the report could not be cleared.
    """
    named = "blocked/test_report.json"
    (tmp_path / "blocked").mkdir()
    (tmp_path / "blocked" / "test_report.json").mkdir()      # unlink() cannot remove this
    result = builtin_library().evaluate(
        Binding("command_exit", {"argv": _exits(code), "report": named, "timeout": 120}),
        workspace=tmp_path, pre_state={})
    assert result.outcome == outcome, f"exit {code} must decide, not an absence: {result.detail}"
    if outcome == "fail":
        assert "could not be cleared" in result.detail and named in result.detail, \
            f"the fail detail must say why no report was read: {result.detail!r}"
        assert "exit 2, expected 0" in result.detail, result.detail
