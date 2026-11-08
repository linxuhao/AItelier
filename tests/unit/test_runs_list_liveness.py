"""GET /api/runs carries the trace-derived liveness the per-run path already has.

The acceptance contract, clause by clause:

1. `the-list-carries-trace-derived-liveness` — every non-terminal row carries
   `idle_seconds`, `liveness_from` and `stalled`. The activity clock is the
   newest TRACE row when there is one, the run row's `updated_at` otherwise.
2. `one-rule-drives-both-surfaces` — the list values equal what `summarise_run`
   computes for the same run, field by field, and the per-run verdict keeps its
   wording. Sharing is proven by moving one threshold and seeing both move.
3. `cost-and-anonymous-projection-are-bounded` — at most one trace query per
   non-terminal run on the page, and `execution_progress` gains exactly the three
   liveness keys and nothing else.

Nothing here mocks the rule. `summarise_run` and the list builder both run the
real `core.run_driver.run_liveness`. The only fake is the skillflow store, which
records every `trace_query` so the cost clause is counted, not assumed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from starlette.requests import Request

import core.run_driver as run_driver
from api import authz, run_routers
from core.db_manager import DBManager

# Fixed clock. Every fixture timestamp is an offset from NOW so the fixtures do
# not drift with wall time, and the test pins `_utc_now` to NOW.
NOW = datetime(2026, 11, 8, 14, 0, 0, tzinfo=timezone.utc)


def _ts(seconds_before_now: float) -> str:
    """A skillflow UTC timestamp `seconds_before_now` before NOW."""
    return (NOW - timedelta(seconds=seconds_before_now)).strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture
def clock(monkeypatch):
    """Pin the one liveness clock to NOW. The rule reads time only via `_utc_now`."""
    monkeypatch.setattr(run_driver, "_utc_now", lambda: NOW)
    return NOW


class _Store:
    """The skillflow surface the list builder and `summarise_run` touch.

    `runs` maps project_id -> list of run dicts, newest first (as `list_runs`
    returns them). `traces` maps run_id -> list of trace `created_at` strings,
    oldest first; the newest is the last element, which is what the engine's
    `ORDER BY id DESC LIMIT 1` would return. Every `trace_query` call is recorded
    in `calls` so clause 3 can count it.
    """

    def __init__(self, runs=None, traces=None, steps=None):
        self.runs = runs or {}
        self.traces = traces or {}
        self.steps = steps or {}
        self.calls: list[tuple[str, str]] = []
        self._by_id = {r["id"]: r for rs in self.runs.values() for r in rs}

    # ── run lookups (engine semantics) ──
    def list_runs(self, project_id=None, status=None):
        if project_id is None:
            return [r for rs in self.runs.values() for r in rs]
        return list(self.runs.get(project_id, []))

    def get_run(self, run_id):
        return self._by_id.get(run_id)

    def get_run_by_project(self, project_id, graph_name=None):
        # engine: newest non-completed run for the project, else None
        for r in self.runs.get(project_id, []):
            if r.get("status") != "completed":
                return r
        return None

    def get_steps(self, run_id, **_):
        return list(self.steps.get(run_id, []))

    def trace_query(self, run_id, sql, params=()):
        self.calls.append((run_id, sql))
        if "COUNT(*)" in sql:
            return [{"n": len(self.traces.get(run_id, []))}]
        if "created_at" in sql and "LIMIT" in sql:
            rows = self.traces.get(run_id, [])
            return [{"created_at": rows[-1]}] if rows else []
        # tool_result / refused-call scans: no refused calls in these fixtures
        return []

def _run(run_id, project_id, status, *, updated_seconds_ago, graph="dpe_default_v2",
         current_node="t_impl"):
    return {"id": run_id, "project_id": project_id, "graph_name": graph,
            "status": status, "current_node": current_node,
            "updated_at": _ts(updated_seconds_ago),
            "created_at": _ts(updated_seconds_ago + 60),
            "error_reason": "", "error": ""}


@pytest.fixture
def db(tmp_path):
    return DBManager(str(tmp_path / "liveness.db"))


def _seed_projects(db, pids):
    """Create real project rows through the DB, so `list_projects_with_stats` returns them."""
    for pid in pids:
        db.ensure_project(pid, pid)


@pytest.fixture
def registry():
    """The config registry the builder consults for labels. Real shape, no behaviour."""
    class _M:
        label = "DPE Pipeline"
        has_task_loop = True
        registers_generated_pipeline = False
        registers_generated_addon = False
        repo_mode = "code"

    class _R:
        def get(self, name):
            return _M()
    return _R()


def _install_store(monkeypatch, store):
    """Route the builder's and the per-run path's skillflow to our store."""
    import api.run_routers as rr
    import api.dependencies as deps
    import api._cache_stats as cs
    monkeypatch.setattr(rr, "get_skillflow", lambda: store)
    monkeypatch.setattr(deps, "get_skillflow", lambda: store)
    monkeypatch.setattr(cs, "get_skillflow", lambda: store, raising=False)
    # `compute_cache_stats_per_step` imports get_skillflow inside the function.
    monkeypatch.setattr("api.dependencies.get_skillflow", lambda: store)
    return store


def _list_rows(db, registry, monkeypatch, store):
    _install_store(monkeypatch, store)
    rows = run_routers._list_all_runs_uncached(None, db, registry)
    return {r["project_id"]: r for r in rows}


# ── Clause 1: the list carries trace-derived liveness ─────────────────────────


def test_clause1a_trace_fresh_beats_old_run_row(db, registry, monkeypatch, clock):
    """(a) run-row updated_at OLD, trace FRESH → liveness_from='trace', idle from the trace."""
    _seed_projects(db, ["p-fresh"])
    run = _run("run-a", "p-fresh", "running:t_impl", updated_seconds_ago=3 * 3600)
    store = _Store(
        runs={"p-fresh": [run]},
        traces={"run-a": [_ts(9 * 3600), _ts(90)]},   # newest trace 90s ago
    )
    row = _list_rows(db, registry, monkeypatch, store)["p-fresh"]

    assert row["liveness_from"] == "trace"
    # idle agrees with the TRACE time (90s), not the run row (3h).
    assert row["idle_seconds"] == pytest.approx(90.0, abs=1.0)
    assert row["idle_seconds"] < 3600
    assert row["stalled"] is False


def test_clause1b_no_trace_rows_falls_back_to_run_row(db, registry, monkeypatch, clock):
    """(b) a run with no trace rows → liveness_from='run_row' from the run row's updated_at."""
    _seed_projects(db, ["p-untraced"])
    run = _run("run-b", "p-untraced", "running:t_impl", updated_seconds_ago=400)
    store = _Store(runs={"p-untraced": [run]}, traces={})
    row = _list_rows(db, registry, monkeypatch, store)["p-untraced"]

    assert row["liveness_from"] == "run_row"
    assert row["idle_seconds"] == pytest.approx(400.0, abs=1.0)
    assert row["stalled"] is False


