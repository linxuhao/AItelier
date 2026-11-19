"""Witnesses for issue iss-274a62ec46184da0: freeze only what cannot move.

The invariant: a cache-stats derived row may be served WITHOUT any trace read
ONLY while its run cannot move. skillflow's terminal run statuses are
``SkillFlow.TERMINAL_RUN_STATUSES == ("failed", "completed")``; a terminal run
cannot move by itself. ``failed`` is terminal, not permanent — the ONE method
that reopens it is ``SkillFlow.reactivate_run`` — so the row is dropped at the
host seam that performs that move (``SkillflowHost.reactivate_run``), after the
engine commits it.

Part 1 instruments ``compute_cache_stats_per_step`` so ENTERING it is
observable, and counts real ``sqlite3.connect`` calls on ``trace.db`` paths:

  * a terminal run's row past every freshness window is served with no trace
    read, and the served payload is byte-identical to the inline computation
    and to what the row holds;
  * a run in ``running`` or ``paused`` past the window still recomputes;
  * ``failed`` — not only ``completed`` — takes the no-trace-read path (the red
    arm against the pre-change module: a failed row used a 300 s window).

Part 2 drives the REAL retry entry points over a real ``AItelierSkillFlow``,
a real FastAPI app and a real per-project trace database: the HTTP route
``POST /api/projects/{id}/retry`` and ``DBManager.retry_project`` (the
meta-agent tool's path). After reactivation the sidecar row is gone and the
next batch recomputes the run, so the served numbers are the post-retry totals.

Part 3 is the structural half: comments are stripped before matching, so a
comment naming the guarded file cannot stand in for the call it describes, and
the tree's reactivation call sites are enumerated against the one seam.
"""
import io
import json
import logging
import re
import sqlite3
import time
import tokenize
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from skillflow.graph import PipelineGraph, StepNode

import api._cache_stats as cs
from api import dependencies as deps
from api import meta_routers as mr
from api import project_routers as pr
from api import run_routers as rr
from api.auth import get_optional_user
from core.run_launcher import start_config_run

# The engine double, the real trace-DB writer and the isolated-sidecar fixtures
# of the cache-stats suite. Reusing them keeps this witness about THIS change:
# the trace databases, the SQL and the sidecar are the shipped ones.
from tests.unit.test_cache_stats import (
    _TraceEngine,  # noqa: F401 - fixture dependency of `installed`
    _write_usage_rows,
    engine,        # noqa: F401 - fixture
    installed,     # noqa: F401 - fixture
    stats_env,     # noqa: F401 - fixture
)
# The real host + state mirror + workspace the integration suite boots offline.
from tests.integration.test_state_graph_entrypoints import live

# The unwrapped computation, for the "same trace data, same payload" comparison:
# a spy installed for observability must never be the thing being compared.
_REAL_PER_STEP = cs.compute_cache_stats_per_step


# ── instruments ────────────────────────────────────────────────────────────

def _enter(monkeypatch):
    """Make ENTERING compute_cache_stats_per_step observable.

    Records the run ids the batch actually asked for and still delegates to the
    real function — so "no trace read" is witnessed at the entry the batch calls,
    not inferred from a count downstream.
    """
    entered = []
    real = cs.compute_cache_stats_per_step

    def spy(run_id):
        entered.append(run_id)
        return real(run_id)

    monkeypatch.setattr(cs, "compute_cache_stats_per_step", spy)
    return entered


def _count_trace_opens(monkeypatch):
    """Count real SQLite opens of a per-project trace database."""
    opened = []
    real_connect = sqlite3.connect

    def counting(database, *args, **kwargs):
        if str(database).endswith("trace.db"):
            opened.append(str(database))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", counting)
    return opened


def _sidecar_row(run_id):
    """The persisted (stats_json, computed_at, frozen) row, or None."""
    conn = sqlite3.connect(str(cs._sidecar_path()))
    try:
        return conn.execute(
            "SELECT stats_json, computed_at, frozen FROM run_cache_stats "
            "WHERE run_id = ?", (run_id,)).fetchone()
    finally:
        conn.close()


def _age_the_row(run_id):
    """Move a stored row past every freshness window without sleeping."""
    conn = sqlite3.connect(str(cs._sidecar_path()))
    try:
        conn.execute(
            "UPDATE run_cache_stats SET computed_at = ? WHERE run_id = ?",
            (time.time() - (cs.freshness_seconds() + 3600.0), run_id))
        conn.commit()
    finally:
        conn.close()


