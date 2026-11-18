"""Unit tests for compute_cache_stats_batch: its error handling, the derived
stats it now persists, and the startup prefetch that keeps its cost off the
request path.

The first class is the original contract: a single run's failure in
compute_cache_stats_per_step does not crash the entire batch — it logs a warning
and continues with remaining runs.

The rest are the witnesses for the derived-stats sidecar
(api/_cache_stats.py's module header states why it exists):
`docs/investigations/run-cold-read-20261118/breakdown.json` measured 8,205.1 ms
of the first GET /api/runs of a process inside this batch, aggregating 233
per-project trace databases — 483 MB of reads for 662 ms of CPU — because those
numbers are derived from rows that never change again. So:

  * `TestTheSidecarServesWhatTheTraceDatabasesSay` — the endpoint's answer equals
    the inline computation for >= 5 runs, and a token_usage row inserted into a
    trace database moves the reported total by exactly the inserted amount once
    the freshness window has elapsed.
  * `TestTheThreeDegradations` — a missing, corrupt and unwritable sidecar each
    log a warning and fall back to the inline computation; the endpoint still
    answers 200 with the right numbers.
  * `TestTheCheapPathCountsTheOpens` — with a populated sidecar the batch opens
    NO trace database (counted at sqlite3.connect, not inferred) …
  * `TestTheStoreThatCannotNameItsSource` — … and a store that cannot name its
    trace root gets nothing persisted, because a row has to be an answer about
    a trace database somebody can point at.
  * `TestTheStartupPrefetch` — the lifespan starts the warm-up on a daemon
    thread and the warm-up itself never raises (startup must not fail or block
    because of it).
"""
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import api._cache_stats as cs
import api.main as api_main
import api.run_routers as rr
from api import _read_cache
from api import dependencies as deps
from api.dependencies import (get_config_registry, get_db_manager,
                              get_workspace_manager)
from api.main import app
from core.db_manager import DBManager
from core.workspace_manager import WorkspaceManager

from api._cache_stats import compute_cache_stats_batch, merge_stats


def _mock_stats(hit: int, miss: int, prompt: int = 0, completion: int = 0) -> Dict[str, Any]:
    """Helper: build a per-step stats dict matching _build_stats_dict output."""
    from api._cache_stats import _build_stats_dict
    return _build_stats_dict(hit, miss, prompt, completion)


class TestComputeCacheStatsBatch:
    """Test suite for compute_cache_stats_batch error handling."""

    def test_empty_input(self):
        """Empty run_ids list returns empty dict."""
        assert compute_cache_stats_batch([]) == {}

    def test_all_succeed(self):
        """All runs succeed — returns expected stats."""
        run_ids = ["run-1", "run-2"]

        def mock_per_step(run_id: str):
            if run_id == "run-1":
                return {"step_0": _mock_stats(10, 5)}
            elif run_id == "run-2":
                return {"step_0": _mock_stats(20, 10)}
            return {}

        with patch(
            "api._cache_stats.compute_cache_stats_per_step",
            side_effect=mock_per_step,
        ):
            result = compute_cache_stats_batch(run_ids)

        assert "run-1" in result
        assert result["run-1"]["cache_hit_tokens"] == 10
        assert result["run-1"]["cache_miss_tokens"] == 5

        assert "run-2" in result
        assert result["run-2"]["cache_hit_tokens"] == 20
        assert result["run-2"]["cache_miss_tokens"] == 10

    def test_skips_failing_run(self, caplog):
        """A single failing run is skipped; remaining runs still return stats."""
        run_ids = ["run-ok-1", "run-bad", "run-ok-2"]

        call_count = 0

        def mock_per_step(run_id: str):
            nonlocal call_count
            call_count += 1
            if run_id == "run-bad":
                raise RuntimeError("Corrupt trace DB")
            return {"step_0": _mock_stats(5, 3)}

        with patch(
            "api._cache_stats.compute_cache_stats_per_step",
            side_effect=mock_per_step,
        ):
            result = compute_cache_stats_batch(run_ids)

        # Both good runs should be in result
        assert "run-ok-1" in result
        assert "run-ok-2" in result
        # The bad run should be absent
        assert "run-bad" not in result

        # A warning should have been logged
        warning_messages = [
            r.message for r in caplog.records if r.levelname == "WARNING"
        ]
        assert any("run-bad" in msg for msg in warning_messages)
        assert any("Corrupt trace DB" in msg for msg in warning_messages)

    def test_all_fail(self, caplog):
        """All runs fail — returns empty dict with no crash."""
        run_ids = ["run-bad-1", "run-bad-2"]

        def mock_per_step(run_id: str):
            raise RuntimeError("Failing: " + run_id)

        with patch(
            "api._cache_stats.compute_cache_stats_per_step",
            side_effect=mock_per_step,
        ):
            result = compute_cache_stats_batch(run_ids)

        assert result == {}

        # One warning per failing run, and no other warning in this path: these
        # run ids are not in any trace database, so nothing is persisted and the
        # sidecar has nothing to say.
        warning_messages = [
            r.message for r in caplog.records if r.levelname == "WARNING"
            and "Failed to compute cache stats" in r.message
        ]
        assert len(warning_messages) == 2

    def test_partial_data_some_runs_empty(self):
        """Runs with no token_usage data (empty per_step) are absent from result."""
        run_ids = ["run-empty", "run-with-data"]

        def mock_per_step(run_id: str):
            if run_id == "run-empty":
                return {}
            return {"step_0": _mock_stats(10, 5)}

        with patch(
            "api._cache_stats.compute_cache_stats_per_step",
            side_effect=mock_per_step,
        ):
            result = compute_cache_stats_batch(run_ids)

        # Empty run should be absent
        assert "run-empty" not in result
        # Run with data should be present
        assert "run-with-data" in result
        assert result["run-with-data"]["cache_hit_tokens"] == 10

    def test_exception_types_caught(self, caplog):
        """Various exception types (ValueError, OSError, etc.) are all caught."""
        run_ids = ["run-value-error", "run-ok"]

        def mock_per_step(run_id: str):
            if run_id == "run-value-error":
                raise ValueError("Bad parameter")
            return {"step_0": _mock_stats(3, 2)}

        with patch(
            "api._cache_stats.compute_cache_stats_per_step",
            side_effect=mock_per_step,
        ):
            result = compute_cache_stats_batch(run_ids)

        assert "run-value-error" not in result
        assert "run-ok" in result

        warning_messages = [
            r.message for r in caplog.records if r.levelname == "WARNING"
        ]
        assert any("run-value-error" in msg for msg in warning_messages)


