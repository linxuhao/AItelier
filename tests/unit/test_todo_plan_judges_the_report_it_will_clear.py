"""The freeze-time path audit judges the report a command declares, by the plan's own rule.

`command_exit` accepts a `report` path, and the run-time half of that binding is already safe:
`core.todo_runtime._clear_report` contains the report's PARENT to the workspace and unlinks the
entry without following it, so a binding that escapes the workspace is a reason at run time, never
a deletion - which means the command's report is neither removed nor read and the exit code alone
decides. Safe, but discovered only when the item runs: the plan froze, the work was done, and the
report the step declared was never this run's.

So the audit (`core.todo_templates.check_declared_paths`) judges the report path while the plan is
still being frozen, with the SAME `core.todo_plan._relative` call it already applies to declared
delta paths. These tests pin both halves of that:

* an absolute report path, and one holding a `..` component, are REFUSED, each refusal naming the
  path it refused and stating the ground - the same ground, spelled the same way, as a delta path
  outside the workspace;
* a relative report path is NOT narrowed: in the workspace, under a parent that does not exist
  yet, absent altogether, and even identical to a declared delta path (a report is not a delta
  claim, so judging it as one would be a false refusal).
"""
import pytest

from core.todo_plan import Refusal, TodoPlan
from core.todo_runtime import builtin_library
from core.todo_templates import check_declared_paths

#: The sentence `todo_plan._relative` refuses with, shared verbatim by every path it judges.
GROUND = "must be a relative path inside the workspace"
COMMAND = [["true"], 0]          # argv and expected exit code: nothing here runs the command


def item(item_id: str, predicates, needs=()) -> dict:
    return {"id": item_id, "title": item_id, "needs": list(needs), "predicates": predicates}


def command_exit(**args) -> dict:
    argv, expect = COMMAND
    return {"predicate": "command_exit", "args": {"argv": argv, "expect": expect, **args}}


def plan_data(workspace, predicates) -> dict:
    return {"attempt_id": "att-report", "node_key": "execution.demo", "context_hash": "c" * 64,
            "base_sha": "0" * 40, "workspace": str(workspace), "scope": ["app.py"],
            "goal": "a command declares where its report goes",
            "items": [item("verify", predicates)]}


def workspace_at(tmp_path, *files):
    """A real workspace; a file listed here exists before the plan is frozen."""
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)   # a test may build the workspace itself before it audits
    for name in files:
        (workspace / name).write_text("x = 1\n")
    return workspace


def audit(tmp_path, predicates, *files):
    workspace = workspace_at(tmp_path, *files)
    return check_declared_paths(TodoPlan.from_json(plan_data(workspace, predicates), builtin_library()))


def refusal(tmp_path, predicates, *files) -> Refusal:
    with pytest.raises(Refusal) as caught:
        audit(tmp_path, predicates, *files)
    return caught.value


# -- refused: a report the run could never establish as its own ------------------------------

def test_an_absolute_report_path_is_refused_while_the_plan_is_frozen(tmp_path):
    """`/etc/hostname` is neither removed nor read at run time, so freezing it freezes a dead item."""
    caught = refusal(tmp_path, [command_exit(report="/etc/hostname")])
    assert caught.code == "invalid_plan", caught.reason
    assert "/etc/hostname" in caught.reason, "the refusal must name the report path it refused"
    assert GROUND in caught.reason, caught.reason
    assert "command_exit.report" in caught.reason, "the refusal must name the binding it came from"


def test_a_report_path_holding_a_dotdot_component_is_refused(tmp_path):
    caught = refusal(tmp_path, [command_exit(report="../outside/x.json")])
    assert caught.code == "invalid_plan", caught.reason
    assert "../outside/x.json" in caught.reason
    assert GROUND in caught.reason, caught.reason
    assert "command_exit.report" in caught.reason


def test_a_report_under_a_symlinked_parent_that_escapes_the_workspace_is_refused(tmp_path):
    """c1: the parent is a link that resolves OUTSIDE the workspace, so the run would never clear it.

    `report: "link/x.json"` is lexically relative with no `..`, so `_relative` alone let it freeze.
    The run contains the report's parent with `verify_path_safe` and refuses to clear it, so the
    audit must apply that SAME call to the parent. Pinned against the escape, not the spelling: the
    report text carries no `..` and no leading `/`.
    """
    workspace = workspace_at(tmp_path, "app.py")
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "link").symlink_to(outside, target_is_directory=True)
    caught = refusal(tmp_path, [command_exit(report="link/x.json")], "app.py")
    assert caught.code == "invalid_plan", caught.reason
    assert "link/x.json" in caught.reason, "the refusal must name the report it refused"
    assert "command_exit.report" in caught.reason, "the refusal must name the binding it came from"
    assert "its parent resolves to a place outside the workspace" in caught.reason, caught.reason


