"""An expired read cache entry must be served, not waited for.

`GET /api/runs` took 18.30s every time its entry had expired and 0.065s on the
cached repeats (measured on the live backend, process started_at
2026-11-18T13:47:36Z). The SPA polls it every 10s and `DEFAULT_TTL` is 10s, so
almost every poll paid the rebuild — and the rebuild is ~1545 projects of
per-project enrichment (11-16s), not the DB query (`list_projects_with_stats`
returned 1545 rows in 0.10s) and not the YAML registry (0.00ms once warm).

So this file pins the three states apart, because the cache has to behave
differently in each and the difference is the fix:

* ABSENT (a cold miss, and the first read after `clear()`) is computed INLINE
  and the other callers wait for it — `test_a_cold_miss_is_computed_once_and_
  other_callers_wait` and `test_clear_makes_the_next_read_a_cold_miss`. This is
  the anti-stampede property the module exists for; making it asynchronous would
  turn one reboot into N concurrent 1545-project rebuilds.
* EXPIRED is returned to the caller IMMEDIATELY while exactly one background
  rebuild runs — `test_an_expired_entry_is_served_without_waiting_for_the_
  builder`, `test_two_concurrent_expired_reads_start_exactly_one_rebuild`,
  `test_the_fresh_value_appears_after_the_rebuild_finishes`.
* EXPIRED and the rebuild FAILS keeps the previous value and raises nothing —
  `test_a_failing_rebuild_keeps_the_old_value_and_does_not_raise`.

`test_a_rebuild_running_across_a_write_does_not_republish_the_old_bytes` covers
the one thing the background rebuild can break that the synchronous one could
not: a rebuild that began before a write landed holding pre-write data, and the
next read after `clear()` returning it as if it were fresh.

Every test drives `cached()` and asserts the RESULT; the only internals touched
are the entry's timestamp (written, not slept, so the cache's own clock is the
one under test) and `_store` reads to show a value was published or not.
"""
from __future__ import annotations

import logging
import threading
import time

from api import _read_cache as rc

KEY = ("runs", None)
REFRESH_THREAD = "read-cache-refresh"


def _expire(key, value, ttl=rc.DEFAULT_TTL):
    """Put `value` in the store already older than `ttl`."""
    rc._store[key] = (time.monotonic() - ttl - 1, value)


def _called(message):
    """A builder that must never run — it is only handed to a call that has to
    come out of the store."""
    raise AssertionError(message)


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _wait_for_refreshes(timeout=5.0):
    deadline = time.monotonic() + timeout
    for thread in threading.enumerate():
        if thread.name == REFRESH_THREAD:
            thread.join(max(0.0, deadline - time.monotonic()))


def test_a_cold_miss_is_computed_once_and_other_callers_wait():
    """The property the module was written for, and the one the fix must not
    spend: with no entry at all there is nothing to serve, so the first caller
    builds and the others WAIT for that one build rather than starting their
    own. At 11-16s per rebuild, a cold process serving k tabs this way would be
    k rebuilds of 1545 projects on one core."""
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def slow_build():
        calls.append(1)
        entered.set()
        release.wait(5)
        return "cold"

    first = []
    second = []
    first_thread = threading.Thread(target=lambda: first.append(rc.cached(KEY, slow_build)))
    first_thread.start()
    assert entered.wait(5), "the cold miss never reached the builder"

    second_thread = threading.Thread(target=lambda: second.append(rc.cached(KEY, slow_build)))
    second_thread.start()
    time.sleep(0.05)
    assert second == [], (
        "a caller arriving during the COLD computation returned before it "
        "finished — the anti-stampede property is gone")

    release.set()
    first_thread.join(5)
    second_thread.join(5)
    assert first == ["cold"] and second == ["cold"]
    assert calls == [1], f"{len(calls)} cold builds for one key"


def test_an_expired_entry_is_served_without_waiting_for_the_builder():
    """The 18.30s -> 0.065s case: the entry we have is too old to be called
    fresh and is still far better than making this request pay for the rebuild."""
    _expire(KEY, "old")
    built = threading.Event()

    def slow_build():
        time.sleep(0.4)
        built.set()
        return "new"

    started_at = time.monotonic()
    value = rc.cached(KEY, slow_build)
    elapsed = time.monotonic() - started_at

    assert value == "old", (
        "an expired entry was rebuilt INLINE — the caller paid for the rebuild")
    assert elapsed < 0.2, f"the caller waited {elapsed:.2f}s for the rebuild"
    assert built.wait(5), "no background rebuild was started for the stale value"
    assert _wait_until(lambda: rc._store.get(KEY, (0.0, None))[1] == "new"), (
        "the background rebuild never published its value")


