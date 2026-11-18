"""A few seconds of memory for the two reads that everybody makes at once.

The dashboard polls `/api/repos` and `/api/runs` every 10 seconds. Measured on
the live deployment those two cost ~1.13s of CPU together, and this backend is
one uvicorn process — one interpreter, one GIL — so the work does not spread
across the box's other fifteen idle cores. Twelve parallel `/api/repos` calls
returned in 6.53s, all of them between 6.33s and 6.52s: perfect serialization.
That puts the saturation point at about 10s / 1.13s ≈ 9 simultaneous visitors,
after which the core is fully consumed by polling and there is nothing left for
the scheduler that is driving live pipeline runs in the same process.

The response is the same bytes for every anonymous viewer, and it was recomputed
per tab per tick. Caching it for a few seconds does not make any single request
faster; it makes the Nth concurrent request free, which is the only number that
was going to hurt.

Three properties this has to get right:

**The key carries the identity.** These endpoints filter by owner. A cache keyed
only on the path would serve one user's filtered list to another — turning a
performance fix into a disclosure. Callers pass the owner filter as part of the
key; on the current deployment it is always None, and the key still says so.

**One computation per miss, not N.** A plain check-then-compute lets every
request that arrives during a cold window start its own computation, which is
precisely the stampede this exists to prevent — worst at exactly the moment it
matters, after a restart, when every waiting tab polls a cold process at once
(measured: 15.6s for the first `/api/projects` against a cold page cache). So a
miss takes a per-key lock and the others wait for its result. That is the COLD
path and it is still synchronous on purpose: there is nothing to serve while the
process has never built the answer at all.

**An EXPIRED entry is not a reason to wait.** It used to be: the caller that
found the entry too old rebuilt it under the lock and every poll arriving during
that rebuild queued behind it. Measured on the live backend, `GET /api/runs`
took 18.30s each time its entry had expired and 0.065s on the cached repeats —
against a SPA that polls every 10s and a `DEFAULT_TTL` of 10s, so nearly every
poll paid the rebuild. The rebuild is ~1545 projects of per-project enrichment
(11-16s), not the DB query (`list_projects_with_stats` returned 1545 rows in
0.10s) and not the YAML registry (0.00ms per call once warm). So an expired
entry is served to the caller immediately and a single background thread
rebuilds it: the value is at most one rebuild older, and the request that used
to pay 18s now pays microseconds. Exactly one rebuild runs per key at a time —
the per-key lock is what says so (a rebuild is in progress exactly when the lock
is held), so reads arriving during the rebuild get the stale value and start
nothing.

**A failing rebuild must not cost the entry or reach a caller.** It is logged
and dropped, the previous value keeps being served, and the next expired read
tries again. A rebuild also refuses to publish into a store that was dropped
while it ran: `clear()` exists for write-invalidation, and a rebuild that began
before the write landed holds pre-write data, so publishing it would resurrect
exactly what the write invalidated. `clear()` therefore bumps a generation, and
the next read after it is a COLD miss (see above).

Deliberately not a general-purpose cache: no eviction policy beyond the TTL, no
size bound, no invalidation hooks. It holds one entry per distinct query and
those are few. Reach for something real before using it for anything else.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Hashable

logger = logging.getLogger(__name__)

# Aligned with the SPA's poll interval (web/src/views/UnifiedDashboard.svelte:165
# — `setInterval(..., 10000)`), and that alignment is the whole point.
#
# Under continuous demand a key rebuilds once per TTL, so the FIXED cost of this
# cache is `keys / TTL * rebuild`. It does not depend on how many people are
# watching: two visitors and five hundred pay the same. At TTL 5 against a 10s
# poll that was two rebuilds per poll — measured ~550ms each across the three
# cached endpoints, so about a third of the single core, burned on recomputing
# an answer nobody had asked to change. Matching the TTL to the poll halves it.
#
# Longer would halve it again, and is the wrong trade: effective staleness is
# TTL + poll, so 10s already means a dashboard can show something 20s old, and
# this page's job is watching a live run. Writes do not wait for the TTL — the
# invalidation middleware drops the store on any mutating request — so the only
# thing that ages is out-of-band change, which is exactly the scheduler
# advancing a run, which is exactly what a 20s-old dashboard is still fine for.
#
# The real win left on the table is not a bigger number: all three cached
# endpoints rebuild the SAME underlying data (`list_projects_with_stats` plus
# `enrich_project_status` per project). Sharing that would cut the fixed cost by
# three rather than by two. That is a refactor, not a constant.
DEFAULT_TTL = 10.0

# Both dicts are keyed on values an ANONYMOUS caller picks: `/api/runs` takes
# free-form `config_name` and `status` query params, so `?config_name=zzz1`
# mints a fresh entry in each. `_store` at least turns over by TTL; `_locks` had
# no eviction at all and `clear()` never touched it, so ~180 bytes per distinct
# key accumulated forever on a hostname whose stated problem is that the callers
# are strangers. A cap is the honest fix: the keys are not ours to bound.
_MAX_KEYS = 512

_store: dict[Hashable, tuple[float, Any]] = {}
_locks: dict[Hashable, threading.Lock] = {}
_locks_guard = threading.Lock()

# Bumped by everything that DROPS the store (`clear()`, the key-cap reset). A
# background rebuild records the generation it started in and will not publish
# into a later one — see `_publish` and the docstring. There is deliberately no
# per-key structure for this: the lock table is already bounded, and a second
# table keyed on caller-supplied values would be an unbounded one.
_generation = 0


def _lock_for(key: Hashable) -> threading.Lock:
    global _generation
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            if len(_locks) >= _MAX_KEYS:
                # Drop everything rather than pretend to be an LRU. These are
                # 10-second entries; losing them costs one recomputation, and a
                # wrong eviction policy here would be more code than the cache.
                _locks.clear()
                _store.clear()
                _generation += 1
            lock = _locks[key] = threading.Lock()
        return lock


def _publish(key: Hashable, value: Any, generation: int) -> bool:
    """Store a rebuild's result unless the store was dropped while it ran."""
    with _locks_guard:
        if generation != _generation:
            return False
        _store[key] = (time.monotonic(), value)
        return True