def test_clause1b_no_timestamp_anywhere_is_none(db, registry, monkeypatch, clock):
    """No trace and no run-row timestamp → liveness_from='none', idle null, not stalled."""
    _seed_projects(db, ["p-bare"])
    run = {"id": "run-c", "project_id": "p-bare", "graph_name": "dpe_default_v2",
           "status": "running:t_impl", "current_node": "t_impl",
           "updated_at": None, "created_at": None,
           "error_reason": "", "error": ""}
    store = _Store(runs={"p-bare": [run]}, traces={})
    row = _list_rows(db, registry, monkeypatch, store)["p-bare"]

    assert row["liveness_from"] == "none"
    assert row["idle_seconds"] is None
    assert row["stalled"] is False


def test_clause1_stalled_when_idle_reaches_threshold(db, registry, monkeypatch, clock):
    """stalled is true exactly when idle_seconds >= the shared stall threshold."""
    threshold = run_driver._STALL_HINT_S
    _seed_projects(db, ["p-edge"])
    run = _run("run-e", "p-edge", "running:t_impl", updated_seconds_ago=0)
    store = _Store(runs={"p-edge": [run]}, traces={"run-e": [_ts(threshold)]})
    row = _list_rows(db, registry, monkeypatch, store)["p-edge"]

    assert row["idle_seconds"] == pytest.approx(float(threshold), abs=1.0)
    assert row["stalled"] is True


