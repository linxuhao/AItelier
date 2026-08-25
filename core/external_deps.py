"""Every capability that needs something outside this repo, in one table.

Two consumers, one source — the same shape as `forge_registry_check.RULES`:

  * the ERROR a capability raises when its resource is missing or unreachable,
  * the DOC listing what a deployment has to provide.

They are joined here because they drift apart otherwise, and the failure mode of
that drift is specific: a capability dies naming a URL nobody recognises, the doc
describes a variable nobody set, and the reader cannot connect the two. Every
message below therefore names the CONFIG KEY — not merely the value it resolved
to. "godot-builder unreachable (http://godot-builder:8080)" is a true sentence
that does not tell you what to edit.

Nothing here is required to boot. A clean checkout starts with none of it; the
capabilities each one backs simply refuse, and say so.
"""

from __future__ import annotations


class Dep:
    """One external resource, the capability it backs, and how to point at it."""

    __slots__ = ("key", "capability", "resource", "default", "how", "without")

    def __init__(self, key: str, capability: str, resource: str, *,
                 default: str = "", how: str = "", without: str = ""):
        self.key = key                  # the env var / secret file name
        self.capability = capability    # what stops working, in the user's words
        self.resource = resource        # what has to exist out there
        self.default = default          # what it falls back to, if anything
        self.how = how                  # how to point it somewhere real
        self.without = without          # what happens when it is absent

    def __repr__(self) -> str:
        return f"Dep({self.key!r})"


DEPS: tuple[Dep, ...] = (
    Dep("llm_providers.json",
        capability="every agent step — an agent with no model cannot run",
        resource="an account with SOME LLM provider",
        how="AItelier is provider-agnostic: `llm_providers.json` maps a "
            "provider name to a base_url and the NAME of the key it reads, and "
            "an agent_config's `model` field selects one. Adding a provider is "
            "one entry there plus a secret file of that name — no code names a "
            "vendor. The shipped entries are examples, not requirements",
        without="agent steps fail at whichever provider their model resolves to"),
    Dep("GITHUB_TOKEN",
        capability="cloning a private repo, pushing, and opening a PR",
        resource="a fine-grained GitHub PAT (Contents R/W, Pull requests R/W)",
        how="~/.aitelier-secrets/GITHUB_TOKEN. An EMPTY file is valid and means "
            "'no credentials' — public clone still works",
        without="private clone and push fail; public repos are unaffected"),
    Dep("SEARXNG_URL",
        capability="the `web_search` tool",
        resource="a SearXNG instance that exposes format=json",
        how="export SEARXNG_URL=\"http://localhost:8888\"",
        without="agents fall back to model knowledge; `web_fetch` still works "
                "on any URL"),
    Dep("AITELIER_MEDIA_MCP_URL",
        capability="the `gen_image_asset` / `gen_audio_asset` tools",
        resource="an MCP media server. It holds the models and the GPU — and "
                 "the CAST: the roster that keeps a character looking like "
                 "itself and sounding like itself across runs",
        default="http://mcp_server:9003/mcp",
        how="export AITELIER_MEDIA_MCP_URL=\"http://<host>:9003/mcp\". NOT "
            "`AITELIER_MCP_URL` — that is the DSH plugin's variable for the "
            "opposite direction (where Harness finds AItelier)",
        without="asset generation refuses; nothing else is affected. Note "
                "the roster is DURABLE STATE on that server — repoint this at a "
                "different one mid-project and every cast character is recast, "
                "so it comes back with a new face and a new voice"),
    Dep("GODOT_BUILDER_URL",
        capability="the Godot parse gate and head-less play-test "
                   "(`godot_compile`, `godot_playtest`, `gdscript_check`)",
        resource="the `godot-builder` sidecar",
        default="http://godot-builder:8080",
        how="it SHIPS IN THIS REPO as a compose service, so this usually needs "
            "no configuration — `docker compose up -d godot-builder`. Point the "
            "variable elsewhere only to use a builder you host yourself",
        without="the gate reports `gate_skipped` and the reviewer is told the "
                "code shipped UNVERIFIED — it does not silently pass"),
    Dep("GODOT_VISION_URL",
        capability="the Godot readability gate (`godot_vision`)",
        resource="an OpenAI-compatible VISION endpoint (the default is an "
                 "address on the author's own network)",
        how="export GODOT_VISION_URL / GODOT_VISION_MODEL / "
            "GODOT_VISION_CONTEXT_TOKENS, or set GODOT_VISION_FALLBACK_KEY to "
            "use a hosted vision model",
        without="the gate reports itself BLIND rather than passing a game "
                "nobody looked at"),
)

BY_KEY = {d.key: d for d in DEPS}


