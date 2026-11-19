"""A burst of distinct cache keys must not cost an entry it never touched.

`api/_read_cache.py` bounds its lock table and, on the way past that bound, used
to do `_locks.clear(); _store.clear(); _generation += 1`. So ONE caller minting
enough distinct keys destroyed every other key's value in the process, and the
next read of each of those keys was a COLD inline rebuild — for the dashboard
keys (`("projects", owner)`, `("repos", None)`, `("runs", owner, include_all)`)
that is ~1.35s of per-project enrichment on the request thread of the one
uvicorn process that also drives live runs.

Measured by the director on the base commit f9b9609ccc (probe
reports/mainred-1118/probe_keycap.py; 50ms stand-in for the enrichment pass):

    mints=500  locks=501 store=501  builds_after_mints=0 read_after_mints=0.0ms  entry_survived=True
    mints=511  locks=512 store=512  builds_after_mints=0 read_after_mints=0.0ms  entry_survived=True
    mints=512  locks=  1 store=  1  builds_after_mints=1 read_after_mints=50.1ms entry_survived=False
    mints=600  locks= 89 store= 89  builds_after_mints=1 read_after_mints=50.1ms entry_survived=False

Two things were wrong and this file witnesses both separately:

* **the store must survive the lock table's reset.** `_locks` is a table of
  in-flight flags and can be dropped like a hat; `_store` holds answers. This is
  what `test_the_lock_table_resetting_does_not_drop_the_store` and
  `test_a_rebuild_running_across_the_reset_still_publishes` isolate — they lower
  `_MAX_KEYS` so the reset really fires instead of trusting a constant's size.
* **the store needs a bound of its own, applied one entry at a time.** A cap
  that is met by emptying the table is the same defect wearing a different
  constant. `test_eviction_is_one_entry_at_a_time_…` lowers `_MAX_ENTRIES` to
  make the eviction observable; `test_five_thousand_distinct_keys_…` checks that
  the real bound holds and that it is met by *filling*, not by wiping.

`clear()` still has to mean INVALIDATE after all of this — a fix for a flood
that bought its quiet by making writes cheap-to-ignore would be a different bug
— so the last test re-checks the write path on top of a flood.

Every test drives `cached()`; the internals touched are `_store` / `_locks` (to
inject an entry and to show what a burst left behind) and the two bound
constants (to make the mechanisms fire at a size a unit test can afford). The
cache is process-global, so each test starts from `reset()`.
"""
from __future__ import annotations

import threading
import time

import pytest

from api import _read_cache as rc

# The key the dashboard actually polls: api/main.py's startup prefetch and
# api/run_routers.py build ("runs", owner, include_all) for an anonymous caller.
KEY = ("runs", None, False)
REFRESH_THREAD = "read-cache-refresh"


def _mint(count: int, start: int = 0) -> None:
    """Mint `count` distinct keys, the way a caller-chosen filter would."""
    for i in range(start, start + count):
        rc.cached(("minted", i), lambda i=i: i)


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _refreshing():
    return [thread for thread in threading.enumerate()
            if thread.name == REFRESH_THREAD]


def _must_not_build(message):
    raise AssertionError(message)


def _stored(key):
    entry = rc._store.get(key)
    return entry[1] if entry else None