def test_clause1_terminal_rows_carry_no_liveness(db, registry, monkeypatch, clock):
    """completed / failed / paused rows are outside the contract: no liveness keys."""
    pids = ["p-done", "p-failed", "p-paused"]
    _seed_projects(db, pids)
    store = _Store(
        runs={
            "p-done": [_run("r-done", "p-done", "completed", updated_seconds_ago=5)],
            "p-failed": [_run("r-failed", "p-failed", "failed", updated_seconds_ago=5)],
            "p-paused": [_run("r-paused", "p-paused", "paused", updated_seconds_ago=5)],
        },
        traces={"r-done": [_ts(5)], "r-failed": [_ts(5)], "r-paused": [_ts(5)]},
    )
    rows = _list_rows(db, registry, monkeypatch, store)
    for pid in pids:
        for key in ("idle_seconds", "liveness_from", "stalled"):
            assert key not in rows[pid], f"{pid} is terminal and must not carry {key}"


def test_clause1_selects_the_same_run_as_the_enrichment(db, registry, monkeypatch, clock):
    """A project whose NEWEST run is completed and whose older run is live.

    The list must measure the live run, the one `get_run_by_project` (the engine's
    own rule, used by enrich_project_status) returns, not the completed newest one.
    `list_runs` is newest first, so the live run is the second element here.
    """
    _seed_projects(db, ["p-mixed"])
    newest_done = _run("r-mixed-done", "p-mixed", "completed", updated_seconds_ago=10)
    older_live = _run("r-mixed-live", "p-mixed", "running:t_impl", updated_seconds_ago=700)
    store = _Store(runs={"p-mixed": [newest_done, older_live]},
                   traces={"r-mixed-live": [_ts(700)]})

    # The engine's own selection, which enrich_project_status relies on.
    assert store.get_run_by_project("p-mixed")["id"] == "r-mixed-live"

    row = _list_rows(db, registry, monkeypatch, store)["p-mixed"]
    assert row["idle_seconds"] == pytest.approx(700.0, abs=1.0)
    assert row["stalled"] is True


# ── Clause 2: one rule drives both surfaces ───────────────────────────────────


FIXTURES = {
    "trace-fresh-run-row-old": dict(
        run=("r2a", "running:t_impl", 3 * 3600), traces=[_ts(9 * 3600), _ts(90)]),
    "no-trace-run-row-fresh": dict(
        run=("r2b", "running:t_impl", 200), traces=[]),
    "trace-stalled": dict(
        run=("r2c", "running:t_impl", 60), traces=[_ts(3600)]),
    "no-timestamp": dict(
        run=("r2d", "running:t_impl", None), traces=[]),
}


def _fixture_run(pid, run_id, status, ago):
    if ago is None:
        return {"id": run_id, "project_id": pid, "graph_name": "dpe_default_v2",
                "status": status, "current_node": "t_impl", "updated_at": None,
                "created_at": None, "error_reason": "", "error": ""}
    return _run(run_id, pid, status, updated_seconds_ago=ago)


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_clause2_list_equals_summarise_run_field_by_field(
        name, db, registry, monkeypatch, clock):
    """Each list value equals what summarise_run computes for the SAME run and store."""
    fx = FIXTURES[name]
    run_id, status, ago = fx["run"]
    pid = f"p-{name}"
    _seed_projects(db, [pid])
    run = _fixture_run(pid, run_id, status, ago)
    store = _Store(runs={pid: [run]}, traces={run_id: fx["traces"]},
                   steps={run_id: [{"step_id": "t_impl", "status": "running"}]})

    list_row = _list_rows(db, registry, monkeypatch, store)[pid]
    summary = run_driver.summarise_run(store, _WS(), registry, run_id)

    # Field by field, each one asserted on its own: the list and the per-run path
    # must agree exactly, including None, since "could not tell" must match too.
    assert list_row["idle_seconds"] == summary["idle_seconds"], f"{name}: idle_seconds"
    assert list_row["liveness_from"] == summary["liveness_from"], f"{name}: liveness_from"
    assert list_row["stalled"] == summary["stalled"], f"{name}: stalled"