class TestUnknownCacheRowsStayOutOfTheRatio:
    """A turn whose provider reported no cache info must not be counted a miss.

    End-to-end over the real path: AIGateway._extract_usage builds the payload,
    it is stored as JSON exactly as dpe_pipeline traces it, and the real SQL
    (COALESCE over json_extract) aggregates it. That is the whole chain in
    which an unmeasured turn either stays out of the denominator or silently
    becomes a full miss.
    """

    @staticmethod
    def _usage(prompt, completion, **cache_fields):
        """One traced turn, as the gateway would record it."""
        from core.ai_router import AIGateway
        return AIGateway._extract_usage(SimpleNamespace(usage=SimpleNamespace(
            prompt_tokens=prompt, completion_tokens=completion, **cache_fields)))

    @staticmethod
    def _fake_skillflow(rows):
        """rows: list of (step_id, usage payload) for a single run 'run-x'."""
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE skillflow_trace (run_id TEXT, step_id TEXT, "
            "category TEXT, event TEXT, payload_json TEXT)"
        )
        for step_id, payload in rows:
            conn.execute(
                "INSERT INTO skillflow_trace VALUES (?, ?, 'usage', "
                "'token_usage', ?)",
                ("run-x", step_id, json.dumps(payload)),
            )
        conn.commit()

        class _SF:
            def trace_query(self, run_id, sql, params):
                return conn.execute(sql, params).fetchall()

        return _SF()

    def _stats(self, rows):
        sf = self._fake_skillflow(rows)
        with patch("api.dependencies.get_skillflow", return_value=sf):
            return compute_cache_stats_batch(["run-x"])["run-x"]

    def test_mixed_known_and_unknown_uses_known_rows_only(self):
        """A silent-provider turn adds nothing to either side of the ratio."""
        stats = self._stats([
            ("1", self._usage(1000, 50, prompt_cache_hit_tokens=800,
                              prompt_cache_miss_tokens=200)),
            ("2", self._usage(9000, 60)),   # Ollama Cloud shape: says nothing
        ])
        assert stats["cache_hit_tokens"] == 800
        assert stats["cache_miss_tokens"] == 200
        # Counting the unmeasured 9000 as a miss would give 800/10000 = 0.08.
        assert stats["hit_ratio"] == 0.8
        # …but keeping it out of the RATIO must not keep it out of the TOTAL.
        # This line used to read `total_tokens == 1000`, pinning the subset as
        # the total; measured on jinyong-numbers 2026-09-02 that showed 3.4M
        # for a run that had processed 78.8M.
        assert stats["covered_tokens"] == 1000
        assert stats["prompt_tokens"] == 10000
        assert stats["completion_tokens"] == 110
        assert stats["total_tokens"] == 10110

    def test_all_unknown_reports_undefined_not_zero(self):
        """No measured tokens at all -> undefined ratio, never a 0% claim."""
        stats = self._stats([
            ("1", self._usage(4000, 30)),
            ("2", self._usage(6000, 40)),
        ])
        assert stats["hit_ratio"] is None
        assert stats["covered_tokens"] == 0
        # Unknown cache accounting is not zero work: the tokens were processed.
        assert stats["total_tokens"] == 10070

    def test_all_measured_zero_hit_reports_a_real_zero(self):
        """A provider that DID report cached_tokens=0 aggregates to a real 0.0,
        which is what "undefined" above must stay distinguishable from."""
        stats = self._stats([
            ("1", self._usage(4000, 30,
                              prompt_tokens_details=SimpleNamespace(cached_tokens=0))),
        ])
        assert stats["hit_ratio"] == 0.0
        assert stats["covered_tokens"] == 4000
        assert stats["total_tokens"] == 4030


