"""Plugin step 12, the DSH side: the reserved project `aitelier-home` runs its
driver as the steward role.

Covered here: the role-to-project mapping, the role-dependent completion route,
compaction policy and start (model, environment, refusal without a steward
entry), `capabilities.role` at provisioning, and the steward's profile without
the `mcp-aitelier` wiring, and its chat channel plugins (step 19). Ordinary
projects are asserted unchanged next to each steward case."""
import asyncio
import copy
import json
import sys

import pytest

from core import dsh_driver
from core.dsh_driver import (DshDrivers, RESERVED_ROLES, compaction_config, completion_route,
                             load_roles, role_for_project)
from core.drivers import DriverRegistry
from core.state_database import StateDatabase
from core.state_service import StateService

STEWARD_PROJECT = "aitelier-home"
STEWARD = {"route": "steward-route", "reasoning_effort": "high",
           "compaction": {"thresholdRatio": 0.4, "retainTokens": 3}, "prompt": "steward.md",
           "presented": ["ask_owner", "close_question"], "forbidden": ["subagent"],
           "presented_only": True}


def _table(steward=True):
    table = copy.deepcopy(load_roles())
    table.pop("steward", None)
    if steward:
        table["steward"] = copy.deepcopy(STEWARD)
    return table


@pytest.fixture
def roles_file(tmp_path, monkeypatch):
    """Point the runtime at a roles.json with (or, rewritten, without) a steward."""
    path = tmp_path / "roles.json"
    path.write_text(json.dumps(_table()))
    monkeypatch.setattr(dsh_driver, "ROLES_FILE", path)
    return path


@pytest.fixture
def runtime(tmp_path):
    db = StateDatabase(str(tmp_path / "state.sqlite"))
    service = StateService(db, actor="fixture", project_read_trusted=True)
    service.store.create_project("alpha", "Alpha")
    service.store.create_project(STEWARD_PROJECT, "Home")
    registry = DriverRegistry(db, "dsh-fixture-pepper-" * 3)
    registry.seed("fixture-owner-token")
    return DshDrivers(db, registry, tmp_path / "drivers", command=[sys.executable,
        "-c", "import time; time.sleep(90)"])


def _patch(runtime, row):
    import yaml

    class _Any(yaml.SafeLoader):
        pass
    _Any.add_multi_constructor("tag:yaml.org,2002:",
                               lambda loader, suffix, node: loader.construct_scalar(node))
    path = runtime.root / row["driver_id"] / "home" / "profiles" / "driver" / "cordis.patch.yml"
    return yaml.load(path.read_text(), Loader=_Any)


def _inserted_ids(patches):
    return [p["id"] for p in next(p for p in patches if "insert" in p)["insert"]]


def test_only_the_reserved_project_is_the_steward():
    assert RESERVED_ROLES == {STEWARD_PROJECT: "steward"}
    assert role_for_project(STEWARD_PROJECT) == "steward"
    for other in ("alpha", "aitelier", "aitelier-home-2", "", None):
        assert role_for_project(other) == "director"


def test_completion_route_and_compaction_follow_the_projects_role(monkeypatch):
    monkeypatch.delenv("AITELIER_DSH_COMPACT_RATIO", raising=False)
    table = _table()
    steward = {"project_id": STEWARD_PROJECT, "session_id": "root", "provider": "aitelier", "model": ""}
    director = dict(steward, project_id="alpha")
    assert completion_route(steward, "root", table) == "steward-route"
    assert completion_route(director, "root", table) == table["director"]["route"]
    # Children are workers whoever the root is; the row's model still wins for the root.
    assert completion_route(steward, "child", table) == table["worker"]["route"]
    assert completion_route(dict(steward, model="owner-pick"), "root", table) == "owner-pick"
    root = compaction_config(steward, table)["modelPolicies"][0]
    assert root == {"provider": "aitelier", "model": "steward-route", "thresholdRatio": 0.4, "retainTokens": 3}
    assert compaction_config(director, table)["modelPolicies"][0] == {
        "provider": "aitelier", "model": table["director"]["route"], **table["director"]["compaction"]}


def test_a_table_without_a_steward_refuses_the_steward_and_nobody_else(monkeypatch):
    monkeypatch.delenv("AITELIER_DSH_COMPACT_RATIO", raising=False)
    table = _table(steward=False)
    steward = {"project_id": STEWARD_PROJECT, "session_id": "root", "provider": "aitelier", "model": ""}
    for call in (lambda: completion_route(steward, "root", table), lambda: compaction_config(steward, table)):
        with pytest.raises(ValueError, match="no steward entry"):
            call()
    director = dict(steward, project_id="alpha")
    assert completion_route(director, "root", table) == table["director"]["route"]
    compaction_config(director, table)


