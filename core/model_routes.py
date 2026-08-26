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
import os
from pathlib import Path

# Absolute, not CWD-relative. A miss used to degrade silently (a concrete
# `provider/model` still worked); now that agent_configs carry INTERNAL names, a
# miss makes `resolve("flash")` raise and every agent dies — including the
# `host`/`default` sentinel, whose target defaults to the bare "flash". Docker
# (WORKDIR /app) and root-run pytest both happen to be fine; anything launched
# from another directory was not.
DEFAULT_ROUTES_FILE = str(Path(__file__).resolve().parent.parent / "model_routes.json")


class ModelRoutes:
    """Loads `model_routes.json`; resolves an internal name to candidates."""

    def __init__(self, path: str | os.PathLike | None = None):
        self._path = Path(path or DEFAULT_ROUTES_FILE)
        self._routes: dict[str, list[str]] = {}
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

    def resolve(self, model_name: str) -> list[str]:
        """Candidates for `model_name`, best first.

        A concrete `provider/model` passes through as its own single candidate.
        A BARE name that is not a route is an error, not a passthrough: now that
        agent_configs carry internal names, a typo ("flsh") would otherwise sail
        through to litellm and come back as "LLM Provider NOT provided", which
        names neither the role nor the table it should have been added to.
        """
        if model_name in self._routes:
            return list(self._routes[model_name])
        if "/" in model_name:
            return [model_name]
        known = ", ".join(self.names()) or "(none)"
        raise RuntimeError(
            f"model '{model_name}' is neither a 'provider/model' nor a route in "
            f"{self._path}. Known routes: {known}. Add it there, or use a "
            f"concrete provider/model.")

    def names(self) -> list[str]:
        return sorted(self._routes)


_CACHE: dict[str, ModelRoutes] = {}


def get_routes(path: str | os.PathLike | None = None) -> ModelRoutes:
    """Process-wide cached table (re-read only when the path differs)."""
    key = str(path or DEFAULT_ROUTES_FILE)
    if key not in _CACHE:
        _CACHE[key] = ModelRoutes(key)
    return _CACHE[key]


def reset_cache() -> None:
    """Test hook: drop the cached tables so a new file is picked up."""
    _CACHE.clear()
