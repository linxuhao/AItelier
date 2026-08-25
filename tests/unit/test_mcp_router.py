"""The MCP endpoint: does it answer, and is every tool's authority declared?

Two classes of test, and the split is the point.

1. END-TO-END over HTTP. Mounting a FastMCP sub-app has three independent ways to
   look correct and answer nothing: the parent never runs the mounted app's
   lifespan (its only job is `session_manager.run()`), the sub-app registers its
   own `/mcp` route so mounting at `/mcp` composes to `/mcp/mcp`, and the
   method-based `write_gate` would 403 every call. None of those show up in a unit
   call of the tool function. One real POST catches all three.

2. AUTHORITY. `_TOOL_KIND` has no default: a tool that forgets to declare itself
   must not become readable-by-anyone. `test_every_tool_declares_its_authority`
   is the check that keeps that true as tools are added.
"""

import json
import pathlib

import pytest

from api import mcp_router
from core import tool_guards
from api.mcp_router import ToolDenied, _TOOL_KIND, build_mcp

MCP_HEADERS = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}


def _rpc(client, method: str, params: dict | None = None, rpc_id: int = 1):
    body = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", json=body, headers=MCP_HEADERS)


def _initialized(client):
    """Complete the MCP handshake and return the client, ready for tool calls."""
    r = _rpc(client, "initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    })
    assert r.status_code == 200, r.text
    return client


# ── End-to-end: the endpoint actually answers ───────────────────────────────

def test_the_endpoint_completes_a_handshake_at_the_documented_url(client):
    r = _rpc(client, "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    })
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["result"]["serverInfo"]["name"] == "aitelier"


def test_the_url_is_exactly_slash_mcp_not_slash_mcp_slash_mcp(client):
    # The sub-app registers its own route; mounting at /mcp with the default
    # streamable_http_path would put the server at /mcp/mcp — a config that reads
    # correctly in cordis.yml and 404s in production.
    assert _rpc(client, "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"}}).status_code == 200
    r = client.post("/mcp/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                    headers=MCP_HEADERS)
    assert r.status_code != 200


def test_tools_list_reaches_the_model(client):
    _initialized(client)
    r = _rpc(client, "tools/list", {}, rpc_id=2)
    assert r.status_code == 200, r.text
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert "list_pipelines" in names
    # Every registered tool must be one we classified — the server and the
    # authority table cannot drift apart.
    assert names <= set(_TOOL_KIND), f"undeclared tools: {names - set(_TOOL_KIND)}"


def test_a_read_tool_answers_over_the_wire(client):
    _initialized(client)
    r = _rpc(client, "tools/call",
             {"name": "list_pipelines", "arguments": {}}, rpc_id=3)
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert result.get("isError") is not True, result
    payload = json.loads(result["content"][0]["text"])
    # Oracle is the registry itself, not a hardcoded name: the test data dir is
    # isolated, so which configs exist varies, and a name assertion would be
    # testing the fixture. Non-emptiness is asserted separately so "reported
    # nothing, registry had nothing" cannot pass as agreement.
    from api.dependencies import get_config_registry
    expected = {m.config_name for m in get_config_registry().list()}
    assert expected, "no configs registered — this test would prove nothing"
    assert {p["config"] for p in payload["pipelines"]} == expected


# ── Authority ────────────────────────────────────────────────────────────────

def test_every_tool_declares_its_authority():
    build_mcp()
    assert _TOOL_KIND, "no tools registered"
    for name, kind in _TOOL_KIND.items():
        assert kind in ("read", "write"), f"{name} declares {kind!r}"


def test_an_unknown_tool_is_never_treated_as_a_read():
    with pytest.raises(ToolDenied):
        mcp_router._authorize("no_such_tool", None)


def test_a_write_tool_with_no_identifiable_caller_is_denied(monkeypatch):
    monkeypatch.setitem(_TOOL_KIND, "_probe_write", "write")
    monkeypatch.setattr(mcp_router.authz, "gate_enabled", lambda: True)
    with pytest.raises(ToolDenied, match="no verifiable identity"):
        mcp_router._authorize("_probe_write", None)


def test_a_read_tool_needs_no_identity_even_with_the_gate_on(monkeypatch):
    monkeypatch.setattr(mcp_router.authz, "gate_enabled", lambda: True)
    mcp_router._authorize("list_pipelines", None)      # must not raise


def test_the_gate_being_off_does_not_open_a_read_only_hole(monkeypatch):
    """With the gate configured, a write tool is denied unless authz says yes —
    the same verdict write_gate would have reached for a POST."""
    monkeypatch.setitem(_TOOL_KIND, "_probe_write", "write")
    monkeypatch.setattr(mcp_router.authz, "gate_enabled", lambda: True)
    monkeypatch.setattr(mcp_router.authz, "request_can_write", lambda r: False)
    monkeypatch.setattr(mcp_router.authz, "write_denial_reason",
                        lambda r: "write_denied_not_a_writer")
    monkeypatch.setattr(mcp_router, "_request_from", lambda ctx: object())
    with pytest.raises(ToolDenied):
        mcp_router._authorize("_probe_write", object())


# ── The whole surface, over the wire ─────────────────────────────────────────

@pytest.fixture
def gate_off(monkeypatch):
    """Pin the gate OFF explicitly.

    `authz.gate_enabled()` reads the ambient Cloudflare config, so whether a write
    tool is reachable in a test depended on the developer's .env. Deciding it here
    makes each test say which world it is testing.
    """
    monkeypatch.setattr(mcp_router.authz, "gate_enabled", lambda: False)


@pytest.fixture
def gate_on_authorized(monkeypatch):
    """Gate ON, caller authorized — the path that proves Context injection works."""
    monkeypatch.setattr(mcp_router.authz, "gate_enabled", lambda: True)
    monkeypatch.setattr(mcp_router.authz, "request_can_write", lambda r: True)


def test_an_authorized_write_reaches_the_tool_when_the_gate_is_on(
        client, gate_on_authorized):
    """The regression test for Context injection.

    With the gate on, `_authorize` needs the live request. When FastMCP was not
    injecting a Context, this call came back `denied: … no verifiable identity`
    while every gate-off test stayed green — the endpoint would have shipped
    write-dead and looked fine.
    """
    _initialized(client)
    r = _rpc(client, "tools/call",
             {"name": "edit_pipeline",
              "arguments": {"config": "definitely_not_here", "graph_yaml": "steps: []"}},
             rpc_id=20)
    payload = json.loads(r.json()["result"]["content"][0]["text"])
    assert "denied" not in (payload.get("error") or ""), payload
    assert "not a generated pipeline" in payload["error"]


def test_an_unauthorized_write_is_denied_over_the_wire(client, monkeypatch):
    monkeypatch.setattr(mcp_router.authz, "gate_enabled", lambda: True)
    monkeypatch.setattr(mcp_router.authz, "request_can_write", lambda r: False)
    monkeypatch.setattr(mcp_router.authz, "write_denial_reason",
                        lambda r: "write_denied_not_a_writer")
    _initialized(client)
    r = _rpc(client, "tools/call",
             {"name": "edit_pipeline",
              "arguments": {"config": "x", "graph_yaml": "steps: []"}}, rpc_id=21)
    payload = json.loads(r.json()["result"]["content"][0]["text"])
    assert payload["error"].startswith("denied:")


def test_a_read_is_reachable_with_no_credentials_at_all(client, monkeypatch):
    """The reason `/mcp` is exempt from the method gate in the first place."""
    monkeypatch.setattr(mcp_router.authz, "gate_enabled", lambda: True)
    monkeypatch.setattr(mcp_router.authz, "request_can_write", lambda r: False)
    _initialized(client)
    r = _rpc(client, "tools/call", {"name": "list_pipelines", "arguments": {}},
             rpc_id=22)
    payload = json.loads(r.json()["result"]["content"][0]["text"])
    assert "pipelines" in payload

def test_the_advertised_surface_covers_the_four_artefact_kinds(client):
    _initialized(client)
    r = _rpc(client, "tools/list", {}, rpc_id=9)
    names = {t["name"] for t in r.json()["result"]["tools"]}
    for expected in ("list_pipelines", "get_pipeline", "edit_pipeline",
                     "list_roles", "get_role", "edit_role",
                     "list_templates", "get_template", "edit_template",
                     "list_tools", "get_tool", "edit_tool",
                     "export_pipeline", "import_pipeline",
                     "run_pipeline", "get_run_status"):
        assert expected in names, expected


def test_every_advertised_tool_carries_a_description(client):
    """A 16-tool surface with no guidance is a surface nobody can use correctly."""
    _initialized(client)
    r = _rpc(client, "tools/list", {}, rpc_id=10)
    for t in r.json()["result"]["tools"]:
        assert (t.get("description") or "").strip(), t["name"]


def test_editing_a_pipeline_that_is_not_generated_is_refused(client, gate_off):
    _initialized(client)
    r = _rpc(client, "tools/call",
             {"name": "edit_pipeline",
              "arguments": {"config": "dpe_default", "graph_yaml": "steps: []"}},
             rpc_id=11)
    payload = json.loads(r.json()["result"]["content"][0]["text"])
    assert "error" in payload and "not a generated pipeline" in payload["error"]


def test_a_broken_graph_is_rejected_before_anything_is_written(client, tmp_path,
                                                               monkeypatch, gate_off):
    import yaml
    from core import pipeline_registry as pr
    monkeypatch.setenv("AITELIER_GENERATED_CONFIGS_DIR", str(tmp_path))
    good = {"name": "gen_probe", "steps": [
        {"id": "a", "step_type": "tool", "tool_name": "read_file",
         "transitions": [{"to": None}]}]}
    text = yaml.safe_dump(good, sort_keys=False)
    (tmp_path / "gen_probe.yaml").write_text(text, encoding="utf-8")

    _initialized(client)
    r = _rpc(client, "tools/call",
             {"name": "edit_pipeline",
              "arguments": {"config": "gen_probe", "graph_yaml": "steps: []"}},
             rpc_id=12)
    payload = json.loads(r.json()["result"]["content"][0]["text"])
    assert "error" in payload
    # The file must be untouched — a rejected edit that already overwrote the
    # graph leaves a pipeline that is broken in a way the caller was told did
    # not happen.
    assert (tmp_path / "gen_probe.yaml").read_text() == text


def test_a_tool_whose_source_does_not_import_is_rejected(client, tmp_path,
                                                        monkeypatch, gate_off):
    from core import pipeline_bundle as pb
    monkeypatch.setattr(pb, "_generated_tools_dir", lambda: tmp_path)
    _initialized(client)
    r = _rpc(client, "tools/call",
             {"name": "edit_tool",
              "arguments": {"name": "probe_tool", "tool_yaml": "name: probe_tool\n",
                            "impl_py": "def probe_tool(:\n"}}, rpc_id=13)
    payload = json.loads(r.json()["result"]["content"][0]["text"])
    assert "error" in payload and "does not import" in payload["error"]
    assert not (tmp_path / "probe_tool").exists()


def test_a_tool_that_imports_but_defines_nothing_is_rejected(client, tmp_path,
                                                             monkeypatch, gate_off):
    """The loader looks the function up BY THE TOOL'S NAME. A module that imports
    cleanly and defines something else registers fine and fails a whole run later."""
    from core import pipeline_bundle as pb
    monkeypatch.setattr(pb, "_generated_tools_dir", lambda: tmp_path)
    _initialized(client)
    r = _rpc(client, "tools/call",
             {"name": "edit_tool",
              "arguments": {"name": "probe_tool", "tool_yaml": "name: probe_tool\n",
                            "impl_py": "def something_else(**kw):\n    return {}\n"}},
             rpc_id=14)
    payload = json.loads(r.json()["result"]["content"][0]["text"])
    assert "error" in payload and "no callable named" in payload["error"]
    assert not (tmp_path / "probe_tool").exists()


def test_run_pipeline_refuses_an_unknown_config_and_names_the_real_ones(client, gate_off):
    _initialized(client)
    r = _rpc(client, "tools/call",
             {"name": "run_pipeline", "arguments": {"config": "nope"}}, rpc_id=15)
    payload = json.loads(r.json()["result"]["content"][0]["text"])
    assert "error" in payload and "Available:" in payload["error"]


# ── wait_for_run ─────────────────────────────────────────────────────────────

class _Notifications:
    def __init__(self): self.subs = []
    def subscribe(self, cb): self.subs.append(cb)
    def unsubscribe(self, cb): self.subs.remove(cb)

    async def emit(self, event_type, run_id, payload=None):
        class _N:
            pass
        n = _N(); n.event_type = event_type; n.run_id = run_id
        n.payload = payload or {}
        for cb in list(self.subs):
            await cb(n)


class _WaitSF:
    """A skillflow whose run status can be flipped from the test."""
    def __init__(self, status="running"):
        self.notifications = _Notifications()
        self.status = status
        self.reads = 0

    def get_run(self, run_id):
        self.reads += 1
        if run_id in ("missing", "p1"):
            return None          # "p1" is a PROJECT id — it resolves via list_runs
        return {"id": run_id, "status": self.status, "graph_name": "gen_x",
                "project_id": "p1", "current_node": "step2"}

    def list_runs(self, project_id=None):
        if project_id != "p1":
            return []
        return [{"id": "r1", "status": self.status, "graph_name": "gen_x",
                 "project_id": "p1", "current_node": "step2"}]


def _wait_tool():
    """The undecorated wait_for_run, captured from a fresh registration."""
    captured = {}

    def tool(name, kind, description):
        def deco(fn):
            captured[name] = fn
            return fn
        return deco
    mcp_router._register_wait_tool(tool)
    return captured["wait_for_run"]


@pytest.fixture
def wait_sf(monkeypatch):
    sf = _WaitSF()
    import api.dependencies as deps
    monkeypatch.setattr(deps, "get_skillflow", lambda: sf)
    return sf


@pytest.mark.asyncio
async def test_wait_returns_at_once_when_the_run_has_already_settled(wait_sf):
    wait_sf.status = "completed"
    out = await _wait_tool()("r1", timeout_seconds=30)
    assert out["status"] == "completed" and out["timed_out"] is False


@pytest.mark.asyncio
async def test_wait_returns_the_moment_a_checkpoint_fires(wait_sf):
    import asyncio
    task = asyncio.create_task(_wait_tool()("r1", timeout_seconds=30))
    await asyncio.sleep(0)                      # let it subscribe
    wait_sf.status = "paused"
    await wait_sf.notifications.emit("checkpoint_paused", "r1")
    out = await asyncio.wait_for(task, 5)
    assert out["status"] == "paused"
    assert out["settled_on"] == "checkpoint_paused"
    # It used to say "A person approves it in the AItelier UI — this endpoint
    # cannot", while answer_checkpoint is registered on this very endpoint and
    # run_pipeline's own note already points at it.
    assert "answer_checkpoint" in out["next"]
    assert "cannot" not in out["next"]


@pytest.mark.asyncio
async def test_wait_also_returns_on_failure_not_only_on_success(wait_sf):
    """A watcher that only matches the happy path sits through a failure, and
    silence is indistinguishable from still-running."""
    import asyncio
    task = asyncio.create_task(_wait_tool()("r1", timeout_seconds=30))
    await asyncio.sleep(0)
    wait_sf.status = "failed"
    await wait_sf.notifications.emit("run_failed", "r1")
    out = await asyncio.wait_for(task, 5)
    assert out["status"] == "failed" and out["settled_on"] == "run_failed"


@pytest.mark.asyncio
async def test_an_event_for_another_run_does_not_wake_this_wait(wait_sf):
    import asyncio
    task = asyncio.create_task(_wait_tool()("r1", timeout_seconds=1))
    await asyncio.sleep(0)
    await wait_sf.notifications.emit("checkpoint_paused", "SOMEONE_ELSE")
    out = await asyncio.wait_for(task, 5)
    assert out["timed_out"] is True and out["status"] == "waiting"


@pytest.mark.asyncio
async def test_a_timeout_is_reported_as_still_waiting_not_as_an_error(wait_sf):
    out = await _wait_tool()("r1", timeout_seconds=1)
    assert out["timed_out"] is True
    assert out["status"] == "waiting"
    assert "error" not in out
    assert "not an error" in out["next"]


@pytest.mark.asyncio
async def test_the_subscription_is_always_removed(wait_sf):
    await _wait_tool()("r1", timeout_seconds=1)
    wait_sf.status = "completed"
    await _wait_tool()("r1", timeout_seconds=1)
    assert wait_sf.notifications.subs == [], "leaked a subscriber per call"


@pytest.mark.asyncio
async def test_it_subscribes_before_reading_the_status(wait_sf):
    """The race this ordering exists to close.

    Read-then-subscribe leaves a hole exactly one status-read wide: the run
    settles in the gap, its event fires with nobody listening, and the wait then
    blocks the whole timeout on a run that already stopped — reported as still
    running, which is false. Emitting from inside the status read proves the
    subscriber was already attached.
    """
    import asyncio
    sf = wait_sf
    original = sf.get_run

    def racing_get_run(run_id):
        if sf.reads == 0:                       # during the FIRST read
            asyncio.get_running_loop().create_task(
                sf.notifications.emit("run_completed", "r1"))
            sf.status = "completed"
        return original(run_id)

    sf.get_run = racing_get_run
    out = await asyncio.wait_for(_wait_tool()("r1", timeout_seconds=30), 5)
    assert out["timed_out"] is False
    assert out["status"] == "completed"


@pytest.mark.asyncio
async def test_an_unknown_run_is_reported_not_waited_on(wait_sf):
    out = await _wait_tool()("missing", timeout_seconds=30)
    assert out["error"].startswith("no run")


@pytest.mark.asyncio
async def test_zero_means_look_now_not_wait_the_default(wait_sf):
    """`timeout_seconds or DEFAULT` made 0 falsy, so asking for one look with no
    wait blocked for the full 45s default. The first version of this test asserted
    `elapsed >= 1` and passed on 45 — an upper bound is the half that matters."""
    import asyncio
    started = asyncio.get_running_loop().time()
    out = await _wait_tool()("r1", timeout_seconds=0)
    assert asyncio.get_running_loop().time() - started < 1
    assert out["timed_out"] is True and out["status"] == "waiting"


@pytest.mark.asyncio
async def test_the_timeout_is_clamped_to_the_ceiling(wait_sf, monkeypatch):
    """Above the client's own per-call timeout the client hangs up first, so an
    unbounded wait buys nothing and reads as a broken tool."""
    import asyncio
    captured = {}

    async def fake_wait_for(aw, timeout):
        captured["timeout"] = timeout
        aw.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
    await _wait_tool()("r1", timeout_seconds=10 ** 9)
    assert captured["timeout"] == mcp_router._WAIT_MAX_S


# ── Review fixes ─────────────────────────────────────────────────────────────

def test_a_tool_name_cannot_escape_the_generated_tools_directory():
    """`get_tool` is a credential-free READ and its name becomes a directory name.

    Unvalidated, `get_tool("../../<anywhere>")` reads that directory's tool.yaml /
    impl.py / README.md off the box with no token at all — and the same name
    reaches a `mkdir` + `write_text` on the import path.
    """
    for bad in ("../../etc", "a/b", ".hidden", "", "x" * 100, None):
        assert tool_guards.bad_tool_name(bad), f"accepted {bad!r}"
    for ok in ("fetch_prices", "godot_compile", "t2", "a-b"):
        assert tool_guards.bad_tool_name(ok) is None, f"rejected {ok!r}"


def test_get_tool_refuses_a_traversing_name_over_the_wire(client, gate_off):
    _initialized(client)
    r = _rpc(client, "tools/call",
             {"name": "get_tool", "arguments": {"name": "../../../../etc"}}, rpc_id=30)
    payload = json.loads(r.json()["result"]["content"][0]["text"])
    assert "invalid tool name" in payload["error"]


def test_a_pipeline_is_reachable_by_the_name_list_pipelines_returned():
    """The registry keys pipelines by the graph's `name:`, which for the flagship
    config differs from its filename (configs/dpe_default.yaml → dpe_default_v2).
    Resolving by filename made get_pipeline answer "call list_pipelines" for a name
    list_pipelines had just returned."""
    import yaml
    from pathlib import Path
    cfg = Path(__file__).resolve().parents[2] / "configs" / "dpe_default.yaml"
    declared = (yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}).get("name")
    assert declared and declared != cfg.stem, (
        "fixture assumption gone: this test needs a config whose graph name differs "
        "from its filename")
    assert mcp_router._builtin_stem(declared) == cfg.stem
    assert mcp_router._builtin_config_path(declared) == cfg
    assert mcp_router._load_roles(declared), "built-in roles must resolve too"


