"""The freeze-time path audit judges the report a command declares, by the plan's own rule.

`command_exit` accepts a `report` path, and the run-time half of that binding is already safe:
`core.todo_runtime._clear_report` contains the report's PARENT to the workspace and unlinks the
entry without following it, so a binding that escapes the workspace is a reason at run time, never
a deletion - which means the command's report is neither removed nor read and the exit code alone
decides. Safe, but discovered only when the item runs: the plan froze, the work was done, and the
report the step declared was never this run's.

So the audit (`core.todo_templates.check_declared_paths`) judges the report path while the plan is
still being frozen, with the SAME `core.todo_plan._relative` call it also applies to declared
delta paths. That call is the ANSWERING site for a report on EVERY route, because `report` is not
one of the keys `TodoPlan._binding` validates inside `TodoPlan.from_json`. For a DELTA path it is
not: `_binding` applies `_relative` to every `path`/`paths`/`pattern` while the plan is
CONSTRUCTED, so on the production route (`core.todo_cli._plan_from_args`, this audit's only
production caller) the plan refuses first and the audit is never entered. The audit's own
`_relative` call for delta paths is therefore a backstop, reachable only for a plan built without
`from_json`; it is kept, so the same rule answers on both routes. These tests pin both halves of
that, and they measure WHICH site answered instead of inferring it from which sentence came back:

* an absolute report path, and one holding a `..` component, are REFUSED, each refusal naming the
  path it refused and stating the ground - the same ground, spelled the same way, as a delta path
  outside the workspace;
* the production route refuses a delta path with the audit NEVER ENTERED (a recorder wrapped around
  the audit proves it) for both spellings, while a hand-built `TodoPlan(...)` - no `from_json` -
  is refused BY the audit with the same code and the same sentence, and an in-workspace delta path
  on that same hand-built route still freezes;
* a relative report path is NOT narrowed: in the workspace, under a parent that does not exist
  yet, absent altogether, and even identical to a declared delta path (a report is not a delta
  claim, so judging it as one would be a false refusal).
"""
import json

import pytest

import core.todo_cli as todo_cli
from core import todo_templates as templates
from core.todo_plan import Refusal, TodoPlan
from core.todo_runtime import Binding, Item, builtin_library
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
    # Through the MODULE attribute on purpose: `core.todo_cli` resolves the audit the same way
    # (`templates.check_declared_paths`), so a recorder installed there observes these calls too.
    return templates.check_declared_paths(TodoPlan.from_json(plan_data(workspace, predicates), builtin_library()))


def refusal(tmp_path, predicates, *files) -> Refusal:
    with pytest.raises(Refusal) as caught:
        audit(tmp_path, predicates, *files)
    return caught.value


def hand_built(workspace, predicates) -> TodoPlan:
    """A plan built WITHOUT `from_json`: the one route that reaches the audit's own delta backstop.

    `TodoPlan`'s dataclass constructor validates nothing - `TodoPlan._binding` is what applies
    `todo_plan._relative`, and it runs only inside `from_json` - so a plan can arrive at
    `check_declared_paths` carrying an absolute or `..`-holding delta path. No production caller
    builds one this way (`core.todo_cli._plan_from_args` always goes through `from_json`); this
    exists to measure the backstop the audit keeps for exactly that shape.
    """
    return TodoPlan(attempt_id="att-report", node_key="execution.demo", context_hash="c" * 64,
                    base_sha="0" * 40, workspace=str(workspace), scope=("app.py",),
                    goal="a command declares where its report goes",
                    items=(Item(id="verify", title="verify",
                                predicates=tuple(Binding(p["predicate"], dict(p["args"])) for p in predicates)),))


def audit_hand_built(tmp_path, predicates, *files):
    return templates.check_declared_paths(hand_built(workspace_at(tmp_path, *files), predicates))


