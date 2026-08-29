#!/usr/bin/env python3
"""Measure what each endpoint ACTUALLY caches, and what we RECORD that it cached.

Why this exists
---------------
The trace showed `opencodego/glm-5.3-flash` at a 0.0% prefix-cache hit rate over
168 turn>=2 calls, while `opencodego/deepseek-v4-flash` on the same provider hit
94.8%. That looked like "this endpoint does not cache", and a routing change was
nearly made on it. A direct HTTP probe then showed the endpoint caching fine
(6,272 / 6,295 tokens on the second call), so the 0.0% was a MEASUREMENT
artifact somewhere between the wire and `usage.cache_hit_tokens` — and a cost
comparison built on it would have moved traffic for no reason.

So this probe measures both numbers side by side, per endpoint:

  RAW      what the endpoint reports over plain HTTP (ground truth)
  GATEWAY  what `AIGateway.last_usage` reports after litellm + streaming +
           `stream_chunk_builder` re-assembly (what the trace stores)

A gap between the two columns is OUR bug. A low RAW number is THEIR cache.
Both are worth knowing and only one is worth changing routes over.

`tools` is varied because that is the other difference between the probe that
disagreed with production and production itself: DPE steps always carry tools,
and an upstream that declines to cache tool-carrying requests would look
identical to a reporting bug from the trace alone.

Usage
-----
    python scripts/probe_cache_behavior.py                  # every routed endpoint
    python scripts/probe_cache_behavior.py ark/glm-5.3 ...  # only these

Run it INSIDE the app container (it needs the mounted secrets):
    docker exec aitelier bash -lc 'cd /app && python scripts/probe_cache_behavior.py'

It spends real tokens: ~4 calls x ~6k prompt tokens per endpoint.
"""

from __future__ import annotations

import json
import os
import random
import signal
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.ai_router import AIGateway, _read_secret            # noqa: E402
from core.model_routes import config_or_example, get_routes    # noqa: E402

# ~6k tokens: comfortably over every provider's minimum cacheable prefix (1k for
# DeepSeek, 256 for Anthropic) so a miss cannot be blamed on the prompt's size.
PREFIX_LINES = 420
WORDS = ("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu "
         "xi omicron pi rho sigma tau upsilon phi chi psi omega").split()

TOOL = [{
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a file from the repository",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"}},
                       "required": ["path"]},
    },
}]


def make_prefix(seed: int) -> str:
    """A prompt no cache has seen before, so 'cold' is honestly cold.

    Seeded per (endpoint, tools) pair rather than shared: a prefix reused across
    endpoints would be warm on the second one for a reason that has nothing to
    do with that endpoint, and providers that proxy the same upstream would
    quietly hit each other's cache.
    """
    rng = random.Random(seed)
    return "\n".join(" ".join(rng.choice(WORDS) for _ in range(12)) + "."
                     for _ in range(PREFIX_LINES))