def test_a_config_composed_at_boot_resolves_the_same_way(monkeypatch):
    """dpe_game is dpe_default_v2 + configs/addons/game_harness.yaml, composed at
    boot and never written to a file. Resolving by FILE made get_pipeline /
    list_roles / list_templates / get_role / get_template / export_pipeline all
    answer "no pipeline 'dpe_game' — call list_pipelines" for a name
    list_pipelines had just returned — on 18 of the 30 runs in the DB. Same dead
    end the test above closed for dpe_default_v2, one config short.
    """
    from types import SimpleNamespace
    from api import dependencies
    from core import addon_registry
    # The registry is what list_pipelines answers from; stand in for it so this
    # stays a unit test, while the composition itself comes from the real files.
    monkeypatch.setattr(dependencies, "get_config_registry",
                        lambda: SimpleNamespace(
                            get=lambda n: object() if n == "dpe_game" else None))
    monkeypatch.setattr(addon_registry, "describe_config",
                        lambda n: {"base": "dpe_default_v2",
                                   "addons": ["game_harness"]})

    src = mcp_router._config_source("dpe_game")
    assert src is not None, "a registered composed config must resolve"
    assert src["kind"] == "composed"
    assert src["role_stems"] == ["dpe_default", "game_harness"]
    assert "configs/dpe_default.yaml" in src["origin"]
    assert "configs/addons/game_harness.yaml" in src["origin"]

    # The roles it names really do merge: game_designer exists only in the addon's
    # agent_configs file, researcher only in the base's. One stem would lose one.
    roles = mcp_router._load_roles("dpe_game")
    assert roles and "game_designer" in roles and "researcher" in roles

    # export genuinely cannot serve it — but it must say WHY, naming the sources,
    # not send the caller back round the loop.
    _, err = mcp_router._roles_or_error("dpe_game")
    assert err is None
    assert mcp_router._config_source("no_such_pipeline_xyz") is None


