# tests/skillflow/test_coding_impl_gate_absence.py
#
# The absence accounting, measured on the REAL `configs/coding_impl.yaml`, the
# REAL `AItelierSkillFlow` and the REAL `run_tests` tool, driven by the REAL
# `core/scheduler.py:_run_skillflow_tick` — one call per tick. The tick's own
# deferral hold (`observe_run` → `silent` → return), its inline-tool drain, its
# claim phase and its expiry branch are the production code; so is the host's
# hold inside `AItelierSkillFlow.advance_run`, which the tick reaches through
# `_advance_off_the_loop`. The only stand-in is the LLM agent that would run a
# claimed `implement` step (`aitelier.runner.AgentStepRunner`): it writes one
# code change through the real tool path and returns.
#
# The subject is a gate that NEVER produces a verdict — NOT a fixture that lets
# go on the Nth call (the forbidden technique: `if [ "$n" -lt 2 ]`). The poles
# below are the SAME never-answering gate; only the episode's wall-clock
# ceiling and the observation window differ, because those are the only knobs
# that are supposed to matter.
#
# What must hold at every pole, including the one where the absence outlives
# the ceiling:
#
#   * `implement_runs == 1` — the absence spends no implement cycle, ever;
#   * the run NEVER ends on `Cycle limit exceeded`;
#   * when it does end, it ends NAMING THE ABSENCE;
#   * the rows the hold parks — the step rows and the run row — are left as
#     the absence found them (`_assert_test_row_untouched`).
#
# ROUND 6's correction, and the reason the drain matters: round 5's driver
# called `advance_run` once per tick and did not drain, while the real tick
# drains consecutive inline tool steps IN ONE TICK — it carries the run from
# `test` through `test_evidence`/`test_outcome` and on to `implement`. A driver
# that does not drain reports implement_runs=2 for a run that spends 1. Round
# 10 stops re-stating the tick at all: the driver below calls it.
import asyncio
import json
import time
import types
from pathlib import Path

import yaml

from skillflow import PipelineGraph
import skillflow as _skillflow_pkg
from skillflow.core import StepResult
from skillflow.tool_loader import ToolLoader

