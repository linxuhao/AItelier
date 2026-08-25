"""MCP endpoint — expose AItelier's pipeline surface to any MCP-speaking host.

Mounted at `/mcp` on the same FastAPI app as the rest of the control plane, so it
inherits the container's live singletons (SkillFlow, ToolLoader, AgentConfigs, the
config registry) instead of reconstructing them. That is the whole reason it lives
in-process: a host-side proxy would have to re-expose every one of these through
HTTP first.

Consumed by DeepSeek Harness through `@deepseek-ai/dsh-mcp-client`:

    - id: mcp-aitelier
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: aitelier
        transport: streamable-http
        url: http://aitelier:4444/mcp

…and by anything else that speaks streamable-HTTP MCP.

── AUTHORIZATION: WHY THIS FILE HAS ITS OWN GATE ────────────────────────────────

`api/main.py:write_gate` decides by HTTP METHOD — `{GET, HEAD, OPTIONS}` pass, the
rest need a writer. MCP streamable-HTTP sends EVERY tool call, read or write, as
`POST /mcp`. The method carries no information here, so a path-level gate has only
two settings and both are wrong:

  - leave `/mcp` gated  → `list_pipelines` demands a writer token
  - exempt `/mcp`       → `edit_pipeline` runs with NO authorization at all

So the gate moves to the tool. Every tool declares `kind="read"` or `kind="write"`,
and `_authorize` re-applies exactly the same `authz` verdict the middleware would
have. The middleware exemption for this path (in `api/main.py`) is what makes reads
work; this function is what keeps writes closed. Those two changes only make sense
together — neither is safe alone.

It fails CLOSED: `_TOOL_KIND` has no default, and `test_mcp_router.py` asserts every
registered tool appears in it. A tool added without a declaration cannot quietly
become world-writable; it raises at import.
"""

from __future__ import annotations

import functools
import inspect
import os
from typing import Callable

import anyio.to_thread

from mcp.server.fastmcp import Context, FastMCP

from api import authz
from core.tool_guards import (bad_config_name, bad_tool_name,
                              tool_source_error)

# Every tool's authorization class. No default on purpose — see the module
# docstring. `read` answers questions; `write` changes state on disk, in the
# registry, or in the run table.
_TOOL_KIND: dict[str, str] = {}

# The event loop the endpoint runs on, captured at lifespan open so a tool
# body executing in a worker thread can still schedule background work.
_MAIN_LOOP = None
# Strong refs to in-flight background drivers: asyncio keeps only a weak
# reference to a bare task, so one dropped here would be garbage-collected
# mid-run and the pipeline would stop moving for no visible reason.
_DRIVERS: set = set()


class ToolDenied(Exception):
    """The caller may not run this tool. Surfaces to the model as a tool error."""


class ToolError(Exception):
    """A tool cannot answer, for a reason the caller can act on.

    Caught centrally in `_wrap`. Deep helpers used to signal failure by RETURNING a
    sentinel (`_load_roles` answered `{"__error__": "<msg>"}` on unreadable JSON) —
    which no caller checked, so `list_roles` called `.get()` on the message string
    and the AttributeError escaped as a raw traceback, delivering neither the
    diagnosis nor a usable error. A raise cannot be ignored by omission.
    """


def _request_from(ctx: Context):
    """The live Starlette request behind this MCP call, when there is one.

    stateless_http mode hands the request through the session context. A missing
    request means the call did not arrive over HTTP (a direct unit-test call), and
    the caller decides what that means — this function never guesses.
    """
    try:
        return ctx.request_context.request
    except Exception:
        return None


def _authorize(name: str, ctx: Context | None) -> None:
    """Apply the same verdict `write_gate` would have, per tool rather than per path."""
    kind = _TOOL_KIND.get(name)
    if kind is None:
        # Unreachable through `tool()`, which registers the kind. Belt and braces:
        # an unknown tool is never a read.
        raise ToolDenied(f"tool '{name}' declares no authorization class")
    if kind == "read":
        return
    if not authz.gate_enabled():
        return          # local dev: the gate is inactive for the whole app
    request = None if ctx is None else _request_from(ctx)
    if request is None:
        # A write tool reached over a transport with no request to check. Denying
        # is the only safe reading: the alternative is granting write authority to
        # a caller nobody identified.
        raise ToolDenied(
            f"'{name}' changes state and this call carries no verifiable identity")
    if not authz.request_can_write(request):
        code = authz.write_denial_reason(request)
        # `detail` is denial_body's human-message key ("message" was a guess, and
        # the .get fallback silently shipped the bare code to the model — live DSH
        # run 2026-08-24 showed `denied: write_denied_not_authenticated`). Keep the
        # code too: the message alone doesn't grep.
        body = authz.denial_body(code)
        raise ToolDenied(f"{body.get('detail', code)} ({code})")


def _wrap(mcp: FastMCP, fn: Callable, name: str) -> Callable:
    """Authorize, then run. Errors come back as text the model can act on.

    The context is taken from the server's per-request contextvar, NOT from a `ctx`
    parameter. Both routes were tried and the parameter one is a trap twice over:
    `functools.wraps` makes `inspect.signature` report the WRAPPED function's
    params, so FastMCP never saw a `ctx` to inject and every write was denied the
    moment the gate came on — and publishing a corrected signature then leaked
    `ctx` into the tool's input schema, inviting the model to pass a value for it.
    `get_context()` reads the same request without touching the tool's contract.
    """
    @functools.wraps(fn)
    async def _inner(*args, **kwargs):
        try:
            _authorize(name, mcp.get_context())
        except ToolDenied as e:
            return {"error": f"denied: {e}"}
        try:
            # Every tool body but `wait_for_run` is SYNCHRONOUS, and some of them are
            # slow: run_pipeline shells out to git (workspace setup), the edit tools
            # re-register a graph, export reads every tool file. Awaiting those
            # inline pinned the uvicorn loop for their whole duration — SSE streams
            # stopped flushing, the web UI hung, and the scheduler's cross-thread
            # notification callbacks queued unexecuted. One MCP call could freeze the
            # entire control plane. Authorization stays on the loop above (it reads a
            # request-scoped contextvar); only the body moves off it.
            if inspect.iscoroutinefunction(fn):
                return await fn(*args, **kwargs)
            return await anyio.to_thread.run_sync(
                functools.partial(fn, *args, **kwargs))
        except ToolError as e:
            return {"error": str(e)}
    return _inner


# Hosts this endpoint will answer to. The SDK enforces DNS-rebinding protection by
# rejecting an unexpected `Host` header with 421 — worth keeping, because the port
# is published on the host's loopback and a browser tricked into posting there
# would otherwise reach the read tools. But the default list knows nothing about
# this deployment, so a correct `url:` in cordis.yml would 421 with a message the
# operator sees only in the container log. Name the real hosts instead:
#   - aitelier:*     the docker service name DSH connects to over the shared network
#   - localhost:* / 127.0.0.1:*   the published loopback port (CLI, local clients)
#   - testserver     Starlette's TestClient
# `:*` is the SDK's wildcard-port form. Extend with AITELIER_MCP_ALLOWED_HOSTS
# (comma-separated) for a tunnel hostname.
_DEFAULT_ALLOWED_HOSTS = ["aitelier:*", "localhost:*", "127.0.0.1:*",
                          "localhost", "127.0.0.1", "aitelier", "testserver"]


