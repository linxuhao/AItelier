# core/model_routes.py
"""Internal model names → an ordered list of concrete `provider/model` candidates.

An agent_config names an INTERNAL model ("flash"); this table says which real
endpoints can serve it, in preference order. Nothing below this layer knows an
internal name exists: `AIGateway` resolves it at construction and every
downstream quirk (the DSML content-leak parser, Anthropic `cache_control`, the
DeepSeek `reasoning_effort`-via-extra_body workaround) keys off the CONCRETE
provider exactly as before.

The order is a preference, not a rotation. See `AIGateway._failover` for why
round-robin is not offered: provider prefix caches are per-provider, and this
workload is 26:1 prefill:decode with an 89.4% hit rate (measured over 464M
input tokens). Splitting turns across providers turns cached input into
full-price input, which costs far more than any per-token plan discount saves.

Unknown names pass through unchanged, so a config naming a concrete
`provider/model` keeps working with no table entry.
"""

from __future__ import annotations

import json
import threading
import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def config_or_example(name: str) -> str:
    """`<name>` if the deployment has one, else the committed `<name>.example`.

    The provider half of this system is DEPLOYMENT config, not repo content:
    which vendors you hold accounts with, what their endpoints are, and which
    of them can serve each internal model. Committing one operator's answer
    makes the repo look like it only runs on those vendors, and quietly rots
    when they change. So `llm_providers.json` and `model_routes.json` are
    gitignored like `.env`, and what ships is `<name>.example.json`.

    The example is a working fallback rather than a template you must copy,
    because "a clean checkout must be able to start" is a tested contract
    (tests/unit/test_clean_checkout_starts.py) and breaking it to make a point
    about configurability would be a bad trade. It names public vendor
    endpoints only — no self-hosted address of anyone's — so a fresh checkout
    runs as soon as it has a key, and an operator who wants different providers
    copies the example and edits it.

    Absolute, not CWD-relative: agent_configs carry INTERNAL model names now, so
    a table the process cannot find is not a silent degradation any more — it
    is every agent failing at once.
    """
    real = _REPO_ROOT / name
    if real.is_file():
        return str(real)
    return str(_REPO_ROOT / f"{Path(name).stem}.example{Path(name).suffix}")


def default_routes_file() -> str:
    """Resolved on every call, never captured at import.

    It was a module constant, and that froze the answer at import time: on a
    checkout holding only the example, the first API write correctly created the
    real `model_routes.json` and `reset_cache()` correctly dropped the cache —
    but `get_routes(None)` still keyed on the example path, re-read the example,
    and the newly added model did not exist. `add_model` returned success and
    `AIGateway("newmodel")` raised "is neither a 'provider/model' nor a route"
    until the container was recreated. A write that reports success and changes
    nothing is the worst of the three possible outcomes.
    """
    return config_or_example("model_routes.json")