def test_starting_the_steward_without_a_steward_entry_fails_before_provisioning(runtime, roles_file):
    roles_file.write_text(json.dumps(_table(steward=False)))
    for model in (None, "driver"):  # a named model does not skip the check
        with pytest.raises(ValueError, match="aitelier-home runs as steward, and roles.json has no steward entry"):
            asyncio.run(runtime.start(STEWARD_PROJECT, model=model, actor="fixture"))
    assert runtime.get(STEWARD_PROJECT) is None, "nothing was provisioned"
    assert not runtime.root.exists() or not any(runtime.root.iterdir())


def test_provisioning_records_the_role_in_capabilities(runtime, roles_file):
    steward = runtime._provision(STEWARD_PROJECT, "aitelier", "steward-route", "fixture")
    director = runtime._provision("alpha", "aitelier", "driver", "fixture")
    assert runtime.registry.get(steward["driver_id"])["capabilities"] == {
        "runtime": "dsh", "project_id": STEWARD_PROJECT, "role": "steward"}
    assert runtime.registry.get(director["driver_id"])["capabilities"] == {
        "runtime": "dsh", "project_id": "alpha", "role": "director"}


def test_steward_profile_has_no_state_mcp_and_its_own_compaction(runtime, roles_file, monkeypatch):
    monkeypatch.delenv("AITELIER_DSH_COMPACT_RATIO", raising=False)
    steward = runtime._provision(STEWARD_PROJECT, "aitelier", "steward-route", "fixture")
    director = runtime._provision("alpha", "aitelier", "driver", "fixture")
    runtime._environment(steward)
    runtime._environment(director)
    steward_patch, director_patch = _patch(runtime, steward), _patch(runtime, director)
    assert "mcp-aitelier" not in _inserted_ids(steward_patch)
    assert _inserted_ids(steward_patch)[:2] == ["dsh-director", "aitelier-driver"]
    assert "mcp-aitelier" in _inserted_ids(director_patch)
    # Apart from the State MCP entry, the local MCP services and the compaction
    # policy, the two profiles are the same. The steward gets none of the local
    # services: its role presents no tool of theirs, and the pwmcp entry pointed
    # it at the SHARED browser, where its own `initialize` evicted a director.
    compaction = {p["id"]: p for p in steward_patch if p.get("id")}["compaction-basic"]["config"]
    assert compaction["modelPolicies"][0] == {"provider": "aitelier", "model": "steward-route",
                                              "thresholdRatio": 0.4, "retainTokens": 3}
    strip = lambda patches: [p for p in patches if p.get("id") != "compaction-basic" and "insert" not in p]
    assert strip(steward_patch) == strip(director_patch)
    extras = {"mcp-aitelier", "mcp-devmcp", "mcp-zvec", "mcp-agentmcp", "mcp-pwmcp"}
    # Step 24.1a (design-steward 7.8.18): the steward's inserts are the director's without
    # those five, plus ONE fixed entry for the backend's gateway (its own connections).
    steward_inserts = next(p for p in steward_patch if "insert" in p)["insert"]
    assert ([e for e in steward_inserts if e["id"] != "mcp-sw"]
            == [e for e in next(p for p in director_patch if "insert" in p)["insert"]
                if e["id"] not in extras])
    assert [e["config"] for e in steward_inserts if e["id"] == "mcp-sw"] == [{
        "serverName": "sw", "transport": "streamable-http", "url": runtime.base_url.rstrip("/") + "/api/steward/mcp",
        "headers": {"X-AItelier-Driver-Token": "process.env.AITELIER_DRIVER_TOKEN"},
        "failOnStartupError": False, "toolCallTimeoutMs": 180000, "reconnect": {"maxAttempts": 1000000}}]
    assert "mcp-sw" not in _inserted_ids(director_patch)
    raw = (runtime.root / steward["driver_id"] / "home" / "profiles" / "driver" / "cordis.patch.yml").read_text()
    assert "!!js process.env.AITELIER_DRIVER_TOKEN" in raw and "__DRIVER_TOKEN__" not in raw


def test_the_steward_profile_does_not_change_when_its_connections_do(runtime, roles_file, monkeypatch, tmp_path):
    """Step 24.1a: adding or deleting a connection never touches the steward's profile
    (the gateway announces list_changed instead); a director's profile is unchanged by it."""
    from core.state_service import StateService
    from core.state_steward_connections import StateStewardConnections
    monkeypatch.setenv("AITELIER_STEWARD_SECRETS_DIR", str(tmp_path / "secrets"))
    steward = runtime._provision(STEWARD_PROJECT, "aitelier", "steward-route", "fixture")
    director = runtime._provision("alpha", "aitelier", "driver", "fixture")
    profile = lambda row: (runtime.root / row["driver_id"] / "home" / "profiles" / "driver" / "cordis.patch.yml")
    runtime._environment(steward)
    runtime._environment(director)
    before = profile(steward).read_bytes(), profile(director).read_bytes()
    owner = StateService(runtime.db, actor="owner:o@example.com", project_read_trusted=True)
    import core.state_steward_connections as sc
    import socket

    def resolve(host, port):
        if host != "mcp_server":
            raise socket.gaierror(host)
        return [(2, 1, 6, "", ("172.30.0.5", 0))]
    monkeypatch.setattr(sc, "RESOLVER", resolve)
    owner.steward_connections.upsert("media", "Media", "http://mcp_server:9003/mcp", visibility="public")
    runtime._environment(steward)
    runtime._environment(director)
    owner.steward_connections.delete("media")
    runtime._environment(steward)
    runtime._environment(director)
    assert (profile(steward).read_bytes(), profile(director).read_bytes()) == before
    assert isinstance(owner.steward_connections, StateStewardConnections)