class _WS:
    """summarise_run needs a workspace only to read outputs; a missing dir is fine."""

    def get_final_path(self, *_a, **_k):
        from pathlib import Path
        return Path("/nonexistent-liveness-fixture")


def test_clause2_verdict_wording_is_unchanged(clock):
    """The per-run verdict for the same inputs keeps its current wording."""
    store = _Store(
        runs={"p": [_run("v-fresh", "p", "running:t_impl", updated_seconds_ago=200)]},
        traces={"v-fresh": [_ts(200)]},
        steps={"v-fresh": [{"step_id": "t_impl", "status": "running"}]})
    out = run_driver.summarise_run(store, _WS(), _Registry(), "v-fresh")
    assert out["verdict"] == "in-progress (last advanced 3m20s ago, liveness from trace)"

    store2 = _Store(
        runs={"p": [_run("v-stuck", "p", "running:t_impl", updated_seconds_ago=3600)]},
        traces={"v-stuck": [_ts(3600)]},
        steps={"v-stuck": [{"step_id": "t_impl", "status": "running"}]})
    out2 = run_driver.summarise_run(store2, _WS(), _Registry(), "v-stuck")
    assert out2["verdict"].startswith(
        "in-progress but NOT ADVANCING for 1h00m (liveness from trace) — possibly stalled:")


class _Registry:
    def get(self, name):
        return None


def test_clause2_one_threshold_moves_both_surfaces(db, registry, monkeypatch, clock):
    """Change ONE thing once — the shared threshold — and the per-run verdict AND the list row both move.

    A run idle 400s is advancing under the 600s threshold and stalled under 300s.
    If the list kept a private copy of the threshold, it would not move with the
    per-run verdict, and this test would catch it.
    """
    _seed_projects(db, ["p-thresh"])
    run = _run("r-thresh", "p-thresh", "running:t_impl", updated_seconds_ago=400)
    store = _Store(runs={"p-thresh": [run]}, traces={"r-thresh": [_ts(400)]},
                   steps={"r-thresh": [{"step_id": "t_impl", "status": "running"}]})

    before_row = _list_rows(db, registry, monkeypatch, store)["p-thresh"]
    before_verdict = run_driver.summarise_run(store, _WS(), _Registry(), "r-thresh")["verdict"]
    assert before_row["stalled"] is False
    assert "NOT ADVANCING" not in before_verdict

    # The ONE change: lower the shared threshold. Nothing else is edited.
    monkeypatch.setattr(run_driver, "_STALL_HINT_S", 300)

    after_row = _list_rows(db, registry, monkeypatch, store)["p-thresh"]
    after_verdict = run_driver.summarise_run(store, _WS(), _Registry(), "r-thresh")["verdict"]
    assert after_row["stalled"] is True, "list row ignored the shared threshold"
    assert "NOT ADVANCING" in after_verdict, "per-run verdict ignored the shared threshold"


# ── Clause 3: bounded cost and anonymous projection ───────────────────────────