def test_only_an_unregistered_name_is_told_to_call_list_pipelines(monkeypatch):
    """A config the registry DOES know, whose roles this repo cannot read (one
    registered from the skillflow package), must be told what it is and where its
    graph is — "call list_pipelines" there is a loop with no exit."""
    from types import SimpleNamespace
    from api import dependencies
    from core import addon_registry
    monkeypatch.setattr(dependencies, "get_config_registry",
                        lambda: SimpleNamespace(
                            get=lambda n: object() if n == "addon_converter" else None))
    monkeypatch.setattr(addon_registry, "describe_config",
                        lambda n: {"base": "addon_converter", "addons": []})

    src = mcp_router._config_source("addon_converter")
    assert src is not None and src["kind"] == "external"
    _, err = mcp_router._roles_or_error("addon_converter")
    assert err and "call list_pipelines" not in err["error"]
    assert "skillflow package" in err["error"]
    assert "get_pipeline" in err["error"]

    _, err = mcp_router._roles_or_error("no_such_pipeline_xyz")
    assert err and "call list_pipelines" in err["error"]


class _FakeSF:
    """Just enough skillflow for the run-ref and step-status paths."""

    def __init__(self, runs, steps=None):
        self._runs = runs
        self._steps = steps or {}

    def get_run(self, ref):
        return next((r for r in self._runs if r["id"] == ref), None)

    def list_runs(self, project_id=None):
        return [r for r in self._runs if r["project_id"] == project_id]

    def get_steps(self, run_id):
        return self._steps.get(run_id, [])