def test_a_report_under_a_symlinked_parent_that_stays_inside_the_workspace_is_accepted(tmp_path):
    """c2: a link that resolves INSIDE the workspace is the run's own tree and must still freeze.

    The containment is on the resolved parent, not on the presence of a symlink, so refusing every
    linked parent would narrow a plan the run can clear. Asserted against `verify_path_safe` truth
    too, so the accepted shape is the one the run would also accept.
    """
    workspace = workspace_at(tmp_path, "app.py")
    (workspace / "real").mkdir()
    (workspace / "link").symlink_to("real", target_is_directory=True)
    result = audit(tmp_path, [command_exit(report="link/x.json")], "app.py")
    assert result.measured is True and result.declared == ()
    assert result.notes == (), "an accepted report invents no note"


def test_a_report_whose_final_component_is_a_symlink_outside_the_workspace_is_accepted(tmp_path):
    """c3: the run removes the LINK and never follows it, so this shape is safe and must stay accepted.

    `_owned_entry` contains the PARENT only and `unlink`s the entry, so `out/x.json -> /elsewhere`
    removes the link and leaves its target. Refusing it would be a false refusal of a safe plan.
    """
    workspace = workspace_at(tmp_path, "app.py")
    outside = tmp_path / "outside-target.json"
    outside.write_text("{}")
    (workspace / "out").mkdir()
    (workspace / "out" / "x.json").symlink_to(outside)
    result = audit(tmp_path, [command_exit(report="out/x.json")], "app.py")
    assert result.measured is True and result.declared == ()
    assert outside.exists(), "the audit itself must not touch the link's target"


def test_a_report_under_a_symlink_loop_parent_is_refused_because_the_run_could_never_clear_it(tmp_path):
    """c5: a parent that cannot be resolved at all is a reason the run cannot clear the report either.

    `out -> out` is a loop: resolving it raises, and `_owned_entry` turns that into "could not be
    resolved". The audit refuses the same report at freeze, so the dead item is not frozen.
    """
    workspace = workspace_at(tmp_path, "app.py")
    (workspace / "out").symlink_to("out")                # a directory that is only itself
    caught = refusal(tmp_path, [command_exit(report="out/x.json")], "app.py")
    assert caught.code == "invalid_plan", caught.reason
    assert "out/x.json" in caught.reason and "cannot be resolved inside the workspace" in caught.reason, caught.reason


def test_a_parent_the_run_links_out_after_freeze_is_only_caught_at_run_time(tmp_path):
    """c4: the boundary freeze time cannot see, measured on the REAL `command_exit`, no stub.

    The audit reads the disk as it stands at freeze time. The report's parent `d` does not exist
    yet, so the audit accepts it. Then `d` is created as a symlink to a directory OUTSIDE the
    workspace - this stands in for the run's own command making the link - and the real
    `command_exit` predicate runs `["true"]` (a real subprocess, exit 0).

    What must hold, asserted on the actual `PredicateResult`:
      * the run REFUSES to clear the report: its detail says so and names the cause;
      * no report is READ: the verdict is the exit code (0 matches `expect`), so it passes, and
        the detail carries the "could not be cleared before the run" reason instead of an absence;
      * the file the link points at, outside the workspace, is neither removed nor read.
    """
    from core.todo_runtime import PredicateResult

    workspace = workspace_at(tmp_path, "app.py")
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "x.json"
    victim.write_text('{"repo_gate_absent": true, "repo_gate": {"script": "stranger"}}')
    assert not (workspace / "d").exists(), "the parent must be missing at freeze for this to prove anything"

    data = plan_data(workspace, [command_exit(report="d/x.json")])
    plan = TodoPlan.from_json(data, builtin_library())
    audit_result = check_declared_paths(plan)            # the audit accepts it: nothing links out yet
    assert audit_result.measured is True

    # The run creates the link after the freeze. This is the boundary, not a freeze-time refusal.
    (workspace / "d").symlink_to(outside, target_is_directory=True)

    binding = plan.items[0].predicates[0]
    result = builtin_library().evaluate(binding, workspace=workspace, pre_state={})
    assert isinstance(result, PredicateResult)
    assert result.predicate == "command_exit"
    assert result.outcome == "pass", result.detail         # the exit code (0) decides; no report was read
    assert "could not be cleared before the run" in result.detail, result.detail
    assert "outside the workspace" in result.detail, result.detail
    assert "no report was read" in result.detail, result.detail
    assert "declared in d/x.json that it measured nothing" not in result.detail, \
        "the stranger's absence declaration must not be read as this run's"
    assert victim.exists() and "stranger" in victim.read_text(), \
        "a file outside the workspace is neither removed nor read"


