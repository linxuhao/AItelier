"""An unchanged project row must cost no skillflow work to enrich.

Measured on the live backend on 2026-11-18, and not re-derived here:

  * one `GET /api/runs` cache rebuild took **26874 ms**;
  * while it ran, `GET /api/projects` — a different cache key — took **26.22 s**
    and `GET /health` answered in **2.8 ms**. One interpreter, one GIL: the
    page's other reads queue behind that loop.
  * neither the DB (`list_projects_with_stats(None)`: 1545 rows in 0.10 s) nor
    the YAML registry (0.00 ms per call, warm) is the cost. It is
    `enrich_project_status` at 4.1 ms per row over all 1545 rows, 10.6 ms per
    row over the 200 most recently active — one run lookup, one trace query for
    the failure reason, one config manifest lookup and one `get_steps` call per
    row, per pass, on a 10 s dashboard poll, for three endpoints that rebuild the
    same list.
  * two snapshots of all 1545 rows 12 s apart differed in **ZERO** rows by
    (status, updated_at, current_project_step). The scheduler mirrors every run
    change into the project row, so "unchanged row" is the overwhelming case.

What an unchanged-row pass costs now is the number that matters, and it is
CALLS, not seconds: **zero** — no `get_run_by_project`, no `list_runs`, no
`get_steps`, no `describe_config`, no manifest lookup. Only a dict lookup per
row. The three above are the only calls that were measured, and all three are
asserted zero here.

The four ways this can be wrong, each a class below:

  * caching the dict handed to callers instead of the derived fields — callers
    keep mutating the result, so one caller's edit would become the next
    caller's answer;
  * keying only on the project id — a row that MOVED must recompute;
  * keying only on the row — a run can advance between two scheduler writes of
    the mirror row, so the entry needs a TTL as well, and the TTL has to be
    injectable or the test sleeps for it;
  * memoising a partial answer, i.e. freezing a transient skillflow failure.

`tests/unit/test_enrich_project_status.py` is the behavioural contract for this
function and passes unchanged: the memo changes the cost, not the answer.
"""
from __future__ import annotations

import threading
import time

import pytest

from api import dependencies as deps


class _Engine:
    """A skillflow stand-in that records the calls enrichment makes per row."""

    def __init__(self, runs: dict[str, dict] | None = None, steps: list | None = None):
        self.runs = dict(runs or {})
        self.steps = list(steps or [])
        self.calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def _note(self, name: str, arg: str) -> None:
        with self._lock:
            self.calls.append((name, arg))

    def get_run_by_project(self, pid):
        self._note("get_run_by_project", pid)
        return self.runs.get(pid)

    def list_runs(self, pid):
        self._note("list_runs", pid)
        run = self.runs.get(pid)
        return [run] if run else []

    def get_steps(self, run_id):
        self._note("get_steps", run_id)
        return list(self.steps)

    def describe_config(self, name):
        # `core.addon_registry.describe_config` delegates to `get_skillflow()`
        # looked up on api.dependencies at call time, so this is the one enrich
        # actually reaches.
        self._note("describe_config", name)
        return {"base": name, "addons": []}

    def names(self) -> list[str]:
        with self._lock:
            return [name for name, _ in self.calls]

    def calls_for(self, pid: str) -> list[str]:
        with self._lock:
            return [name for name, arg in self.calls if arg in (pid, f"run-{pid}")]

    def reset(self) -> None:
        with self._lock:
            self.calls.clear()


def _run(pid: str = "p1", status: str = "running", node: str | None = "t_impl",
         graph: str = "dpe_default_v2", **extra) -> dict:
    run = {"id": f"run-{pid}", "project_id": pid, "status": status,
           "current_node": node, "graph_name": graph}
    run.update(extra)
    return run


def _row(pid: str = "p1", status: str = "running:3",
         updated_at: str = "2026-11-18T10:00:00", step: str = "3",
         config: str = "dpe_default_v2") -> dict:
    """A project row as `list_projects_with_stats` returns one."""
    return {"project_id": pid, "status": status, "updated_at": updated_at,
            "current_project_step": step, "config_name": config, "title": "t"}


