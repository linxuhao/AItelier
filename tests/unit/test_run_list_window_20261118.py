"""`GET /api/runs` costs the window it shows, and says what it left out.

Measured on the state DB 2026-11-18T16:05Z, and not re-derived here: the project
mirror held **1545** rows, **243** of them updated inside 30 days — the page's
own horizon — and ONE enrichment/stats pass over all 1545 cost **8.9-28.1 s** of
this backend's single core (`api/_read_cache.py`'s docstring carries the same
cost model: one uvicorn process, one interpreter, one GIL, shared with the
scheduler driving live runs). The owner's decision: the list defaults to the last
30 days and the archive stays one explicit request away.

One class per clause of the frozen contract:

  * `test_the_plain_request_returns_exactly_the_rows_inside_the_window`
  * `test_an_out_of_window_row_costs_no_enrichment_stats_or_run_lookup`
  * `test_include_all_reaches_the_archive_and_the_cache_holds_two_keys`
  * `test_the_window_cutoff_is_one_clock_read_per_request` (the "exactly one
    clock read" clause, and that it is per REQUEST rather than per row)

Nothing sleeps across the boundary. The module clock (`_runs_now`) is pinned, so
"inside" and "outside" are facts of the fixture rather than of when the suite
ran. The only fake engine records WHICH project ids and run ids it was asked
about, so "the archive cost nothing" is counted, not assumed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import api.run_routers as rr
from api import _read_cache
from api.dependencies import get_db_manager, get_workspace_manager
from api.main import app
from core.db_manager import DBManager
from core.workspace_manager import WorkspaceManager

# The pinned instant, matching the measurement the contract names.
NOW = datetime(2026, 11, 18, 16, 5, 0, tzinfo=timezone.utc)
# …and the three rows that matter around its 30-day cutoff.
INSIDE = "2026-11-16 09:00:00"          # two days old
ON_THE_CUTOFF = "2026-10-19 16:05:00"   # exactly NOW - RUNS_WINDOW_DAYS
ONE_SECOND_OLDER = "2026-10-19 16:04:59"


class _Clock:
    """A pinned clock that COUNTS its reads — the seam `_runs_now` provides."""

    def __init__(self, now: datetime):
        self.now = now
        self.reads = 0

    def __call__(self) -> datetime:
        self.reads += 1
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = _Clock(NOW)
    monkeypatch.setattr(rr, "_runs_now", c)
    return c


class _Store:
    """The skillflow surface the list builder touches, and a record of who it asked.

    `runs_by_project` maps project_id -> list of run dicts, newest first (what
    `list_runs` returns). `asked` / `trace_asked` record every project id and run
    id the builder reached for, so an out-of-window row's zero cost is observed.
    """

    def __init__(self):
        self.runs_by_project: dict[str, list[dict]] = {}
        self.traces: dict[str, list[str]] = {}
        self.asked: list[str] = []
        self.trace_asked: list[str] = []
        self.enriched: list[str] = []
        self.batches: list[list[str]] = []

    def list_runs(self, project_id=None, status=None):
        self.asked.append(project_id)
        return list(self.runs_by_project.get(project_id, []))

    def trace_query(self, run_id, sql, params=()):
        self.trace_asked.append(run_id)
        if "COUNT(*)" in sql:
            return [{"n": len(self.traces.get(run_id, []))}]
        if "created_at" in sql and "LIMIT" in sql:
            rows = self.traces.get(run_id, [])
            return [{"created_at": rows[-1]}] if rows else []
        return []


@pytest.fixture
def engine(monkeypatch):
    """A fake skillflow plus recorders for the two per-row functions.

    `enrich_project_status` and `compute_cache_stats_batch` are the pass the
    window exists to cut, so both are replaced by recorders here — one project id
    and one run-id list each. `get_skillflow` is patched in `api.run_routers`
    only, which is the name the builder calls.
    """
    store = _Store()

    def _enrich(row):
        store.enriched.append(row["project_id"])
        return row

    def _batch(run_ids):
        store.batches.append(list(run_ids))
        return {}

    monkeypatch.setattr(rr, "get_skillflow", lambda: store)
    monkeypatch.setattr(rr, "enrich_project_status", _enrich)
    monkeypatch.setattr(rr, "compute_cache_stats_batch", _batch)
    return store


@pytest.fixture
def api(tmp_path):
    """A TestClient over a DB this test owns, so `updated_at` can be pinned.

    Same overrides `tests/conftest.py:client_fixture` installs (a temp DB and a
    temp workspace), plus the DB handle itself: the window is read off
    `runs.updated_at`, so the test has to be able to write that column.
    """
    db = DBManager(str(tmp_path / "runs_window.db"))
    ws = WorkspaceManager(str(tmp_path / "ws"))
    app.dependency_overrides[get_db_manager] = lambda: db
    app.dependency_overrides[get_workspace_manager] = lambda: ws
    app.state._test_mode = True
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, db=db)
    app.state._test_mode = False
    app.dependency_overrides.clear()


def _seed(db, project_id: str, updated_at: str) -> None:
    """A real mirror row whose `updated_at` is exactly what the window reads."""
    db.ensure_project(project_id, project_id)
    with db.get_connection() as conn:
        conn.execute("UPDATE runs SET updated_at = ? WHERE project_id = ?",
                     (updated_at, project_id))
        conn.commit()


def _run(run_id: str, project_id: str, status: str = "completed") -> dict:
    return {"id": run_id, "project_id": project_id, "graph_name": "dpe_default_v2",
            "status": status, "current_node": "t_impl",
            "updated_at": "2026-11-18 15:00:00",
            "created_at": "2026-11-18 14:00:00",
            "error_reason": "", "error": ""}


# ── Clause 1: the default list IS the window ─────────────────────────────────


def test_the_plain_request_returns_exactly_the_rows_inside_the_window(api, engine, clock):
    """Rows on both sides of the cutoff, plus one that cannot be dated at all.

    The cutoff is inclusive (`>=`), so the row written exactly at it is inside;
    one second older is not, and neither is the row with an EMPTY `updated_at` —
    the window is a claim about when the row last moved, and a row that cannot
    prove it moved recently is an old row by definition. All four are still
    counted, so the page can see what it is not showing.
    """
    _seed(api.db, "p-inside", INSIDE)
    _seed(api.db, "p-edge", ON_THE_CUTOFF)
    _seed(api.db, "p-outside", ONE_SECOND_OLDER)
    _seed(api.db, "p-undated", "")

    resp = api.client.get("/api/runs")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert sorted(r["project_id"] for r in body["runs"]) == ["p-edge", "p-inside"]
    assert body["window_days"] == rr.RUNS_WINDOW_DAYS == 30
    assert body["total_count"] == 4, "every row the mirror returned is counted"
    assert body["returned_count"] == len(body["runs"]) == 2


# ── Clause 2: the archive costs nothing ──────────────────────────────────────


def test_an_out_of_window_row_costs_no_enrichment_stats_or_run_lookup(api, engine, clock):
    """The filter runs BEFORE the pass — that ordering IS the fix.

    One row inside the window and one outside, both with a skillflow run. Each of
    the three per-row costs — `enrich_project_status`, the per-row `list_runs`,
    the cache-stats batch — must be reached for the inside row only, and no run
    id belonging to the outside row may reach the batch at all.
    """
    _seed(api.db, "p-window", INSIDE)
    _seed(api.db, "p-archive", ONE_SECOND_OLDER)
    engine.runs_by_project["p-window"] = [
        _run("run-window", "p-window", status="running:t_impl")]
    engine.runs_by_project["p-archive"] = [_run("run-archive", "p-archive")]

    body = api.client.get("/api/runs").json()
    assert [r["project_id"] for r in body["runs"]] == ["p-window"]

    # ZERO work for the out-of-window row, counted one function at a time.
    assert engine.enriched == ["p-window"], "enrichment ran for the archive"
    assert engine.asked == ["p-window"], "list_runs was called for the archive"
    assert engine.batches == [["run-window"]], (
        f"cache stats were computed over {engine.batches}, which must be the "
        f"windowed run ids and nothing else")
    assert "run-archive" not in engine.trace_asked, "a trace was read for the archive"
    assert "p-archive" not in engine.enriched
    assert "p-archive" not in engine.asked


# ── Clause 3: include_all, and the cache keeps two keys ─────────────────────


def test_include_all_reaches_the_archive_and_the_cache_holds_two_keys(api, engine, clock):
    """`?include_all=true` returns the old row; nothing else touches the key.

    The read cache is keyed on `(owner, the boolean)` and on nothing else, so a
    caller-supplied `status` / `config_name` — which used to mint a key per
    random value and, with the key cap, flush the real entries — is filtered on
    the cached copy instead. Two keys, however many query strings.
    """
    _seed(api.db, "p-now", INSIDE)
    _seed(api.db, "p-then", ONE_SECOND_OLDER)

    plain = api.client.get("/api/runs").json()
    assert [r["project_id"] for r in plain["runs"]] == ["p-now"]

    everything = api.client.get("/api/runs?include_all=true").json()
    assert sorted(r["project_id"] for r in everything["runs"]) == ["p-now", "p-then"]
    assert everything["window_days"] == 30
    assert everything["total_count"] == everything["returned_count"] == 2

    # Arbitrary strings in the OTHER query params must not reach the key table.
    for i in range(4):
        api.client.get(f"/api/runs?status=nonsense{i}&config_name=zzz{i}")
        api.client.get(f"/api/runs?include_all=true&config_name=random-{i}")
        api.client.get(f"/api/runs?include_all=false&status=other{i}")

    assert len(_read_cache._store) == 2, (
        f"expected (owner, False) and (owner, True); got "
        f"{sorted(_read_cache._store, key=repr)}")
    # The lock table is the one that had NO eviction at all — a key per query
    # string accumulated there forever, so the same two-keys claim covers it.
    assert len(_read_cache._locks) == 2, sorted(_read_cache._locks, key=repr)


# ── One clock read per request ──────────────────────────────────────────────


def test_the_window_cutoff_is_one_clock_read_per_request(api, engine, clock):
    """Exactly one clock read per request — not per row, and not per rebuild.

    The cutoff is taken in the handler, before the cache is consulted, so a hit
    still reads it once (the cutoff is this request's) and a miss adds none.
    """
    _seed(api.db, "p-a", INSIDE)
    _seed(api.db, "p-b", ONE_SECOND_OLDER)

    api.client.get("/api/runs")
    assert clock.reads == 1, "a cold request reads the clock once"
    api.client.get("/api/runs")
    assert clock.reads == 2, "a cache HIT still reads the clock once for its cutoff"
    api.client.get("/api/runs?include_all=true")
    assert clock.reads == 3, "one per request, whatever the answer is"