from core import gate_deferral
from core.skillflow_host import AItelierSkillFlow

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _wire(tmp_path, monkeypatch, *, episode_max, wait, counter, tail):
    """Real graph, real host class, real `run_tests`, real repo gate.

    `counter` lives OUTSIDE the worktree so the gate leaves no uncommitted file
    behind it, and `tail` is a script that NEVER answers — it does not count
    attempts and does not change its mind on a later call.
    """
    monkeypatch.setattr(gate_deferral, "GATE_DEFERRAL_EPISODE_MAX_SECONDS",
                        episode_max)
    monkeypatch.setattr(gate_deferral, "GATE_DEFERRAL_WAIT_SECONDS", wait)
    monkeypatch.setattr(gate_deferral, "LEDGER", gate_deferral.DeferralLedger())
    # One step makes ONE gate run now, so this pause is not on the default
    # path; it stays read at call time for a deployment that raises the
    # attempts constant deliberately.
    monkeypatch.setenv("AITELIER_REPO_GATE_RETRY_DELAY_SECONDS", "0")

    loader = ToolLoader(Path(_skillflow_pkg.__file__).parent / "tools")
    loader.add_tools_dir(_REPO_ROOT / "aitelier" / "tools")
    sf = AItelierSkillFlow(
        str(tmp_path / "sf.db"), tool_loader=loader,
        workspace_base=str(tmp_path / "ws"),
        projects_base=str(tmp_path / "proj"),
        stale_threshold_seconds=60)

    from tests.code_output_fixture import init_code_repo
    project = tmp_path / "proj" / "p"
    init_code_repo(project)
    (project / "tests").mkdir(parents=True, exist_ok=True)
    (project / "tests" / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n")
    (project / ".gitignore").write_text("__pycache__/\n*.pyc\n")
    script = project / "run_tests.sh"
    script.write_text("#!/bin/sh\n"
                      "cd \"$(dirname \"$0\")\"\n"
                      f"n=$(cat '{counter}' 2>/dev/null || echo 0)\n"
                      "n=$((n + 1))\n"
                      f"echo \"$n\" > '{counter}'\n" + tail)
    script.chmod(0o755)
    from skillflow.output_targets import git
    git(project, "add", "-A")
    git(project, "commit", "-qm", "fixture: a gate that never answers")

    for name, cfg in (yaml.safe_load(
            (_REPO_ROOT / "agent_configs" / "coding_impl.yaml").read_text(
                encoding="utf-8")) or {}).items():
        try:
            sf.register_agent_config_from_dict(name, cfg)
        except Exception:
            pass
    graph = PipelineGraph.from_yaml(_REPO_ROOT / "configs" / "coding_impl.yaml")
    sf.register_graph(graph)
    from core.seed_publication import publish_seeds, seed_dir
    publish_seeds(seed_dir(sf, "p", graph.name), {
        "plan.md": "Implement the isolated fixture and run the declared gate.\n"})
    run_id = sf.create_run(graph.name, {"project_id": "p"})
    sf.start_run(run_id)
    return sf, run_id


def _gate_calls(counter):
    return int(counter.read_text().strip()) if counter.exists() else 0


# A gate that declares its own absence on EVERY call, forever. It never becomes
# a verdict, so no pole below can "pass the second time".
_SILENT_TAIL = (
    "printf '%s\\n' '"
    "AITELIER_REPO_GATE_UNMEASURED={\"state\":\"blocked\",\"reason\":"
    "\"godot-builder unreachable: gate NOT run\"}'\n"
    "exit 3\n")


def _drive(sf, run_id, monkeypatch, *, ticks, clock_step, second_driver=True):
    """Call the REAL `core/scheduler._run_skillflow_tick` once per tick.

    Two production holds stand between a silent gate and the implement loop,
    and this driver walks both:

    * the TICK hold — `_run_skillflow_tick` reads `gate_deferral.observe_run`
      and returns early on `silent` (it never reaches `advance_run`);
    * the HOST hold — `AItelierSkillFlow.advance_run` refuses while
      `gate_deferral.hold_blocks_advance` says the episode is live. The tick
      never reaches it while its own hold is working, so a second production
      driver does: after every tick whose own log line says it held the run,
      `core/run_driver._step` (the driver butler-driven runs use) steps the
      same run once, exactly as a second caller arriving during the hold
      would. `second_driver=False` leaves the tick as the only driver.

    Returns `(implement_runs, statuses, outcomes, nodes)`:

    * `implement_runs` — how many times a claimed `implement` step was
      executed, by either driver;
    * `statuses` — the run's status before each tick, then its final status;
    * `outcomes` — what each tick decided, read off the tick's OWN `tick_log`
      line: `silent` for `gate_deferral_hold`, `reacquire` for
      `gate_deferral_reacquire`, `expired` followed by `terminal:<reason>`
      for `gate_absence_terminal`, `none` otherwise;
    * `nodes` — the run's `current_node` after each tick.

    What is replaced, and why each is not the hold:

    * `get_skillflow` / `_get_or_create_skillflow_run` point the tick at this
      test's host and run instead of the server's;
    * `_sync_project_status_to_db` / `_reconcile_lease` / `_odbg` write the
      server's project table, lease and debug log, none of which exist here;
    * `tick_log` is recorded instead of written to the operator's log file;
    * `aitelier.runner.AgentStepRunner` is the LLM agent: it writes one code
      change for a claimed step and returns an empty result;
    * `gate_deferral.time` is a clock the test moves by `clock_step` per tick.
      The ceiling is measured from the episode's start, so moving the clock is
      the only way to observe an expiry without sleeping for hours. Every
      reader in `gate_deferral` (the tick's `observe_run` and the host's
      `hold_blocks_advance`) reads the same clock.
    """
    from core import run_driver, scheduler
    import aitelier.runner as agent_runner
    from tests.code_output_fixture import write_claim_code

    clock = [time.time()]
    monkeypatch.setattr(gate_deferral, "time",
                        types.SimpleNamespace(time=lambda: clock[0]))
    logged = []
    monkeypatch.setattr(scheduler, "tick_log",
                        lambda project_id, outcome, **detail:
                        logged.append((outcome, detail)))
    monkeypatch.setattr(scheduler, "_odbg", lambda *a, **k: None)
    monkeypatch.setattr(scheduler, "get_skillflow", lambda: sf)
    monkeypatch.setattr(scheduler, "_get_or_create_skillflow_run",
                        lambda project_id: run_id)
    monkeypatch.setattr(scheduler, "_sync_project_status_to_db",
                        lambda project_id: None)
    monkeypatch.setattr(scheduler, "_reconcile_lease", lambda *a: None)
    monkeypatch.setattr(scheduler, "_claim_retry_after", {})

    executed = []
    agent_errors = []

    class _Implementer:
        def __init__(self, **_ignored):
            pass

        async def execute(self, claimed):
            executed.append(claimed.step_id)
            try:
                write_claim_code(sf, run_id, claimed)
            except Exception as exc:
                agent_errors.append(repr(exc))
                raise
            return StepResult(flags={})

    monkeypatch.setattr(agent_runner, "AgentStepRunner", _Implementer)

    statuses, outcomes, nodes = [], [], []
    for _ in range(ticks):
        run = sf.get_run(run_id)
        statuses.append(run["status"])
        if run["status"] != "running":
            break
        clock[0] += clock_step
        before = len(logged)
        asyncio.run(scheduler._run_skillflow_tick("p", None))
        said = [outcome for outcome, _detail in logged[before:]]
        if "gate_deferral_hold" in said:
            outcomes.append("silent")
            if second_driver:
                asyncio.run(run_driver._step(sf, None, None, run_id,
                                             False, 1))
        elif "gate_absence_terminal" in said:
            detail = dict(logged[before:])["gate_absence_terminal"]
            outcomes.append("expired")
            outcomes.append("terminal:" + str(detail.get("reason") or ""))
        elif "gate_deferral_reacquire" in said:
            outcomes.append("reacquire")
        else:
            outcomes.append("none")
        nodes.append(sf.get_run(run_id).get("current_node"))
    else:
        statuses.append(sf.get_run(run_id)["status"])
    assert agent_errors == [], agent_errors
    assert set(executed) <= {"implement"}, executed
    return executed.count("implement"), statuses, outcomes, nodes


def _assert_never_cycles(statuses, outcomes, reason_source):
    text = " ".join(statuses + [str(o) for o in outcomes] + [str(reason_source)])
    assert "Cycle limit exceeded" not in text


def _prime_ledger(sf, run_id):
    """Prime the global LEDGER by calling observe_run once, exactly as the
    production scheduler does on the first tick. Returns the first observation."""
    return gate_deferral.observe_run(sf, run_id)


def test_pole_a_a_ceiling_far_past_the_window_spends_one_implement_cycle(
        tmp_path, monkeypatch):
    """(a) Ceiling >> observation window: the absence is silent throughout.

    `implement_runs` must be 1 and the run must NOT be terminal — the run's gate
    has not answered, so there is nothing to end. The run must also be parked at
    the loop-external gate, which is only reachable if the absence edge actually
    fired: on the r5 candidate the same two lines sat on `test_evidence`, where
    `from_file` resolves against a step that writes nothing, so the edge could
    never match and this pole spent 4 implement cycles on `Cycle limit
    exceeded` instead.
    """
    counter = tmp_path / "calls.txt"
    sf, run_id = _wire(tmp_path, monkeypatch, episode_max=100000,
                       wait=1, counter=counter, tail=_SILENT_TAIL)
    first = _prime_ledger(sf, run_id)
    assert first["state"] == "none"          # no report yet: nothing to account

    implement_runs, statuses, outcomes, nodes = _drive(
        sf, run_id, monkeypatch, ticks=40,
        clock_step=gate_deferral.wait_seconds())
    _assert_test_row_untouched(sf, run_id)

    assert implement_runs == 1, implement_runs
    assert statuses[-1] == "running", statuses
    assert "expired" not in outcomes, outcomes
    assert _gate_calls(counter) >= 1
    assert nodes[-1] == "test_gate_absent", nodes
    assert sf.get_run(run_id)["current_node"] == "test_gate_absent"
    _assert_never_cycles(statuses, outcomes,
                         sf.get_run(run_id).get("error_reason"))


def test_pole_b_a_zero_ceiling_is_clamped_and_cannot_charge_the_absence(
        tmp_path, monkeypatch):
    """(b) ceiling 0. Review measured this pole on the r4 candidate as
    `implement_runs=4` + `Cycle limit exceeded`.

    Here the pole is a MIS-SET KNOB, and the reading is that a mis-set knob may
    not be what charges an absence to the implementer: `episode_max_seconds()`
    refuses the zero and falls back, so the run stays silent and non-terminal
    with `implement_runs == 1` instead of being declared a test failure.
    """
    counter = tmp_path / "calls.txt"
    sf, run_id = _wire(tmp_path, monkeypatch, episode_max=0, wait=1,
                       counter=counter, tail=_SILENT_TAIL)
    assert gate_deferral.episode_max_seconds() > 0
    _prime_ledger(sf, run_id)

    implement_runs, statuses, outcomes, nodes = _drive(
        sf, run_id, monkeypatch, ticks=15, clock_step=1)
    _assert_test_row_untouched(sf, run_id)

    assert implement_runs == 1, implement_runs
    assert statuses[-1] == "running", statuses
    assert "expired" not in outcomes, outcomes
    assert nodes[-1] == "test_gate_absent", nodes
    _assert_never_cycles(statuses, outcomes,
                         sf.get_run(run_id).get("error_reason"))


def test_pole_c_a_tiny_ceiling_inside_the_window_is_honoured_and_uncharged(
        tmp_path, monkeypatch):
    """(c) ceiling tiny (3 s) and HONOURED, observed for a window shorter than
    the ceiling (20 ticks x 0.1 s = 2 s): the absence never expires, so the run
    stays silent and non-terminal, `implement_runs == 1`, and the rows the hold
    parks are untouched. This is the non-expiring small-ceiling pole of
    `an-expired-absence-is-never-charged-to-the-implementer`; the expiring one
    is `test_pole_c_a_tiny_ceiling_ends_the_run_naming_the_absence` (C′).
    """
    counter = tmp_path / "calls.txt"
    sf, run_id = _wire(tmp_path, monkeypatch, episode_max=3, wait=1,
                       counter=counter, tail=_SILENT_TAIL)
    assert gate_deferral.episode_max_seconds() == 3
    _prime_ledger(sf, run_id)

    implement_runs, statuses, outcomes, nodes = _drive(
        sf, run_id, monkeypatch, ticks=20, clock_step=0.1)
    _assert_test_row_untouched(sf, run_id)

    assert implement_runs == 1, implement_runs
    assert statuses[-1] == "running", statuses
    assert "expired" not in outcomes, outcomes
    assert "silent" in outcomes, outcomes
    assert nodes[-1] == "test_gate_absent", nodes
    _assert_never_cycles(statuses, outcomes,
                         sf.get_run(run_id).get("error_reason"))


def test_pole_c_a_tiny_ceiling_ends_the_run_naming_the_absence(
        tmp_path, monkeypatch):
    """(C′) ceiling tiny, and small enough to be HONORED (unlike 0, which is a
    mis-set knob), observed past the ceiling. Review measured this pole on the
    r4 candidate as `implement_runs=4` + `Cycle limit exceeded`.

    It must now be `implement_runs == 1` and a terminal status whose reason
    names the absence.
    """
    counter = tmp_path / "calls.txt"
    sf, run_id = _wire(tmp_path, monkeypatch, episode_max=3, wait=1,
                       counter=counter, tail=_SILENT_TAIL)
    assert gate_deferral.episode_max_seconds() == 3
    _prime_ledger(sf, run_id)

    implement_runs, statuses, outcomes, nodes = _drive(
        sf, run_id, monkeypatch, ticks=20, clock_step=3)
    _assert_test_row_untouched(sf, run_id, run_status="failed")

    assert implement_runs == 1, implement_runs
    assert nodes[-1] == "test_gate_absent", nodes
    endings = [o for o in outcomes if str(o).startswith("terminal:")]
    assert endings, outcomes
    assert gate_deferral.absent_terminal_names_no_failure(endings[0])
    assert statuses[-1] == "failed", statuses
    reason = sf.get_run(run_id).get("error_reason") or ""
    assert gate_deferral.ABSENCE_TERMINAL in reason
    _assert_never_cycles(statuses, outcomes, reason)


def test_a_gate_that_finally_answers_clears_the_deferral(tmp_path, monkeypatch):
    """The other pole: the deferral is not a one-way door. Once a report that
    GRADED the code exists, the run advances again and the absence is cleared.
    """
    counter = tmp_path / "calls.txt"
    sf, run_id = _wire(tmp_path, monkeypatch, episode_max=100000, wait=1,
                       counter=counter, tail=_SILENT_TAIL)
    _prime_ledger(sf, run_id)
    _drive(sf, run_id, monkeypatch, ticks=8, clock_step=1)
    assert gate_deferral.hold_blocks_advance(run_id)
    _assert_test_row_untouched(sf, run_id)

    # The gate's answer arrives: the report now says the code was graded.
    report_path = gate_deferral.find_test_report(sf, run_id)
    assert report_path is not None, "the run must have recorded its out_dir"
    data = json.loads(Path(report_path).read_text(encoding="utf-8"))
    data.pop("repo_gate_absent", None)
    data.pop("repo_gate_unmeasured", None)
    data["passed"] = True
    data["failures"] = []
    data["summary"] = "the gate ran, nothing failed"
    report_path.write_text(json.dumps(data), encoding="utf-8")

    cleared = gate_deferral.observe_run(sf, run_id)
    assert cleared["state"] == "none"
    assert not gate_deferral.hold_blocks_advance(run_id)


def _step_rows(sf, run_id):
    cols = [d[0] for d in sf._conn.execute(
        "SELECT * FROM skillflow_steps WHERE run_id=?", (run_id,)).description]
    rows = sf._conn.execute(
        "SELECT * FROM skillflow_steps WHERE run_id=?", (run_id,)).fetchall()
    return cols, [dict(zip(cols, tuple(r))) for r in rows]


def _rows_by_step(sf, run_id):
    return {r["step_id"]: r for r in _step_rows(sf, run_id)[1]}


def _claims_per_instance(sf, run_id):
    rows = sf.trace_query(
        run_id,
        "SELECT step_id, step_instance_id, COUNT(*) FROM skillflow_trace "
        "WHERE run_id = ? AND event = 'claimed' "
        "AND step_instance_id IS NOT NULL GROUP BY step_instance_id",
        (run_id,))
    return {(r[0], r[1]): r[2] for r in rows or []}


def _assert_test_row_untouched(sf, run_id, *, run_status="running"):
    """Assert the row invariants while the gate is silent.
    Called from every pole so no hold-breaking mutation passes unnoticed.

    The four step-row assertions the criterion names: the `test` row is
    `completed`, its `retry_count` is 0, its `release_count` is 0, and every
    step instance was claimed exactly once (`claim_epoch == 1` on the `test`
    row, and one `claimed` trace event per instance).

    Then the RUN row. Measured in round 10: a hold that lets the run advance
    ends the run through the absence gate's only transition and writes no step
    row at all — every step row is identical to the held run's, and the
    `test_gate_absent` row stays `pending` — so no step-row assertion can see
    that break. The run row can: while the gate is silent it is `running` and
    parked at `test_gate_absent`; at the expiring pole it is `failed` there.
    """
    rows = _rows_by_step(sf, run_id)
    test_row = rows["test"]
    assert test_row["status"] == "completed", (
        f"test row status drifted: {test_row['status']}")
    assert test_row["retry_count"] == 0, (
        f"deferral charged a retry: {test_row['retry_count']}")
    assert test_row["release_count"] == 0, (
        f"deferral released the claim: {test_row['release_count']}")
    assert test_row["claim_epoch"] == 1, (
        f"step re-claimed during deferral: {test_row['claim_epoch']}")
    claims = _claims_per_instance(sf, run_id)
    reclaimed = {key: n for key, n in claims.items() if n != 1}
    assert claims and reclaimed == {}, (
        f"an instance was claimed more than once: {reclaimed}")
    run = sf.get_run(run_id)
    assert (run["status"], run["current_node"]) == (
        run_status, "test_gate_absent"), (
        f"the run row moved while the gate was silent: "
        f"status={run['status']} node={run['current_node']}")


def test_the_deferral_hold_leaves_real_step_rows_untouched(tmp_path, monkeypatch):
    """Criterion 2, measured on the REAL graph's step rows — not a stub.

    While the never-answering gate is silent, the `test` step has already run
    and completed, and the hold parks the run at `test_gate_absent`. What must
    be TRUE of the rows the hold does NOT touch:

    * the `test` row is `completed`;
    * its `retry_count` is 0 — the deferral never charges a retry;
    * its `release_count` is 0 — the deferral never releases the claim;
    * its `claim_epoch` is 1 — the step instance was claimed exactly once.

    Each is a separate assertion so the mutation that breaks it names itself.
    """
    counter = tmp_path / "calls.txt"
    sf, run_id = _wire(tmp_path, monkeypatch, episode_max=100000, wait=1,
                       counter=counter, tail=_SILENT_TAIL)
    _prime_ledger(sf, run_id)
    _drive(sf, run_id, monkeypatch, ticks=40,
           clock_step=gate_deferral.wait_seconds())
    test_row = _rows_by_step(sf, run_id)["test"]
    assert test_row["status"] == "completed", test_row
    assert test_row["retry_count"] == 0, test_row
    assert test_row["release_count"] == 0, test_row
    assert test_row["claim_epoch"] == 1, test_row
    _assert_test_row_untouched(sf, run_id)


def test_the_tick_hold_alone_keeps_the_rows_where_the_absence_left_them(
        tmp_path, monkeypatch):
    """The TICK hold (`core/scheduler.py:_run_skillflow_tick`) measured on its
    own. With both holds in series, a break in one is covered by the other; so
    here the host's `hold_blocks_advance` answers False (the host hold is out)
    and the tick is the only driver. The tick's own `silent` return must then
    keep the run parked at `test_gate_absent` with the rows untouched, and
    each wait that runs out (`reacquire`) must re-run the gate step exactly
    once — one gate call per re-acquisition, never an implement cycle.
    """
    counter = tmp_path / "calls.txt"
    sf, run_id = _wire(tmp_path, monkeypatch, episode_max=100000, wait=1,
                       counter=counter, tail=_SILENT_TAIL)
    monkeypatch.setattr(gate_deferral, "hold_blocks_advance",
                        lambda run_id, **_kw: False)
    implement_runs, statuses, outcomes, nodes = _drive(
        sf, run_id, monkeypatch, ticks=20, clock_step=1, second_driver=False)
    _assert_test_row_untouched(sf, run_id)
    assert implement_runs == 1, implement_runs
    assert statuses[-1] == "running", statuses
    first = outcomes.index("silent")
    assert set(outcomes[first:]) == {"silent", "reacquire"}, outcomes
    assert outcomes.count("silent") >= 8, outcomes
    assert _gate_calls(counter) == 1 + outcomes.count("reacquire"), (
        _gate_calls(counter), outcomes)


def test_the_host_hold_alone_keeps_the_rows_where_the_absence_left_them(
        tmp_path, monkeypatch):
    """The HOST hold (`AItelierSkillFlow.advance_run`) measured on its own.
    The tick is handed a `none` answer, so its early return is out, while the
    REAL `observe_run` still records the episode in the ledger the host reads.
    Every tick then walks on into `advance_run`, and the host's refusal alone
    must keep the run parked at `test_gate_absent` with the rows untouched
    while the wait runs, and let it through only to re-run the gate step once
    per wait that ran out (`due`).
    """
    counter = tmp_path / "calls.txt"
    sf, run_id = _wire(tmp_path, monkeypatch, episode_max=100000, wait=1,
                       counter=counter, tail=_SILENT_TAIL)
    real_observe = gate_deferral.observe_run
    seen = []

    def _observe_but_answer_none(sf_, run_id_, **kw):
        seen.append(real_observe(sf_, run_id_, **kw)["state"])
        return {"state": "none", "remaining": 0.0, "gate": "", "reason": ""}

    monkeypatch.setattr(gate_deferral, "observe_run", _observe_but_answer_none)
    implement_runs, statuses, outcomes, nodes = _drive(
        sf, run_id, monkeypatch, ticks=20, clock_step=1, second_driver=False)
    _assert_test_row_untouched(sf, run_id)
    assert implement_runs == 1, implement_runs
    assert statuses[-1] == "running", statuses
    first = seen.index("silent")
    assert set(seen[first:]) == {"silent", "due"}, seen
    assert seen.count("silent") >= 8, seen
    assert _gate_calls(counter) == 1 + seen[first:].count("due"), (
        _gate_calls(counter), seen)