def test_two_concurrent_expired_reads_start_exactly_one_rebuild():
    """Serving stale must not become the new stampede: both readers get the old
    value, and the key rebuilds once, not once per reader."""
    _expire(KEY, "old")
    calls = []
    entered = threading.Event()
    release = threading.Event()
    barrier = threading.Barrier(3)

    def slow_build():
        calls.append(1)
        entered.set()
        release.wait(2)
        return "new"

    a, b = [], []

    def read(out):
        barrier.wait(5)
        out.append(rc.cached(KEY, slow_build))

    first = threading.Thread(target=read, args=(a,))
    second = threading.Thread(target=read, args=(b,))
    first.start()
    second.start()
    barrier.wait(5)
    first.join(5)
    second.join(5)

    assert entered.wait(5), "no rebuild was started at all"
    assert a == ["old"] and b == ["old"], (
        f"an expired read did not return the stale value: {a} {b}")

    # A third read while that rebuild is still running must also get the stale
    # value and must start nothing of its own.
    extra = []
    assert rc.cached(KEY, lambda: extra.append(1) or "nope") == "old"
    time.sleep(0.05)
    assert extra == [] and calls == [1], (
        f"{len(calls) + len(extra)} rebuilds for one key while one was running")

    release.set()
    assert _wait_until(lambda: rc._store.get(KEY, (0.0, None))[1] == "new"), (
        "the one rebuild never published its value")


def test_the_fresh_value_appears_after_the_rebuild_finishes():
    """Staleness is bounded by one rebuild, not permanent: the next read after
    the rebuild lands must be the new value and must not build again."""
    _expire(KEY, "old")
    built = []

    def build():
        built.append(1)
        return "new"

    assert rc.cached(KEY, build) == "old", (
        "the first read rebuilt instead of serving the expired entry")
    assert _wait_until(lambda: rc._store.get(KEY, (0.0, None))[1] == "new"), (
        "the rebuilt value never appeared")
    assert rc.cached(KEY, lambda: _called("rebuilt a fresh entry")) == "new"
    assert built == [1]


def test_a_failing_rebuild_keeps_the_old_value_and_does_not_raise(caplog):
    """The dashboard's readers must not inherit the rebuild's exception, and a
    failed rebuild must not cost the entry — otherwise one bad enrichment makes
    every poll a cold miss (and therefore a synchronous 11-16s rebuild)."""
    _expire(KEY, "old")
    attempts = []

    def exploding():
        attempts.append(1)
        raise RuntimeError("the per-project enrichment blew up")

    with caplog.at_level(logging.WARNING, logger="api._read_cache"):
        assert rc.cached(KEY, exploding) == "old", (
            "the rebuild's exception reached the caller")
        assert _wait_until(lambda: attempts == [1]), "no rebuild was attempted"
        assert _wait_until(lambda: any(
            "read cache" in record.getMessage() and record.levelno >= logging.WARNING
            for record in caplog.records)), "the failed rebuild was not logged"

    assert rc._store[KEY][1] == "old", "a failed rebuild dropped the entry"
    # The lock was released, so the key is not stuck: the next expired read
    # tries again, and this time the new value lands.
    assert rc.cached(KEY, lambda: "new") == "old"
    assert _wait_until(lambda: rc._store.get(KEY, (0.0, None))[1] == "new"), (
        "a rebuild that failed once left the key unable to rebuild again")


def test_clear_makes_the_next_read_a_cold_miss():
    """Write-invalidation is unchanged: the middleware's `clear()` must take the
    synchronous path, not serve the entry it just invalidated."""
    calls = []
    assert rc.cached(KEY, lambda: calls.append("cold") or "v1") == "v1"
    rc.clear()
    assert rc.cached(KEY, lambda: calls.append("after-clear") or "v2") == "v2"
    assert calls == ["cold", "after-clear"]
    assert rc._store[KEY][1] == "v2"


def test_a_rebuild_running_across_a_write_does_not_republish_the_old_bytes():
    """A synchronous rebuild could not outlive the request that started it; a
    background one can. Without that guard, a write that lands mid-rebuild is
    immediately undone: the rebuild stores what it computed from the pre-write
    state, with a fresh timestamp, and every read for the next TTL serves it."""
    _expire(KEY, "old")
    entered = threading.Event()
    release = threading.Event()

    def pre_write_build():
        entered.set()
        release.wait(2)
        return "PRE-WRITE"

    assert rc.cached(KEY, pre_write_build) == "old"
    assert entered.wait(5), "no background rebuild was started"
    rc.clear()          # a mutating request went through the middleware
    release.set()
    _wait_for_refreshes()

    assert KEY not in rc._store, (
        "a rebuild that began before the write republished the pre-write value")
    assert rc.cached(KEY, lambda: "post-write") == "post-write"
