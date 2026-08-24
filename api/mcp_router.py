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
        from core import pipeline_registry as pr
        bad = bad_config_name(config)
        if bad:
            return {"error": bad}
        path = pr.generated_configs_dir() / f"{config}.yaml"
        if not path.exists():
            builtin = _builtin_config_path(config)
            if builtin is None:
                return {"error": f"no pipeline '{config}' — call list_pipelines"}
            path = builtin
        import yaml
        text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(text) or {}
        steps = data.get("steps") or []
        return {
            "config": config,
            "path": str(path),
            "editable": not _is_builtin(path),
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
        roles = _load_roles(config)
        if roles is None:
            return {"error": f"no roles for '{config}' — call list_pipelines"}
        return {"config": config,
                "roles": [{"role": k,
                           "model": (v or {}).get("model"),
                           "template": (v or {}).get("template"),
                           "tools": (v or {}).get("tools") or []}
                          for k, v in sorted(roles.items())]}

    @tool("get_role", "read",
          "Read one role's full config, including its system prompt.")
    def get_role(config: str, role: str) -> dict:
        roles = _load_roles(config)
        if roles is None:
            return {"error": f"no roles for '{config}'"}
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
    # declared name — see _builtin_stem.
    stem = _builtin_stem(config)
    if not stem:
        return None
    ac = _repo_configs_dir().parent / "agent_configs" / f"{stem}.yaml"
    if ac.exists():
        return yaml.safe_load(ac.read_text(encoding="utf-8")) or {}
    return None


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
        roles = _load_roles(config)
        if roles is None:
            return {"error": f"no roles for '{config}'"}
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
          "List a pipeline's templates. A generated pipeline stores each role's "
          "prompt inline as that role's template — one template per agent role.")
    def list_templates(config: str) -> dict:
        roles = _load_roles(config)
        if roles is None:
            return {"error": f"no roles for '{config}'"}
        return {"config": config,
                "templates": [{"role": r, "chars": len((c or {}).get("system_prompt") or "")}
                              for r, c in sorted(roles.items())]}

    @tool("get_template", "read", "Read one role's prompt template in full.")
    def get_template(config: str, role: str) -> dict:
        roles = _load_roles(config)
        if roles is None:
            return {"error": f"no roles for '{config}'"}
        resolved = _resolve_role(config, role, roles)
        if resolved is None:
            return {"error": f"no role like '{role}' in '{config}'. "
                             f"Have: {', '.join(sorted(roles))}"}
        return {"config": config, "role": resolved,
                "template": (roles[resolved] or {}).get("system_prompt") or ""}

    @tool("edit_template", "write",
          "Replace one role's prompt template. This is the main way to change what a "
          "generated pipeline's agent actually does. Replaces the whole prompt — read "
          "it with get_template first.")
    def edit_template(config: str, role: str, template: str) -> dict:
        if not (template or "").strip():
            return {"error": "template is empty — a role with no prompt falls back "
                             "to a generic one, which is almost never what you want"}
        roles = _load_roles(config)
        if roles is None:
            return {"error": f"no roles for '{config}'"}
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
          "Start a run of any registered pipeline and return its run_id immediately. "
          "Runs are LONG and may pause for human approval, so this never blocks — "
          "poll get_run_status. `seed_text` is the input; list_pipelines' input_hint "
          "says what each pipeline expects. `against_project` runs it against an "
          "existing project's repo.")
    def run_pipeline(config: str, seed_text: str = "", name: str = "",
                     against_project: str = "") -> dict:
        from api.dependencies import (db_instance, get_config_registry,
                                      get_workspace_manager)
        from core.run_launcher import generate_run_id, start_config_run
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
        return {"run_id": result.get("run_id"), "project_id": pid, "config": config,
                "scheduler_owned": bool(getattr(manifest, "scheduler_owned", False)),
                "note": "Started. Poll get_run_status(run_id); a paused run is "
                        "waiting for answer_checkpoint."}

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
        out["next"] = ("Paused at a checkpoint. A person approves it in the "
                       "AItelier UI — this endpoint cannot.")
    elif status == "failed":
        out["error_reason"] = run.get("error_reason") or ""
    elif timed_out:
        out["next"] = ("Still running — this is not an error. Call wait_for_run "
                       "again to keep waiting.")
    return out