class TestABurstOfDistinctKeys:
    """The defect as it was measured: fresh before the burst, cold after it."""

    @pytest.mark.parametrize("burst", [512, 600])
    def test_a_fresh_entry_is_still_fresh_after_the_burst(self, burst):
        """Both burst sizes from the measurement. A fresh entry must come out of
        the burst with its stored value, an unchanged build counter, no rebuild
        scheduled and no generation movement — i.e. the read after the burst is
        the 0.0ms one, not a 50ms (or 1.35s) rebuild."""
        rc.reset()
        builds = []

        def build():
            builds.append(1)
            return "the-dashboard-answer"

        assert rc.cached(KEY, build) == "the-dashboard-answer"
        assert builds == [1]
        entry = rc._store[KEY]
        generation = rc._generation

        _mint(burst)

        assert _stored(KEY) == "the-dashboard-answer", (
            f"{burst} distinct keys cost this entry its value")
        assert rc.cached(KEY, build) == "the-dashboard-answer", (
            "the read after the burst did not return the stored value")
        time.sleep(0.05)   # long enough for a background rebuild to have run
        assert builds == [1], (
            f"{burst} distinct keys made an untouched key rebuild "
            f"{len(builds) - 1} extra time(s) — a cold inline rebuild or a "
            "scheduled background one, which is the whole defect")
        assert _refreshing() == [], "a background rebuild was scheduled"
        assert rc._store[KEY] is entry, (
            "the entry was rewritten by the burst, so this is not the value "
            "that was fresh before it")
        assert rc._generation == generation, (
            "the burst moved the generation, which makes every entry stale and "
            "every write-adjacent read pay a rebuild")

    def test_the_lock_table_resetting_does_not_drop_the_store(self, monkeypatch):
        """The mechanism, at a size that forces it.

        `_MAX_KEYS` is lowered to 8 so 24 distinct keys reset the lock table
        three times: the fix does not depend on the real constant being too
        large for the burst to reach. What must survive each reset is the store
        and the generation — the values are not the lock table's to throw away.
        """
        monkeypatch.setattr(rc, "_MAX_KEYS", 8)
        rc.reset()
        assert rc.cached(KEY, lambda: "value") == "value"
        entry = rc._store[KEY]
        generation = rc._generation

        _mint(24)

        assert len(rc._locks) <= 8, (
            f"the lock table grew past its bound: {len(rc._locks)}")
        assert _stored(KEY) == "value", "the lock table's reset dropped the store"
        assert rc._store[KEY] is entry, "the store's entry was rewritten"
        assert rc._generation == generation, (
            "the lock-table reset bumped the generation: every entry just became "
            "stale, and every rebuild in flight was made unpublishable")
        assert rc.cached(KEY, lambda: _must_not_build(
            "a reset of the lock table rebuilt an untouched key")) == "value"

    def test_a_rebuild_running_across_the_reset_still_publishes(self, monkeypatch):
        """A rebuild in flight is work somebody already paid for.

        The old reset bumped the generation, which is the guard `_publish` uses
        to refuse a rebuild that began before a WRITE — so a full lock table
        silently discarded a valid rebuild as if a write had landed. With the
        store no longer dropped there is nothing to refuse, and the value the
        rebuild computes must land.
        """
        monkeypatch.setattr(rc, "_MAX_KEYS", 8)
        rc.reset()
        rc._store[KEY] = (time.monotonic() - rc.DEFAULT_TTL - 1, "old",
                          rc._generation)
        entered = threading.Event()
        release = threading.Event()

        def build():
            entered.set()
            release.wait(5)
            return "new"

        assert rc.cached(KEY, build) == "old", (
            "the expired entry was rebuilt inline instead of served")
        assert entered.wait(5), "no background rebuild was started"

        _mint(24)                       # forces the lock table's reset
        release.set()

        assert _wait_until(lambda: _stored(KEY) == "new"), (
            "a rebuild that was in flight when the lock table reset was "
            f"discarded; the store holds {_stored(KEY)!r}")