def _inline(run_id):
    """What the trace databases say: the per-step fold, computed inline."""
    merged = None
    for step_stats in _REAL_PER_STEP(run_id).values():
        merged = cs.merge_stats(merged, step_stats)
    return merged


# ═══════════════════════════════════════════════════════════════════════════
# 1. The freeze rule: terminal -> no trace read, live -> recompute
# ═══════════════════════════════════════════════════════════════════════════

class TestATerminalRowIsServedWithoutATraceRead:
    @pytest.mark.parametrize("status", ["completed", "failed"])
    def test_past_every_window_a_terminal_run_opens_no_trace_database(
            self, engine, installed, monkeypatch, status):
        """`completed` AND `failed` are terminal, so their rows are served free.

        The row is aged past the freshness window on purpose: without the
        freeze, a `failed` row (and a `completed` one, before this change) is
        recomputed the moment that window elapses. Ageing it makes the two
        behaviours distinguishable instead of both looking cache-served.
        """
        rid = f"r-{status}"
        engine.add_run("p-terminal", rid, status, usage=[
            ("1", {"cache_hit_tokens": 30, "cache_miss_tokens": 20,
                   "prompt_tokens": 50, "completion_tokens": 5}),
        ])
        first = cs.compute_cache_stats_batch([rid])[rid]
        row = _sidecar_row(rid)
        assert row is not None, "the first batch must have persisted the run"
        assert row[2] == 1, f"{status} is terminal, so its row must be frozen"
        assert json.loads(row[0]) == first

        _age_the_row(rid)
        entered = _enter(monkeypatch)
        opened = _count_trace_opens(monkeypatch)
        engine.trace_calls.clear()

        served = cs.compute_cache_stats_batch([rid])

        assert entered == [], "a terminal run's row was recomputed"
        assert opened == [], f"trace databases opened: {opened}"
        assert engine.trace_calls == [], "trace_query was called"
        assert served[rid] == json.loads(row[0]), (
            "the served payload must be the payload the row holds")
        expected = _inline(rid)
        assert served[rid] == expected
        assert json.dumps(served[rid]) == json.dumps(expected), (
            "the served payload must be byte-identical to the inline "
            "computation for the same trace data")
        assert row[0] == json.dumps(expected), (
            "the persisted payload must be byte-identical to the inline "
            "computation for the same trace data")

    @pytest.mark.parametrize("status", ["running:t_impl", "paused"])
    def test_a_live_run_past_its_window_still_recomputes(
            self, engine, installed, monkeypatch, status):
        """Only terminal runs freeze; a live run's numbers still move."""
        rid = "r-live"
        engine.add_run("p-live", rid, status, usage=[
            ("1", {"prompt_tokens": 10, "completion_tokens": 1}),
        ])
        before = cs.compute_cache_stats_batch([rid])[rid]
        assert _sidecar_row(rid)[2] == 0, (
            f"{status} can still move, so its row must not be frozen")

        # A new turn landed and the freshness window elapsed: the batch has to
        # read the trace database to see it.
        _write_usage_rows(engine.trace_db_path("p-live"),
                          [(rid, "2", {"prompt_tokens": 600,
                                       "completion_tokens": 30})])
        _age_the_row(rid)
        entered = _enter(monkeypatch)

        after = cs.compute_cache_stats_batch([rid])[rid]

        assert entered == [rid], "a live run past its window must be recomputed"
        assert after["total_tokens"] == before["total_tokens"] + 630
        assert after == _inline(rid)


class TestTheFreezeTradesNoFallbackAway:
    """A derived cache that cannot do its job must not become a failure mode."""

    def test_a_missing_sidecar_still_computes_a_failed_run_inline(
            self, engine, installed, stats_env, caplog):
        engine.add_run("p-missing", "r-missing", "failed", usage=[
            ("1", {"prompt_tokens": 5, "completion_tokens": 1})])
        assert not stats_env.sidecar.exists()
        caplog.set_level(logging.WARNING, logger="api._cache_stats")

        served = cs.compute_cache_stats_batch(["r-missing"])

        assert served["r-missing"] == _inline("r-missing")
        assert any("did not exist" in r.getMessage() for r in caplog.records)

    def test_a_corrupt_sidecar_still_computes_a_failed_run_inline(
            self, engine, installed, stats_env, caplog):
        stats_env.sidecar.write_bytes(b"this is not a sqlite database\n" * 40)
        engine.add_run("p-corrupt", "r-corrupt", "failed", usage=[
            ("1", {"prompt_tokens": 7, "completion_tokens": 2})])
        caplog.set_level(logging.WARNING, logger="api._cache_stats")

        served = cs.compute_cache_stats_batch(["r-corrupt"])

        assert served["r-corrupt"] == _inline("r-corrupt")
        assert any("corrupt" in r.getMessage() for r in caplog.records)