def test_a_project_id_with_several_runs_names_the_run_it_picked():
    """Resolving a project id takes the NEWEST run, silently. get_step_output
    ("jinyong-ux", "finalize") therefore read the dpe_game workspace and answered
    "no promoted output" while the meta_conversation run's step1_goals.json sat on
    disk at 3340 bytes."""
    from core.trace_reader import resolve_run_ref, resolve_run_row
    runs = [{"id": "new", "project_id": "p", "graph_name": "dpe_game",
             "status": "failed"},
            {"id": "old", "project_id": "p", "graph_name": "meta_conversation",
             "status": "completed"}]
    sf = _FakeSF(runs)

    row, info = resolve_run_ref(sf, "p")
    assert row["id"] == "new"
    assert info["resolved_by"] == "project_id" and info["config"] == "dpe_game"
    assert [o["run_id"] for o in info["other_runs"]] == ["old"]
    assert "NEWEST" in info["note"]

    # A run id resolves exactly, with nothing to disclose.
    row, info = resolve_run_ref(sf, "old")
    assert row["id"] == "old" and info == {}
    # The old single-value entry point still means the same thing.
    assert resolve_run_row(sf, "p")["id"] == "new"


def test_a_step_that_ran_and_wrote_nothing_is_not_reported_as_not_run():
    """get_run_summary listed git_sync_pre `completed` while get_step_output said
    it "may not have run yet". Three different facts shared one sentence: not in
    the graph, not run, ran and wrote nothing."""
    steps = {"r": [{"step_id": "git_sync_pre", "status": "completed"},
                   {"step_id": "5_review", "status": "pending"}]}
    sf = _FakeSF([{"id": "r", "project_id": "p", "graph_name": "dpe_game"}], steps)
    row = {"id": "r", "graph_name": "dpe_game"}

    ran = mcp_router._no_step_output_reason(sf, row, "git_sync_pre")
    assert "RAN" in ran and "not run yet" not in ran

    never = mcp_router._no_step_output_reason(sf, row, "5_review")
    assert "has NOT run" in never and "pending" in never

    absent = mcp_router._no_step_output_reason(sf, row, "finalize")
    assert "no step 'finalize' in config 'dpe_game'" in absent
    assert "git_sync_pre" in absent, "it must list the steps this config does have"


def test_a_built_in_roles_prompt_is_read_from_the_file_the_role_names():
    """Built-in roles carry `template: step1_5_researcher.md` and keep the prompt
    in templates/; only a GENERATED pipeline stores it inline as `system_prompt`.
    Reading system_prompt alone answered {"template": ""} with no error for every
    role of 10 of the 12 built-in configs."""
    roles = mcp_router._load_roles("dpe_default_v2")
    assert roles and "researcher" in roles
    assert not (roles["researcher"] or {}).get("system_prompt"), (
        "fixture assumption gone: this test needs a role whose prompt is a FILE")

    text, source = mcp_router._role_prompt(roles["researcher"])
    assert source == "templates/step1_5_researcher.md"
    assert len(text) > 500, "the real prompt, not an empty string"

    # Every role of the flagship config must report a real size — the measured
    # symptom was 0 chars across the board.
    for name, cfg in roles.items():
        body, src = mcp_router._role_prompt(cfg)
        assert body, f"{name} reported an empty prompt ({src})"

    # An inline prompt still wins, and still says so.
    assert mcp_router._role_prompt({"system_prompt": "hi"}) == (
        "hi", "inline (system_prompt)")
    # A named file that is not there is reported, not silently blank.
    _, src = mcp_router._role_prompt({"template": "no_such_template.md"})
    assert "NOT FOUND" in src
    # A role with neither says so rather than looking like an empty prompt.
    _, src = mcp_router._role_prompt({})
    assert "neither" in src