def test_both_refusals_come_from_the_plan_s_own_relative_path_rule(tmp_path):
    """Not a second rule for reports: the same call, the same code, the same sentence.

    The delta refusal is measured in the same run so this cannot pass by comparing two spellings
    of one hand-written string: the report refusal must equal the delta refusal with only the
    binding name substituted.
    """
    delta = refusal(tmp_path, [{"predicate": "file_changed", "args": {"path": "/etc/hostname"}}])
    absolute = refusal(tmp_path, [command_exit(report="/etc/hostname")])
    dotdot = refusal(tmp_path, [command_exit(report="../outside/x.json")])
    assert {delta.code, absolute.code, dotdot.code} == {"invalid_plan"}
    assert absolute.reason == delta.reason.replace("item verify: file_changed.path",
                                                   "item verify: command_exit.report"), \
        "a report path is judged by `todo_plan._relative`, exactly as a declared delta path is"
    assert dotdot.reason == f"item verify: command_exit.report {GROUND}, got '../outside/x.json'"


def test_the_report_rule_reads_the_plan_so_it_refuses_even_without_a_workspace(tmp_path):
    """Containment is decided by the plan, not by what is on disk: a missing workspace costs nothing here."""
    data = plan_data(tmp_path / "no-such-workspace", [command_exit(report="/etc/hostname")])
    with pytest.raises(Refusal) as caught:
        check_declared_paths(TodoPlan.from_json(data, builtin_library()))
    assert caught.value.code == "invalid_plan" and "/etc/hostname" in caught.value.reason


def test_a_report_that_is_not_a_nonempty_string_is_refused_by_the_plan_not_by_the_audit(tmp_path):
    """The audit holds ONE report value, so the plan's type check is what keeps a list out of it.

    `check_declared_paths` judges the report with a single `_relative` call and holds no
    element-wise loop, which is sound only while `report` is a non-empty string: `_relative` judges
    `PurePosixPath(str(value))`, so a list would arrive as its own repr - the repr of
    `["../x"]` splits on `/` into `"['.."` and `"x']"`, so no component is `..` and containment
    would pass. The refusal therefore has to happen in the plan, and this test puts it there:
    `TodoPlan.from_json` raises BEFORE the audit is reachable. Loosening the type check to admit a
    list fails here first.
    """
    for bad in (["a.json", "b.json"], ["../outside/x.json"], 7, None, ""):
        with pytest.raises(Refusal) as caught:      # the PLAN refuses; `check_declared_paths` never runs
            TodoPlan.from_json(plan_data(tmp_path / "ws", [command_exit(report=bad)]), builtin_library())
        assert caught.value.code == "invalid_plan", caught.value.reason
        assert "command_exit.report" in caught.value.reason, "the refusal must name the binding"
        assert "wrong type or is empty" in caught.value.reason, caught.value.reason


# -- accepted: the audit must not narrow a legitimate plan -----------------------------------

def test_a_relative_report_path_in_the_workspace_is_accepted(tmp_path):
    result = audit(tmp_path, [command_exit(report="out/test_report.json")], "app.py")
    assert result.measured is True
    assert result.declared == (), "a report is not a declared delta path and must not be reported as one"


def test_a_report_under_a_parent_that_does_not_exist_yet_is_accepted(tmp_path):
    """The command may create the directory it writes into; that is an ordinary change."""
    workspace = workspace_at(tmp_path, "app.py")
    assert not (workspace / "reports").exists(), "the parent must be missing for this to prove anything"
    result = audit(tmp_path, [command_exit(report="reports/deep/new.json")], "app.py")
    assert result.declared == () and result.notes == (), "no refusal, and no note invented about it"


def test_a_command_exit_binding_without_a_report_is_accepted(tmp_path):
    result = audit(tmp_path, [command_exit()], "app.py")
    assert result.measured is True and result.declared == ()


def test_a_report_that_is_also_a_declared_delta_path_is_accepted(tmp_path):
    """The false-refusal guard: a report may legitimately NAME a file the item also declares.

    Adding report paths to the delta `declared`/duplicate machinery would refuse this plan - one
    path, declared once and written once - which is why containment is the only thing judged.
    """
    result = audit(tmp_path, [{"predicate": "files_changed", "args": {"paths": ["app.py"]}},
                              command_exit(report="app.py")], "app.py")
    assert result.declared == ("app.py",), "the report did not enter the delta bookkeeping"
