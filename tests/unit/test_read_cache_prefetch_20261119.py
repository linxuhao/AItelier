"""The startup warm-up must build all THREE keys the dashboard reads.

`api/main.py:warm_first_dashboard_read` built one: the anonymous default
`GET /api/runs`. The two reads a visitor actually opens first were still computed
inline by the first request after every restart, because `api/_read_cache.py`
computes a key this process has never built INLINE (the stale-serve design cannot
help an absent entry — there is nothing to serve). Measured by the director on
the live backend at 19:32Z (container recreated 19:00:05Z, load 6.4-7.0, no
browser attached, keys never built):

    GET /api/projects  first call 10.072298 s / 845,769 B   then 0.154613 s
    GET /api/repos     first call  5.882613 s / 535,213 B   then 0.138388 s

So the warm-up now builds `("runs", None, False)`, `("repos", None)` and
`("projects", None)` — the SAME tuple each route builds for an anonymous caller
with the default window, through `api/_read_cache.cached` and the same uncached
builder the route calls. The win is latency, not throughput: the same CPU moves
into the startup thread at boot.

Three things are witnessed here, in that order:

* **All three keys get built.** Run the warm-up, then read each key through
  `cached()` with a builder that raises if it is ever called.
* **Each warmed key IS the key its route builds.** Not a copied literal: each
  route is asked, over HTTP, which key it consults (the `cached()` call it makes
  is spied on), and that key has to be served out of the warmed entry with no
  build. Change a route's key tuple (or its builder's argument list) and this
  file fails, which is the whole point — a warm-up that warms a key nobody reads
  would pass a test written against a copy of the tuple.
* **One failing builder costs its own key only.** The warm-up must never raise
  and must not stop the keys behind it in the loop: the failure is logged at
  WARNING and the other two keys are still built.

**How the warm-up is exercised, and why.** `warm_first_dashboard_read` returns
`None` by design when `app.dependency_overrides` is non-empty: under `TestClient`
the routes answer from overrides while the warm-up would build from the
SINGLETONS, so warming then would warm a value no request reads (pinned by
`test_a_composed_app_is_not_warmed`). The conftest `client` fixture installs
exactly those overrides, so the tests here make the singletons BE what the routes
resolve — `get_db_manager`/`get_config_registry` are pointed at the fixture's
database and a stub registry, the same registry is put in the override table the
routes read — and then lift the override table for the duration of the call. That
runs the production entry point (`warm_first_dashboard_read`, engine boot
included) rather than a copy of its body, and the HTTP half of the test then
resolves the same objects back.
"""
from __future__ import annotations

import logging
import time
from types import SimpleNamespace

import pytest

import api.main as api_main
import api.project_routers as project_routers
import api.run_routers as run_routers
from api import _read_cache as rc
from api import dependencies as deps
from api.dependencies import get_config_registry, get_db_manager
from api.main import app

#: The three key families the dashboard reads. These are NOT the key tuples —
#: the tuples are asserted by asking each route (`test_the_route_reads_the_key_
#: the_warm_up_built`), so a route that changes its tuple fails rather than
#: agreeing with a literal copied here.
WARMED_FAMILIES = {"runs", "repos", "projects"}

#: The per-key spans the warm-up reports next to the two it already had.
SPAN_NAMES = {"engine_ms", "warm_ms", "runs_ms", "repos_ms", "projects_ms"}


def _must_not_build(key):
    """A builder that must never run: it is only handed to a read that has to be
    answered out of the store, so its message names the key that was not warm."""
    def _boom():
        raise AssertionError(
            f"{key!r} was not warm: the request path started building it")
    return _boom


def _warm_keys():
    return list(rc._store)


def _families():
    return {key[0] for key in rc._store}


def _fresh(key):
    return rc._fresh(rc._store.get(key), time.monotonic(), rc.DEFAULT_TTL)


class _Registry:
    """A config registry stand-in: `.get()` is the only method the three uncached
    builders touch, and the mirror is empty here, so nothing is ever found."""

    def get(self, name):
        return None