@pytest.fixture(autouse=True)
def _fresh_memo():
    """The memo is process-global; a test must not inherit another one's rows.

    `getattr` on purpose: this suite has to be runnable against the tree BEFORE
    the memo existed (that is the red arm — the assertions below must be the
    thing that fails there, not an import of a helper that does not exist yet).
    """
    clear = getattr(deps, "clear_enrichment_memo", lambda: None)
    clear()
    yield
    clear()


@pytest.fixture
def engine(monkeypatch):
    def _install(runs=None, steps=None) -> _Engine:
        sf = _Engine(runs, steps)
        monkeypatch.setattr(deps, "get_skillflow", lambda: sf)
        # Same stub the behavioural suite uses: no real registry in a unit test.
        monkeypatch.setattr(deps, "get_config_registry", lambda: None)
        return sf
    return _install


class TestAnUnchangedRowPassDoesNoSkillflowWork:
    def test_the_second_pass_over_unchanged_rows_makes_zero_calls(self, engine):
        sf = engine({f"p{i}": _run(f"p{i}") for i in range(5)},
                    steps=[{"step_id": "1", "status": "completed"},
                           {"step_id": "2", "status": "pending"}])
        rows = [_row(f"p{i}") for i in range(5)]

        cold = [dict(deps.enrich_project_status(r)) for r in rows]
        assert len(sf.names()) > 0, "the first pass must really pay, or this proves nothing"

        sf.reset()
        warm = [dict(deps.enrich_project_status(_row(f"p{i}"))) for i in range(5)]

        assert sf.names() == [], (
            "a second pass over unchanged rows must do no skillflow work, got "
            f"{sf.names()}")
        # ...and it is the same answer, field for field.
        assert warm == cold
        assert warm[0]["completed_project_steps"] == ["1"]
        assert warm[0]["current_project_step"] == "t_impl"
        # the live node wins over the DB's cached one (AT-15), memoised or not
        assert warm[0]["status"] == "running:t_impl"

    def test_a_row_with_no_run_is_free_on_the_second_pass_too(self, engine):
        sf = engine({})
        deps.enrich_project_status(_row("p-norun", status="planning"))
        assert len(sf.names()) > 0
        sf.reset()
        out = deps.enrich_project_status(_row("p-norun", status="planning"))
        assert sf.names() == []
        assert out["status"] == "planning"

    def test_a_row_with_no_run_and_no_status_still_gets_planning(self, engine):
        """The `elif not project.get("status")` branch is memoised too — it sets
        a field, so its value has to survive the memo."""
        sf = engine({})
        assert deps.enrich_project_status(_row("p-new", status=""))["status"] == "planning"
        sf.reset()
        assert deps.enrich_project_status(_row("p-new", status=""))["status"] == "planning"
        assert sf.names() == []


class TestARowThatMovedRecomputes:
    def test_a_changed_status_recomputes(self, engine):
        sf = engine({"p1": _run("p1", status="running", node="t_impl")})
        deps.enrich_project_status(_row("p1", status="running:3"))
        sf.reset()

        out = deps.enrich_project_status(_row("p1", status="running:t_impl"))

        assert "get_run_by_project" in sf.calls_for("p1")
        assert out["status"] == "running:t_impl"
        assert out["current_project_step"] == "t_impl"

    def test_a_changed_updated_at_recomputes(self, engine):
        """The scheduler writes status/step/updated_at together; a row whose
        updated_at moved is a row something happened to, even if the status
        string happens to be the same."""
        sf = engine({"p1": _run("p1", status="completed", node=None)})
        deps.enrich_project_status(_row("p1", status="completed",
                                        updated_at="2026-11-18T10:00:00"))
        sf.reset()

        deps.enrich_project_status(_row("p1", status="completed",
                                        updated_at="2026-11-18T10:00:31"))

        assert "get_run_by_project" in sf.calls_for("p1")

    def test_a_changed_current_step_or_config_recomputes(self, engine):
        sf = engine({"p1": _run("p1")})
        deps.enrich_project_status(_row("p1"))
        sf.reset()
        deps.enrich_project_status(_row("p1", step="t_impl"))
        assert "get_run_by_project" in sf.calls_for("p1"), "current_project_step moved"

        sf.reset()
        deps.enrich_project_status(_row("p1", step="t_impl", config="gen_other"))
        assert "get_run_by_project" in sf.calls_for("p1"), "config_name moved"