def production_refusal(tmp_path, offered: dict) -> Refusal:
    """Drive the PRODUCTION route: a plan JSON on disk through `core.todo_cli._plan_from_args`.

    That is the first thing `cmd_create` does and the only place the audit is called from, so
    whatever refuses here is what a director's `create` sees, in the order it happens.
    """
    path = tmp_path / "offered-plan.json"
    path.write_text(json.dumps(offered))
    args = todo_cli._parser().parse_args(["create", "--plan-json", str(path), "--store", str(tmp_path / "store")])
    assert args.plan_json == str(path) and args.template is None, "the plan-JSON route, not a template"
    with pytest.raises(Refusal) as caught:
        todo_cli._plan_from_args(args, builtin_library())
    return caught.value


def audit_entries(monkeypatch) -> list[TodoPlan]:
    """Note every entry into the audit, then delegate to it unchanged.

    `core/todo_cli.py` binds the module (`from core import todo_templates as templates`) and calls
    `templates.check_declared_paths(plan)`, so the attribute replaced here IS the one the production
    route resolves - there is no `check_declared_paths` name on `todo_cli` to patch. With this in
    place "the audit was not entered" is measured rather than inferred from the refusal's wording.
    """
    assert todo_cli.templates is templates, "the route looks the audit up on this module; patch that"
    entered: list[TodoPlan] = []
    delegate = templates.check_declared_paths

    def recorder(plan):
        entered.append(plan)
        return delegate(plan)

    monkeypatch.setattr(todo_cli.templates, "check_declared_paths", recorder)
    return entered


def relative_calls(monkeypatch) -> list[tuple[str, object]]:
    """Note every `(where, value)` the AUDIT's own `_relative` call is asked about, then delegate.

    `core/todo_templates` imports `_relative` into its own namespace, so replacing it here replaces
    the call the audit makes and leaves the plan's copy (`core.todo_plan._relative`, used by
    `TodoPlan._binding`) untouched: that is what lets the two sites be told apart.
    """
    calls: list[tuple[str, object]] = []
    delegate = templates._relative

    def recorder(value, where):
        calls.append((where, value))
        return delegate(value, where)

    monkeypatch.setattr(templates, "_relative", recorder)
    return calls


def grounded(refused: Refusal, where: str) -> str:
    """The sentence a refusal carries once the binding that produced it is taken off the front.

    Derived from the refusal itself, so comparing two of these compares the RULE that produced
    them - not two hand-written spellings of one string.
    """
    prefix = f"{where} "
    assert refused.reason.startswith(prefix), f"the refusal must name {where!r}: {refused.reason}"
    return refused.reason[len(prefix):]


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


DELTA_ABSOLUTE = {"predicate": "file_changed", "args": {"path": "/etc/hostname"}}
DELTA_DOTDOT = {"predicate": "files_changed", "args": {"paths": ["../outside/x.json"]}}


def test_the_delta_refusal_arrives_with_the_audit_never_entered(tmp_path, monkeypatch):
    """The production route refuses a delta path while the plan is CONSTRUCTED, not in the audit.

    Measured with a recorder around the audit (`audit_entries`), for BOTH spellings
    `todo_plan._relative` refuses - an absolute path and one holding `..` - driven the way a
    director drives them: a plan JSON on disk through `core.todo_cli._plan_from_args`. A test that
    only read the refusal's wording could not tell the two sites apart, because they share the
    sentence; this one asserts on the site.
    """
    entered = audit_entries(monkeypatch)
    for offered, where, value in ((DELTA_ABSOLUTE, "item verify: file_changed.path", "/etc/hostname"),
                                  (DELTA_DOTDOT, "item verify: files_changed.paths", "../outside/x.json")):
        refused = production_refusal(tmp_path, plan_data(tmp_path / "ws", [offered]))
        assert refused.code == "invalid_plan", refused.reason
        assert GROUND in refused.reason, refused.reason
        assert where in refused.reason, "the refusal must name the binding it came from"
        assert repr(value) in refused.reason, "the refusal must name the path it refused"
        assert entered == [], ("the audit was entered, so this refusal is not the plan's: "
                               f"{[p.attempt_id for p in entered]}")
    assert not (tmp_path / "ws").exists(), "the plan was refused, so nothing about a workspace was consulted"


