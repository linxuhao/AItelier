# External dependencies

Everything AItelier can do that needs a resource **outside this repository**, and
the config that points at it.

None of it is required to boot. A clean checkout starts with all of it absent;
the capabilities each one backs refuse and say so, naming the config key. The
rest of AItelier — the pipelines, the web UI, the CLI, the MCP endpoint — works.

This page is GENERATED from `core.external_deps.DEPS`, the same table the error
messages are built from, so a capability cannot be documented here and blame a
different variable at run time. Edit the table, not this file.

| Capability | Config | Needs | Without it |
|---|---|---|---|
| every agent step — an agent with no model cannot run | `llm_providers.json` | an account with SOME LLM provider | agent steps fail at whichever provider their model resolves to — and with a single candidate a spent quota parks the scheduler until it reopens instead of failing over |
| cloning a private repo, pushing, and opening a PR | `GITHUB_TOKEN` | a fine-grained GitHub PAT (Contents R/W, Pull requests R/W) | private clone and push fail; public repos are unaffected |
| the `web_search` tool | `SEARXNG_URL` | a SearXNG instance that exposes format=json | agents fall back to model knowledge; `web_fetch` still works on any URL |
| the `gen_image_asset` / `gen_audio_asset` tools | `AITELIER_MEDIA_MCP_URL`<br>default `http://mcp_server:9003/mcp` | an MCP media server. It holds the models and the GPU — and the CAST: the roster that keeps a character looking like itself and sounding like itself across runs | asset generation refuses; nothing else is affected. Note the roster is DURABLE STATE on that server — repoint this at a different one mid-project and every cast character is recast, so it comes back with a new face and a new voice |
| the Godot parse gate and head-less play-test (`godot_compile`, `godot_playtest`, `gdscript_check`) | `GODOT_BUILDER_URL`<br>default `http://godot-builder:8080` | the `godot-builder` sidecar | the gate reports `gate_skipped` and the reviewer is told the code shipped UNVERIFIED — it does not silently pass |
| the Godot readability gate (`godot_vision`) | `model_routes.json: vision` | at least one OpenAI-compatible VISION endpoint | the gate reports itself BLIND rather than passing a game nobody looked at |

## Which LLM provider

AItelier is **provider-agnostic**, in two layers. Nothing in the code names a
vendor:

| File | Maps |
|---|---|
| [`llm_providers.json`](../llm_providers.json) | provider name → `base_url` + the NAME of the key it reads |
| [`model_routes.json`](../model_routes.json) | model → ordered list of endpoints (`provider/model-id`) |

An `agent_config`'s `model` field names a MODEL (`flash`, `pro`, `glm`); the
table says which ENDPOINTS can serve it. A **provider** is the host, an
**endpoint** is `provider/model-id`, a **model** is an ordered list of them. Adding your own is one
entry in each plus a secret file — the rows below are examples of the pattern,
not requirements.

| Provider | Base URL | Key it reads |
|---|---|---|
| `deepseek` | `https://api.deepseek.com/` | `DEEPSEEK_API_KEY` |
| `ark` | `https://ark.cn-beijing.volces.com/api/coding/v3` | `ARK_API_KEY` |
| `localqwen` | `http://100.68.74.107:8000/v1` | `LOCAL_QWEN_API_KEY` |

### Why a second candidate is worth a second account

`AIGateway` binds the FIRST candidate and moves to the next only on an
**endpoint** error — a dead key, a spent token plan, a 429, a 5xx, a dropped
connection. A bad request or an oversized context is never failed over: every
candidate would reject it identically, so walking the list turns one clear error
into several and spends the quota you are trying to conserve.

With a single candidate, a spent plan stops the pipeline: `core/scheduler.py`
parks every tick until the provider says the window reopens. With two, the next
call is simply served elsewhere. That is why
`core.external_deps.required_llm_keys()` reports only the first candidate's key
(you can run on one) while `failover_llm_keys()` reports the rest (you should
not want to).

### More than one plan with the same vendor

A provider entry is a `(base_url, key name)` pair, not a vendor — so a second
token plan on the SAME vendor is two entries and one route, with no code change:

```json
// llm_providers.json
"ark":  { "base_url": "https://ark…/api/coding/v3", "api_key_env": "ARK_API_KEY" },
"ark2": { "base_url": "https://ark…/api/coding/v3", "api_key_env": "ARK_API_KEY_2" }
```
```json
// model_routes.json
"flash": ["ark/deepseek-v4-flash", "ark2/deepseek-v4-flash", "deepseek/deepseek-v4-flash"]
```

Add `ARK_API_KEY_2` to the `secrets:` block in `docker-compose.yml` (both the
service list and the top-level definition) and drop the key in
`~/.aitelier-secrets/ARK_API_KEY_2`.