class TestFreshnessIsBoundedByTheTtlToo:
    def test_an_entry_older_than_the_ttl_is_recomputed(self, engine):
        """The row did NOT change; the run advanced anyway (the scheduler mirrors
        its state on the next tick). The TTL is what bounds that window — and it
        is injectable, so this crosses it without sleeping for it."""
        sf = engine({"p1": _run("p1", status="running", node="t_impl")})
        deps.enrich_project_status(_row("p1"))
        sf.reset()
        assert deps.enrich_project_status(_row("p1"))["current_project_step"] == "t_impl"
        assert sf.names() == [], "inside the TTL the entry is reused"

        out = deps.enrich_project_status(_row("p1"), ttl=0.0)

        assert "get_run_by_project" in sf.calls_for("p1"), "ttl=0 means every entry is stale"
        assert out["current_project_step"] == "t_impl"

    def test_the_ttl_really_is_a_duration(self, engine):
        """Not just the degenerate ttl=0: a short real TTL elapses in 60 ms."""
        sf = engine({"p1": _run("p1")})
        deps.enrich_project_status(_row("p1"), ttl=0.05)
        sf.reset()
        deps.enrich_project_status(_row("p1"), ttl=0.05)
        assert sf.names() == []
        time.sleep(0.06)
        deps.enrich_project_status(_row("p1"), ttl=0.05)
        assert "get_run_by_project" in sf.calls_for("p1")

    def test_the_default_ttl_is_the_module_constant(self, engine, monkeypatch):
        """`ttl=None` reads ENRICH_MEMO_TTL at call time, so the constant is the
        knob a deployment turns."""
        monkeypatch.setattr(deps, "ENRICH_MEMO_TTL", 0.0, raising=False)
        sf = engine({"p1": _run("p1")})
        deps.enrich_project_status(_row("p1"))
        sf.reset()
        deps.enrich_project_status(_row("p1"))
        assert "get_run_by_project" in sf.calls_for("p1")


class TestOneCallersMutationCannotReachTheNext:
    def test_mutating_the_returned_dict_leaves_the_next_answer_clean(self, engine):
        """`project_routers` sets config_label / is_authoring / has_task_loop on
        the dict it gets back, and `repo_routers` enriches for the side effect.
        If the memo held the dict, the next caller would read this one's edits."""
        sf = engine({"p1": _run("p1")},
                    steps=[{"step_id": "1", "status": "completed"}])
        first = deps.enrich_project_status(_row("p1"))
        first["config_label"] = "MUTATED BY A CALLER"
        first["completed_project_steps"].append("999")
        first["config_addons"].append("an addon nobody declared")
        first["is_authoring"] = True

        sf.reset()
        second = deps.enrich_project_status(_row("p1"))

        assert sf.names() == [], "the clean answer must come from the memo"
        assert second["config_label"] != "MUTATED BY A CALLER"
        assert second["completed_project_steps"] == ["1"]
        assert second["config_addons"] == []
        assert "is_authoring" not in second

    def test_the_containers_are_copies_not_the_same_objects(self, engine):
        sf = engine({"p1": _run("p1")},
                    steps=[{"step_id": "1", "status": "completed"}])
        first = deps.enrich_project_status(_row("p1"))
        second = deps.enrich_project_status(_row("p1"))
        assert second["completed_project_steps"] is not first["completed_project_steps"]
        assert second["config_addons"] is not first["config_addons"]

    def test_the_enrichment_still_lands_on_the_callers_own_dict(self, engine):
        """`repo_routers.py:90` calls this for the side effect and drops the
        return value — returning a copy without mutating would silently stop
        enriching the repo list."""
        engine({"p1": _run("p1")})
        row = _row("p1")
        out = deps.enrich_project_status(row)
        assert out is row
        assert row["current_project_step"] == "t_impl"
        assert row["completed_project_steps"] == []