def test_the_audit_is_the_backstop_for_a_delta_path_no_from_json_validated(tmp_path, monkeypatch):
    """The other route: a hand-built `TodoPlan(...)` is refused BY `check_declared_paths` itself.

    `TodoPlan`'s constructor validates nothing, so this plan carries a delta path that
    `TodoPlan._binding` would have refused inside `from_json`. The audit's own `_relative` call is
    what answers here - pinned by recording the `(where, value)` the audit asked it about, so the
    refusal cannot be credited to a site that never ran - with the same code and the same sentence,
    and an in-workspace path on that same hand-built route is still accepted.
    """
    calls = relative_calls(monkeypatch)
    for offered, where, value in ((DELTA_ABSOLUTE, "item verify: file_changed.path", "/etc/hostname"),
                                  (DELTA_DOTDOT, "item verify: files_changed.paths", "../outside/x.json")):
        calls.clear()
        with pytest.raises(Refusal) as caught:
            audit_hand_built(tmp_path, [offered], "app.py")
        assert caught.value.code == "invalid_plan", caught.value.reason
        assert GROUND in caught.value.reason and where in caught.value.reason, caught.value.reason
        assert repr(value) in caught.value.reason, caught.value.reason
        assert (where, value) in calls, f"the audit's own `_relative` call is not what answered: {calls}"

    accepted = audit_hand_built(tmp_path, [{"predicate": "file_changed", "args": {"path": "app.py"}}], "app.py")
    assert accepted.measured is True, "a hand-built plan's in-workspace path is judged, not skipped"
    assert accepted.declared == ("app.py",) and accepted.existing == ("app.py",)
    assert accepted.notes == (), "an accepted path invents no note"


def test_both_refusals_come_from_the_plan_s_own_relative_path_rule(tmp_path, monkeypatch):
    """Not a second rule for reports: the same function, the same code, the same sentence - and now
    also the same measured SITE story.

    Compared as REFUSALS: the binding prefix is taken off each reason (`grounded`), so what is
    compared is the sentence the rule produced, not two hand-written spellings of one string. And
    the recorder around the audit shows which site answered: a report refusal really does arrive
    from the audit on the production route (its call is the answering one), the delta refusal on
    that route arrives with the audit NEVER entered, and the same delta refusal from a hand-built
    plan arrives from the audit's backstop call.
    """
    entered = audit_entries(monkeypatch)

    report_absolute = refusal(tmp_path, [command_exit(report="/etc/hostname")])
    assert [p.attempt_id for p in entered] == ["att-report"], "a report refusal must come from the audit"
    entered.clear()
    report_dotdot = refusal(tmp_path, [command_exit(report="../outside/x.json")])
    assert [p.attempt_id for p in entered] == ["att-report"], "a report refusal must come from the audit"

    entered.clear()
    delta_production = production_refusal(tmp_path, plan_data(tmp_path / "ws", [DELTA_ABSOLUTE]))
    assert entered == [], "the production delta refusal must arrive with the audit never entered"

    entered.clear()
    with pytest.raises(Refusal) as caught:
        audit_hand_built(tmp_path, [DELTA_ABSOLUTE], "app.py")
    delta_backstop = caught.value
    assert [p.attempt_id for p in entered] == ["att-report"], "the backstop delta refusal is the audit's"

    with pytest.raises(Refusal) as caught:
        audit_hand_built(tmp_path, [DELTA_DOTDOT], "app.py")
    delta_dotdot = caught.value

    assert {report_absolute.code, report_dotdot.code, delta_production.code,
            delta_backstop.code, delta_dotdot.code} == {"invalid_plan"}
    assert grounded(report_absolute, "item verify: command_exit.report") == \
        grounded(delta_backstop, "item verify: file_changed.path"), \
        "a report path is judged by `todo_plan._relative`, exactly as a declared delta path is"
    assert grounded(report_dotdot, "item verify: command_exit.report") == \
        grounded(delta_dotdot, "item verify: files_changed.paths"), \
        "both spellings share one sentence, whichever binding asked for it"
    assert delta_production.reason == delta_backstop.reason, \
        "the plan's rule and the audit's backstop produce one sentence, not two"


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