def _rebuild(key: Hashable, build: Callable[[], Any], lock: threading.Lock,
             generation: int) -> None:
    """One background rebuild. Owns `lock` for its whole life — that IS the flag.

    A failure is logged and nothing else: the previous entry stays in `_store`,
    callers keep being served it, and the next expired read starts a new attempt.
    """
    try:
        try:
            value = build()
        except Exception:
            logger.warning(
                "read cache: background rebuild for %r failed; serving the "
                "previous value until a later rebuild succeeds", key,
                exc_info=True)
            return
        if not _publish(key, value, generation):
            logger.debug(
                "read cache: rebuild for %r finished after clear(); discarded", key)
    finally:
        lock.release()


def cached(key: Hashable, build: Callable[[], Any], ttl: float = DEFAULT_TTL) -> Any:
    """Return `build()`'s result, reusing one from the last `ttl` seconds.

    The returned object is SHARED between callers. Treat it as read-only —
    mutating it mutates what the next caller gets. Everything here is handed
    straight to the JSON serializer, which does not mutate.

    Three states, three behaviours: FRESH is returned; ABSENT (a cold miss, or
    the first read after `clear()`) is computed inline while other callers wait
    for it; EXPIRED is returned immediately while one background rebuild runs.
    A non-positive `ttl` means "do not reuse" — there is no window in which the
    value would be fresh, so serving it stale would be serving a value the
    caller declared unusable, and that takes the blocking path.
    """
    # Read BEFORE `hit`: anything that drops the store afterwards bumps the
    # generation, and a rebuild from this call must not publish into that.
    generation = _generation
    now = time.monotonic()
    hit = _store.get(key)
    if hit is not None and now - hit[0] < ttl:
        return hit[1]

    lock = _lock_for(key)
    if hit is not None and ttl > 0:
        # EXPIRED: the lock says whether a rebuild already owns this key. A
        # non-blocking acquire is the whole election — no second structure that
        # could disagree with the lock about whether a rebuild is running.
        if not lock.acquire(blocking=False):
            return hit[1]
        started = False
        try:
            threading.Thread(
                target=_rebuild, args=(key, build, lock, generation),
                name="read-cache-refresh", daemon=True,
            ).start()
            started = True
        finally:
            if not started:
                # No thread to be had (a process at its thread limit, say).
                # Fall through to the blocking path rather than never
                # refreshing again — that is the behaviour this cache had
                # before it served stale values.
                lock.release()
        if started:
            return hit[1]

    with lock:
        # Re-check under the lock: whoever held it may have just filled the
        # entry, and this is the whole point — one computation, N readers.
        hit = _store.get(key)
        now = time.monotonic()
        if hit is not None and now - hit[0] < ttl:
            return hit[1]
        value = build()
        _store[key] = (time.monotonic(), value)
        return value


def clear() -> None:
    """Drop everything. For tests, and for a caller that knows it just wrote."""
    global _generation
    with _locks_guard:
        _store.clear()
        # `_locks` too: it used to be left behind, which made "clear" not clear
        # and let the lock table grow without any bound at all.
        _locks.clear()
        # A rebuild in flight holds a lock that is no longer in the table, so it
        # cannot make the next read wait; this stops it from publishing what it
        # computed from the pre-write state.
        _generation += 1