class _SummarySF(_FakeSF):
    """_FakeSF plus the trace probe summarise_run uses for liveness."""

    def trace_query(self, run_id, sql, params):
        return []


def _summarise(run, steps):
    from types import SimpleNamespace
    from core.run_driver import summarise_run
    sf = _SummarySF([run], {run["id"]: steps})
    ws = SimpleNamespace(get_final_path=lambda *a, **k: pathlib.Path("/nonexistent"))
    registry = SimpleNamespace(get=lambda name: None)
    return summarise_run(sf, ws, registry, run["id"])


def test_a_claim_time_failure_reaches_first_failure():
    """`claim_next_step` rejecting at the node never marks a step row, so the only
    record is the RUN row's error. first_failure — promised by this function's
    docstring and by the get_run_summary tool description — came back null on run
    32ee04de while status, verdict and run_error all said failed."""
    run = {"id": "r", "project_id": "p", "graph_name": "dpe_game",
           "status": "failed", "current_node": "1",
           "error_reason": "Required context source resolved to no content: finalize"}
    out = _summarise(run, [{"step_id": "git_sync_pre", "status": "completed"},
                           {"step_id": "1", "status": "pending"}])
    ff = out["first_failure"]
    assert ff, "a failed run must not report first_failure: null"
    assert ff["step"] == "1", "the node it stopped at"
    assert "no content: finalize" in ff["error"]
    assert ff["from"] == "run_error"
    assert "no step row is marked failed" in ff["note"]


def test_a_step_row_failure_still_wins_over_the_run_level_error():
    """The fallback must not paper over the precise answer when there is one."""
    run = {"id": "r", "project_id": "p", "graph_name": "dpe_game",
           "status": "failed", "current_node": "5",
           "error_reason": "run-level blurb"}
    out = _summarise(run, [{"step_id": "1", "status": "completed"},
                           {"step_id": "2", "status": "failed",
                            "error": "the architect blew up"}])
    ff = out["first_failure"]
    assert ff["step"] == "2" and ff["error"] == "the architect blew up"
    assert "from" not in ff, "a real step failure needs no provenance caveat"


def test_a_completed_run_has_no_first_failure():
    run = {"id": "r", "project_id": "p", "graph_name": "dpe_game",
           "status": "completed", "current_node": "done", "error_reason": ""}
    assert _summarise(run, [{"step_id": "done", "status": "completed"}])[
        "first_failure"] is None