class TestMergeStatsIsTheOnlyMerge:
    """run_routers and repo_routers each re-derived total/ratio inline; now
    there is one merge and it must carry every field."""

    def test_merge_from_none_copies(self):
        from api._cache_stats import merge_stats, _build_stats_dict
        s = _build_stats_dict(8, 2, 100, 10)
        assert merge_stats(None, s) == s

    def test_merge_sums_all_four_and_rederives(self):
        from api._cache_stats import merge_stats, _build_stats_dict
        a = _build_stats_dict(8, 2, 100, 10)
        b = _build_stats_dict(0, 0, 9000, 60)   # a silent-provider step
        m = merge_stats(a, b)
        assert m["cache_hit_tokens"] == 8 and m["cache_miss_tokens"] == 2
        assert m["covered_tokens"] == 10
        assert m["hit_ratio"] == 0.8            # silent step stays out of the ratio
        assert m["prompt_tokens"] == 9100
        assert m["completion_tokens"] == 70
        assert m["total_tokens"] == 9170        # …and inside the total

    def test_the_routers_use_it(self):
        import inspect, api.run_routers, api.repo_routers
        for mod in (api.run_routers, api.repo_routers):
            src = inspect.getsource(mod)
            assert "merge_stats(" in src, mod.__name__
            assert 'merged["cache_hit_tokens"] +=' not in src, mod.__name__


# ═══════════════════════════════════════════════════════════════════════════
# Witnesses for the derived-stats sidecar
# ═══════════════════════════════════════════════════════════════════════════
#
# Everything below runs the REAL module over REAL per-project trace databases
# (a real `skillflow_trace` table, real SQL), so "the sidecar served the same
# numbers" is a comparison between the shipped code paths and not between two
# stand-ins. Only the engine object, the state mirror and `enrich_project_status`
# are doubles — the first two are what a test cannot own, the third is a per-row
# cost this module has nothing to do with (tests/unit/test_enrich_project_status.py
# covers it).

_TRACE_SCHEMA = """
CREATE TABLE IF NOT EXISTS skillflow_trace (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           TEXT NOT NULL,
    step_id          TEXT,
    step_instance_id INTEGER,
    seq              INTEGER NOT NULL,
    category         TEXT NOT NULL,
    event            TEXT NOT NULL,
    payload_json     TEXT NOT NULL DEFAULT '{}',
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
)
"""


