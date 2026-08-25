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
    Dep("DEEPSEEK_API_KEY",
        capability="every agent step — this is the pipeline's model",
        resource="an LLM provider account (api.deepseek.com)",
        how=("a Docker SECRET FILE, deliberately not an env var, so test and "
             "build subprocesses that inherit os.environ never receive it:\n"
             "  mkdir -p ~/.aitelier-secrets && chmod 700 ~/.aitelier-secrets\n"
             "  printf '%s' \"sk-…\" > ~/.aitelier-secrets/DEEPSEEK_API_KEY\n"
             "  chmod 600 ~/.aitelier-secrets/DEEPSEEK_API_KEY"),
        without="every model call fails at the provider, on the first step"),
    Dep("ARK_API_KEY",
        capability="the Ark-hosted models (the default host agent)",
        resource="a Volcengine Ark token plan",
        how="same secret-file rule as DEEPSEEK_API_KEY, at "
            "~/.aitelier-secrets/ARK_API_KEY. One key serves deepseek, glm, "
            "doubao, kimi and minimax through one OpenAI-compatible endpoint",
        without="models whose provider is `ark` in llm_providers.json cannot run"),
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
        resource="an MCP media server holding the image/audio models and the GPU",
        default="http://mcp_server:9003/mcp",
        how="export AITELIER_MEDIA_MCP_URL=\"http://<host>:9003/mcp\". NOT "
            "`AITELIER_MCP_URL` — that is the DSH plugin's variable for the "
            "opposite direction (where Harness finds AItelier)",
        without="asset generation refuses; nothing else is affected"),
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
    dep = BY_KEY.get(key)
    if dep is None:                      # an unregistered key is still a fact
        return f"{key} is not configured.{(' ' + detail) if detail else ''}"
    return _lines(dep, f"{dep.capability} is unavailable: {dep.key} is not set.",
                  detail)


def unreachable(key: str, url: str, error: object = "") -> str:
    """"X is configured but did not answer" — a different fix from `missing`."""
    dep = BY_KEY.get(key)
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