def test_clause3_three_nonterminal_runs_cost_three_trace_queries(
        db, registry, monkeypatch, clock):
    """Three non-terminal runs on the page cost exactly three trace queries.

    The builder reuses the list it already fetched for cache stats, so liveness
    adds ONE trace read per non-terminal run and nothing per step or per trace
    row. Terminal runs add none.
    """
    pids = ["p-x1", "p-x2", "p-x3", "p-done3"]
    _seed_projects(db, pids)
    runs = {
        "p-x1": [_run("rx1", "p-x1", "running:t_impl", updated_seconds_ago=30)],
        "p-x2": [_run("rx2", "p-x2", "running:t_impl", updated_seconds_ago=40)],
        "p-x3": [_run("rx3", "p-x3", "running:t_impl", updated_seconds_ago=50)],
        "p-done3": [_run("rxd", "p-done3", "completed", updated_seconds_ago=5)],
    }
    traces = {rid: [_ts(10 * (i + 1)), _ts(5)] for i, rid in enumerate(
        ["rx1", "rx2", "rx3", "rxd"])}
    store = _Store(runs=runs, traces=traces)
    _list_rows(db, registry, monkeypatch, store)

    nonterminal = {"rx1", "rx2", "rx3"}
    liveness_calls = [c for c in store.calls
                      if c[0] in nonterminal and "created_at" in c[1] and "LIMIT" in c[1]]
    assert len(liveness_calls) == 3, (
        f"expected one liveness trace query per non-terminal run, got {len(liveness_calls)}")
    # the terminal run is not read for liveness at all
    assert not [c for c in store.calls if c[0] == "rxd" and "created_at" in c[1]
                and "LIMIT" in c[1]]


def test_clause3_projection_adds_exactly_the_three_liveness_keys(monkeypatch):
    """execution_progress gains exactly idle_seconds, liveness_from, stalled and nothing else."""
    import ast
    import inspect
    src = inspect.getsource(authz.execution_progress)
    tree = ast.parse(src.strip())
    whitelist = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Set):
            whitelist = {elt.value for elt in node.value.elts
                         if isinstance(elt, ast.Constant)}
            break
    assert whitelist is not None, "execution_progress whitelist not found"

    base_whitelist = {"id", "project_id", "name", "status", "config_name", "config_label",
                      "created_at", "updated_at", "started_at", "completed_at", "priority",
                      "current_project_step", "current_node", "latest_status", "latest_step",
                      "last_update", "task_count", "completed_count", "running_count",
                      "failed_count", "pending_count", "task_summary", "cache_stats",
                      "has_task_loop", "is_authoring", "repo_less", "step_count",
                      "completed_steps", "failed_steps", "active_step"}

    added = whitelist - base_whitelist
    removed = base_whitelist - whitelist
    assert added == {"idle_seconds", "liveness_from", "stalled"}, added
    assert removed == set(), f"projection dropped base keys: {removed}"


def _anon_request():
    app = SimpleNamespace(state=SimpleNamespace(_test_mode=False))
    return Request({"type": "http", "method": "GET", "path": "/api/runs",
                    "headers": [(b"cf-ray", b"anon-ray")], "app": app,
                    "query_string": b"", "root_path": "", "scheme": "http",
                    "server": ("testserver", 80), "client": ("testclient", 1)})


def test_clause3_anonymous_projection_keeps_only_derived_scalars(monkeypatch):
    """An anonymous reader gets the three scalars and never a trace path, SQL or identifier."""
    monkeypatch.setattr(authz, "gate_enabled", lambda: True)
    row = {"id": "p1", "project_id": "p1", "status": "running:t_impl",
           "idle_seconds": 12.5, "liveness_from": "trace", "stalled": False,
           "updated_at": "2026-11-08 13:59:00", "run_row_updated_at": "2026-11-08 13:00:00",
           "trace_path": "/home/x/trace.db", "trace_sql": "SELECT created_at ...",
           "run_id": "uuid-leak", "graph_name": "dpe_default_v2", "owner_email": "a@b"}
    out = authz.execution_progress(_anon_request(), row)

    assert {"idle_seconds", "liveness_from", "stalled"} <= set(out)
    assert out["idle_seconds"] == 12.5
    assert out["liveness_from"] == "trace"
    assert out["stalled"] is False
    # `updated_at` is deliberately NOT in this list: it is a pre-existing public
    # whitelist key (base api/authz.py), not something this change exposes.
    for leaked in ("trace_path", "trace_sql", "run_id", "graph_name", "owner_email",
                   "run_row_updated_at"):
        assert leaked not in out, f"{leaked} must not reach an anonymous reader"
    # every projected value is a derived scalar, never a path/SQL/identifier string
    for key in ("idle_seconds", "liveness_from", "stalled"):
        assert not (isinstance(out[key], str) and ("/" in out[key] or "SELECT" in out[key]))