class ModelRoutes:
    """Loads `model_routes.json`; resolves an internal name to candidates."""

    def __init__(self, path: str | os.PathLike | None = None):
        self._path = Path(path or default_routes_file())
        self._routes: dict[str, list[str]] = {}
        self._rot_n: dict[str, int] = {}    # route -> size of its rotate pool
        self._load()

    def _load(self) -> None:
        if not self._path.is_file():
            return  # no table = every name is concrete; failover is off
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            # A malformed table must not silently disable routing for every
            # agent — that would look like "failover isn't working" with no
            # trace of why.
            raise RuntimeError(f"{self._path}: could not be parsed: {e}") from e
        if not isinstance(raw, dict):
            raise RuntimeError(f"{self._path}: expected an object at the top level")
        for name, candidates in raw.items():
            if name.startswith("_"):
                continue  # "_comment" and friends
            if isinstance(candidates, str):
                candidates = [candidates]
            if isinstance(candidates, dict):
                # Rotation form: {"rotate": [...], "fallback": [...]}. The
                # rotate pool spreads SUBSCRIPTION quota (each plan has its own
                # 5h/weekly window; sticky-first-candidate leaves every other
                # plan's window to expire unused). Fallback stays ordered and
                # LAST — pay-as-you-go money is only spent when every plan in
                # the pool has failed or is parked. Rotation happens per
                # resolve(rotate=True) call — one gateway per STEP — which is
                # the cache-safe granularity: the 26:1 prefill:decode economy
                # lives INSIDE a step's tool loop (the transcript replayed every
                # turn); across steps only the system prompt is shared.
                pool = candidates.get("rotate")
                tail = candidates.get("fallback", [])
                unknown = set(candidates) - {"rotate", "fallback"}
                if unknown:
                    raise RuntimeError(
                        f"{self._path}: route '{name}' has unknown key(s) "
                        f"{sorted(unknown)} — only 'rotate' and 'fallback'")
                if (not isinstance(pool, list) or not pool
                        or not isinstance(tail, list)):
                    raise RuntimeError(
                        f"{self._path}: route '{name}': 'rotate' must be a "
                        f"non-empty list and 'fallback' a list")
                self._rot_n[name] = len(pool)
                candidates = list(pool) + list(tail)
            if not isinstance(candidates, list) or not candidates:
                raise RuntimeError(
                    f"{self._path}: route '{name}' must be a non-empty list")
            for c in candidates:
                if not isinstance(c, str) or "/" not in c:
                    raise RuntimeError(
                        f"{self._path}: route '{name}' candidate {c!r} must be "
                        f"'provider/model'")
                if c.split("/", 1)[0] in raw:
                    # One level only. A route pointing at a route would make
                    # resolution order depend on dict iteration order.
                    raise RuntimeError(
                        f"{self._path}: route '{name}' candidate {c!r} names "
                        f"another route; candidates must be concrete")
            self._routes[name] = list(candidates)

    def resolve(self, model_name: str, rotate: bool = False) -> list[str]:
        """Candidates for `model_name`, best first.

        A concrete `provider/model` passes through as its own single candidate.
        A BARE name that is not a route is an error, not a passthrough: now that
        agent_configs carry internal names, a typo ("flsh") would otherwise sail
        through to litellm and come back as "LLM Provider NOT provided", which
        names neither the role nor the table it should have been added to.

        `rotate=True` (the gateway — i.e. once per STEP) advances this route's
        rotation pool by one so consecutive steps start on different plans;
        the pool keeps its relative order for failover and the fallback tail is
        never rotated into the head. Default False so every other reader
        (external_deps key derivation, the vision judge panel) stays
        deterministic.
        """
        if model_name in self._routes:
            lst = self._routes[model_name]
            n = self._rot_n.get(model_name, 0)
            if rotate and n > 1:
                with _rot_lock:
                    k = _rot_counters[model_name] = (
                        _rot_counters.get(model_name, -1) + 1) % n
                return lst[k:n] + lst[:k] + lst[n:]
            return list(lst)
        if "/" in model_name:
            return [model_name]
        known = ", ".join(self.names()) or "(none)"
        raise RuntimeError(
            f"model '{model_name}' is neither a 'provider/model' nor a route in "
            f"{self._path}. Known routes: {known}. Add it there, or use a "
            f"concrete provider/model.")

    def names(self) -> list[str]:
        return sorted(self._routes)


# Per-route rotation counters. In-process on purpose: a restart resetting the
# rotation to the first plan costs nothing, and persisting a fairness counter
# would be more machinery than the fairness is worth.
_rot_counters: dict[str, int] = {}
_rot_lock = threading.Lock()

_CACHE: dict[str, ModelRoutes] = {}


def get_routes(path: str | os.PathLike | None = None) -> ModelRoutes:
    """Process-wide cached table (re-read only when the path differs)."""
    key = str(path or default_routes_file())
    if key not in _CACHE:
        _CACHE[key] = ModelRoutes(key)
    return _CACHE[key]


def reset_cache() -> None:
    """Test hook: drop the cached tables so a new file is picked up."""
    _CACHE.clear()