def test_corrupt_roles_json_reaches_the_model_as_a_message_not_a_traceback(
        tmp_path, monkeypatch):
    """_load_roles used to RETURN {"__error__": "<str>"}; no caller checked it, so
    list_roles called .get() on the string and the AttributeError escaped raw."""
    monkeypatch.setenv("AITELIER_GENERATED_CONFIGS_DIR", str(tmp_path))
    (tmp_path / "gen_broken.roles.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(mcp_router.ToolError, match="not valid JSON"):
        mcp_router._load_roles("gen_broken")


def test_a_tool_error_is_reported_to_the_model(client, gate_off, tmp_path,
                                               monkeypatch):
    monkeypatch.setenv("AITELIER_GENERATED_CONFIGS_DIR", str(tmp_path))
    (tmp_path / "gen_broken.roles.json").write_text("{not json", encoding="utf-8")
    _initialized(client)
    r = _rpc(client, "tools/call",
             {"name": "list_roles", "arguments": {"config": "gen_broken"}}, rpc_id=31)
    payload = json.loads(r.json()["result"]["content"][0]["text"])
    assert "not valid JSON" in payload["error"]


def test_a_failed_reload_reverts_the_roles_file_and_reports_it(tmp_path, monkeypatch):
    """reload_generated_pipeline NEVER raises — it RETURNS {"error": ...}. Catching
    only an exception left the revert as dead code and reported success."""
    from core import pipeline_registry as pr
    monkeypatch.setenv("AITELIER_GENERATED_CONFIGS_DIR", str(tmp_path))
    rj = tmp_path / "gen_x.roles.json"
    rj.write_text('{"gen_x__a": {"system_prompt": "before"}}', encoding="utf-8")
    monkeypatch.setattr(pr, "reload_generated_pipeline",
                        lambda *a, **k: {"error": "no persisted config gen_x.yaml"})

    captured = {}

    def tool(name, kind, description):
        def deco(fn):
            captured[name] = fn
            return fn
        return deco
    mcp_router._register_edit_tools(tool)

    out = captured["edit_template"]("gen_x", "a", "after")
    assert "reload failed" in out["error"]
    assert json.loads(rj.read_text())["gen_x__a"]["system_prompt"] == "before", \
        "the roles file was not reverted — the edit silently shadows the live config"


def test_edit_role_names_the_role_the_caller_typed_when_it_cannot_resolve(tmp_path,
                                                                          monkeypatch):
    monkeypatch.setenv("AITELIER_GENERATED_CONFIGS_DIR", str(tmp_path))
    (tmp_path / "gen_x.roles.json").write_text(
        '{"gen_x__author": {"model": "host"}}', encoding="utf-8")
    captured = {}

    def tool(name, kind, description):
        def deco(fn):
            captured[name] = fn
            return fn
        return deco
    mcp_router._register_edit_tools(tool)

    out = captured["edit_role"]("gen_x", "auther", model="x")   # typo
    assert "'auther'" in out["error"], out          # not "'None'"


def test_editing_a_tool_makes_the_loader_forget_the_old_function(monkeypatch):
    """ToolLoader has no reload/rescan/refresh and load_fn caches forever, so the
    previous probe-for-a-method was a silent no-op: edit_tool reported success and
    the OLD implementation kept running until restart."""
    class _Loader:
        def __init__(self):
            self._cache = {"fetch_prices": ({}, lambda: "old")}
            self._tool_dir_cache = {"fetch_prices": "/somewhere"}
            self.added = []

        def add_tools_dir(self, path):
            self.added.append(path)
            self._cache.clear()
            self._tool_dir_cache.clear()

    loader = _Loader()
    import api.dependencies as deps
    monkeypatch.setattr(deps, "get_tool_loader", lambda: loader)
    mcp_router._reload_tools()
    assert loader._cache == {} and loader._tool_dir_cache == {}
    assert loader.added, "nothing invalidated the loader"


def test_a_tool_whose_import_hangs_is_rejected_instead_of_hanging_the_server():
    """The probe execs agent-written module-level code. In-process and without a
    timeout, a waiting loop at import time pinned the server with no way to cancel
    and ran its side effects in the live backend."""
    err = tool_guards.tool_source_error(
        "sleeper", "import time\ntime.sleep(120)\ndef sleeper(**kw):\n    return {}\n")
    assert err and "did not finish importing" in err


def test_the_probe_lists_what_the_module_actually_defines():
    err = tool_guards.tool_source_error(
        "wanted", "def something_else(**kw):\n    return {}\n")
    assert "no callable named 'wanted'" in err
    assert "something_else" in err, "the message must say what IS defined"


@pytest.mark.asyncio
async def test_a_sync_tool_body_does_not_run_on_the_event_loop():
    """One MCP call used to freeze the whole control plane: run_pipeline shells out
    to git, and awaiting it inline stalled SSE, the web UI and the scheduler's
    cross-thread callbacks for its full duration."""
    import asyncio
    import threading

    loop_thread = threading.get_ident()
    seen = {}

    class _Mcp:
        def get_context(self):
            return None

    def probe():
        seen["thread"] = threading.get_ident()
        return {"ok": True}

    mcp_router._TOOL_KIND["_probe_sync"] = "read"
    try:
        wrapped = mcp_router._wrap(_Mcp(), probe, "_probe_sync")
        assert await wrapped() == {"ok": True}
    finally:
        mcp_router._TOOL_KIND.pop("_probe_sync", None)
    assert seen["thread"] != loop_thread, "sync tool ran on the event loop"


# ── Ultrareview fixes ────────────────────────────────────────────────────────

@pytest.mark.parametrize("attack", [
    "../probe_secret",                 # relative traversal
    "/etc/passwd",                     # absolute RHS REPLACES the base in pathlib
    "../../../../etc/hosts",
    "a/b",
])
def test_a_config_name_cannot_read_outside_the_configs_dir(client, attack, tmp_path,
                                                           monkeypatch):
    """The credential-free read tools built `<configs>/<config>.yaml` unchecked.

    `get_tool(name)` was guarded and `get_pipeline(config)` was not — same class,
    one surface hardened and the other not. Reads need no credentials and `/mcp`
    is exempt from the method gate, so this returned the file's full text to an
    unauthenticated caller. Reproduced live against the running container before
    the fix: both the relative form and an absolute `config` leaked a file from
    outside the configs directory.
    """
    monkeypatch.setenv("AITELIER_GENERATED_CONFIGS_DIR", str(tmp_path / "configs"))
    (tmp_path / "configs").mkdir()
    (tmp_path / "probe_secret.yaml").write_text("password: s3cret\n", encoding="utf-8")

    _initialized(client)
    for tool_name, args in (("get_pipeline", {"config": attack}),
                            ("list_roles", {"config": attack}),
                            ("get_template", {"config": attack, "role": "r"}),
                            ("export_pipeline", {"config": attack}),
                            ("list_tools", {"pipeline": attack})):
        r = _rpc(client, "tools/call", {"name": tool_name, "arguments": args},
                 rpc_id=40)
        payload = json.loads(r.json()["result"]["content"][0]["text"])
        assert "error" in payload, f"{tool_name} answered for {attack!r}: {payload}"
        assert "s3cret" not in json.dumps(payload), f"{tool_name} leaked the file"


def test_the_two_writers_into_the_shared_tools_dir_use_one_guard():
    """`import_pipeline` accepted names `edit_tool` then refuses to address.

    A bundle installing a tool called `my tool` succeeded, and every later
    `edit_tool`/`get_tool` on it answered "invalid tool name" — installed and
    uneditable, breaking the surface's promise that generated tools are editable.
    """
    from core import pipeline_bundle as pb
    assert pb.bad_tool_name is tool_guards.bad_tool_name
    for bad in ("my tool", "a/b", "..", "x" * 100, "étude", ""):
        assert tool_guards.bad_tool_name(bad), f"accepted {bad!r}"


@pytest.mark.asyncio
async def test_an_event_with_no_run_id_wakes_nobody(wait_sf):
    """`if rid and rid != run_id` short-circuits on a missing rid, so an event
    nobody could attribute woke EVERY concurrent waiter — each then reporting a
    settle-shaped answer for a run that had not settled."""
    import asyncio
    task = asyncio.create_task(_wait_tool()("r1", timeout_seconds=1))
    await asyncio.sleep(0)
    await wait_sf.notifications.emit("run_failed", None)      # unattributable
    out = await asyncio.wait_for(task, 5)
    assert out["timed_out"] is True and out["settled_on"] is None


# ── The drive loop: scheduler runs, the agent decides ────────────────────────

def test_generated_pipelines_are_scheduler_owned():
    """A run must advance whoever started it.

    Generated pipelines were `scheduler_owned: False` — "butler-driven so
    checkpoints relay in-chat" — which makes the STARTER responsible for stepping
    it. Fine while the butler is the only starter; the moment the MCP endpoint
    started one it sat at `running` forever (verified live) while wait_for_run
    truthfully reported "still running".
    """
    from core.pipeline_registry import GEN_HINTS
    assert GEN_HINTS["scheduler_owned"] is True


def test_run_pipeline_attaches_a_driver(monkeypatch):
    """Butler-owned configs have NO poller. Starting one without attaching a driver
    is starting a run nobody will ever advance."""
    started = {}
    monkeypatch.setattr(mcp_router, "_start_driver",
                        lambda rid, **kw: started.update(run_id=rid, **kw) or True)

    captured = {}

    def tool(name, kind, description):
        def deco(fn):
            captured[name] = fn
            return fn
        return deco
    mcp_router._register_run_tools(tool)

    class _M:
        scheduler_owned = False
    monkeypatch.setattr("api.dependencies.get_config_registry",
                        lambda: type("R", (), {"get": lambda s, n: _M(),
                                               "list": lambda s: []})())
    monkeypatch.setattr("core.run_launcher.start_config_run",
                        lambda *a, **k: {"status": "ok", "run_id": "R1"})
    monkeypatch.setattr("core.run_launcher.generate_run_id", lambda c: "pid1")

    out = captured["run_pipeline"]("code_review", seed_text="x")
    assert out["run_id"] == "R1"
    assert started == {"run_id": "R1", "scheduler_owned": False,
                       "auto_approve": True}, started


def test_checkpoints_ask_is_reachable_and_auto_is_the_default(monkeypatch):
    """The default answers human-approval gates for you. That is what a test-drive
    wants and a bypass for a production run, so the other mode must exist and the
    tool description must say so."""
    started = {}
    monkeypatch.setattr(mcp_router, "_start_driver",
                        lambda rid, **kw: started.update(**kw) or True)
    captured = {}

    def tool(name, kind, description):
        def deco(fn):
            captured[name] = fn
            captured[name + "__desc"] = description
            return fn
        return deco
    mcp_router._register_run_tools(tool)
    monkeypatch.setattr("api.dependencies.get_config_registry",
                        lambda: type("R", (), {"get": lambda s, n: type("M", (), {"scheduler_owned": True})(),
                                               "list": lambda s: []})())
    monkeypatch.setattr("core.run_launcher.start_config_run",
                        lambda *a, **k: {"status": "ok", "run_id": "R2"})
    monkeypatch.setattr("core.run_launcher.generate_run_id", lambda c: "pid2")

    assert captured["run_pipeline"]("dpe_default_v2")["checkpoints"] == "auto"
    assert started["auto_approve"] is True
    assert captured["run_pipeline"]("dpe_default_v2", checkpoints="ask")["checkpoints"] == "ask"
    assert started["auto_approve"] is False
    assert "invalid" in str(captured["run_pipeline"]("x", checkpoints="maybe"))
    desc = captured["run_pipeline__desc"]
    assert "checkpoints='ask'" in desc and "BYPASSES" in desc


def test_answer_checkpoint_refuses_a_run_that_is_not_paused(monkeypatch):
    sf = _WaitSF(); sf.status = "running"
    import api.dependencies as deps
    monkeypatch.setattr(deps, "get_skillflow", lambda: sf)
    captured = {}

    def tool(name, kind, description):
        def deco(fn):
            captured[name] = fn
            return fn
        return deco
    mcp_router._register_run_tools(tool)
    out = captured["answer_checkpoint"]("r1")
    assert "not paused" in out["error"]


class _ApproveSpy(_WaitSF):
    """_WaitSF that records whether the approval actually reached skillflow."""

    def __init__(self, status="paused"):
        super().__init__(status)
        self.approved = []

    def approve_checkpoint(self, run_id):
        self.approved.append(run_id)
        return "next_node"


def _run_tools(sf, monkeypatch):
    import api.dependencies as deps
    monkeypatch.setattr(deps, "get_skillflow", lambda: sf)
    captured = {}

    def tool(name, kind, description):
        def deco(fn):
            captured[name + "__desc"] = description
            captured[name] = fn
            return fn
        return deco
    mcp_router._register_run_tools(tool)
    return captured


def test_feedback_on_an_approval_is_refused_not_silently_dropped(monkeypatch):
    """skillflow's approve_checkpoint(run_id) has no feedback parameter — only
    reject_checkpoint(run_id, step_id, feedback) does. Accepting `feedback` on an
    approve was a promise the endpoint could not keep: run c6dce51c was approved
    with three binding amendments, the call returned success, and the next agent
    copied the architecture verbatim with none of them. The checkpoint_approved
    trace payload is {"step_id": "2", "next_node": "2_review"} — the text is not
    even recorded."""
    sf = _ApproveSpy()
    captured = _run_tools(sf, monkeypatch)

    out = captured["answer_checkpoint"]("r1", "approve", "please also do X, Y, Z")
    assert "error" in out, out
    assert "no feedback channel" in out["error"].lower() or \
           "NO feedback channel" in out["error"]
    assert "reject" in out["error"], "it must name what actually delivers the text"
    assert sf.approved == [], "the approval must NOT have gone through"

    # Whitespace is not feedback; an approval with nothing attached still works.
    assert captured["answer_checkpoint"]("r1", "approve", "   ")["decision"] == \
        "approve"
    assert captured["answer_checkpoint"]("r1")["decision"] == "approve"
    assert sf.approved == ["r1", "r1"]

    # And the description warns before the call rather than after.
    desc = captured["answer_checkpoint__desc"]
    assert "no feedback channel" in desc.lower()


def test_a_rejection_without_feedback_is_refused(monkeypatch):
    """The step it goes back to has nothing to act on otherwise."""
    sf = _WaitSF(); sf.status = "paused"
    import api.dependencies as deps
    monkeypatch.setattr(deps, "get_skillflow", lambda: sf)
    captured = {}

    def tool(name, kind, description):
        def deco(fn):
            captured[name] = fn
            return fn
        return deco
    mcp_router._register_run_tools(tool)
    assert "needs feedback" in captured["answer_checkpoint"]("r1", "reject")["error"]


# ── One name, one resolver, for every tool that takes a run ─────────────────

_RUN_TAKING_TOOLS = ["answer_checkpoint", "get_run_summary", "get_run_status",
                     "wait_for_run", "stop_pipeline", "trace_list", "trace_search",
                     "trace_read", "get_step_output"]


@pytest.mark.asyncio
async def test_every_run_taking_tool_publishes_the_same_parameter_name():
    """The endpoint shipped two spellings — `run_id` on five tools, `run` on four.
    A model that learned one had to relearn the other mid-investigation."""
    mcp = build_mcp()
    schemas = {t.name: (t.inputSchema or {}) for t in await mcp.list_tools()}
    for name in _RUN_TAKING_TOOLS:
        props = set((schemas[name].get("properties") or {}))
        assert "run_id" in props, f"{name} publishes {sorted(props)}"
        assert "run" not in props, f"{name} still publishes the old spelling"


@pytest.mark.asyncio
async def test_every_run_taking_tool_says_it_accepts_a_project_id():
    """Worse than the two spellings was the two BEHAVIOURS behind them: the
    `run`-named tools resolved a run id OR a project id, the `run_id`-named ones
    only a run id (verified live: get_run_summary("jinyong-jianghu") →
    "no run 'jinyong-jianghu'"), except stop_pipeline, which was named `run_id`
    and resolved permissively anyway. Renaming alone would have made the strict
    tools merely LOOK like they take a project id."""
    mcp = build_mcp()
    for t in await mcp.list_tools():
        if t.name in _RUN_TAKING_TOOLS:
            assert "project_id" in (t.description or ""), (
                f"{t.name} does not tell the caller a project id works")


def test_every_run_taking_tool_actually_resolves_a_project_id(monkeypatch):
    """Not just the description: the resolver behind each one."""
    import asyncio
    import api.dependencies as deps
    from core import trace_reader

    sf = _WaitSF(); sf.status = "paused"
    monkeypatch.setattr(deps, "get_skillflow", lambda: sf)
    monkeypatch.setattr(deps, "get_workspace_manager",
                        lambda: _NoWorkspace(), raising=False)
    monkeypatch.setattr(trace_reader, "trace_rows",
                        lambda *a, **k: {"run": {"id": "r1", "project_id": "p1"},
                                         "resolved": {"resolved_by": "project_id"},
                                         "rows": []})
    # get_run_summary is the one measured live: it answered "no run
    # 'jinyong-jianghu'" for a project id trace_list accepted.
    from core import run_driver
    monkeypatch.setattr(deps, "get_config_registry", lambda: None, raising=False)
    monkeypatch.setattr(run_driver, "summarise_run",
                        lambda sf_, ws_, reg_, rid: {"run_id": rid})
    captured = {}

    def tool(name, kind, description):
        def deco(fn):
            captured[name] = fn
            return fn
        return deco
    mcp_router._register_run_tools(tool)
    mcp_router._register_wait_tool(tool)
    mcp_router._register_trace_tools(tool)
    mcp_router._register_lifecycle_tools(tool)

    calls = {
        "get_run_status": lambda f: f("p1"),
        "get_run_summary": lambda f: f("p1"),
        "wait_for_run": lambda f: asyncio.run(f("p1", timeout_seconds=0)),
        "trace_list": lambda f: f("p1"),
        "trace_search": lambda f: f("p1", "x"),
        "trace_read": lambda f: f("p1", 1),
        "get_step_output": lambda f: f("p1", "some_step"),
    }
    for name, call in calls.items():
        out = call(captured[name])
        assert "no run 'p1'" not in str(out.get("error", "")), (
            f"{name} still refuses a project id: {out}")
        assert out.get("run_id") == "r1" or out.get("entries") is not None, (
            f"{name} must answer for the run it landed on: {out}")

    # The two MUTATING ones resolve permissively too, but every guard they had
    # still applies to the run they landed on, and the reply names it.
    out = captured["answer_checkpoint"]("p1", "reject")
    assert "needs feedback" in out["error"], out
    sf.status = "running"
    out = captured["answer_checkpoint"]("p1")
    assert "not paused" in out["error"] and "r1" in out["error"]


class _NoWorkspace:
    def get_final_path(self, *a, **k):
        return pathlib.Path("/nonexistent/step")


def test_a_truncated_step_file_says_so_in_band_and_in_a_flag(tmp_path, monkeypatch):
    """The description said "in full"; the code cut every file at 20000 chars with
    no marker. Measured on t_impl of ac25585e: 10 files, 95518 chars, 3 of them
    sliced mid-sentence and nothing saying so."""
    import api.dependencies as deps
    from types import SimpleNamespace

    d = tmp_path / "step"
    d.mkdir()
    big = "x" * (mcp_router._STEP_FILE_CAP + 500)
    (d / "big.md").write_text(big, encoding="utf-8")
    (d / "small.md").write_text("short", encoding="utf-8")

    sf = _WaitSF()
    monkeypatch.setattr(deps, "get_skillflow", lambda: sf)
    monkeypatch.setattr(deps, "get_workspace_manager",
                        lambda: SimpleNamespace(get_final_path=lambda *a, **k: d))
    captured = {}

    def tool(name, kind, description):
        def deco(fn):
            captured[name] = fn
            return fn
        return deco
    mcp_router._register_lifecycle_tools(tool)
    get_step_output = captured["get_step_output"]

    out = get_step_output("r1", "s")
    assert out["truncated"] == {"big.md": {
        "total_chars": len(big),
        "returned_chars": mcp_router._STEP_FILE_CAP}}
    assert "TRUNCATED" in out["files"]["big.md"]
    assert str(len(big)) in out["files"]["big.md"], "the real size, in band"
    assert "small.md" not in out["truncated"]
    assert out["files"]["small.md"] == "short"

    # …and the documented way past the cap returns that one file whole.
    one = get_step_output("r1", "s", file="big.md")
    assert set(one["files"]) == {"big.md"}
    assert one["files"]["big.md"] == big and "truncated" not in one

    # A path that climbs out of the step directory is not a file of this step.
    assert "no file" in get_step_output("r1", "s", file="../../etc/passwd")["error"]


@pytest.mark.asyncio
async def test_the_three_ambiguous_parameters_carry_a_description():
    """0 of ~40 published parameters carried one. Not all of them need one — these
    three do, because guessing them wrong costs a round trip: WHICH id a run tool
    wants, WHICH spelling of a pipeline's name (dpe_default.yaml declares
    'dpe_default_v2'), and WHICH of a graph's step ids."""
    mcp = build_mcp()
    blank, seen = [], set()
    for t in await mcp.list_tools():
        props = (t.inputSchema or {}).get("properties") or {}
        for key in ("run_id", "config", "step"):
            if key not in props:
                continue
            seen.add(key)
            if not (props[key].get("description") or "").strip():
                blank.append(f"{t.name}.{key}")
    assert seen == {"run_id", "config", "step"}, seen
    assert not blank, f"published with an empty description: {blank}"


@pytest.mark.asyncio
async def test_no_tool_publishes_an_uncallable_schema():
    """A `**kwargs` wrapper publishes ONE required field called `kwargs`.

    That is what shipped for the skillflow_docs_* tools: the model could see them,
    and every call failed pydantic validation before reaching the tool. Nothing
    caught it because every test called the python function directly — the
    published SCHEMA is a separate artifact and this is the test that reads it.
    """
    mcp = build_mcp()
    for t in await mcp.list_tools():
        props = set((t.inputSchema or {}).get("properties") or {})
        assert "kwargs" not in props and "args" not in props, (
            f"{t.name} publishes a varargs placeholder as a parameter: {sorted(props)}")


@pytest.mark.asyncio
async def test_every_tool_is_callable_with_its_own_required_arguments():
    """Each tool's declared `required` list must be arguments the function really
    takes — a schema that asks for something the callable rejects is unusable."""
    import inspect as _inspect
    mcp = build_mcp()
    for t in await mcp.list_tools():
        schema = t.inputSchema or {}
        required = schema.get("required") or []
        fn = mcp._tool_manager.get_tool(t.name).fn
        params = _inspect.signature(fn).parameters
        accepts_kw = any(p.kind is _inspect.Parameter.VAR_KEYWORD
                         for p in params.values())
        for arg in required:
            assert arg in params or accepts_kw, (
                f"{t.name} requires {arg!r} which its function does not accept")


# ── get_step_output: the run_id label is not provenance ─────────────────────

def test_two_runs_of_one_config_are_told_they_share_the_step_dir():
    """The files come from a path keyed by project+config, with no run in it.

    Two runs of the same config on one project promote into the SAME directory,
    the later overwriting the earlier. Asking for the older run's id then returns
    the newer run's files, and the reply used to stamp `run_id: <older>` over
    them with nothing indicating the collision.
    """
    from api import mcp_router

    row = {"id": "run-old", "project_id": "p1", "graph_name": "dpe_game"}

    class _SF:
        def list_runs(self, project_id=None, **kw):
            assert project_id == "p1"
            return [{"id": "run-new", "graph_name": "dpe_game"},
                    {"id": "run-old", "graph_name": "dpe_game"},
                    {"id": "other", "graph_name": "meta_conversation"}]

    share = mcp_router._runs_sharing_step_dir(_SF(), row)
    assert [r["id"] for r in share] == ["run-new", "run-old"], (
        "both runs of dpe_game share the directory; the meta_conversation run "
        "does not and must not be listed")


def test_one_run_per_config_says_nothing():
    """The ordinary case must stay silent — a disclosure on every call is noise."""
    from api import mcp_router

    class _SF:
        def list_runs(self, project_id=None, **kw):
            return [{"id": "run-1", "graph_name": "dpe_game"},
                    {"id": "run-2", "graph_name": "meta_conversation"}]

    assert mcp_router._runs_sharing_step_dir(
        _SF(), {"id": "run-1", "project_id": "p1", "graph_name": "dpe_game"}) == []


def test_a_broken_run_listing_does_not_break_the_read():
    """Provenance is a nicety; failing to compute it must not lose the files."""
    from api import mcp_router

    class _SF:
        def list_runs(self, project_id=None, **kw):
            raise RuntimeError("db gone")

    assert mcp_router._runs_sharing_step_dir(
        _SF(), {"id": "r", "project_id": "p", "graph_name": "c"}) == []
