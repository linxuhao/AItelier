# tests/skillflow/test_coding_impl_gate_absence.py
#
# The absence accounting, measured on the REAL `configs/coding_impl.yaml`, the
# REAL `AItelierSkillFlow` and the REAL `run_tests` tool, driven in the order
# `core/scheduler.py:_run_skillflow_tick` uses — INCLUDING its inline-tool
# drain (drain loop at lines 1299-1306).
#
# The subject is a gate that NEVER produces a verdict — NOT a fixture that lets
# go on the Nth call (the forbidden technique: `if [ "$n" -lt 2 ]`). The three
# poles below are the SAME never-answering gate; only the episode's wall-clock
# ceiling differs, because that is the only knob that is supposed to matter.
#
# What must hold at every pole, including the one where the absence outlives
# the ceiling:
#
#   * `implement_runs == 1` — the absence spends no implement cycle, ever;
#   * the run NEVER ends on `Cycle limit exceeded`;
#   * when it does end, it ends NAMING THE ABSENCE.
#
# ROUND 6's correction, and the whole reason the drain is here: round 5's
# driver called `advance_run` once per tick and did not drain, while the real
# tick drains consecutive inline tool steps IN ONE TICK — it carries the run
# from `test` through `test_evidence`/`test_outcome` and on to `implement`.
# A driver that does not drain reports implement_runs=2 for a run that spends
# 1: it proves a world that does not exist. That one tick is the entire gap
# between 1 and 2, and it is why the round-5 `_drive` could not see the dead
# `from_file` edge on `test_evidence`.
import json
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


def _drive(sf, run_id, *, tick_ledger, ticks, clock_step):
    """The scheduler's own order, INCLUDING the inline-tool drain.

    Returns `(implement_runs, statuses, tick_outcomes, nodes)`. Nothing here
    re-implements the tick: it calls the two `gate_deferral` readers the tick
    calls, in the order the tick calls them, refuses to advance when they say
    to hold, and drains inline tool steps the way the tick does.

    `clock_step` is how far the WALL CLOCK moves between ticks. The scheduler
    never waits out a real hold in a test, so the clock is fast-forwarded
    instead; the episode ceiling is measured from the episode's start, so
    fast-forwarding is the only way to observe an expiry without sleeping for
    hours. It is passed per pole because the observation window is the thing
    the poles differ in.
    """
    import time
    clock = time.time()
    implement_runs = 0
    statuses = []
    outcomes = []
    nodes = []
    for _ in range(ticks):
        run = sf.get_run(run_id)
        statuses.append(run["status"])
        if run["status"] != "running":
            break
        clock += clock_step
        decision = gate_deferral.observe_run(sf, run_id, now=clock,
                                             ledger=tick_ledger)
        outcomes.append(decision["state"])
        if decision["state"] == "silent":
            continue
        if decision["state"] == "expired":
            sf.fail_run(run_id, decision["reason"])
            statuses.append(sf.get_run(run_id)["status"])
            outcomes.append("terminal:" + decision["reason"])
            break
        node = sf.advance_run(run_id)
        drain = 0
        while node is not None and drain < 20 \
                and sf._get_resolver_for_run(run_id).is_tool(node):
            node = sf.advance_run(run_id)
            drain += 1
        nodes.append(node)
        run = sf.get_run(run_id)
        if node is None:
            if run["status"] == "running":
                continue
            statuses.append(run["status"])
            break
        claimed = sf.claim_next_step(run_id)
        if claimed is None:
            continue
        assert claimed.step_id == "implement", claimed.step_id
        implement_runs += 1
        from tests.code_output_fixture import write_claim_code
        write_claim_code(sf, run_id, claimed)
        sf.confirm_step(claimed.token, StepResult(flags={}))
    return implement_runs, statuses, outcomes, nodes


def _assert_never_cycles(statuses, outcomes, reason_source):
    text = " ".join(statuses + [str(o) for o in outcomes] + [str(reason_source)])
    assert "Cycle limit exceeded" not in text