def required_llm_keys() -> list[str]:
    """The provider key(s) the SHIPPED agent_configs actually need.

    Derived, never hard-coded. A constant here would be a vendor name in a
    provider-agnostic system, and it would go stale the moment the configs move
    — which is exactly how the CLI came to tell a new user to create
    DEEPSEEK_API_KEY on the same install where the README (correctly) said
    ARK_API_KEY. Empty when nothing can be determined; callers must cope.
    """
    import json
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    try:
        providers = json.loads(
            (root / "llm_providers.json").read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return []
    used: set[str] = set()
    try:
        for f in sorted((root / "agent_configs").glob("*.yaml")):
            for m in re.finditer(r'^\s+model:\s*"?([a-z0-9_]+)/',
                                 f.read_text(encoding="utf-8"), re.M):
                used.add(m.group(1))
    except OSError:
        return []
    return sorted({providers[p]["api_key_env"] for p in used
                   if p in providers and providers[p].get("api_key_env")})


def _provider_dep(key: str) -> Dep | None:
    """A Dep for an LLM key that `llm_providers.json` declares.

    The provider keys are not constants here on purpose — that would make this
    module name vendors, which is the one thing the provider-agnostic design
    avoids. A provider someone adds tomorrow gets exactly the same message as
    the shipped ones, because both are read from the same file.
    """
    import json
    from pathlib import Path
    try:
        path = Path(__file__).resolve().parent.parent / "llm_providers.json"
        providers = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for name, cfg in (providers or {}).items():
        if (cfg or {}).get("api_key_env") != key:
            continue
        return Dep(
            key,
            capability=f"models served by the `{name}` provider",
            resource=f"an account with `{name}` ({(cfg or {}).get('base_url')})",
            how=(f"a Docker SECRET FILE, deliberately not an env var, so test "
                 f"and build subprocesses that inherit os.environ never receive "
                 f"it:\n"
                 f"  mkdir -p ~/.aitelier-secrets && chmod 700 ~/.aitelier-secrets\n"
                 f"  printf '%s' \"<key>\" > ~/.aitelier-secrets/{key}\n"
                 f"  chmod 600 ~/.aitelier-secrets/{key}\n"
                 f"Or select a different provider: `{name}` is one entry in "
                 f"llm_providers.json, and an agent_config's `model` picks it"),
            without=f"any agent whose model resolves to `{name}` fails there")
    return None


def resolve(key: str) -> Dep | None:
    """The registered Dep, or one synthesized from the provider registry."""
    return BY_KEY.get(key) or _provider_dep(key)


def _lines(dep: Dep, headline: str, detail: str) -> str:
    """One scannable block. A single run-on paragraph carrying a shell snippet
    is skimmed past; the reader needs to find the fix, not read prose."""
    out = [headline]
    if detail:
        out.append(f"  {detail}")
    out.append(f"  Needs: {dep.resource}")
    if dep.how:
        how = dep.how.replace("\n", "\n  ")
        out.append(f"  Set it: {how}")
    if dep.without:
        out.append(f"  Without it: {dep.without}.")
    return "\n".join(out)


def missing(key: str, detail: str = "") -> str:
    """"You asked for X and its config is not set" — naming the config."""
    dep = resolve(key)
    if dep is None:                      # an unregistered key is still a fact
        return f"{key} is not configured.{(' ' + detail) if detail else ''}"
    return _lines(dep, f"{dep.capability} is unavailable: {dep.key} is not set.",
                  detail)


def unreachable(key: str, url: str, error: object = "") -> str:
    """"X is configured but did not answer" — a different fix from `missing`."""
    dep = resolve(key)
    err = f" ({error})" if error else ""
    if dep is None:
        return f"{key} ({url}) could not be reached{err}."
    where = (f"{dep.key} is at its default {url}"
             if dep.default and url == dep.default else f"{dep.key}={url}")
    return _lines(dep,
                  f"{dep.capability} is unavailable: {where} did not answer{err}.",
                  "")


def render_markdown() -> str:
    """The doc table. Generated from this table so the two cannot disagree."""
    out = ["| Capability | Config | Needs | Without it |",
           "|---|---|---|---|"]
    for d in DEPS:
        default = f"<br>default `{d.default}`" if d.default else ""
        out.append(f"| {d.capability} | `{d.key}`{default} | {d.resource} "
                   f"| {d.without or '—'} |")
    return "\n".join(out)


def render_providers() -> str:
    """The provider registry AS IT STANDS, rendered from the file itself.

    Listed separately from DEPS because these rows are examples of a pattern,
    not a fixed set of requirements — hard-coding "DeepSeek and Ark" into the
    dependency table said the opposite of what the design does.
    """
    import json
    from pathlib import Path
    try:
        providers = json.loads(
            (Path(__file__).resolve().parent.parent / "llm_providers.json")
            .read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return "_(llm_providers.json unreadable)_"
    out = ["| Provider | Base URL | Key it reads |", "|---|---|---|"]
    for name, cfg in providers.items():
        out.append(f"| `{name}` | `{(cfg or {}).get('base_url', '')}` "
                   f"| `{(cfg or {}).get('api_key_env', '—')}` |")
    return "\n".join(out)