def _write_usage_rows(path: Path, rows) -> None:
    """Append token_usage traces, exactly as the engine writes them.

    ``rows`` is a list of ``(run_id, step_id, payload)``. The payload keys are
    the ones `AIGateway._extract_usage` produces and the batch's SQL reads:
    cache_hit_tokens / cache_miss_tokens / prompt_tokens / completion_tokens.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(_TRACE_SCHEMA)
        offset = conn.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 FROM skillflow_trace").fetchone()[0]
        for i, (run_id, step_id, payload) in enumerate(rows):
            conn.execute(
                "INSERT INTO skillflow_trace "
                "(run_id, step_id, seq, category, event, payload_json) "
                "VALUES (?, ?, ?, 'usage', 'token_usage', ?)",
                (run_id, step_id, offset + i, json.dumps(payload)))
        conn.commit()
    finally:
        conn.close()


class _TraceEngine:
    """The skillflow surface the batch touches, backed by real trace.db files.

    ``_trace_db_path`` is the attribute api/_cache_stats keys persisted rows on,
    so a double that lacks it gets NOTHING cached (see
    `TestTheStoreThatCannotNameItsSource`). This one has it, so these tests
    exercise the sidecar rather than a stand-in for it. `trace_query` opens the
    project's real trace database, which is what the counting witness counts.
    """

    def __init__(self, root: Path):
        self._root = Path(root)
        self._trace_db_path = str(root)
        self.runs: Dict[str, dict] = {}
        self.trace_calls: List[str] = []

    # ── fixture building ──
    def add_run(self, project_id: str, run_id: str, status: str,
                usage=()) -> None:
        self.runs[run_id] = {
            "id": run_id, "project_id": project_id,
            "graph_name": "dpe_default_v2", "status": status,
            "current_node": "t_impl", "error_reason": "", "error": "",
            "updated_at": "2026-11-18 15:00:00",
            "created_at": "2026-11-18 14:00:00",
        }
        _write_usage_rows(self.trace_db_path(project_id),
                          [(run_id, step_id, payload) for step_id, payload in usage])

    def trace_db_path(self, project_id: str) -> Path:
        # Keyed on the private root, not `_trace_db_path`: a test that sets the
        # latter to None is saying "this store cannot NAME its trace root", and
        # the store still has to be able to read its own files.
        return self._root / project_id / "trace.db"

    def _trace_db_path_for(self, project_id: str):
        return self.trace_db_path(project_id)

    # ── skillflow surface ──
    def trace_query(self, run_id, sql, params=()):
        run = self.runs.get(run_id)
        if run is None:
            raise KeyError(f"no such run: {run_id}")
        path = self.trace_db_path(run["project_id"])
        self.trace_calls.append(str(path))
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, tuple(params)).fetchall()
        finally:
            conn.close()

    def get_run(self, run_id):
        run = self.runs.get(run_id)
        return dict(run) if run else None

    def list_runs(self, project_id=None, status=None):
        return [dict(r) for r in self.runs.values()
                if (not project_id or r["project_id"] == project_id)
                and (not status or r["status"] == status)]


class _Registry:
    """ConfigRegistry shape the list builder reads: label + the repo/loop flags."""

    class _Manifest:
        label = "DPE Pipeline"
        has_task_loop = True
        registers_generated_pipeline = False
        registers_generated_addon = False
        repo_mode = "code"

    def get(self, name):
        return self._Manifest()


@pytest.fixture
def stats_env(tmp_path, monkeypatch):
    """A data root this test owns: its own sidecar file and its own trace root.

    `AITELIER_CACHE_STATS_DB` is the documented override
    (core/datadir.cache_stats_sidecar_path), so each test gets an empty sidecar
    instead of whatever the session's earlier tests left in the data root.
    """
    monkeypatch.setenv("AITELIER_CACHE_STATS_DB", str(tmp_path / "cache-stats.db"))
    return SimpleNamespace(sidecar=tmp_path / "cache-stats.db",
                           trace_root=tmp_path / "workspaces")


@pytest.fixture
def engine(stats_env):
    """The engine double, installed as THE loaded singleton the batch consults.

    `_skillflow_instance` is what makes it "this process already has its engine":
    api/_cache_stats._loaded_engine never boots one, it only reports one that is
    up. Both accessors are pointed at the same object so a route and the batch
    cannot be reading two different stores.
    """
    eng = _TraceEngine(stats_env.trace_root)
    return eng


@pytest.fixture
def installed(engine, monkeypatch):
    monkeypatch.setattr(deps, "_skillflow_instance", engine)
    monkeypatch.setattr(deps, "get_skillflow", lambda: engine)
    monkeypatch.setattr(rr, "get_skillflow", lambda: engine)
    return engine


@pytest.fixture
def api(tmp_path, installed, monkeypatch):
    """A TestClient over a state mirror this test owns.

    Same overrides `tests/conftest.py:client_fixture` installs, plus the config
    registry (the endpoint takes it as a dependency and the real one would boot
    the real engine) and an `enrich_project_status` that adds nothing — the
    per-row enrichment is a different term with its own file.
    """
    db = DBManager(str(tmp_path / "stats_mirror.db"))
    ws = WorkspaceManager(str(tmp_path / "unused-ws"))
    app.dependency_overrides[get_db_manager] = lambda: db
    app.dependency_overrides[get_workspace_manager] = lambda: ws
    app.dependency_overrides[get_config_registry] = lambda: _Registry()
    app.state._test_mode = True
    monkeypatch.setattr(rr, "enrich_project_status", lambda row: row)
    try:
        with TestClient(app) as client:
            yield SimpleNamespace(client=client, db=db)
    finally:
        app.state._test_mode = False
        app.dependency_overrides.clear()


def _seed_mirror(db, project_ids, status="completed") -> None:
    """One real state-mirror row per project, dated inside the 30-day window.

    `status` is the MIRROR row's status, and it is `completed` by default so the
    liveness pass (`_attach_liveness`) skips these rows: it queries the project's
    trace database for every NON-terminal mirror row, and a test that counted
    trace-database opens would then be counting that pass rather than the batch.
    """
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    for pid in project_ids:
        db.ensure_project(pid, pid)
        with db.get_connection() as conn:
            conn.execute("UPDATE runs SET updated_at = ?, status = ? "
                         "WHERE project_id = ?", (stamp, status, pid))
            conn.commit()


def _inline(engine, run_ids) -> Dict[str, Dict[str, Any]]:
    """What the batch must equal: compute_cache_stats_per_step folded by project."""
    out: Dict[str, Dict[str, Any]] = {}
    for run_id in run_ids:
        project_id = engine.runs[run_id]["project_id"]
        merged: Dict[str, Any] | None = None
        for step_stats in cs.compute_cache_stats_per_step(run_id).values():
            merged = merge_stats(merged, step_stats)
        if merged is not None:
            out[project_id] = merged if project_id not in out else merge_stats(
                out[project_id], merged)
    return out


def _cache_stats_by_project(body) -> Dict[str, Any]:
    return {row["project_id"]: row["cache_stats"] for row in body["runs"]}


def _refresh(client) -> dict:
    """A request that cannot be served from the read cache.

    `reset()` forgets the built value (a test needs the process to forget, see
    tests/conftest.py:_clear_read_cache); the next GET then builds the list —
    from the sidecar if it can, inline if it must.
    """
    _read_cache.reset()
    response = client.get("/api/runs")
    assert response.status_code == 200, response.text
    return response.json()


class TestTheSidecarServesWhatTheTraceDatabasesSay:
    """The endpoint's numbers, and where they come from."""

    @pytest.fixture
    def populated(self, engine, api):
        """Six projects / seven runs: silent-provider steps, a run with no
        traces at all, a project with two runs (the fold), and statuses that
        cover both sides of the freshness rule."""
        engine.add_run("p-one", "r-one", "completed", usage=[
            ("1", {"cache_hit_tokens": 800, "cache_miss_tokens": 200,
                   "prompt_tokens": 1000, "completion_tokens": 50}),
            ("2", {"prompt_tokens": 9000, "completion_tokens": 60}),
        ])
        engine.add_run("p-two", "r-two", "running:t_impl", usage=[
            ("1", {"cache_hit_tokens": 5, "cache_miss_tokens": 5,
                   "prompt_tokens": 10, "completion_tokens": 2}),
        ])
        # No token_usage traces at all: cache_stats is None, and that answer is
        # persisted too (an empty payload), or its trace database would be
        # reopened on every read.
        engine.add_run("p-three", "r-three", "completed", usage=[])
        engine.add_run("p-four", "r-four", "completed", usage=[
            ("1", {"cache_hit_tokens": 40, "cache_miss_tokens": 10,
                   "prompt_tokens": 50, "completion_tokens": 4}),
        ])
        engine.add_run("p-four", "r-five", "failed", usage=[
            ("1", {"prompt_tokens": 600, "completion_tokens": 30}),
        ])
        engine.add_run("p-five", "r-six", "paused", usage=[
            ("1", {"cache_hit_tokens": 1, "cache_miss_tokens": 9,
                   "prompt_tokens": 10, "completion_tokens": 1}),
        ])
        engine.add_run("p-six", "r-seven", "completed", usage=[
            ("1", {"cache_hit_tokens": 12, "cache_miss_tokens": 4,
                   "prompt_tokens": 16, "completion_tokens": 8}),
        ])
        _seed_mirror(api.db, ["p-one", "p-two", "p-three", "p-four",
                              "p-five", "p-six"])
        first = _refresh(api.client)      # populates the sidecar
        second = _refresh(api.client)     # served from it
        return SimpleNamespace(api=api, engine=engine,
                               first=first, second=second)

    def test_the_served_rows_equal_the_inline_computation(self, populated):
        """>= 5 runs: what the endpoint returns is the per-step fold, exactly.

        The second response is the one that matters — the first computed inline
        by definition, the second is what the sidecar handed back. Both are
        compared, because a sidecar that agrees with itself and not with the
        trace databases is exactly the failure a cache can hide.
        """
        engine = populated.engine
        run_ids = list(engine.runs)
        assert len(run_ids) >= 5
        expected = _inline(engine, run_ids)
        assert set(expected) == {"p-one", "p-two", "p-four", "p-five", "p-six"}

        for body in (populated.first, populated.second):
            got = _cache_stats_by_project(body)
            assert got["p-three"] is None, "a run with no traces has no stats"
            for project_id, stats in expected.items():
                assert got[project_id] == stats, project_id
            # The silent-provider step is inside the total and outside the ratio.
            assert got["p-one"]["total_tokens"] == 10110
            assert got["p-one"]["hit_ratio"] == 0.8
            # p-four's two runs are folded into one answer.
            assert got["p-four"]["total_tokens"] == 50 + 4 + 600 + 30

    def test_a_row_inserted_into_a_trace_db_moves_the_total_by_exactly_that_much(
            self, populated, caplog):
        """The mutation witness: TTL/refresh path, exact delta.

        Three reads of the same live run: (1) served from the populated sidecar,
        unchanged — the row is inside its freshness window; (2) still unchanged
        after a token_usage row was inserted, because a live run's row is served
        until its window elapses; (3) after the window is aged out, the number
        moves by exactly the inserted prompt+completion, which only the trace
        database could have told us.
        """
        api, engine = populated.api, populated.engine
        before = _cache_stats_by_project(_refresh(api.client))
        total_before = before["p-two"]["total_tokens"]

        _write_usage_rows(engine.trace_db_path("p-two"), [
            ("r-two", "2", {"cache_hit_tokens": 100, "cache_miss_tokens": 50,
                            "prompt_tokens": 500, "completion_tokens": 25}),
        ])

        still_cached = _cache_stats_by_project(_refresh(api.client))
        assert still_cached["p-two"]["total_tokens"] == total_before, (
            "the row is inside its freshness window, so it must be served as-is")

        # Age every persisted row past the window: this is the TTL elapsing, in
        # the only way a test can wait for it without sleeping.
        long_ago = time.time() - (cs.freshness_seconds() + 3600.0)
        conn = sqlite3.connect(str(cs._sidecar_path()))
        try:
            conn.execute("UPDATE run_cache_stats SET computed_at = ?", (long_ago,))
            conn.commit()
        finally:
            conn.close()

        after = _cache_stats_by_project(_refresh(api.client))
        assert after["p-two"]["total_tokens"] == total_before + 500 + 25
        assert after["p-two"]["prompt_tokens"] == before["p-two"]["prompt_tokens"] + 500
        assert after["p-two"]["completion_tokens"] == (
            before["p-two"]["completion_tokens"] + 25)

    def test_the_sidecar_holds_a_row_per_run_including_the_silent_ones(
            self, populated):
        """One row per run, and the no-token-data run is one of them."""
        conn = sqlite3.connect(str(cs._sidecar_path()))
        try:
            rows = dict(conn.execute(
                "SELECT run_id, stats_json FROM run_cache_stats").fetchall())
        finally:
            conn.close()
        assert set(rows) == {"r-one", "r-two", "r-three", "r-four",
                             "r-five", "r-six", "r-seven"}
        assert json.loads(rows["r-three"]) == {}, (
            "'this run has no token data' is an answer and has to be stored")