class TestTheResidentSetIsBounded:
    """The other half: a bound that is honoured without being a wipe."""

    def test_the_bounds_are_named_module_constants(self):
        """Whichever number the bound is, it is a name in the module — not a
        literal in the middle of an algorithm, and not smaller than the burst
        the issue measures."""
        for name in ("_MAX_KEYS", "_MAX_ENTRIES"):
            assert hasattr(rc, name), f"api/_read_cache.py declares no {name}"
            bound = getattr(rc, name)
            assert isinstance(bound, int) and bound > 0, f"{name}={bound!r}"
        assert rc._MAX_ENTRIES <= rc._MAX_KEYS, (
            f"the store's bound ({rc._MAX_ENTRIES}) is larger than the lock "
            f"table's ({rc._MAX_KEYS}); len(_store) <= _MAX_KEYS is pinned "
            "outside this module")
        assert rc._MAX_KEYS > 600, (
            f"the bound is {rc._MAX_KEYS}: at 512 or below, a several-hundred-"
            "key burst would evict exactly the entries it was minted alongside")

    def test_five_thousand_distinct_keys_stay_under_both_bounds(self):
        """5000 keys is ten times the burst that used to wipe the store. The
        store must be held AT its bound (bounded by filling, not by emptying)
        and the lock table under its own."""
        rc.reset()
        _mint(5000)
        assert len(rc._store) == rc._MAX_ENTRIES, (
            f"the store holds {len(rc._store)} entries against a bound of "
            f"{rc._MAX_ENTRIES} — over it, or emptied rather than held there")
        assert len(rc._locks) <= rc._MAX_KEYS, (
            f"the lock table holds {len(rc._locks)} entries against a bound of "
            f"{rc._MAX_KEYS}")

    def test_eviction_is_one_entry_at_a_time_and_prefers_the_invalidated_one(
            self, monkeypatch):
        """`_MAX_ENTRIES` lowered to 8 so the eviction is observable: the bound
        is met by dropping ONE entry, and the entry it drops is an
        already-invalidated one (its generation is old, so it was going to be
        rebuilt on the next read anyway) rather than a current value."""
        monkeypatch.setattr(rc, "_MAX_ENTRIES", 8)
        rc.reset()
        stale = (time.monotonic() - rc.DEFAULT_TTL - 1, "stale", rc._generation - 1)
        fresh = (time.monotonic(), "fresh", rc._generation)
        rc._store[KEY] = stale
        rc._store[("pinned",)] = fresh

        _mint(6)                        # 2 + 6 == the bound: nothing evicted yet
        assert len(rc._store) == 8, "the store went over its bound"
        assert KEY in rc._store and ("pinned",) in rc._store

        _mint(1, start=6)               # 9 entries: exactly one must go
        assert len(rc._store) == 8, (
            f"the store holds {len(rc._store)} entries against a bound of 8 — "
            "the bound must be met one entry at a time, not by emptying it")
        assert KEY not in rc._store, (
            "the entry with an OLD generation (already invalidated by a write) "
            "was kept while a current value was evicted")
        assert ("pinned",) in rc._store, (
            "a current-generation entry was evicted before the invalidated one")


class TestInvalidationStillMeansInvalidation:
    def test_clear_after_a_burst_still_serves_stale_and_rebuilds_behind_it(self):
        """A fix for the flood must not be bought by making `clear()` cheaper to
        ignore: after a burst, the middleware's invalidation still marks the
        entry stale, the read that follows is served at once, and exactly one
        background rebuild lands the new value."""
        rc.reset()
        built_on = []
        assert rc.cached(
            KEY, lambda: built_on.append(threading.current_thread().name) or "v1") == "v1"
        _mint(600)
        rc.clear()                      # a mutating request went through

        rebuilds = []
        started_at = time.monotonic()
        value = rc.cached(
            KEY,
            lambda: rebuilds.append(threading.current_thread().name) or "v2")
        elapsed = time.monotonic() - started_at

        assert value == "v1", (
            "the read after clear() did not serve the previously built value")
        assert elapsed < 0.2, f"the caller waited {elapsed:.2f}s for the rebuild"
        assert threading.main_thread().name not in rebuilds, (
            "the calling thread ran the builder after a write")
        assert _wait_until(lambda: _stored(KEY) == "v2"), (
            "the write invalidated the entry and no rebuild republished it")
        assert rebuilds == [REFRESH_THREAD]
        assert built_on == [threading.main_thread().name]
