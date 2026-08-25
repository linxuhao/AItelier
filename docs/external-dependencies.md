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
| every agent step — this is the pipeline's model | `DEEPSEEK_API_KEY` | an LLM provider account (api.deepseek.com) | every model call fails at the provider, on the first step |
| the Ark-hosted models (the default host agent) | `ARK_API_KEY` | a Volcengine Ark token plan | models whose provider is `ark` in llm_providers.json cannot run |
| cloning a private repo, pushing, and opening a PR | `GITHUB_TOKEN` | a fine-grained GitHub PAT (Contents R/W, Pull requests R/W) | private clone and push fail; public repos are unaffected |
| the `web_search` tool | `SEARXNG_URL` | a SearXNG instance that exposes format=json | agents fall back to model knowledge; `web_fetch` still works on any URL |
| the `gen_image_asset` / `gen_audio_asset` tools | `AITELIER_MEDIA_MCP_URL`<br>default `http://mcp_server:9003/mcp` | an MCP media server holding the image/audio models and the GPU | asset generation refuses; nothing else is affected |
| the Godot parse gate and head-less play-test (`godot_compile`, `godot_playtest`, `gdscript_check`) | `GODOT_BUILDER_URL`<br>default `http://godot-builder:8080` | the `godot-builder` sidecar | the gate reports `gate_skipped` and the reviewer is told the code shipped UNVERIFIED — it does not silently pass |
| the Godot readability gate (`godot_vision`) | `GODOT_VISION_URL` | an OpenAI-compatible VISION endpoint (the default is an address on the author's own network) | the gate reports itself BLIND rather than passing a game nobody looked at |

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
| Model → provider | [`llm_providers.json`](../llm_providers.json) |