def _allowed_hosts() -> list[str]:
    extra = [h.strip() for h in
             os.getenv("AITELIER_MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    return _DEFAULT_ALLOWED_HOSTS + extra


def build_mcp() -> FastMCP:
    """Construct the MCP server with every AItelier tool registered.

    Stateless + JSON responses: the endpoint holds no per-client session, so a DSH
    restart, a second client, or an interleaved CLI call all behave identically —
    the same property `skillflow-mcp` gets from reconnecting by `run_id`.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    # streamable_http_path="/" because the caller mounts the BARE ASGI app at
    # "/mcp" (see api/main.py). It is unused in that path but keeps a standalone
    # `streamable_http_app()` — used by tests and by anyone serving this directly
    # — from composing to "/mcp/mcp".
    mcp = FastMCP("aitelier", stateless_http=True, json_response=True,
                  streamable_http_path="/",
                  transport_security=TransportSecuritySettings(
                      enable_dns_rebinding_protection=True,
                      allowed_hosts=_allowed_hosts(),
                      allowed_origins=_allowed_hosts()))

    def tool(name: str, kind: str, description: str):
        def deco(fn):
            _TOOL_KIND[name] = kind
            mcp.add_tool(_wrap(mcp, fn, name), name=name,
                         description=description)
            return fn
        return deco

    _register_read_tools(tool)
    _register_bundle_tools(tool)
    _register_edit_tools(tool)
    _register_run_tools(tool)
    _register_wait_tool(tool)
    _register_trace_tools(tool)
    _register_lifecycle_tools(tool)
    return mcp


# ── Read tools ───────────────────────────────────────────────────────────────

def _register_read_tools(tool):

    @tool("list_pipelines", "read",
          "List every registered pipeline (config) with its repo mode, whether the "
          "scheduler drives it, and whether it was generated by pipeline_forge. "
          "Start here — the names it returns are the `config` argument everywhere else.")
    def list_pipelines() -> dict:
        from api.dependencies import get_config_registry
        out = []
        for m in get_config_registry().list():
            out.append({
                "config": m.config_name,
                "generated": m.config_name.startswith("gen_"),
                "scheduler_owned": bool(getattr(m, "scheduler_owned", False)),
                "repo_mode": getattr(m, "repo_mode", None),
                "description": getattr(m, "description", "") or "",
                "input_hint": getattr(m, "input_hint", "") or "",
            })
        return {"pipelines": sorted(out, key=lambda p: p["config"])}

    @tool("get_pipeline", "read",
          "Read one pipeline's graph YAML plus the names of its roles, templates and "
          "tools. This is the source of truth for `edit_pipeline`.")
    def get_pipeline(config: str) -> dict:
        src = _config_source(config)
        if src is None:
            return {"error": f"no pipeline '{config}' — call list_pipelines"}
        import yaml
        path = src["path"]
        if path is not None:
            text = path.read_text(encoding="utf-8")
            data = yaml.safe_load(text) or {}
        else:
            # Nothing on disk declares this graph — it is composed at boot, or it
            # ships inside skillflow. The LIVE graph is then the only artefact, so
            # serialise the one that actually runs rather than refusing.
            data = _live_graph_dict(config)
            if data is None:
                return {"error": f"'{config}' is registered but its graph could not "
                                 f"be read back ({src['origin']})"}
            text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
        steps = data.get("steps") or []
        return {
            "config": config,
            "path": str(path) if path is not None else None,
            "source": src["origin"],
            "editable": path is not None and not _is_builtin(path),
            "graph_yaml": text,
            "steps": [{"id": s.get("id"), "type": s.get("step_type"),
                       "agent_config": s.get("agent_config"),
                       "tool_name": s.get("tool_name")}
                      for s in steps if isinstance(s, dict)],
        }

    @tool("list_roles", "read",
          "List the agent roles (model + template + tools) a pipeline's agent steps "
          "use. For a generated pipeline these live in <slug>.roles.json.")
    def list_roles(config: str) -> dict:
        roles, err = _roles_or_error(config)
        if err:
            return err
        return {"config": config,
                "roles": [{"role": k,
                           "model": (v or {}).get("model"),
                           "template": (v or {}).get("template"),
                           "tools": (v or {}).get("tools") or []}
                          for k, v in sorted(roles.items())]}

    @tool("get_role", "read",
          "Read one role's full config, including its system prompt.")
    def get_role(config: str, role: str) -> dict:
        roles, err = _roles_or_error(config)
        if err:
            return err
        if role not in roles:
            return {"error": f"no role '{role}' in '{config}'. "
                             f"Have: {', '.join(sorted(roles))}"}
        return {"config": config, "role": role, "value": roles[role]}


# ── Shared helpers ───────────────────────────────────────────────────────────

def _repo_configs_dir():
    from pathlib import Path
    return Path(__file__).resolve().parent.parent / "configs"


def _builtin_stem(config: str) -> str | None:
    """The `configs/` file stem whose graph DECLARES `config` as its name.

    The registry keys pipelines by the graph's `name:` field, not by filename, and
    for the flagship pipeline the two differ: `configs/dpe_default.yaml` declares
    `name: "dpe_default_v2"`. Resolving by filename made every read tool answer
    "no pipeline 'dpe_default_v2' — call list_pipelines" for a name `list_pipelines`
    had just returned — a loop with no exit, on the most important config there is.
    """
    if bad_config_name(config):
        return None
    d = _repo_configs_dir()
    if (d / f"{config}.yaml").exists():
        return config
    import yaml
    for p in sorted(d.glob("*.yaml")):
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            continue          # a malformed sibling must not hide the match
        if isinstance(data, dict) and data.get("name") == config:
            return p.stem
    return None


def _builtin_config_path(config: str):
    stem = _builtin_stem(config)
    if not stem:
        return None
    p = _repo_configs_dir() / f"{stem}.yaml"
    return p if p.exists() else None


def _agent_configs_dir():
    return _repo_configs_dir().parent / "agent_configs"


def _live_graph_dict(config: str) -> dict | None:
    """The registered graph, serialised — for a config with no file behind it."""
    from api.dependencies import get_config_registry
    mf = get_config_registry().get(config)
    if mf is None:
        return None
    try:
        return mf.graph_provider().to_dict()
    except Exception:
        return None


def _config_source(config: str) -> dict | None:
    """Where a REGISTERED config's graph and roles actually live.

    `list_pipelines` enumerates the LIVE registry, which holds two kinds of config
    that `configs/*.yaml` does not: one composed at boot (`dpe_game` is
    `dpe_default_v2` + `configs/addons/game_harness.yaml`, assembled by
    `core/addon_registry.py` and never written to a file) and one registered from
    the skillflow package (`addon_converter`). Resolving by file alone made six
    read tools answer "no pipeline 'dpe_game' — call list_pipelines" for a name
    list_pipelines had just returned, on 18 of the 30 runs in the DB — the same
    dead end `_builtin_stem` closed for `dpe_default_v2`, one config short.

    Returns None only when the registry does not know `config` at all. That, and
    only that, is when "call list_pipelines" is advice rather than a loop.
    """
    from core import pipeline_registry as pr
    if bad_config_name(config):
        return None
    gen = pr.generated_configs_dir() / f"{config}.yaml"
    if gen.exists():
        return {"kind": "generated", "path": gen, "role_stems": [],
                "origin": f"generated pipeline, {gen}"}
    stem = _builtin_stem(config)
    if stem:
        return {"kind": "builtin", "path": _repo_configs_dir() / f"{stem}.yaml",
                "role_stems": [stem], "origin": f"built-in, configs/{stem}.yaml"}
    from api.dependencies import get_config_registry
    if get_config_registry().get(config) is None:
        return None
    addons: list = []
    base = config
    try:
        from core.addon_registry import describe_config
        d = describe_config(config) or {}
        base, addons = d.get("base") or config, list(d.get("addons") or [])
    except Exception:
        pass
    if addons:
        base_stem = _builtin_stem(base) or base
        # Addon roles live in `agent_configs/<addon>.yaml` beside the base's own —
        # role names are flat across those files, so a plain merge is the same
        # table the runner resolves against.
        stems = [base_stem] + [a for a in addons
                               if (_agent_configs_dir() / f"{a}.yaml").exists()]
        return {"kind": "composed", "path": None, "role_stems": stems,
                "base": base, "addons": addons,
                "origin": ("composed at boot from configs/%s.yaml (%s) + %s"
                           % (base_stem, base,
                              " + ".join(f"configs/addons/{a}.yaml"
                                         for a in addons)))}
    return {"kind": "external", "path": None, "role_stems": [],
            "origin": "registered from the skillflow package — its graph and roles "
                      "ship with the engine, not with this repo"}


def _roles_or_error(config: str):
    """(roles, error-dict). Splits "no such pipeline" from "no roles HERE".

    The four role/template tools all answered "no roles for 'X'" for both, which
    for a composed config meant an unregistered name and a perfectly registered
    one were indistinguishable.
    """
    roles = _load_roles(config)
    if roles:
        return roles, None
    src = _config_source(config)
    if src is None:
        return None, {"error": f"no pipeline '{config}' — call list_pipelines"}
    return None, {"error": f"'{config}' has no roles readable here — {src['origin']}. "
                           f"get_pipeline('{config}') reads its graph."}


def _is_builtin(path) -> bool:
    try:
        return _repo_configs_dir() in path.resolve().parents
    except Exception:
        return False


def _load_roles(config: str) -> dict | None:
    """Roles for a generated pipeline (<slug>.roles.json) or a built-in one."""
    import json
    from core import pipeline_registry as pr
    bad = bad_config_name(config)
    if bad:
        raise ToolError(bad)
    rj = pr.generated_configs_dir() / f"{config}.roles.json"
    if rj.exists():
        try:
            return json.loads(rj.read_text(encoding="utf-8")) or {}
        except Exception as e:
            raise ToolError(f"{config}.roles.json is not valid JSON: {e}")
    import yaml
    # Built-in roles live beside the graph and share its FILE stem, not its
    # declared name — see _builtin_stem. A COMPOSED config draws on more than one
    # such file (base + one per addon), so this is a list, not a stem.
    src = _config_source(config)
    if src is None:
        return None
    roles: dict = {}
    for stem in src.get("role_stems") or []:
        ac = _agent_configs_dir() / f"{stem}.yaml"
        if ac.exists():
            roles.update(yaml.safe_load(ac.read_text(encoding="utf-8")) or {})
    return roles or None


def mcp_asgi_app(mcp: FastMCP):
    """The bare streamable-HTTP ASGI app, with no Starlette router in front.

    `FastMCP.streamable_http_app()` returns a Starlette app whose single Route sits
    at `streamable_http_path`. Mounting THAT at "/mcp" makes Starlette redirect
    `POST /mcp` → `307 /mcp/` (the mount strips the prefix, leaving "", and the
    inner router wants "/"). A 307 on a POST is a coin flip across clients, and the
    one that follows it arrives with the body re-sent to a URL the operator never
    configured. Mounting the bare ASGI app removes the inner router entirely, so
    "/mcp" is the whole story.

    Calling this also forces lazy session-manager creation, so `session_manager`
    is available to the caller's lifespan.
    """
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from mcp.server.fastmcp.server import StreamableHTTPASGIApp

    mcp.streamable_http_app()          # side effect: builds the session manager
    assert isinstance(mcp.session_manager, StreamableHTTPSessionManager)
    return StreamableHTTPASGIApp(mcp.session_manager)


class MCPEndpoint:
    """A stable ASGI object whose backing MCP server is swapped by the lifespan.

    `StreamableHTTPSessionManager.run()` refuses a second call on the same
    instance, so a server built at import time can be started exactly once for the
    life of the process. That is invisible until something enters the lifespan
    twice — a test client per test, `uvicorn --reload`, any in-process restart —
    and then every later entry dies at startup rather than at the call site.

    So the route is registered against THIS object once, and the lifespan builds a
    fresh FastMCP each time it opens. Before startup and after shutdown there is
    nothing to serve, and saying so beats a traceback from inside the SDK.
    """

    def __init__(self) -> None:
        self.app = None
        self.server: FastMCP | None = None

    def open(self) -> FastMCP:
        """Build a fresh server + ASGI app. The caller runs its session manager."""
        # Tool bodies run in a worker thread (see `_wrap`), so a background driver
        # started from one cannot use `get_running_loop`. Capture the loop here,
        # where the lifespan IS the loop.
        import asyncio
        global _MAIN_LOOP
        try:
            _MAIN_LOOP = asyncio.get_running_loop()
        except RuntimeError:
            _MAIN_LOOP = None
        self.server = build_mcp()
        self.app = mcp_asgi_app(self.server)
        return self.server

    def close(self) -> None:
        self.app = None
        self.server = None

    async def __call__(self, scope, receive, send):
        if self.app is None:
            from starlette.responses import JSONResponse
            await JSONResponse(
                {"jsonrpc": "2.0", "id": None,
                 "error": {"code": -32603,
                           "message": "AItelier MCP endpoint is not running"}},
                status_code=503)(scope, receive, send)
            return
        await self.app(scope, receive, send)


# ── Move: export / import ────────────────────────────────────────────────────

def _register_bundle_tools(tool):

    @tool("export_pipeline", "read",
          "Export a generated pipeline as one self-contained JSON bundle: its graph, "
          "its roles WITH their prompts, and any custom tool it needs. Hand the "
          "bundle to another AItelier and `import_pipeline` reproduces it there. "
          "Only generated (gen_*) pipelines can be exported — a built-in config "
          "travels with the repo.")
    def export_pipeline(config: str) -> dict:
        from core.pipeline_bundle import BundleError, export_pipeline as _exp
        # Resolve first, so a registered-but-unexportable config is told what it IS
        # and where to read it — the bundle layer only knows "no <config>.yaml",
        # which for dpe_game reads as "unknown pipeline".
        src = _config_source(config)
        if src is None:
            return {"error": f"no pipeline '{config}' — call list_pipelines"}
        if src["kind"] != "generated":
            return {"error": f"'{config}' cannot be exported: a bundle is only "
                             f"self-contained for a gen_* pipeline. This one is "
                             f"{src['origin']} — it travels with the repo. Read it "
                             f"with get_pipeline / get_template instead."}
        try:
            bundle = _exp(config)
        except BundleError as e:
            return {"error": str(e)}
        note = None
        if bundle.get("unresolved_tools"):
            note = ("This pipeline names tools that are neither built-in nor "
                    "generated here, so they are NOT in the bundle and it will not "
                    "run after import until they exist: "
                    + ", ".join(bundle["unresolved_tools"]))
        return {"bundle": bundle, "warning": note}

    @tool("import_pipeline", "write",
          "Install a pipeline from an `export_pipeline` bundle: writes the graph, "
          "roles and any bundled tools, then registers it live. `name` renames it on "
          "the way in. Refuses if a bundled tool already exists here with different "
          "content (tools are global, so overwriting would change them for every "
          "pipeline using them) — pass overwrite_tools=true only if you mean it.")
    def import_pipeline(bundle: dict, name: str = "",
                        overwrite_tools: bool = False) -> dict:
        from api.dependencies import get_config_registry, get_skillflow
        from core.pipeline_bundle import BundleError, import_pipeline as _imp
        # Accept the whole export result or just its `bundle` field — a model that
        # round-trips export→import will hand back whichever it kept.
        if isinstance(bundle, dict) and _is_wrapped_bundle(bundle):
            bundle = bundle["bundle"]
        try:
            result = _imp(get_skillflow(), get_config_registry(), bundle,
                          name=name or None, overwrite_tools=bool(overwrite_tools))
        except BundleError as e:
            return {"error": str(e)}
        # Same stale-cache hole as edit_tool: overwriting a tool the loader has
        # already invoked leaves the old function live until restart, so the
        # imported pipeline would run the PREVIOUS machine's implementation.
        if result.get("tools_installed"):
            _reload_tools()
        return result


def _is_wrapped_bundle(obj: dict) -> bool:
    """True for `export_pipeline`'s envelope rather than a bare bundle.

    export returns {bundle, warning}; a model that round-trips export→import hands
    back whichever half it kept. Rejecting the envelope teaches nothing — accept
    both, but only when the outer object is not itself a bundle.
    """
    from core.pipeline_bundle import BUNDLE_KEY
    return "bundle" in obj and BUNDLE_KEY not in obj


# ── Edit: the four artefact kinds ────────────────────────────────────────────

def _register_edit_tools(tool):

    @tool("edit_pipeline", "write",
          "Replace a generated pipeline's graph YAML and re-register it. The new "
          "graph is parsed and validated BEFORE anything is written; on failure "
          "nothing changes and the error explains why. Read it with get_pipeline "
          "first — this replaces the whole file, it does not patch.")
    def edit_pipeline(config: str, graph_yaml: str) -> dict:
        import yaml as _yaml
        from api.dependencies import get_config_registry, get_skillflow
        from core import pipeline_registry as pr
        bad = bad_config_name(config)
        if bad:
            return {"error": bad}
        path = pr.generated_configs_dir() / f"{config}.yaml"
        if not path.is_file():
            return {"error": f"'{config}' is not a generated pipeline — only "
                             f"gen_* pipelines are editable here."}
        try:
            from skillflow.graph import PipelineGraph
            data = _yaml.safe_load(graph_yaml)
            if not isinstance(data, dict) or not data.get("steps"):
                return {"error": "graph_yaml is not a pipeline (no steps)"}
            data["name"] = config          # the file name is the identity
            PipelineGraph._from_dict(data)
        except Exception as e:
            return {"error": f"rejected, nothing written: {e}"}

        prior = path.read_text(encoding="utf-8")
        path.write_text(_yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
                        encoding="utf-8")
        try:
            result = pr.reload_generated_pipeline(
                get_skillflow(), get_config_registry(), config)
        except Exception as e:
            path.write_text(prior, encoding="utf-8")   # put it back
            return {"error": f"reload failed, reverted: {e}"}
        if isinstance(result, dict) and result.get("error"):
            path.write_text(prior, encoding="utf-8")
            return {"error": f"reload failed, reverted: {result['error']}"}
        return {"config": config, "reloaded": True, "path": str(path)}

    def _write_roles(config: str, roles: dict) -> dict:
        import json as _json
        from api.dependencies import get_config_registry, get_skillflow
        from core import pipeline_registry as pr
        bad = bad_config_name(config)
        if bad:
            return {"error": bad}
        rj = pr.generated_configs_dir() / f"{config}.roles.json"
        prior = rj.read_text(encoding="utf-8") if rj.exists() else None
        rj.write_text(_json.dumps(roles, ensure_ascii=False, indent=2),
                      encoding="utf-8")
        def _revert():
            if prior is None:
                rj.unlink(missing_ok=True)
            else:
                rj.write_text(prior, encoding="utf-8")

        # `reload_generated_pipeline` NEVER raises — its whole body is wrapped and it
        # RETURNS {"error": ...}. Catching only an exception meant the revert was
        # dead code and edit_role/edit_template reported success on a failed reload:
        # roles.json held the new prompt, the live registry held the old one, and
        # nothing said so. Worse for a built-in config, where reload always fails
        # ("no persisted config"): the stray roles.json then SHADOWS the built-in
        # agent_configs yaml in every later _load_roles read, so the edit looked
        # applied forever while every run used the untouched original.
        try:
            result = pr.reload_generated_pipeline(
                get_skillflow(), get_config_registry(), config)
        except Exception as e:
            _revert()
            return {"error": f"reload failed, reverted: {e}"}
        if isinstance(result, dict) and result.get("error"):
            _revert()
            return {"error": f"reload failed, reverted: {result['error']}"}
        return {}

    @tool("edit_role", "write",
          "Change a role's model, tools, temperature or thinking settings. To change "
          "its PROMPT use edit_template — the prompt is the role's template. Only the "
          "fields you pass are changed.")
    def edit_role(config: str, role: str, model: str = "", tools: list = None,
                  temperature: float = None, thinking: dict = None) -> dict:
        roles, err = _roles_or_error(config)
        if err:
            return err
        # Separate name: assigning back to `role` made the failure message
        # interpolate the RESOLUTION (None) instead of what the caller typed —
        # "no role like 'None'" — destroying the input exactly where it is the
        # whole diagnostic. get_template/edit_template already do it this way.
        resolved = _resolve_role(config, role, roles)
        if resolved is None:
            return {"error": f"no role like '{role}' in '{config}'. "
                             f"Have: {', '.join(sorted(roles))}"}
        role = resolved
        cfg = dict(roles.get(role) or {})
        if model:
            cfg["model"] = model
        if tools is not None:
            cfg["tools"] = list(tools)
        if temperature is not None:
            cfg["temperature"] = float(temperature)
        if thinking is not None:
            cfg["thinking"] = thinking
        roles[role] = cfg
        err = _write_roles(config, roles)
        return err or {"config": config, "role": role, "value": cfg}

    @tool("list_templates", "read",
          "List a pipeline's templates with the real size of each. A prompt is "
          "stored one of two ways and `source` says which per role: INLINE on the "
          "role (`system_prompt`, how a generated pipeline does it) or in a "
          "templates/*.md file the role names (`template: step1_5_researcher.md`, "
          "how every built-in role does it).")
    def list_templates(config: str) -> dict:
        roles, err = _roles_or_error(config)
        if err:
            return err
        out = []
        for r, c in sorted(roles.items()):
            text, source = _role_prompt(c)
            out.append({"role": r, "chars": len(text), "source": source})
        return {"config": config, "templates": out}

    @tool("get_template", "read",
          "Read one role's prompt template in full, wherever it is kept — inline on "
          "the role, or in the templates/*.md file the role names. `source` says "
          "which, so a caller can tell an empty prompt from one it failed to find.")
    def get_template(config: str, role: str) -> dict:
        roles, err = _roles_or_error(config)
        if err:
            return err
        resolved = _resolve_role(config, role, roles)
        if resolved is None:
            return {"error": f"no role like '{role}' in '{config}'. "
                             f"Have: {', '.join(sorted(roles))}"}
        text, source = _role_prompt(roles[resolved])
        return {"config": config, "role": resolved, "template": text,
                "chars": len(text), "source": source}

    @tool("edit_template", "write",
          "Replace one role's prompt template. This is the main way to change what a "
          "generated pipeline's agent actually does. Replaces the whole prompt — read "
          "it with get_template first. Only a GENERATED pipeline can be changed "
          "here: a built-in role's prompt is a repo file (get_template's `source` "
          "names it), so changing it is a repo edit, not an API call.")
    def edit_template(config: str, role: str, template: str) -> dict:
        if not (template or "").strip():
            return {"error": "template is empty — a role with no prompt falls back "
                             "to a generic one, which is almost never what you want"}
        roles, err = _roles_or_error(config)
        if err:
            return err
        resolved = _resolve_role(config, role, roles)
        if resolved is None:
            return {"error": f"no role like '{role}' in '{config}'. "
                             f"Have: {', '.join(sorted(roles))}"}
        cfg = dict(roles.get(resolved) or {})
        cfg["system_prompt"] = template
        roles[resolved] = cfg
        err = _write_roles(config, roles)
        return err or {"config": config, "role": resolved, "chars": len(template)}

    @tool("list_tools", "read",
          "List every tool the host can run, marking which are GENERATED (editable "
          "here, live in ~/.AItelier/tools) versus built-in (shipped with AItelier or "
          "skillflow, not editable).")
    def list_tools(pipeline: str = "") -> dict:
        from api.dependencies import get_tool_loader
        from core.pipeline_bundle import _generated_tools_dir, referenced_tool_names
        try:
            names = sorted(get_tool_loader().list_tools())
        except Exception as e:
            return {"error": f"tool registry unavailable: {e}"}
        gen = {p.name for p in _generated_tools_dir().iterdir() if p.is_dir()} \
            if _generated_tools_dir().is_dir() else set()
        wanted = None
        if pipeline:
            import yaml as _yaml
            from core import pipeline_registry as pr
            bad = bad_config_name(pipeline)
            if bad:
                return {"error": bad}
            gp = pr.generated_configs_dir() / f"{pipeline}.yaml"
            if not gp.is_file():
                return {"error": f"no generated pipeline '{pipeline}'"}
            wanted = referenced_tool_names(_yaml.safe_load(gp.read_text(encoding="utf-8")) or {},
                                           _load_roles(pipeline) or {})
        return {"tools": [{"name": n, "generated": n in gen}
                          for n in names if wanted is None or n in wanted]}

    @tool("get_tool", "read",
          "Read a generated tool's source (tool.yaml + impl.py). Built-in tools are "
          "not readable here — they live in the AItelier or skillflow repo.")
    def get_tool(name: str) -> dict:
        from core.pipeline_bundle import _read_tool
        bad = bad_tool_name(name)
        if bad:
            return {"error": bad}
        files = _read_tool(name)
        if files is None:
            return {"error": f"'{name}' is not a generated tool (nothing under the "
                             f"generated-tools directory). Built-in tools live in the "
                             f"repo."}
        return {"tool": name, "files": files}

    @tool("edit_tool", "write",
          "Replace a generated tool's files. The implementation must import and "
          "expose a callable named after the tool, checked BEFORE anything is "
          "written. Creates the tool when it does not exist yet.")
    def edit_tool(name: str, impl_py: str = "", tool_yaml: str = "") -> dict:
        from core.pipeline_bundle import _TOOL_FILES, _generated_tools_dir, _read_tool
        bad = bad_tool_name(name)
        if bad:
            return {"error": bad}
        current = _read_tool(name) or {}
        files = dict(current)
        if impl_py:
            files["impl.py"] = impl_py
        if tool_yaml:
            files["tool.yaml"] = tool_yaml
        if "impl.py" not in files or "tool.yaml" not in files:
            return {"error": "a tool needs both impl.py and tool_yaml; this one would "
                             f"have {sorted(files) or 'nothing'}"}
        err = tool_source_error(name, files["impl.py"])
        if err:
            return {"error": f"rejected, nothing written: {err}"}
        d = _generated_tools_dir() / name
        d.mkdir(parents=True, exist_ok=True)
        for fname, text in files.items():
            if fname in _TOOL_FILES:
                (d / fname).write_text(text, encoding="utf-8")
        _reload_tools()
        return {"tool": name, "created": not current, "path": str(d)}


def _templates_dir():
    return _repo_configs_dir().parent / "templates"


def _role_prompt(cfg: dict) -> tuple[str, str]:
    """One role's actual prompt, and where it came from.

    There are two shapes and the read path only ever knew one. A GENERATED
    pipeline keeps the prompt inline as `system_prompt`; a BUILT-IN role NAMES a
    file — `researcher` carries `template: step1_5_researcher.md` and the prompt
    lives in `templates/`. Reading `system_prompt` alone reported a 0-char prompt,
    with no error, for every role of 10 of the 12 built-in configs; only
    coding_task looked right, and only because its prompts really are inline. The
    write path already knew built-ins were a different animal (see _write_roles);
    the read path did not.

    Returns ("", "<why>") rather than raising: one unreadable role must not take
    down a list_templates over the other twelve.
    """
    cfg = cfg or {}
    inline = cfg.get("system_prompt") or ""
    if inline:
        return inline, "inline (system_prompt)"
    name = (cfg.get("template") or "").strip()
    if not name:
        return "", "none — this role declares neither system_prompt nor template"
    # The name comes from repo YAML, not from the caller, but it is joined onto a
    # path: refuse anything that is not a plain file name.
    p = _templates_dir() / name
    if "/" in name or "\\" in name or not p.is_file():
        return "", f"templates/{name} (NOT FOUND)"
    try:
        return p.read_text(encoding="utf-8"), f"templates/{name}"
    except OSError as e:
        return "", f"templates/{name} (unreadable: {e})"


def _resolve_role(config: str, role: str, roles: dict) -> str | None:
    """Accept a role by its bare name or its namespaced one.

    Roles are stored as `<config>__<role>`, but that prefix is host bookkeeping —
    an agent reading the graph or a human reading a report says `author`. Making
    the caller guess which spelling this tool wants is a needless failure.
    """
    if role in roles:
        return role
    from core.pipeline_registry import _ROLE_SEP
    qualified = f"{config}{_ROLE_SEP}{role}"
    if qualified in roles:
        return qualified
    matches = [r for r in roles if r.rsplit(_ROLE_SEP, 1)[-1] == role]
    return matches[0] if len(matches) == 1 else None


def _reload_tools() -> None:
    """Make the loader forget its cached tools so an edit takes effect now.

    `ToolLoader` has no reload/rescan/refresh — the earlier probe for one of those
    was a silent no-op — and `load_fn` returns `self._cache[name][1]` forever once
    a tool has been invoked. So an `edit_tool` that reported success left the OLD
    implementation live until the process restarted: the agent re-ran the pipeline
    to verify its fix and watched the code it had just replaced run again.
    `add_tools_dir` is the loader's own cache-clearing entry point and is
    idempotent about the directory, so it is the public way to say "re-read".
    """
    from core import datadir
    from api.dependencies import get_tool_loader
    get_tool_loader().add_tools_dir(datadir.tools_dir())


# ── Run ──────────────────────────────────────────────────────────────────────

def _register_run_tools(tool):

    @tool("run_pipeline", "write",
          "Start a run and return its run_id immediately. NOTE THE DEFAULT: "
          "checkpoints='auto' ANSWERS every human-approval gate for you, which is "
          "what a test-drive wants but BYPASSES review gates a production run is "
          "meant to stop at (a design review, a chapter sign-off). Pass "
          "checkpoints='ask' to have the run pause instead and answer each one "
          "deliberately with answer_checkpoint — that is the mode to use when a "
          "person is meant to look. Runs are LONG so this never blocks: use "
          "wait_for_run, then get_run_summary to see what happened. `seed_text` is "
          "the input; list_pipelines' input_hint says what each pipeline expects. "
          "`against_project` runs it against an existing project's repo.")
    def run_pipeline(config: str, seed_text: str = "", name: str = "",
                     against_project: str = "", checkpoints: str = "auto") -> dict:
        from api.dependencies import (db_instance, get_config_registry,
                                      get_workspace_manager)
        from core.run_launcher import generate_run_id, start_config_run
        if checkpoints not in ("auto", "ask"):
            # Checked before anything starts: validating after start_config_run
            # left an orphan run behind for a typo.
            return {"error": f"invalid checkpoints={checkpoints!r} — must be 'auto' "
                             f"(answer them for you) or 'ask' (pause for "
                             f"answer_checkpoint)"}
        manifest = get_config_registry().get(config)
        if not manifest:
            avail = ", ".join(sorted(m.config_name
                                     for m in get_config_registry().list()))
            return {"error": f"unknown pipeline '{config}'. Available: {avail}"}
        ws = get_workspace_manager()
        repo_type, repo_path = "new", None
        if against_project:
            proj = db_instance.get_project(against_project)
            if not proj:
                return {"error": f"against_project '{against_project}' not found"}
            repo_path = proj.get("repo_path")
            if not repo_path:
                try:
                    repo_path = str(ws.get_code_path(against_project))
                except Exception:
                    repo_path = None
            if not repo_path:
                return {"error": f"no repo_path for project '{against_project}'"}
            repo_type = "existing"
        pid = generate_run_id(config)
        result = start_config_run(db_instance, ws, config, pid,
                                  seed_text=seed_text or None,
                                  name=name or config, repo_type=repo_type,
                                  repo_path=repo_path)
        if result.get("status") == "error":
            return {"error": result.get("message")}
        run_id = result.get("run_id")
        owned = bool(getattr(manifest, "scheduler_owned", False))
        # Attach a driver. For a scheduler-owned config the poller advances the run
        # and this only answers checkpoints; for a butler-owned one (code_review,
        # coding_task, …) NOTHING else would advance it at all — that is the bug
        # this fixes: such a run used to sit at `running` forever while
        # wait_for_run truthfully reported "still running".
        _start_driver(run_id, scheduler_owned=owned,
                      auto_approve=(checkpoints == "auto"))
        return {"run_id": run_id, "project_id": pid, "config": config,
                "scheduler_owned": owned, "checkpoints": checkpoints,
                "note": ("Started. wait_for_run(run_id), then get_run_summary(run_id). "
                         + ("Checkpoints are being answered automatically."
                            if checkpoints == "auto" else
                            "It will PAUSE at each checkpoint for answer_checkpoint."))}

    @tool("answer_checkpoint", "write",
          "Approve or reject a run paused at a checkpoint. Rejecting sends the work "
          "back with your feedback, which is how a reviewer asks for a change rather "
          "than stopping the run. Only a run whose status is 'paused' has anything "
          "to answer — check with wait_for_run or get_run_status first.")
    def answer_checkpoint(run_id: str, decision: str = "approve",
                          feedback: str = "") -> dict:
        from api.dependencies import get_skillflow
        sf = get_skillflow()
        run = sf.get_run(run_id)
        if not run:
            return {"error": f"no run '{run_id}'"}
        if run.get("status") != "paused":
            return {"error": f"run '{run_id}' is {run.get('status')}, not paused — "
                             f"there is no checkpoint to answer"}
        decision = (decision or "").strip().lower()
        if decision not in ("approve", "reject"):
            return {"error": f"decision must be 'approve' or 'reject', not "
                             f"{decision!r}"}
        if decision == "reject" and not (feedback or "").strip():
            return {"error": "a rejection needs feedback — the step it goes back to "
                             "has nothing to act on otherwise"}
        try:
            if decision == "approve":
                sf.approve_checkpoint(run_id)
            else:
                # `reject_checkpoint` needs the STEP the run is paused at, and
                # resolving that from a run is exactly what the web/CLI path
                # already does — reuse its resolver instead of re-deriving it,
                # so a rejection over MCP and one from the dashboard cannot
                # disagree about which checkpoint they answered.
                from api.meta_routers import _get_checkpoint_info
                step_id, _label, _rid, _graph = _get_checkpoint_info(
                    run.get("project_id") or "")
                if not step_id:
                    return {"error": f"run '{run_id}' is paused but no checkpoint "
                                     f"step could be resolved for it"}
                sf.reject_checkpoint(run_id, step_id, feedback)
        except Exception as e:
            return {"error": f"{decision} failed: {e}"}
        # Answering releases the run; whoever was driving it keeps going.
        after = sf.get_run(run_id) or {}
        return {"run_id": run_id, "decision": decision,
                "status": after.get("status"), "current_node": after.get("current_node")}

    @tool("get_run_summary", "read",
          "What a run actually did, small enough to read: per-step status, the FIRST "
          "failure with its error, and the final outputs truncated. This is what to "
          "read after wait_for_run — `get_run_status` gives only a status and a node "
          "name, which names neither what broke nor why. Use it to decide whether to "
          "fix the pipeline (edit_template / edit_pipeline / edit_tool) and run it "
          "again.")
    def get_run_summary(run_id: str) -> dict:
        from api.dependencies import (get_config_registry, get_skillflow,
                                      get_workspace_manager)
        from core.run_driver import summarise_run
        return summarise_run(get_skillflow(), get_workspace_manager(),
                             get_config_registry(), run_id)

    @tool("get_run_status", "read",
          "Current status of a run: running / paused-at-checkpoint / completed / "
          "failed, with the step it is on. A paused run needs answer_checkpoint "
          "before it moves.")
    def get_run_status(run_id: str) -> dict:
        from api.dependencies import get_skillflow
        run = get_skillflow().get_run(run_id)
        if not run:
            return {"error": f"no run '{run_id}'"}
        return {"run_id": run_id, "status": run.get("status"),
                "config": run.get("graph_name"), "project_id": run.get("project_id"),
                "current_node": run.get("current_node"),
                "paused": run.get("status") == "paused"}


# Terminal + paused are both "stop waiting": a caller that watches only for the
# happy ending sits through a failure, and silence is indistinguishable from
# still-running. Same reasoning as debugctl's `await`.
_WAIT_EVENTS = frozenset({"checkpoint_paused", "run_completed", "run_failed",
                          "pipeline_failed"})
_SETTLED_STATUSES = frozenset({"paused", "completed", "failed"})

# dsh-mcp-client's DEFAULT_TOOL_CALL_TIMEOUT_MS is 60s, and other MCP hosts are in
# the same range. A wait longer than the CLIENT's timeout does not wait longer —
# the client hangs up and the model sees a transport error instead of "still
# running", which reads as a broken tool rather than a long job. So the default
# sits under that, and the ceiling is documented rather than silently exceeded.
_WAIT_DEFAULT_S = 45
_WAIT_MAX_S = 3600


def _register_wait_tool(tool):

    @tool("wait_for_run", "read",
          "Block until a run pauses at a checkpoint or finishes, then return why. "
          "This is the tool to use after run_pipeline — it is push-based, so it "
          "returns the instant the run settles instead of polling. It waits at most "
          "`timeout_seconds` (default 45) and then returns status 'waiting' with "
          "timed_out=true; that is not a failure, call it again. Waiting longer than "
          "your MCP client's per-call timeout (60s by default) does NOT work — the "
          "client hangs up first. Raise toolCallTimeoutMs before raising this. "
          "timeout_seconds=0 checks and returns without waiting.")
    async def wait_for_run(run_id: str, timeout_seconds: int = None) -> dict:
        import asyncio
        from api.dependencies import get_skillflow

        sf = get_skillflow()
        # `or` would make 0 mean "the default", i.e. a caller asking for one look
        # and no wait would block for 45 seconds. 0 is a real request — check now,
        # return now — so only an ABSENT value means default.
        requested = _WAIT_DEFAULT_S if timeout_seconds is None else int(timeout_seconds)
        timeout = max(0, min(requested, _WAIT_MAX_S))

        settled = asyncio.Event()
        seen: dict = {}

        async def _on_event(notification):
            if notification.event_type not in _WAIT_EVENTS:
                return
            rid = notification.run_id or (notification.payload or {}).get("run_id")
            # `if rid and rid != run_id` short-circuits when rid is missing, so an
            # event nobody could attribute woke EVERY concurrent waiter — each then
            # reports a settle-shaped answer for a run that did not settle. No
            # current emitter drops the run_id (pipeline_failed has none at all yet),
            # so this is the guard matching its own stated intent before something
            # grows one.
            if not rid or rid != run_id:
                return
            seen.update({"event": notification.event_type,
                         "payload": notification.payload or {}})
            settled.set()

        # SUBSCRIBE BEFORE READING THE STATUS. The other order has a hole exactly
        # one status-read wide: the run settles in the gap, its event fires with
        # nobody listening, and the wait then blocks for the full timeout on a run
        # that already stopped — reported as "still running", which is a lie.
        sf.notifications.subscribe(_on_event)
        try:
            run = sf.get_run(run_id)
            if not run:
                return {"error": f"no run '{run_id}'"}
            if run.get("status") in _SETTLED_STATUSES:
                return _wait_result(run_id, run, timed_out=False, event=None)
            if timeout == 0:
                timed_out = True          # asked for a look, not a wait
            else:
                try:
                    await asyncio.wait_for(settled.wait(), timeout)
                    timed_out = False
                except asyncio.TimeoutError:
                    timed_out = True
        finally:
            try:
                sf.notifications.unsubscribe(_on_event)
            except ValueError:
                pass          # already gone; nothing to undo

        # Re-read rather than trusting the event payload: the DB is what every
        # other reader will see, and an event describes one moment.
        return _wait_result(run_id, sf.get_run(run_id) or {}, timed_out,
                            seen.get("event"))


def _wait_result(run_id: str, run: dict, timed_out: bool, event: str | None) -> dict:
    status = run.get("status")
    out = {
        "run_id": run_id,
        "status": "waiting" if (timed_out and status not in _SETTLED_STATUSES)
                  else status,
        "run_status": status,
        "config": run.get("graph_name"),
        "project_id": run.get("project_id"),
        "current_node": run.get("current_node"),
        "timed_out": bool(timed_out),
        "settled_on": event,
    }
    if status == "paused":
        # It CAN: `answer_checkpoint` is registered on this same endpoint, and
        # run_pipeline's own note already tells the caller a run pauses "for
        # answer_checkpoint". Sending them to the UI instead denied a capability
        # they were holding — measured against a live checkpoint approved through
        # this endpoint minutes after the message claimed it was impossible.
        out["next"] = ("Paused at a checkpoint. Answer it here: "
                       "answer_checkpoint(run_id, decision='approve'|'reject', "
                       "feedback=…). The AItelier UI is the other way in, not the "
                       "only one.")
    elif status == "failed":
        out["error_reason"] = run.get("error_reason") or ""
    elif timed_out:
        out["next"] = ("Still running — this is not an error. Call wait_for_run "
                       "again to keep waiting.")
    return out


def _start_driver(run_id: str, *, scheduler_owned: bool, auto_approve: bool) -> bool:
    """Attach a background driver to a run just started over MCP.

    Scheduled onto the endpoint's own loop rather than awaited: `run_pipeline` must
    return the run_id immediately (a run takes minutes to hours), and the tool body
    is executing in a worker thread by then, so `get_running_loop` is not available
    here — `_MAIN_LOOP` is captured at lifespan open for exactly this.

    The task is kept in `_DRIVERS` because asyncio holds only a WEAK reference to a
    bare task: dropped, it would be collected mid-run and the pipeline would stop
    advancing with nothing logged and nothing to see.
    """
    import asyncio

    from api.dependencies import db_instance, get_skillflow, get_workspace_manager
    from core.run_driver import drive_run

    if not run_id or _MAIN_LOOP is None:
        return False

    async def _run():
        try:
            await drive_run(get_skillflow(), db_instance, get_workspace_manager(),
                            run_id, scheduler_owned=scheduler_owned,
                            auto_approve=auto_approve)
        except Exception:
            import logging
            logging.getLogger("aitelier.mcp").warning(
                "background driver for run %s died", run_id, exc_info=True)
        finally:
            # Whoever drove it, the project row must reflect the outcome or the
            # dashboard shows a finished run as still starting up.
            try:
                from core.scheduler import _sync_project_status_to_db
                run = get_skillflow().get_run(run_id) or {}
                if run.get("project_id"):
                    _sync_project_status_to_db(run["project_id"])
            except Exception:
                pass

    fut = asyncio.run_coroutine_threadsafe(_run(), _MAIN_LOOP)
    _DRIVERS.add(fut)
    fut.add_done_callback(_DRIVERS.discard)
    return True


# ── Trace: why did it do that ────────────────────────────────────────────────

def _register_trace_tools(tool):
    """The durable trace, in three shapes plus a way to find a run at all.

    A summary says a step failed; the trace says WHY — it holds every prompt,
    model response, tool call and review verdict. It is also far too large to
    return whole (1000+ rows for one DPE run), which is why this is list → search
    → read rather than one dump: find where to look, then read only that.
    """

    @tool("list_runs", "read",
          "Recent runs, newest first — the entry point when you hold no run_id. "
          "Filter by `config`, `status` ('running' / 'paused' / 'completed' / "
          "'failed') or `project_id`. A 'paused' run is waiting for "
          "answer_checkpoint.")
    def list_runs(config: str = "", status: str = "", project_id: str = "",
                  limit: int = 30) -> dict:
        from api.dependencies import get_skillflow
        from core.trace_reader import list_runs as _ls
        return _ls(get_skillflow(), config=config, status=status,
                   project_id=project_id, limit=limit)

    @tool("trace_list", "read",
          "Compact trace lines for a run (seq, step, category, one-line summary) — "
          "this is where a failed run's actual reason lives, which get_run_summary "
          "only points at. Use errors_only=true to go straight to what broke, then "
          "trace_read(seq) for the full payload. `run` accepts a run_id or a "
          "project_id.")
    def trace_list(run: str, step: str = "", category: str = "",
                   errors_only: bool = False, limit: int = 50,
                   order: str = "desc") -> dict:
        from api.dependencies import get_skillflow
        from core.trace_reader import trace_list as _tl
        return _tl(get_skillflow(), run, step=step, category=category,
                   errors_only=errors_only, limit=limit, order=order)

    @tool("trace_search", "read",
          "Substring search across a run's trace payloads — for when you know what "
          "went wrong but not which step did it (an error message, a file name, a "
          "phrase from a prompt). Returns the same compact lines as trace_list.")
    def trace_search(run: str, query: str, step: str = "", limit: int = 30) -> dict:
        from api.dependencies import get_skillflow
        from core.trace_reader import trace_search as _ts
        return _ts(get_skillflow(), run, query, step=step, limit=limit)

    @tool("trace_read", "read",
          "Full trace payloads for an explicit seq range (max 20 rows) — the actual "
          "prompt, the actual model response, the actual tool result. Get the seq "
          "from trace_list or trace_search first; this is deliberately not a way to "
          "dump a whole run.")
    def trace_read(run: str, seq: int, seq_end: int = None) -> dict:
        from api.dependencies import get_skillflow
        from core.trace_reader import trace_read as _tr
        return _tr(get_skillflow(), run, seq, seq_end)


# ── The rest of the drive / test / fix loop ──────────────────────────────────

def _register_lifecycle_tools(tool):
    """What the loop needs beyond run + watch + edit.

    Compared against the chat butler's own toolset, these are the ones that are
    load-bearing for generate → drive → observe → fix and had no MCP equivalent:
    a way to WRITE a pipeline in the first place, a way to STOP a bad drive, a way
    to RETIRE the ones the loop leaves behind, a way to see a middle step's output,
    and the skillflow spec — because the fix half means editing a graph, and an
    agent editing a graph without the schema is guessing.
    """

    @tool("generate_pipeline", "write",
          "Generate a NEW pipeline from a plain-language description by running "
          "AItelier's grounded generator (pipeline_forge): it surveys the real tool "
          "registry, designs the graph, builds any missing tools, and gates the "
          "result. Returns a run_id immediately — the generator PAUSES at a design "
          "review, so wait_for_run then answer_checkpoint. On completion the "
          "pipeline registers itself as `gen_<slug>` and list_pipelines shows it. "
          "Pass `edit_target=gen_<slug>` to re-generate an existing pipeline with a "
          "change instead of designing from scratch — that is the surgical path; "
          "edit_template / edit_pipeline are for small hand fixes.")
    def generate_pipeline(description: str, name: str = "",
                          edit_target: str = "") -> dict:
        from api.dependencies import db_instance, get_workspace_manager
        from core.run_launcher import generate_run_id, start_config_run
        description = (description or "").strip()
        if not description:
            return {"error": "description is required — what the pipeline should do "
                             "(or, with edit_target, the change to make)."}
        seed_inputs = None
        if edit_target:
            bad = bad_config_name(edit_target)
            if bad:
                return {"error": bad}
            seed_inputs = _forge_baseline(edit_target)
            if isinstance(seed_inputs, dict) and seed_inputs.get("error"):
                return seed_inputs
        pid = generate_run_id("forge-" + (name or description)[:40])
        result = start_config_run(db_instance, get_workspace_manager(),
                                  "pipeline_forge", pid, seed_text=description,
                                  seed_inputs=seed_inputs or None,
                                  name=name or f"forge {description[:40]}")
        if result.get("status") == "error":
            return {"error": result.get("message")}
        return {"run_id": result.get("run_id"), "project_id": pid,
                "config": "pipeline_forge", "edit_target": edit_target or None,
                "note": "The generator is scheduler-driven. wait_for_run(run_id) → "
                        "it pauses at the design review → answer_checkpoint → on "
                        "completion the new gen_<slug> appears in list_pipelines."}

    @tool("stop_pipeline", "write",
          "Cancel a run that is going nowhere — an unbounded loop, a wrong seed, a "
          "drive you no longer want. Marks it failed so the poller skips it. A run "
          "left spinning holds the scheduler's one-project-per-tick slot against "
          "every other project.")
    def stop_pipeline(run_id: str, reason: str = "") -> dict:
        from api.dependencies import get_skillflow
        from core.trace_reader import resolve_run_row
        sf = get_skillflow()
        run = resolve_run_row(sf, run_id)
        if not run:
            return {"error": f"no run '{run_id}'"}
        if run["status"] in ("completed", "failed"):
            return {"run_id": run["id"], "status": run["status"],
                    "message": f"already {run['status']}; nothing to stop"}
        try:
            sf.fail_run(run["id"], reason or "stopped via the MCP endpoint")
        except Exception as e:
            return {"error": f"could not stop it: {e}"}
        return {"run_id": run["id"], "status": "stopped"}

    @tool("archive_pipeline", "write",
          "Retire a generated pipeline. A generate → drive → fix loop leaves failed "
          "attempts behind, and deleting the files alone is NOT enough — the graph "
          "row keeps it runnable, so an archived-by-hand pipeline comes back as a "
          "zombie. This moves the files aside and records the name so the boot scan "
          "skips it; `purge=true` deletes the graph row too and cannot be undone.")
    def archive_pipeline(config: str, purge: bool = False) -> dict:
        from api.dependencies import get_config_registry, get_skillflow
        from core.pipeline_registry import archive_generated_pipeline
        bad = bad_config_name(config)
        if bad:
            return {"error": bad}
        try:
            return archive_generated_pipeline(get_skillflow(), get_config_registry(),
                                              config, purge=bool(purge))
        except Exception as e:
            return {"error": f"archive failed: {e}"}

    @tool("get_step_output", "read",
          "The files ONE step produced, in full. get_run_summary returns only the "
          "final outputs, truncated — when a middle step is the suspect, this is how "
          "to read what it actually wrote. `run` accepts a run_id or a project_id.")
    def get_step_output(run: str, step: str) -> dict:
        from api.dependencies import get_skillflow, get_workspace_manager
        from core.trace_reader import resolve_run_ref
        sf = get_skillflow()
        row, resolved = resolve_run_ref(sf, run)
        if not row:
            return {"error": f"no run '{run}'"}
        if bad_config_name(step):
            return {"error": f"invalid step id {step!r}"}
        # Which run a project id landed on, and what else it could have landed on.
        head = {"run_id": row["id"], "config": row.get("graph_name"), "step": step}
        if resolved:
            head["resolved"] = resolved
        try:
            d = get_workspace_manager().get_final_path(
                row.get("project_id") or "", step, row.get("graph_name") or "")
        except Exception as e:
            return {**head, "error": f"could not resolve step '{step}': {e}"}
        if not d.exists():
            return {**head, "error": _no_step_output_reason(sf, row, step)}
        files = {}
        for item in sorted(d.rglob("*")):
            if item.is_file() and item.name != "_snapshot.json":
                try:
                    files[str(item.relative_to(d))] = item.read_text(
                        encoding="utf-8", errors="replace")[:20000]
                except Exception:
                    pass
        return {**head, "files": files}

    # The skillflow docs tools, with their REAL parameters spelled out. Wrapping
    # them as `**kwargs` published a schema with one required field called
    # `kwargs`, so every call failed validation before reaching the tool — the
    # model could see them and could not use them (caught live, not in tests:
    # nothing asserted the published SCHEMA was callable).
    _SPEC = "This is the authoritative spec for the graph YAML edit_pipeline " \
            "accepts — read it before inventing a field."

    @tool("skillflow_docs_list", "read",
          "List skillflow's own documentation pages and schema sources. " + _SPEC)
    def skillflow_docs_list() -> dict:
        return _native_docs("skillflow_docs_list")

    @tool("skillflow_docs_search", "read",
          "Search skillflow's docs AND its authoritative schema source (graph.py / "
          "core.py) for a term — the fastest way to settle what a graph field "
          "actually means. " + _SPEC)
    def skillflow_docs_search(query: str) -> dict:
        return _native_docs("skillflow_docs_search", query=query)

    @tool("skillflow_docs_read", "read",
          "Read one skillflow doc or schema source, line-numbered, optionally a "
          "line range. Pair it with skillflow_docs_search's hits. " + _SPEC)
    def skillflow_docs_read(topic: str, start_line: int = None,
                            end_line: int = None) -> dict:
        kw = {"topic": topic}
        if start_line is not None:
            kw["start_line"] = start_line
        if end_line is not None:
            kw["end_line"] = end_line
        return _native_docs("skillflow_docs_read", **kw)


def _no_step_output_reason(sf, row: dict, step: str) -> str:
    """Why a step directory is missing — three separate facts under one old message.

    "it may not have run yet" was asserted for `git_sync_pre` on a run whose own
    summary listed that step `completed`: it ran, and it writes no files. The same
    sentence also covered a step that is not in this config's graph at all. A
    caller cannot act on any of the three without being told which one it is.
    """
    try:
        steps = {s["step_id"]: s["status"] for s in sf.get_steps(row["id"])}
    except Exception:
        steps = {}
    config = row.get("graph_name") or "?"
    if steps and step not in steps:
        return (f"no step '{step}' in config '{config}' (run {row['id']}). "
                f"Steps: {', '.join(sorted(steps))}")
    status = steps.get(step)
    if status in (None, "pending"):
        return (f"step '{step}' has NOT run — status "
                f"{status or 'unknown (no step row)'} on run {row['id']}. "
                f"trace_list(run_id) shows what did.")
    return (f"step '{step}' RAN (status '{status}') and promoted no files — its "
            f"step directory does not exist. That is what a step writing nothing "
            f"into the step dir looks like; it is not missing output to hunt for. "
            f"trace_list(run_id, step='{step}') has what it did do.")


def _native_docs(name: str, **kwargs):
    """Call skillflow's OWN docs tool through the live loader.

    Not reimplemented: these read the engine's docs and schema source, so a copy
    would describe a skillflow other than the one actually running.
    """
    from api.dependencies import get_skillflow
    try:
        fn = get_skillflow()._tool_loader.load_fn(name)
    except Exception as e:
        return {"error": f"{name} is unavailable in this skillflow build: {e}"}
    try:
        return fn(**kwargs)
    except Exception as e:
        return {"error": f"{name} failed: {e}"}


def _forge_baseline(edit_target: str):
    """Seed the generator with an existing pipeline so it edits instead of designs.

    De-namespaces the role names first: the host re-applies the `<config>__` prefix
    exactly once at registration, so feeding namespaced names back in makes the
    emitter echo them and registration double-prefixes — `gen_x__gen_x__role` —
    which no longer matches the graph, and every step then silently falls back to
    a generic prompt. Same defence as the butler's own edit mode.
    """
    import json as _json

    import yaml as _yaml

    from core.pipeline_registry import _ROLE_SEP, generated_configs_dir
    gy = generated_configs_dir() / f"{edit_target}.yaml"
    if not gy.exists():
        return {"error": f"edit_target '{edit_target}' not found (no "
                         f"{edit_target}.yaml). Call list_pipelines."}
    prefix = edit_target + _ROLE_SEP
    graph = _yaml.safe_load(gy.read_text(encoding="utf-8")) or {}
    for step in graph.get("steps", []) if isinstance(graph, dict) else []:
        if isinstance(step, dict) and isinstance(step.get("agent_config"), str):
            if step["agent_config"].startswith(prefix):
                step["agent_config"] = step["agent_config"][len(prefix):]
    seeds = {"baseline_graph.yaml": _yaml.safe_dump(graph, sort_keys=False,
                                                    allow_unicode=True)}
    rj = gy.with_suffix(".roles.json")
    if rj.exists():
        try:
            roles = _json.loads(rj.read_text(encoding="utf-8")) or {}
            seeds["baseline_roles.json"] = _json.dumps(
                {(r[len(prefix):] if r.startswith(prefix) else r): v
                 for r, v in roles.items()}, ensure_ascii=False, indent=2)
        except Exception:
            pass
    return seeds
