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

Two properties this has to get right:

**The key carries the identity.** These endpoints filter by owner. A cache keyed
only on the path would serve one user's filtered list to another — turning a
performance fix into a disclosure. Callers pass the owner filter as part of the
key; on the current deployment it is always None, and the key still says so.

**One computation per miss, not N.** A plain check-then-compute lets every
request that arrives during a cold window start its own computation, which is
precisely the stampede this exists to prevent — worst at exactly the moment it
matters, after a restart, when every waiting tab polls a cold process at once
(measured: 15.6s for the first `/api/projects` against a cold page cache). So a
miss takes a per-key lock and the others wait for its result.

Deliberately not a general-purpose cache: no eviction policy beyond the TTL, no
size bound, no invalidation hooks. It holds one entry per distinct query and
those are few. Reach for something real before using it for anything else.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Hashable

# Long enough that a burst of visitors collapses onto one computation, short
# enough that nobody notices: the SPA polls these every 10s, so a 5s entry is
# at most half a poll stale and usually less.
DEFAULT_TTL = 5.0

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


def _lock_for(key: Hashable) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            if len(_locks) >= _MAX_KEYS:
                # Drop everything rather than pretend to be an LRU. These are
                # 5-second entries; losing them costs one recomputation, and a
                # wrong eviction policy here would be more code than the cache.
                _locks.clear()
                _store.clear()
            lock = _locks[key] = threading.Lock()
        return lock


def cached(key: Hashable, build: Callable[[], Any], ttl: float = DEFAULT_TTL) -> Any:
    """Return `build()`'s result, reusing one from the last `ttl` seconds.

    The returned object is SHARED between callers. Treat it as read-only —
    mutating it mutates what the next caller gets. Everything here is handed
    straight to the JSON serializer, which does not mutate.
    """
    now = time.monotonic()
    hit = _store.get(key)
    if hit is not None and now - hit[0] < ttl:
        return hit[1]

    lock = _lock_for(key)
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
    _store.clear()
    # `_locks` too: it used to be left behind, which made "clear" not clear and
    # let the lock table grow without any bound at all.
    with _locks_guard:
        _locks.clear()