class TestTheCheapPathCountsTheOpens:
    """With a populated sidecar, no trace database is opened — counted."""

    @pytest.fixture
    def counted(self, monkeypatch):
        """Count real SQLite opens of a per-project trace database."""
        opened: List[str] = []
        real_connect = sqlite3.connect

        def counting_connect(database, *args, **kwargs):
            if str(database).endswith("trace.db"):
                opened.append(str(database))
            return real_connect(database, *args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", counting_connect)
        return opened

    @pytest.fixture
    def warm(self, engine, installed, stats_env, counted):
        engine.add_run("p-warm", "r-warm", "completed", usage=[
            ("1", {"cache_hit_tokens": 30, "cache_miss_tokens": 20,
                   "prompt_tokens": 50, "completion_tokens": 5}),
        ])
        engine.add_run("p-quiet", "r-quiet", "completed", usage=[])
        # Populate first, and prove the population DID read the trace databases.
        counted.clear()
        cold = compute_cache_stats_batch(["r-warm", "r-quiet"])
        assert cold["r-warm"]["total_tokens"] == 55
        assert counted, "the populating pass must have opened the trace databases"
        return engine

    def test_the_batch_opens_no_trace_database_when_the_rows_are_there(
            self, warm, counted):
        counted.clear()
        warm.trace_calls.clear()
        served = compute_cache_stats_batch(["r-warm", "r-quiet"])
        assert counted == [], f"opened trace databases: {counted}"
        assert warm.trace_calls == [], "trace_query was called"
        # …and it is still the right answer, not an empty one.
        assert served["r-warm"]["total_tokens"] == 55
        assert served["r-warm"]["hit_ratio"] == 0.6
        assert "r-quiet" not in served

    def test_a_request_that_finds_the_rows_opens_no_trace_database(
            self, engine, api, counted):
        """The same claim one layer up: through the shipped GET /api/runs."""
        engine.add_run("p-http", "r-http", "completed", usage=[
            ("1", {"cache_hit_tokens": 1, "cache_miss_tokens": 1,
                   "prompt_tokens": 2, "completion_tokens": 1}),
        ])
        _seed_mirror(api.db, ["p-http"])
        counted.clear()          # the fixture's own writes are not the claim
        first = _refresh(api.client)
        assert _cache_stats_by_project(first)["p-http"]["total_tokens"] == 3
        assert counted, "the first request had to read the trace database"

        counted.clear()
        engine.trace_calls.clear()
        second = _refresh(api.client)
        assert _cache_stats_by_project(second)["p-http"] == {
            "cache_hit_tokens": 1, "cache_miss_tokens": 1, "hit_ratio": 0.5,
            "covered_tokens": 2, "prompt_tokens": 2, "completion_tokens": 1,
            "total_tokens": 3}
        assert counted == [], f"opened trace databases: {counted}"
        assert engine.trace_calls == [], (
            "no trace read may happen for a terminal row either — the liveness "
            "pass only queries non-terminal ones, and this run is completed")


class TestTheThreeDegradations:
    """A derived cache that cannot do its job must not become a failure mode."""

    @pytest.fixture
    def wired(self, engine, installed, api):
        engine.add_run("p-deg", "r-deg", "running:t_impl", usage=[
            ("1", {"cache_hit_tokens": 7, "cache_miss_tokens": 3,
                   "prompt_tokens": 10, "completion_tokens": 2}),
        ])
        _seed_mirror(api.db, ["p-deg"])
        return SimpleNamespace(api=api, engine=engine)

    @staticmethod
    def _warnings(caplog, needle: str) -> List[str]:
        return [r.getMessage() for r in caplog.records
                if r.levelname == "WARNING" and needle in r.getMessage()]

    def _expect_the_inline_answer(self, wired, body, caplog, needle):
        stats = _cache_stats_by_project(body)["p-deg"]
        expected = _inline(wired.engine, ["r-deg"])["p-deg"]
        assert stats == expected
        assert stats["total_tokens"] == 12
        assert self._warnings(caplog, needle), (
            f"no warning mentioning {needle!r}; got "
            f"{[r.getMessage() for r in caplog.records]}")

    def test_a_missing_sidecar_logs_and_computes_inline(
            self, wired, stats_env, caplog):
        """First read of a fresh data root: no file, no rows, full inline cost."""
        assert not stats_env.sidecar.exists()
        caplog.set_level(logging.WARNING, logger="api._cache_stats")
        body = _refresh(wired.api.client)
        self._expect_the_inline_answer(wired, body, caplog, "did not exist")
        assert stats_env.sidecar.exists(), "the read creates the file it needed"
        conn = sqlite3.connect(str(stats_env.sidecar))
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM run_cache_stats").fetchone()[0] == 1
        finally:
            conn.close()

    def test_a_corrupt_sidecar_logs_falls_back_and_is_replaced(
            self, wired, stats_env, caplog):
        """A half-written or non-SQLite file: one inline read, then a fresh file."""
        stats_env.sidecar.write_bytes(b"this is not a sqlite database\n" * 40)
        caplog.set_level(logging.WARNING, logger="api._cache_stats")
        body = _refresh(wired.api.client)
        self._expect_the_inline_answer(wired, body, caplog, "corrupt")
        quarantined = list(stats_env.sidecar.parent.glob("cache-stats.db.corrupt-*"))
        assert quarantined, "the corrupt file must be moved aside, not reused"
        # …and the NEXT read is served from the replacement, not inline again.
        served = _refresh(wired.api.client)
        assert _cache_stats_by_project(served)["p-deg"]["total_tokens"] == 12

    def test_an_unwritable_sidecar_logs_and_still_answers_200(
            self, wired, stats_env, caplog):
        """Read-only sidecar: the read is correct, only the write is lost."""
        if os.geteuid() == 0:
            pytest.skip("root ignores the write bit, so this cannot be set up")
        # A valid, empty sidecar that cannot be written to.
        warm = sqlite3.connect(str(stats_env.sidecar))
        try:
            warm.execute(cs._SIDECAR_DDL)
            warm.commit()
        finally:
            warm.close()
        os.chmod(stats_env.sidecar, 0o400)
        try:
            caplog.set_level(logging.WARNING, logger="api._cache_stats")
            body = _refresh(wired.api.client)
            stats = _cache_stats_by_project(body)["p-deg"]
            assert stats == _inline(wired.engine, ["r-deg"])["p-deg"]
            assert stats["total_tokens"] == 12
            messages = [r.getMessage() for r in caplog.records
                        if r.levelname == "WARNING"
                        and "cache-stats sidecar" in r.getMessage()]
            assert messages, "an unwritable sidecar must be reported"
            assert any(("write failed" in m) or ("unusable" in m) for m in messages), messages
        finally:
            os.chmod(stats_env.sidecar, 0o600)

    def test_an_unopenable_sidecar_path_logs_and_computes_inline(
            self, wired, stats_env, caplog):
        """A directory where the file should be: open fails, the read does not."""
        stats_env.sidecar.mkdir()
        caplog.set_level(logging.WARNING, logger="api._cache_stats")
        body = _refresh(wired.api.client)
        self._expect_the_inline_answer(wired, body, caplog, "cannot be opened")


class TestTheStoreThatCannotNameItsSource:
    """No trace root, no persistence — the reason a unit-test double is safe."""

    def test_without_a_trace_root_nothing_is_written_and_nothing_is_reused(
            self, stats_env, monkeypatch):
        """The gate: a row is only written when its source can be named."""
        engine = _TraceEngine(stats_env.trace_root)
        engine.add_run("p-anon", "r-anon", "completed", usage=[
            ("1", {"prompt_tokens": 4, "completion_tokens": 1})])
        engine._trace_db_path = None       # skillflow configured without one
        monkeypatch.setattr(deps, "_skillflow_instance", engine)
        monkeypatch.setattr(deps, "get_skillflow", lambda: engine)

        assert compute_cache_stats_batch(["r-anon"])["r-anon"]["total_tokens"] == 5
        assert not stats_env.sidecar.exists(), "nothing to key a row on"
        # Recomputing is the proof: a second call reads the trace database again.
        engine.trace_calls.clear()
        assert compute_cache_stats_batch(["r-anon"])["r-anon"]["total_tokens"] == 5
        assert engine.trace_calls, "the second call had to read the trace database"

    def test_a_run_id_the_engine_does_not_know_is_not_persisted(
            self, stats_env, engine, installed):
        """Rows are answers about real runs; a made-up id is not one."""
        engine.add_run("p-real", "r-real", "completed", usage=[
            ("1", {"prompt_tokens": 2, "completion_tokens": 2})])
        with patch("api._cache_stats.compute_cache_stats_per_step",
                   side_effect=lambda run_id: {
                       "1": cs._build_stats_dict(0, 0, 9, 9)}):
            compute_cache_stats_batch(["r-real", "ghost"])
        conn = sqlite3.connect(str(stats_env.sidecar))
        try:
            stored = {row[0] for row in conn.execute(
                "SELECT run_id FROM run_cache_stats")}
        finally:
            conn.close()
        assert stored == {"r-real"}


class TestTheStartupPrefetch:
    """api/main.py starts the warm-up; startup neither waits for it nor fails on it."""

    def test_the_lifespan_starts_it_on_a_daemon_thread(self):
        import inspect
        source = inspect.getsource(api_main.lifespan)
        assert "warm_first_dashboard_read" in source
        assert "daemon=True" in source
        # Started, never joined: a startup that waited for a 14,249.4 ms pass
        # would be a startup that no restart could afford.
        assert ".join(" not in source.split("warm_first_dashboard_read")[0][-200:]

    def test_the_warm_up_warms_the_key_the_request_path_reads(self, monkeypatch):
        """It builds ("runs", None, False) — the anonymous default list."""
        built = []

        class _Engine:
            _trace_db_path = None

        monkeypatch.setattr(deps, "get_skillflow", lambda: _Engine())
        monkeypatch.setattr(deps, "get_db_manager", lambda: "the-db")
        monkeypatch.setattr(deps, "get_config_registry", lambda: _Registry())
        def _list(owner, db, registry, *, cutoff=None):
            built.append((owner, db, cutoff))
            return {"runs": [], "total_count": 0}

        monkeypatch.setattr(rr, "_list_all_runs_uncached", _list)
        _read_cache.reset()
        spans = api_main.warm_first_dashboard_read()
        assert spans is not None and "engine_ms" in spans
        assert ("runs", None, False) in _read_cache._store

    def test_a_failing_warm_up_is_logged_and_swallowed(self, monkeypatch, caplog):
        """Never raises: a broken prefetch costs the first read, not the startup."""
        def _boom():
            raise RuntimeError("engine boot exploded")

        monkeypatch.setattr(deps, "get_skillflow", _boom)
        caplog.set_level(logging.WARNING, logger="api.main")
        assert api_main.warm_first_dashboard_read() is None
        assert any("prefetch failed" in r.getMessage()
                   for r in caplog.records if r.levelname == "WARNING")