# ═══════════════════════════════════════════════════════════════════════════
# 2. Every reactivation path drops the row — through the real entry points
# ═══════════════════════════════════════════════════════════════════════════

def _client_for(live, monkeypatch):
    """A FastAPI app carrying the shipped routers over the live fixture's DB."""
    monkeypatch.setattr(mr, "get_skillflow", lambda: live.sf)
    monkeypatch.setattr(rr, "get_skillflow", lambda: live.sf)
    app = FastAPI()
    for module in (pr, mr, rr):
        app.include_router(module.router)
        app.dependency_overrides[module.get_db_manager] = lambda: live.db
    app.dependency_overrides[get_optional_user] = lambda: None
    return TestClient(app)


def _a_failed_run(live, name, pid):
    """One real isolated run failed through the engine's own retry budget."""
    live.sf.register_graph(PipelineGraph(
        name=name, begin="work", steps=[StepNode(id="work", max_retries=1)]))
    live.registry.register_one(
        live.sf, name,
        hint_overrides={"scheduler_owned": True, "repo_mode": "none"})
    launched = start_config_run(live.db, live.ws, name, pid)
    assert launched.get("status") != "error", launched
    rid = launched["run_id"]
    for _ in range(2):
        live.sf.advance_run(rid)
        claimed = live.sf.claim_next_step(rid)
        assert claimed is not None
        live.sf.fail_step(claimed.token, "exhausted retries", retryable=True)
    assert live.sf.get_run(rid)["status"] == "failed"
    return rid


@pytest.mark.parametrize("live", ["host"], indirect=True)
@pytest.mark.parametrize("entry", ["api", "db"])
def test_reactivation_drops_the_row_and_the_next_batch_sees_post_retry_totals(
        live, monkeypatch, entry):
    """The real retry entry points move the run; the frozen row must not survive.

    ``api`` is ``POST /api/projects/{id}/retry`` (api/project_routers.py).
    ``db`` is ``DBManager.retry_project`` — the path the meta-agent's
    retry_project tool takes. Both arrive at ``SkillflowHost.reactivate_run``.

    The pre-retry total is 55 tokens and a frozen row holds it. A post-retry
    turn worth 630 tokens is written BEFORE the retry, so the only way the next
    batch can report 685 is by reading the traces — any frozen row left behind
    would report 55 forever.
    """
    monkeypatch.setenv("AITELIER_CACHE_STATS_DB", str(live.tmp / "cache-stats.db"))
    live.sf._trace_db_path = str(live.tmp / "trace-root")
    monkeypatch.setattr(deps, "_skillflow_instance", live.sf)

    name = f"frozen_cache_retry_{entry}"
    pid = f"frozen-cache-retry-{entry}"
    rid = _a_failed_run(live, name, pid)
    trace_db = Path(live.sf._trace_db_path) / pid / "trace.db"
    _write_usage_rows(trace_db, [(rid, "work", {
        "cache_hit_tokens": 30, "cache_miss_tokens": 20,
        "prompt_tokens": 50, "completion_tokens": 5})])

    entered = _enter(monkeypatch)
    opened = _count_trace_opens(monkeypatch)

    before = cs.compute_cache_stats_batch([rid])
    assert before[rid]["total_tokens"] == 55
    row = _sidecar_row(rid)
    assert row is not None and row[2] == 1, (
        "a failed run is terminal, so its row must be frozen")
    # Final while failed: even past every window the batch opens nothing.
    _age_the_row(rid)
    entered.clear()
    opened.clear()
    assert cs.compute_cache_stats_batch([rid])[rid] == before[rid]
    assert entered == [] and opened == [], (
        "a failed run's frozen row must be served without a trace read")

    # The turn the retry's next attempt produced, on disk before the move.
    _write_usage_rows(trace_db, [(rid, "work", {
        "prompt_tokens": 600, "completion_tokens": 30})])

    if entry == "api":
        with _client_for(live, monkeypatch) as client:
            response = client.post(f"/api/projects/{pid}/retry")
        assert response.status_code == 200, response.text
    else:
        assert live.db.retry_project(pid) is True
    assert live.sf.get_run(rid)["status"] == "running", (
        "the entry point did not reactivate the failed run")

    assert _sidecar_row(rid) is None, (
        "the reactivation seam left a frozen row behind: the run moved out of "
        "`failed`, so its derived numbers are no longer final")

    entered.clear()
    opened.clear()
    after = cs.compute_cache_stats_batch([rid])

    assert entered == [rid], (
        "the reactivated run must be recomputed from its traces")
    # NB: not counted at sqlite3.connect — the engine keeps a per-project trace
    # connection open, so the recomputation reuses it. `entered` and the exact
    # token delta are the evidence that the traces were read.
    assert after[rid]["total_tokens"] == before[rid]["total_tokens"] + 630, (
        "the served numbers must be the post-retry totals")
    assert after[rid] == _inline(rid)