The spent-window cooldown is keyed on `provider/model`, so `ark/…` and `ark2/…`
park independently — which is what you want, since they are separate plans with
separate windows. Exhausting the first simply moves work to the second for the
rest of that window.

A **spent usage window** is remembered, not merely stepped over: that endpoint
is parked until the instant the provider names, so the steps that follow skip it
instead of each paying one doomed call to rediscover it. Burst throttling is not
— it clears in seconds, and retiring the preferred provider over a momentary
spike would be its own kind of waste. Both arrive as `RateLimitError`; only the
message distinguishes them. The park is per `provider/model`, capped at six
hours, held in memory, and if every candidate is parked the gateway tries the
preferred one anyway — a mis-read timestamp must never be able to make the
system unrunnable.

Each turn's traced usage row records `served_by`, plus `model_route` and
`failed_over_from` when routing was involved. That is what makes a run's spend
attributable to a plan after the fact; without it, tokens billed to the fallback
look exactly like tokens billed to the preferred endpoint.

Failover is **sticky, never round-robin**: one gateway serves one pipeline step,
so every turn of that step hits one endpoint. Provider prefix caches are
per-provider, and this workload measures 26:1 prefill:decode at an 89.4% cache
hit rate — alternating endpoints per call converts cached input into full-price
input and costs far more than a second token plan saves. Spread quota per RUN,
not per call.

The error you get names whichever provider your model resolved to, including one
you added yourself:

```
models served by the `deepseek` provider is unavailable: DEEPSEEK_API_KEY is not set.
  Model 'deepseek/deepseek-v4' resolves to provider 'deepseek', which reads it.
  Needs: an account with `deepseek` (https://api.deepseek.com/)
  Set it: a Docker SECRET FILE, deliberately not an env var, so test and build subprocesses that inherit os.environ never receive it:
    mkdir -p ~/.aitelier-secrets && chmod 700 ~/.aitelier-secrets
    printf '%s' "<key>" > ~/.aitelier-secrets/DEEPSEEK_API_KEY
    chmod 600 ~/.aitelier-secrets/DEEPSEEK_API_KEY
  Or select a different provider: `deepseek` is one entry in llm_providers.json, and an agent_config's `model` picks it
  Without it: any agent whose model resolves to `deepseek` fails there.
```


## What you actually see when one is missing

The message names the config key, not just the value it resolved to — that
distinction is the whole point. `godot-builder unreachable
(http://godot-builder:8080)` is true and useless; it does not say what to edit.

```
the `web_search` tool is unavailable: SEARXNG_URL is not set.
  The pipeline continues without web results.
  Needs: a SearXNG instance that exposes format=json
  Set it: export SEARXNG_URL="http://localhost:8888"
  Without it: agents fall back to model knowledge; `web_fetch` still works on any URL.

the Godot parse gate and head-less play-test (`godot_compile`, `godot_playtest`, `gdscript_check`) is unavailable: GODOT_BUILDER_URL is at its default http://godot-builder:8080 did not answer (Name or service not known).
  Needs: the `godot-builder` sidecar
  Set it: it SHIPS IN THIS REPO as a compose service, so this usually needs no configuration — `docker compose up -d godot-builder`. Point the variable elsewhere only to use a builder you host yourself
  Without it: the gate reports `gate_skipped` and the reviewer is told the code shipped UNVERIFIED — it does not silently pass.
```

`missing` and `unreachable` are different failures with different fixes: one
means nothing is configured, the other means something is configured and did not
answer. A message that conflates them sends you to edit a variable that is
already correct.

## Two shapes of failure

**Refuses.** `web_search`, `gen_image_asset`, `gen_audio_asset`, an LLM call —
the capability is the point, so its absence is an error the caller sees.

**Skips, loudly.** The Godot gates. A missing sidecar is an infrastructure
problem, not a code defect, so the run continues — but the report carries
`gate_skipped: true` and tells the reviewer the code shipped UNVERIFIED, and the
skip is recorded in `~/.AItelier/logs/gate_skips.log` (on the mounted volume, so
it survives container recreation). A gate that quietly passed is how seven
compile errors once shipped; a gate that blocks the pipeline over a sidecar is
its own kind of wrong. Loud-skip is the middle.

## Where the config lives

| Kind | Where |
|---|---|
| API keys | Docker **secret files** in `~/.aitelier-secrets/`, never env vars — so test and build subprocesses that inherit `os.environ` cannot read them |
| Endpoints | environment, via `.env` (see [`.env.example`](../.env.example)) or `docker-compose.yml` |
| Model → provider | [`model_routes.json`](../model_routes.json) (internal name → candidates), then [`llm_providers.json`](../llm_providers.json) (provider → URL + key) |