class _Engine:
    """A skillflow stand-in.

    The builders ask it about runs (`list_runs`, `get_run_by_project`) and the
    empty mirror means they get nothing back — but they DO ask, so patching the
    getter is what keeps the warm-up from booting the real engine inside a unit
    test.
    """

    _trace_db_path = None

    def list_runs(self, **kwargs):
        return []

    def get_run_by_project(self, **kwargs):
        return None

    def get_steps(self, **kwargs):
        return []


@pytest.fixture
def engine(monkeypatch):
    """The stub engine at BOTH names it is reached by.

    `warm_first_dashboard_read` calls `api.dependencies.get_skillflow`, and
    `api/run_routers._list_all_runs_uncached` calls its own module-level import of
    the same function — patching one leaves the other booting the real engine.
    """
    engine = _Engine()
    monkeypatch.setattr(deps, "get_skillflow", lambda: engine)
    monkeypatch.setattr(run_routers, "get_skillflow", lambda: engine)
    return engine


@pytest.fixture
def warm_env(client, engine, monkeypatch):
    """The DB/registry this app's routes resolve, and the real warm-up over them.

    `client` is the conftest TestClient fixture: it owns the isolated DB and the
    dependency overrides, and its `app.state._test_mode` keeps the served
    backend's branch out of the way. `run()` lifts the override table for exactly
    the duration of the call so `warm_first_dashboard_read` (not its inner
    helper) is what executes, then restores it, so the HTTP half of a test is
    back on the composed app with the SAME database.

    `lines` is what the warm-up logged during the `run()` under test, captured
    here rather than through `caplog` because a fixture-time run is recorded in
    pytest's SETUP phase and `caplog.records` would be empty for it.
    """
    db = app.dependency_overrides[get_db_manager]()
    registry = _Registry()
    # The warm-up resolves its dependencies through these getters …
    monkeypatch.setattr(deps, "get_db_manager", lambda: db)
    monkeypatch.setattr(deps, "get_config_registry", lambda: registry)
    # … and the routes resolve `get_config_registry` from the override table, so
    # the same registry has to be there: one composition on both sides.
    monkeypatch.setitem(app.dependency_overrides, get_config_registry,
                        lambda: registry)

    lines: list[str] = []
    logger = logging.getLogger("api.main")

    class _Capture(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    def run():
        handler = _Capture(level=logging.INFO)
        saved_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        saved_overrides = app.dependency_overrides
        app.dependency_overrides = {}
        try:
            return api_main.warm_first_dashboard_read()
        finally:
            app.dependency_overrides = saved_overrides
            logger.setLevel(saved_level)
            logger.removeHandler(handler)

    return SimpleNamespace(db=db, registry=registry, run=run, lines=lines)


@pytest.fixture
def warmed(warm_env):
    spans = warm_env.run()
    assert spans is not None, (
        "the warm-up returned None, so it built nothing: it returns None only "
        "when the engine/database could not be resolved")
    return spans


def test_the_warm_up_builds_all_three_keys(warm_env, warmed):
    """Run it, then read each key it built with a builder that raises if called.

    Three entries in the store, one per family, and reading any of them never
    reaches a builder — that is `cached()`'s FRESH path, i.e. the request that
    follows finds an answer instead of paying the 10.07 s / 5.88 s pass.
    """
    assert SPAN_NAMES <= set(warmed), (
        f"the warm-up reported {sorted(warmed)}; per-key spans and the existing "
        f"engine_ms/warm_ms are expected")
    keys = _warm_keys()
    assert len(keys) == 3, f"the warm-up built {keys!r}, not one key per read"
    assert _families() == WARMED_FAMILIES, (
        f"the warm-up built {_families()}, expected the three dashboard reads")

    for key in keys:
        rc.cached(key, _must_not_build(key))   # raises if this was not warmed
        assert _fresh(key), f"{key!r} was built but is not fresh under DEFAULT_TTL"

    line = ([m for m in warm_env.lines
             if "first-dashboard-read prefetch done" in m] or [""])[-1]
    assert line, f"the warm-up logged no 'prefetch done' line: {warm_env.lines!r}"
    for endpoint in ("/api/runs", "/api/repos", "/api/projects"):
        assert endpoint in line, f"the log line does not name {endpoint}: {line!r}"
    assert "engine" in line and "total" in line, (
        f"the log line lost the shape it already had: {line!r}")


@pytest.mark.parametrize("path", ["/api/runs", "/api/repos", "/api/projects"])
def test_the_route_reads_the_key_the_warm_up_built(warmed, client, monkeypatch,
                                                   path):
    """Each route's OWN key hits the warmed entry — asserted against the route.

    The route is asked over HTTP which key it consults (its `cached()` call is
    spied on) and the builder handed to the cache raises. So this fails, naming
    both, if the route's key tuple changes away from the warm-up's — the property
    a copied literal could never check.
    """
    keys_seen = []
    served = []
    real_cached = rc.cached

    def spy(key, build, ttl=rc.DEFAULT_TTL):
        keys_seen.append(key)
        value = real_cached(key, _must_not_build(key), ttl)
        served.append((key, value))
        return value

    monkeypatch.setattr(rc, "cached", spy)
    warmed_keys = _warm_keys()
    response = client.get(path)

    assert response.status_code == 200
    assert len(keys_seen) == 1, (
        f"{path} consulted the read cache {len(keys_seen)} times: {keys_seen!r}")
    key = keys_seen[0]
    assert key in warmed_keys, (
        f"{path} reads {key!r}, which the warm-up did not build "
        f"(it built {warmed_keys!r}) — the two drifted apart")
    assert _fresh(key), f"{path} read {key!r} but the warmed entry is not fresh"
    # The route got the warmed VALUE, not a second build of an equal one.
    assert served[0][1] is rc._store[key][1], (
        f"{path} did not receive the object the warm-up built for {key!r}")


def test_a_failing_builder_is_logged_and_does_not_stop_the_other_keys(
        warm_env, monkeypatch, caplog):
    """One broken builder costs its own key: no raise, a WARNING, the rest warm.

    The projects pass is made to explode. The warm-up must come back normally,
    log the failure at WARNING naming the key, leave that key unbuilt, and still
    have built the two keys behind it in its loop.
    """
    def _boom(db, owner):
        raise RuntimeError("the projects pass exploded")

    monkeypatch.setattr(project_routers, "_list_projects_uncached", _boom)
    caplog.set_level(logging.WARNING, logger="api.main")

    spans = warm_env.run()   # must not raise

    assert spans is not None, (
        "a failing BUILDER must not make the warm-up return None; only a failed "
        "engine/database resolution does that")
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    messages = [r.getMessage() for r in warnings]
    assert any("projects" in m and "failed" in m for m in messages), (
        f"the failing builder was not logged at WARNING: {messages!r}")
    assert "projects_ms" not in spans, f"a failed key reported a span: {spans!r}"
    assert _families() == {"runs", "repos"}, (
        f"the keys behind the failure were not built: {_warm_keys()!r}")
    for key in _warm_keys():
        rc.cached(key, _must_not_build(key))   # raises if a sibling stopped too


def test_a_composed_app_is_not_warmed(client, caplog):
    """With dependency overrides installed the entry point warms NOTHING.

    The reason the other tests lift the table instead of trusting a helper: a
    warm-up built from the singletons while the routes answer from overrides would
    warm a value no request reads, which is worse than not warming at all.
    """
    caplog.set_level(logging.INFO, logger="api.main")
    assert rc._store == {}
    assert api_main.warm_first_dashboard_read() is None
    assert rc._store == {}, (
        f"a composed app was warmed anyway: {_warm_keys()!r}")
    assert any("dependency overrides" in m.getMessage() for m in caplog.records), (
        "the skip was silent; it should say why")