# ═══════════════════════════════════════════════════════════════════════════
# 3. The structural half: one seam, enumerated with comments stripped
# ═══════════════════════════════════════════════════════════════════════════

_REPO = Path(__file__).resolve().parents[2]
_REACTIVATE_CALL = re.compile(r"\.\s*reactivate_run\s*\(")


def _without_comments(source: str) -> str:
    """``source`` with its ``#`` comment tokens removed.

    String literals are untouched (a ``#`` inside one is not a comment), so a
    comment that NAMES a call is not visible to a matcher, while the call it
    describes — typo'd into a comment or left only as prose — cannot pass for
    code. `discard_run_cache_stats` is described in api/_cache_stats.py's
    header and in core/skillflow_host.py's comment; neither may satisfy a
    structural claim that the drop is wired.
    """
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    return tokenize.untokenize(
        token._replace(string="") if token.type is tokenize.COMMENT else token
        for token in tokens)


def _method(code: str, name: str) -> str:
    start = code.index(f"def {name}(")
    end = code.find("\n    def ", start + 1)
    return code[start:end if end != -1 else len(code)]


class TestTheSeamCoversEveryReactivationSite:
    def test_the_instrument_ignores_comments(self):
        """The structural check's own red guard: comments are not evidence."""
        commented = ("# core/skillflow_host.py calls discard_run_cache_stats("
                     "run_id)\nvalue = 1\n")
        assert "discard_run_cache_stats" not in _without_comments(commented)
        assert "discard_run_cache_stats" in commented

    def test_the_drop_is_wired_after_the_engine_commits_the_move(self):
        host = _without_comments(
            (_REPO / "core/skillflow_host.py").read_text())
        seam = _method(host, "reactivate_run")
        assert "super().reactivate_run" in seam, seam
        assert "discard_run_cache_stats" in seam, (
            "the reactivation seam does not drop the derived row")
        assert seam.index("super().reactivate_run") < seam.index(
            "discard_run_cache_stats"), (
            "the row must be dropped only after the move committed; a refused "
            "reactivation leaves the run — and its final numbers — in place")

        stats = _without_comments((_REPO / "api/_cache_stats.py").read_text())
        assert "def discard_run_cache_stats(" in stats
        assert 'frozenset({"completed", "failed"})' in stats, (
            "a failed run is terminal (SkillFlow.TERMINAL_RUN_STATUSES) and "
            "must take the no-trace-read path")

    def test_every_reactivation_call_site_reaches_that_seam(self):
        """Enumerate the callers; a comment naming one cannot make the list."""
        sites = set()
        for folder in ("api", "core"):
            for path in sorted((_REPO / folder).rglob("*.py")):
                if _REACTIVATE_CALL.search(
                        _without_comments(path.read_text())):
                    sites.add(path.relative_to(_REPO).as_posix())
        assert sites - {"core/skillflow_host.py"} == {
            "api/project_routers.py",   # POST /api/projects/{id}/retry
            "api/meta_routers.py",      # rescue_failed
            "core/db_manager.py",       # retry_project (meta-agent tool)
        }, sites
        assert "core/skillflow_host.py" in sites, (
            "the seam every site passes through must itself call the engine")
        # Production builds the host, not a bare SkillFlow: the seam exists on
        # the object the routes and the DBManager actually drive.
        construction_root = _without_comments(
            (_REPO / "api/dependencies.py").read_text())
        assert "AItelierSkillFlow(" in construction_root