def _providers() -> dict:
    with open(config_or_example("llm_providers.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def raw_call(endpoint: str, prompt: str, tools: bool) -> dict | None:
    """One non-streaming HTTP call, straight at the provider. Ground truth."""
    prov, _, model = endpoint.partition("/")
    entry = (_providers().get(prov) or {})
    base = (entry.get("base_url") or "").rstrip("/")
    key = _read_secret(entry.get("api_key_env") or "") or ""
    if not base:
        return None
    body = {"model": model,
            "messages": [{"role": "system", "content": "Terse."},
                         {"role": "user", "content": prompt}],
            "max_tokens": 16, "temperature": 0}
    if tools:
        body["tools"] = TOOL
        body["tool_choice"] = "auto"
    req = urllib.request.Request(
        f"{base}/chat/completions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}",
                 # Some gateways reject the stdlib default UA outright (403).
                 "User-Agent": "aitelier-cache-probe/1"})
    with urllib.request.urlopen(req, timeout=120) as r:
        usage = json.load(r).get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    hit = usage.get("prompt_cache_hit_tokens")
    if hit is None:
        hit = details.get("cached_tokens")
    return {"pt": usage.get("prompt_tokens"), "hit": hit}


def gateway_call(endpoint: str, prompt: str, tools: bool) -> dict:
    """One call through the production path (litellm, streaming, re-assembly)."""
    g = AIGateway(endpoint, max_output_tokens=16)
    g.generate_native([{"role": "system", "content": "Terse."},
                       {"role": "user", "content": prompt}],
                      tools=TOOL if tools else None)
    u = g.last_usage or {}
    return {"pt": u.get("prompt_tokens"), "hit": u.get("cache_hit_tokens")}


# One endpoint that accepts the connection and then never speaks must not be able
# to end the sweep. The first run of this probe hung for 26 minutes on a single
# streaming call — `raw_call`'s socket timeout does not cover the litellm path,
# and a measurement tool that can stop measuring is worse than a slow one.
CALL_DEADLINE_S = 180


class ProbeTimeout(Exception):
    pass


def with_deadline(fn, *a):
    def _fire(_sig, _frm):
        raise ProbeTimeout(f"no answer in {CALL_DEADLINE_S}s")
    prev = signal.signal(signal.SIGALRM, _fire)
    signal.alarm(CALL_DEADLINE_S)
    try:
        return fn(*a)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, prev)


def pct(hit, pt) -> str:
    if not pt or hit is None:
        return "  n/a" if hit is None else "    -"
    return f"{hit / pt * 100:5.1f}"


def probe(endpoint: str, seed: int) -> None:
    for tools in (False, True):
        label = f"{endpoint}  tools={'on ' if tools else 'off'}"
        row = {}
        for j, (path, fn) in enumerate((("RAW", raw_call), ("GATEWAY", gateway_call))):
            # A prefix per PATH, not per pair: sharing one would let the RAW
            # call warm the cache that GATEWAY is about to call cold, and every
            # "cold" number after the first would be a lie.
            prompt = make_prefix(seed + (1 if tools else 0) + 100 * j)
            try:
                cold = with_deadline(fn, endpoint, prompt, tools)
                if cold is None:
                    row[path] = "unregistered"
                    continue
                # Providers populate a prefix cache asynchronously; a warm call
                # fired immediately can miss for timing alone and read as "does
                # not cache". Every provider doc that states a number states one
                # under a second, so a few seconds is generous, not padding.
                time.sleep(4)
                warm = with_deadline(fn, endpoint, prompt, tools)
                row[path] = (f"cold {pct(cold['hit'], cold['pt'])}% "
                             f"-> warm {pct(warm['hit'], warm['pt'])}%  "
                             f"(pt={warm['pt']})")
            except Exception as e:                       # noqa: BLE001
                row[path] = f"ERROR {type(e).__name__}: {str(e)[:60]}"
        print(f"  {label:44} RAW: {row.get('RAW')}", flush=True)
        print(f"  {'':44} GW : {row.get('GATEWAY')}", flush=True)


def routed_endpoints() -> list[str]:
    """Every concrete endpoint the route table can bind, in table order."""
    routes = get_routes()
    seen, out = set(), []
    for name in ("flash", "pro", "glm", "smart", "vision"):
        try:
            for ep in routes.resolve(name):
                if ep not in seen:
                    seen.add(ep)
                    out.append(ep)
        except Exception:                                # noqa: BLE001
            continue
    return out


def main() -> int:
    endpoints = sys.argv[1:] or routed_endpoints()
    print(f"probing {len(endpoints)} endpoints "
          f"(stream={os.getenv('AITELIER_LLM_STREAM', '1')})\n")
    print("  RAW = what the endpoint reports over plain HTTP")
    print("  GW  = what AIGateway.last_usage records (this is what the trace stores)")
    print("  a RAW/GW gap is our bug; a low RAW warm% is their cache\n")
    for i, ep in enumerate(endpoints):
        probe(ep, seed=1000 + i * 10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
