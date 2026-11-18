"""A write must not make the next dashboard read expensive.

The backend is ONE uvicorn process — one interpreter, one GIL — serving both the
dashboard's reads and the scheduler. `api/main.py:442` calls
`_read_cache.clear()` after EVERY request whose method is not in `_SAFE_METHODS`:
a dashboard click, a mark-read, a driver heartbeat, an MCP or State write. Every
one of those made the next read of `/api/runs` a COLD miss that computed inline.
Measured on the live backend on 2026-11-18 (values, not re-derived here):

  * an untouched poll: **0.083-0.128 s**;
  * the read right after a mutating request: **10.93 s / 8.87 s** while the
    per-project enrichment memo was still warm, and **21.26 / 22.76 / 28.13 s**
    once it had expired;
  * one full 1545-row enrichment pass: **8.9-28.1 s** of this process's single
    core — which is also the scheduler's core.

So using the page is what kept making its reads expensive. The fix is
`clear()` = INVALIDATE, not DROP: the previously built value stays in the store
and the generation bump marks it stale, so the next read returns it at once
while exactly ONE background rebuild runs. Staleness is bounded by that one
rebuild — the read after it lands is the new value — and the generation guard
still refuses a rebuild that began before the write. On the memo side the
backstop is no longer the freshness mechanism (the memo's key is: the row's own
change fields), so a write-triggered rebuild does not re-pay the full pass.

What this file pins, class by class:

  * the read after a write is served while one rebuild runs, and the read after
    that rebuild gets the new value;
  * a rebuild that raises is logged, keeps the previous value, does not reach
    the caller, and is retried by the next read;
  * a write does not make the next read re-pay the enrichment pass at all
    (zero `get_run_by_project` / `list_runs` / `get_steps` in the rebuild);
  * the memo backstop is crossed by injection, and is long enough for the
    cadence a write now pays.

Every test here is written to FAIL against the tree before this change (the
commit this file was added in): the first two because `clear()` dropped the
store, the third because the read after it rebuilt inline on the calling
thread, the fourth because a 30 s backstop re-pays a 8.9-28.1 s pass 120 times
an hour. Nothing here sleeps a backstop out: `ttl=0.0` is the injectable cross.
"""
from __future__ import annotations

import logging
import threading
import time

import pytest

from api import _read_cache as rc
from api import dependencies as deps

KEY = ("runs-after-a-write", None)
KEY_BROKEN = ("runs-after-a-write/failing", None)
KEY_MEMO = ("runs-after-a-write/memo", None)

REFRESH_THREAD = "read-cache-refresh"


def _stored(key):
    """The value the cache currently holds for `key`, whatever its entry shape."""
    entry = rc._store.get(key)
    return entry[1] if entry else None


def _must_not_run(message):
    raise AssertionError(message)


def _warned(caplog):
    return any("read cache" in record.getMessage() and record.levelno >= logging.WARNING
               for record in caplog.records)


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _wait_for_refreshes(timeout=5.0):
    """Block until every background rebuild thread is over.

    Waiting for the WARNING is not waiting for the rebuild: `_rebuild` logs the
    failure inside its `except` and releases the key's lock in the `finally`
    AFTERWARDS, and caplog sees the record before the traceback is formatted. A
    read that lands in that gap finds the lock held and — correctly — starts no
    rebuild, so a test that re-read the instant the log appeared could report
    'the key is stuck' on a healthy cache (measured: the same selection is green
    alone and red inside the full suite).
    """
    deadline = time.monotonic() + timeout
    for thread in threading.enumerate():
        if thread.name == REFRESH_THREAD:
            thread.join(max(0.0, deadline - time.monotonic()))


class TestTheReadAfterAWrite:
    def test_it_serves_the_previous_value_while_exactly_one_rebuild_runs(self):
        """The defect, in one test. `clear()` is the ONLY invalidation signal the
        middleware has, so this is what every click, mark-read, heartbeat and
        MCP write does to the page's own reads.

        The builder after the write blocks: if the calling thread enters it, the
        read paid for the rebuild (the 10.93-28.13 s measurements); if it does
        not, this read is the 0.083-0.128 s one and the rebuild runs behind it.
        """
        rc.clear()
        cold_built_on = []
        assert rc.cached(
            KEY, lambda: cold_built_on.append(threading.current_thread().name)
            or "before") == "before"
        assert cold_built_on == [threading.main_thread().name], (
            "the first read of a key this process has never built must compute "
            "inline on the calling thread — there is nothing to serve")

        rc.clear()          # a mutating request went through the middleware

        entered = threading.Event()
        release = threading.Event()
        rebuilds = []

        def rebuild():
            rebuilds.append(threading.current_thread().name)
            entered.set()
            release.wait(5)
            return "after"

        started_at = time.monotonic()
        value = rc.cached(KEY, rebuild)
        elapsed = time.monotonic() - started_at

        assert value == "before", (
            "the read after a write did not serve the previously built value — "
            "it is still a cold miss, and the caller pays the rebuild")
        assert elapsed < 1.0, f"the caller waited {elapsed:.2f}s for the rebuild"
        assert threading.main_thread().name not in rebuilds, (
            "the read after the write re-entered the builder on the calling "
            "thread instead of behind it")
        assert entered.wait(5), "no background rebuild was started at all"
        assert _wait_until(lambda: rebuilds == [REFRESH_THREAD]), (
            f"the write started {rebuilds}, not exactly one background rebuild")
        release.set()
        assert _wait_until(lambda: _stored(KEY) == "after"), (
            "the one rebuild never published the post-write value")
        # Staleness is bounded by that one rebuild, not by the TTL: the next
        # read is the new value and must not build again.
        assert rc.cached(
            KEY, lambda: _must_not_run("the freshly published value was rebuilt")) == "after"
        assert rebuilds == [REFRESH_THREAD]