class TestAPartialAnswerIsNeverMemoised:
    def test_a_skillflow_failure_is_retried_on_the_next_pass(self, engine):
        sf = engine({"p1": _run("p1")},
                    steps=[{"step_id": "1", "status": "completed"}])

        def _explode(run_id):
            raise RuntimeError("trace db gone")
        sf.get_steps = _explode

        first = deps.enrich_project_status(_row("p1"))
        # What skillflow had already answered is still applied — a transient
        # failure must not blank a row that was already enriched.
        assert first["current_project_step"] == "t_impl"
        assert "completed_project_steps" not in first

        sf.get_steps = lambda run_id: [{"step_id": "1", "status": "completed"}]
        second = deps.enrich_project_status(_row("p1"))
        assert second["completed_project_steps"] == ["1"], (
            "a partial derivation must not be cached; the next pass retries")


class TestTheMemoIsBoundedAndSharedSafely:
    def test_it_does_not_grow_without_bound(self, engine, monkeypatch):
        monkeypatch.setattr(deps, "ENRICH_MEMO_MAX_KEYS", 4, raising=False)
        engine({f"p{i}": _run(f"p{i}") for i in range(50)})
        for i in range(50):
            deps.enrich_project_status(_row(f"p{i}"))
        memo = getattr(deps, "_enrichment_memo", None)
        assert memo is not None, "there is no memo to bound"
        assert len(memo) <= 4
        assert len(deps._enrichment_locks) <= 4

    def test_concurrent_callers_on_one_cold_row_compute_it_once(self, engine):
        """The endpoints run in AnyIO worker threads. A cold row under N
        concurrent requests must not run N enrichments — that stampede is what
        the read cache exists to prevent one layer up."""
        sf = engine({"p1": _run("p1")})
        real = sf.get_run_by_project
        barrier = threading.Barrier(4)

        def _slow(pid):
            time.sleep(0.05)          # hold the derivation open across the others
            return real(pid)
        sf.get_run_by_project = _slow

        results: list[dict] = []

        def _work():
            barrier.wait(timeout=5)
            results.append(deps.enrich_project_status(_row("p1")))

        threads = [threading.Thread(target=_work) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(results) == 4
        assert sf.names().count("get_run_by_project") == 1, sf.names()
        assert all(r["current_project_step"] == "t_impl" for r in results)


class TestTheMemoChangesTheCostNotTheAnswer:
    def test_cold_and_warm_answers_are_identical(self, engine, monkeypatch):
        """Including the two rules the behavioural suite pins: the DB's enriched
        status wins when its prefix agrees, and the failure reason is the
        ENRICHED one."""
        monkeypatch.setattr(
            "core.scheduler._failure_reason",
            lambda run: "Cycle limit exceeded — continuity: 字数超限 5662 字（上限 4500）")
        sf = engine({"p1": _run("p1", status="failed", node=None,
                                error_reason="Cycle limit exceeded")},
                    steps=[{"step_id": "1", "status": "completed"},
                           {"step_id": "2", "status": "pending"}])
        row = _row("p1", status="failed:Cycle limit exceeded — v_smoke: no transition matched")
        cold = dict(deps.enrich_project_status(row))

        sf.reset()
        warm = dict(deps.enrich_project_status(
            _row("p1", status="failed:Cycle limit exceeded — v_smoke: no transition matched")))

        assert sf.names() == []
        assert warm == cold
        assert warm["status"] == (
            "failed:Cycle limit exceeded — v_smoke: no transition matched")
        assert "字数超限" in warm["error_reason"]
        assert warm["completed_project_steps"] == ["1"]

    def test_a_replaced_engine_is_not_served_the_old_engines_answers(self, engine, monkeypatch):
        """Every memoised field was read from one skillflow object. A different
        one is a different database of runs, so its entries cannot be reused —
        this is also why `tests/unit/test_enrich_project_status.py`, which hands
        in a fresh fake per case, keeps passing unchanged."""
        engine({"p1": _run("p1", status="running", node="t_impl")})
        deps.enrich_project_status(_row("p1", status="running:3"))

        other = _Engine({"p1": _run("p1", status="running", node="5")})
        monkeypatch.setattr(deps, "get_skillflow", lambda: other)

        out = deps.enrich_project_status(_row("p1", status="running:3"))

        assert "get_run_by_project" in other.calls_for("p1")
        assert out["status"] == "running:5"
