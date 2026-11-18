"""An expired read cache entry must be served, not waited for.

`GET /api/runs` took 18.30s every time its entry had expired and 0.065s on the
cached repeats (measured on the live backend, process started_at
2026-11-18T13:47:36Z). The SPA polls it every 10s and `DEFAULT_TTL` is 10s, so
almost every poll paid the rebuild — and the rebuild is ~1545 projects of
per-project enrichment (11-16s), not the DB query (`list_projects_with_stats`
returned 1545 rows in 0.10s) and not the YAML registry (0.00ms once warm).

So this file pins the three states apart, because the cache has to behave
differently in each and the difference is the fix:

* ABSENT (a key this process has never built) is computed INLINE and the other
  callers wait for it — `test_a_cold_miss_is_computed_once_and_other_callers_
  wait`. This is the anti-stampede property the module exists for; making it
  asynchronous would turn one reboot into N concurrent 1545-project rebuilds.
* EXPIRED is returned to the caller IMMEDIATELY while exactly one background
  rebuild runs — `test_an_expired_entry_is_served_without_waiting_for_the_
  builder`, `test_two_concurrent_expired_reads_start_exactly_one_rebuild`,
  `test_the_fresh_value_appears_after_the_rebuild_finishes`.
* INVALIDATED (`clear()` after a mutating request) is the same shape as
  EXPIRED: the previous value is served at once and one rebuild runs —
  `test_clear_does_not_serve_pre_write_bytes_as_fresh_and_lands_within_one_
  rebuild`. It is NOT a cold miss any more: the write no longer makes its own
  reads pay for the rebuild (measured 10.93 / 8.87s warm and 21.26 / 22.76 /
  28.13s expired, against 0.083-0.128s for an untouched poll).
* EXPIRED and the rebuild FAILS keeps the previous value and raises nothing —
  `test_a_failing_rebuild_keeps_the_old_value_and_does_not_raise`.

`test_a_rebuild_running_across_a_write_does_not_republish_the_old_bytes` covers
the one thing the background rebuild can break that the synchronous one could
not: a rebuild that began before a write landed holding pre-write data, and the
next read after `clear()` returning it as if it were fresh. The generation guard
is what refuses that publish, and it has to survive `clear()` becoming a
"serve the previous bytes" operation rather than a drop.

Every test drives `cached()` and asserts the RESULT; the only internals touched
are the entry `_store` holds (written, not slept, so the cache's own clock is the
one under test) and `_store` reads to show a value was published or not. Each
test starts by dropping the store outright (`reset()`, not `clear()`): `clear()`
now leaves the previous value in place on purpose, so it is no longer a way to
make a key ABSENT.
"""
from __future__ import annotations

import logging
import threading
import time

from api import _read_cache as rc

KEY = ("runs", None)
REFRESH_THREAD = "read-cache-refresh"


def _expire(key, value, ttl=rc.DEFAULT_TTL):
    """Put `value` in the store already older than `ttl`, in the live generation."""
    rc._store[key] = (time.monotonic() - ttl - 1, value, rc._generation)


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
    rc.reset()
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
    rc.reset()
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
    rc.reset()
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
    rc.reset()
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
    rc.reset()
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


def test_clear_does_not_serve_pre_write_bytes_as_fresh_and_lands_within_one_rebuild():
    """`clear()` runs after EVERY non-GET request (api/main.py:442), so what it
    means decides what a write costs the reads behind it.

    It used to drop the store: the next read was a cold miss computed inline,
    measured at 10.93 / 8.87s with the enrichment memo warm and 21.26 / 22.76 /
    28.13s with it expired, against 0.083-0.128s for an untouched poll. Now it
    invalidates: the previous value is served on the calling thread at once, one
    background rebuild runs, and the read after that rebuild lands is the new
    value. What must NOT happen is the invalidated bytes being treated as if
    nothing had changed — the rebuild is the evidence that they are stale.
    """
    rc.reset()
    calls = []
    assert rc.cached(KEY, lambda: calls.append("cold") or "v1") == "v1"
    assert rc._store[KEY][1] == "v1"

    rc.clear()          # a mutating request went through the middleware

    rebuilds = []

    def build_after_the_write():
        rebuilds.append(threading.current_thread().name)
        return "v2"

    started_at = time.monotonic()
    value = rc.cached(KEY, build_after_the_write)
    elapsed = time.monotonic() - started_at

    assert value == "v1", (
        "the read after clear() did not serve the previously built value")
    assert elapsed < 0.2, f"the caller waited {elapsed:.2f}s for the rebuild"
    assert threading.main_thread().name not in rebuilds, (
        "the read after clear() re-entered the builder on the calling thread — "
        "that is the cold-miss path a write must not force any more")
    assert _wait_until(lambda: rc._store.get(KEY, (0.0, None, -1))[1] == "v2"), (
        "the write invalidated the entry but no rebuild republished it — the "
        "dashboard would serve pre-write bytes for ever")
    assert rebuilds == [REFRESH_THREAD], (
        f"the rebuild ran on {rebuilds}, not on exactly one background thread")
    assert rc.cached(KEY, lambda: _called("rebuilt a freshly published entry")) == "v2"


def test_a_rebuild_running_across_a_write_does_not_republish_the_old_bytes():
    """A synchronous rebuild could not outlive the request that started it; a
    background one can. `clear()` now KEEPS the previous value so the next read
    does not pay for a rebuild, which makes the generation guard the only thing
    standing between a write and its own invalidation: a rebuild that began
    before the write holds pre-write data, and publishing it as fresh would
    resurrect exactly what the write invalidated.

    So the discarded rebuild leaves the previous (stale) value in place, the
    write's own rebuild is the one that lands, and its answer is what the reads
    after it get."""
    rc.reset()
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

    assert rc._store[KEY][1] == "old", (
        "a rebuild that began before the write republished its pre-write bytes")
    after = []

    def post_write_build():
        after.append(threading.current_thread().name)
        return "post-write"

    assert rc.cached(KEY, post_write_build) == "old", (
        "the discarded rebuild's bytes, or a cold inline build, reached a reader")
    assert _wait_until(lambda: rc._store.get(KEY, (0.0, None, -1))[1] == "post-write"), (
        "the write's own rebuild never published")
    assert rc.cached(KEY, lambda: _called("rebuilt after the write landed")) == "post-write"
    assert after == [REFRESH_THREAD]