def _tick_ledger_for(sf, run_id):
    """The tick's ledger, primed from the run exactly as the tick seeds it."""
    ledger = gate_deferral.DeferralLedger()
    first = gate_deferral.observe_run(sf, run_id, ledger=ledger)
    return ledger, first


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
    ledger, first = _tick_ledger_for(sf, run_id)
    assert first["state"] == "none"          # no report yet: nothing to account

    implement_runs, statuses, outcomes, nodes = _drive(
        sf, run_id, tick_ledger=ledger, ticks=40,
        clock_step=gate_deferral.wait_seconds())

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
    ledger, _ = _tick_ledger_for(sf, run_id)

    implement_runs, statuses, outcomes, nodes = _drive(
        sf, run_id, tick_ledger=ledger, ticks=15, clock_step=1)

    assert implement_runs == 1, implement_runs
    assert statuses[-1] == "running", statuses
    assert "expired" not in outcomes, outcomes
    assert nodes[-1] == "test_gate_absent", nodes
    _assert_never_cycles(statuses, outcomes,
                         sf.get_run(run_id).get("error_reason"))


def test_pole_c_a_tiny_ceiling_ends_the_run_naming_the_absence(
        tmp_path, monkeypatch):
    """(c) ceiling tiny, and small enough to be HONORED (unlike 0, which is a
    mis-set knob). Review measured this pole on the r4 candidate as
    `implement_runs=4` + `Cycle limit exceeded`.

    It must now be `implement_runs == 1` and a terminal status whose reason
    names the absence.
    """
    counter = tmp_path / "calls.txt"
    sf, run_id = _wire(tmp_path, monkeypatch, episode_max=3, wait=1,
                       counter=counter, tail=_SILENT_TAIL)
    assert gate_deferral.episode_max_seconds() == 3
    ledger, _ = _tick_ledger_for(sf, run_id)

    implement_runs, statuses, outcomes, nodes = _drive(
        sf, run_id, tick_ledger=ledger, ticks=20, clock_step=3)

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
    ledger, _ = _tick_ledger_for(sf, run_id)
    _drive(sf, run_id, tick_ledger=ledger, ticks=8, clock_step=1)
    assert gate_deferral.hold_blocks_advance(run_id, ledger=ledger)

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

    cleared = gate_deferral.observe_run(sf, run_id, ledger=ledger)
    assert cleared["state"] == "none"
    assert not gate_deferral.hold_blocks_advance(run_id, ledger=ledger)


def _step_rows(sf, run_id):
    cols = [d[0] for d in sf._conn.execute(
        "SELECT * FROM skillflow_steps WHERE run_id=?", (run_id,)).description]
    rows = sf._conn.execute(
        "SELECT * FROM skillflow_steps WHERE run_id=?", (run_id,)).fetchall()
    return cols, [dict(zip(cols, tuple(r))) for r in rows]


def _rows_by_step(sf, run_id):
    return {r["step_id"]: r for r in _step_rows(sf, run_id)[1]}


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
    The `H2` mutation (host `advance_run` no longer refuses during a hold)
    lets the run advance past the silent gate and re-drive the loop, which
    moves `release_count` / `retry_count` off 0 and re-claims; `D1` (absence
    never expires) and the release/retry charge paths are the other readers.
    """
    counter = tmp_path / "calls.txt"
    sf, run_id = _wire(tmp_path, monkeypatch, episode_max=100000, wait=1,
                       counter=counter, tail=_SILENT_TAIL)
    ledger, _ = _tick_ledger_for(sf, run_id)
    _drive(sf, run_id, tick_ledger=ledger, ticks=40,
           clock_step=gate_deferral.wait_seconds())
    test_row = _rows_by_step(sf, run_id)["test"]
    assert test_row["status"] == "completed", test_row
    assert test_row["retry_count"] == 0, test_row
    assert test_row["release_count"] == 0, test_row
    assert test_row["claim_epoch"] == 1, test_row