def test_steward_profile_loads_a_chat_channel_plugin_only_when_its_secrets_exist(runtime, roles_file, monkeypatch, tmp_path):
    """Step 19 B: dsh-channel-slack / dsh-channel-wechat are inserted for the steward only,
    each only when every secret file it reads exists (and the plugin is in the checkout);
    absent, the profile is exactly today's."""
    import functools
    home, checkout = tmp_path / "home", tmp_path / "checkout"
    (home / ".aitelier-secrets").mkdir(parents=True)
    for plugin in dsh_driver.CHANNEL_PLUGINS:
        (checkout / "integrations" / plugin).mkdir(parents=True)
        (checkout / "integrations" / plugin / "index.mjs").write_text("export const name = 'x';")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(dsh_driver, "enabled_channel_plugins",
                        functools.partial(dsh_driver.enabled_channel_plugins, checkout=checkout))
    steward = runtime._provision(STEWARD_PROJECT, "aitelier", "steward-route", "fixture")
    director = runtime._provision("alpha", "aitelier", "driver", "fixture")

    def inserted():
        runtime._environment(steward)
        runtime._environment(director)
        return _inserted_ids(_patch(runtime, steward)), _inserted_ids(_patch(runtime, director))

    without = inserted()
    assert not [i for ids in without for i in ids if i.startswith("dsh-channel-")]
    secret = lambda name: (home / ".aitelier-secrets" / name).write_text("fixture-not-a-token")
    secret("slack-app-token")
    assert inserted() == without, "half of Slack's secrets loads nothing"
    secret("slack-bot-token")
    steward_ids, director_ids = inserted()
    assert steward_ids == without[0] + ["dsh-channel-slack"]
    assert director_ids == without[1], "a director never gets a channel"
    secret("wechat-bridge-token")
    steward_ids, director_ids = inserted()
    assert steward_ids == without[0] + ["dsh-channel-slack", "dsh-channel-wechat"]
    assert director_ids == without[1]
    entries = next(p for p in _patch(runtime, steward) if "insert" in p)["insert"]
    assert {e["id"]: e["name"] for e in entries if e["id"].startswith("dsh-channel-")} == {
        plugin: str(checkout / "integrations" / plugin / "index.mjs") for plugin in dsh_driver.CHANNEL_PLUGINS}
    # A secret without the plugin in the checkout names nothing DSH cannot load.
    (checkout / "integrations" / "dsh-channel-wechat" / "index.mjs").unlink()
    assert inserted()[0] == without[0] + ["dsh-channel-slack"]


def test_start_passes_the_role_only_to_the_steward(runtime, roles_file, monkeypatch):
    monkeypatch.setenv("AITELIER_DSH_ROLE", "steward")  # a leaked variable never reaches a director
    monkeypatch.setenv("AITELIER_DSH_MEMORY_USER", "someone")
    steward = runtime._provision(STEWARD_PROJECT, "aitelier", "steward-route", "fixture")
    director = runtime._provision("alpha", "aitelier", "driver", "fixture")
    steward_env, director_env = runtime._environment(steward), runtime._environment(director)
    assert steward_env["AITELIER_DSH_ROLE"] == "steward"
    assert steward_env["AITELIER_DSH_MEMORY_USER"] == "owner"
    assert "AITELIER_DSH_ROLE" not in director_env and "AITELIER_DSH_MEMORY_USER" not in director_env
    per_driver = {"AITELIER_DRIVER_ID", "AITELIER_DRIVER_TOKEN", "AITELIER_DRIVER_TOKEN_FILE", "DSH_HOME",
                  "AITELIER_DSH_PROJECT", "AITELIER_DSH_SESSION", "AITELIER_DSH_MODEL", "AITELIER_DSH_STATE_DIR"}
    assert ({k: v for k, v in steward_env.items() if k not in per_driver}
            == {**{k: v for k, v in director_env.items() if k not in per_driver},
                "AITELIER_DSH_ROLE": "steward", "AITELIER_DSH_MEMORY_USER": "owner"})


def test_start_defaults_the_steward_to_its_own_route_and_directors_to_theirs(runtime, roles_file):
    async def go():
        try:
            await runtime.start(STEWARD_PROJECT, actor="fixture")
            await runtime.start("alpha", actor="fixture")
        finally:
            await runtime.close()
    asyncio.run(go())
    assert runtime.get(STEWARD_PROJECT)["model"] == "steward-route"
    assert runtime.get("alpha")["model"] == load_roles()["director"]["route"]