class TestARebuildThatFails:
    def test_it_is_logged_keeps_the_previous_value_and_is_retried(self, caplog):
        """The dashboard must not inherit the rebuild's exception, and a failed
        rebuild must not cost the entry: losing it would turn the next read into
        the synchronous multi-second pass this change exists to remove."""
        rc.clear()
        assert rc.cached(KEY_BROKEN, lambda: "before") == "before"
        rc.clear()          # a mutating request went through the middleware

        attempts = []

        def exploding():
            attempts.append(threading.current_thread().name)
            raise RuntimeError("the per-project enrichment blew up")

        with caplog.at_level(logging.WARNING, logger="api._read_cache"):
            assert rc.cached(KEY_BROKEN, exploding) == "before", (
                "the failing rebuild reached the caller, or cost it the entry")
            assert _wait_until(lambda: attempts == [REFRESH_THREAD]), (
                f"the rebuild attempts were {attempts}, expected one background run")
            assert _wait_until(lambda: _warned(caplog)), (
                "the failed rebuild was not logged")

        _wait_for_refreshes()   # the lock is released strictly AFTER the log
        assert _stored(KEY_BROKEN) == "before", "a failed rebuild dropped the entry"

        # The lock was released, so the key is not stuck: the next read serves
        # the previous value again AND retries, and this time it lands.
        assert rc.cached(
            KEY_BROKEN,
            lambda: attempts.append(threading.current_thread().name) or "after") == "before"
        assert _wait_until(lambda: _stored(KEY_BROKEN) == "after"), (
            "a rebuild that failed once left the key unable to rebuild again")


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

    `getattr` on purpose: this file also has to run against the tree BEFORE the
    memo existed, where the failures must be the assertions below.
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
        monkeypatch.setattr(deps, "get_config_registry", lambda: None)
        return sf
    return _install


class TestTheWriteTriggeredRebuildIsCheap:
    def test_a_write_does_not_make_the_next_read_repay_the_enrichment_pass(self, engine):
        """The point of the memo, now that a rebuild happens after every write.

        A cache rebuild walks all 1545 rows; if the write also invalidated the
        memo, every click would re-pay 8.9-28.1 s of the one core. It does not:
        the memo is keyed on the row's OWN change fields, and an unchanged row is
        unchanged whether or not a driver heartbeat just went through.

        Both halves are asserted, and both are red before this change — the read
        after the write rebuilt INLINE there, cost the caller the pass, and the
        only thing keeping it cheap was a 30 s backstop.
        """
        sf = engine({f"p{i}": _run(f"p{i}") for i in range(5)})
        rows = [_row(f"p{i}") for i in range(5)]
        builds = []

        def one_pass():
            builds.append(threading.current_thread().name)
            return [dict(deps.enrich_project_status(_row(r["project_id"])))
                    for r in rows]

        rc.clear()
        first = rc.cached(KEY_MEMO, one_pass)
        assert builds == [threading.main_thread().name]
        paid = len(sf.names())
        assert paid > 0, "the first pass must really pay, or this proves nothing"
        sf.reset()

        rc.clear()          # a mutating request went through the middleware

        again = rc.cached(KEY_MEMO, one_pass)
        assert again is first, (
            "the read after the write built a new list inline instead of serving "
            "the previous one — the caller paid for the rebuild")
        assert builds.count(threading.main_thread().name) == 1, (
            "the calling thread re-entered the builder after a write: "
            f"{builds}")

        assert _wait_until(lambda: _stored(KEY_MEMO) is not first), (
            "the write invalidated the entry but the one rebuild never published")
        assert builds == [threading.main_thread().name, REFRESH_THREAD], (
            f"the rebuild ran on {builds}; expected one background run")
        assert sf.names() == [], (
            "the write-triggered rebuild re-derived rows that had not changed: "
            f"{sf.names()} — every click would re-pay a full enrichment pass")


class TestTheMemoBackstop:
    def test_it_is_a_backstop_not_the_freshness_mechanism(self, engine):
        """The key is the freshness mechanism (it moves whenever the scheduler
        records a change); the backstop only bounds change nobody recorded.
        Crossing it must be INJECTABLE, so this never sleeps for it, and it must
        be long enough for the cadence it is now paid at: one read-cache rebuild
        per write, each walking 1545 rows at 8.9-28.1 s of one core.
        """
        sf = engine({"p1": _run("p1")})
        deps.enrich_project_status(_row("p1"))
        sf.reset()
        deps.enrich_project_status(_row("p1"))
        assert sf.names() == [], "an unchanged row inside the backstop is reused"

        deps.enrich_project_status(_row("p1"), ttl=0.0)

        assert "get_run_by_project" in sf.calls_for("p1"), (
            "crossing the backstop did not recompute — the entry is being kept "
            "for ever")
        assert deps.ENRICH_MEMO_TTL >= 300.0, (
            f"the backstop is {deps.ENRICH_MEMO_TTL}s: one full pass costs "
            "8.9-28.1 s of this process's single core, so at 30 s a write-driven "
            "cadence burns 30-94% of the core re-deriving rows that changed "
            "nothing")
