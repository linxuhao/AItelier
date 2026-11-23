"""roles.json from the Python side: the loader, the completion route and the profile.

integrations/dsh-driver/roles.mjs parses the same file; the parity tests run node on
the same inputs and require the same parse and the same refusals, so "both sides
accept anything" cannot pass as agreement."""
import copy
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from core import dsh_driver
from core.dsh_driver import DshDrivers, ROLES_FILE, completion_route, compaction_config, load_roles, validate_roles
from core.drivers import DriverRegistry
from core.state_database import StateDatabase
from core.state_service import StateService
from tests.support import state_template

REPO = Path(__file__).resolve().parents[2]
ROLES_MJS = REPO / "integrations" / "dsh-driver" / "roles.mjs"
NODE = shutil.which("node")


@pytest.fixture
def runtime(tmp_path, state_db_copy):
    db = StateDatabase(state_db_copy(state_template.state_registry_dsh, tmp_path / "state.sqlite"))
    service = StateService(db, actor="fixture", project_read_trusted=True)
    service.store.create_project("alpha", "Alpha")
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
    return {p.get("id"): p for p in yaml.load(path.read_text(), Loader=_Any)}


def _node_parse(path):
    """roles.mjs's verdict on a file: ("ok", table) or ("error", message)."""
    script = ("import { loadRoles } from " + json.dumps(ROLES_MJS.as_uri()) + ";"
              "try { console.log(JSON.stringify({ ok: loadRoles(process.argv[1]) })); }"
              "catch (error) { console.log(JSON.stringify({ error: String(error.message) })); }")
    out = subprocess.run([NODE, "--input-type=module", "-e", script, str(path)],
                         capture_output=True, text=True, timeout=60, check=True).stdout
    result = json.loads(out)
    return ("ok", result["ok"]) if "ok" in result else ("error", result["error"])


def _python_parse(path):
    try:
        return ("ok", load_roles(path))
    except ValueError as error:
        return ("error", str(error))


def _variant(edit):
    table = json.loads(ROLES_FILE.read_text())
    edit(table)
    return table


# A valid steward entry (plugin step 12), for the tables that carry one.
STEWARD = {"route": "driver", "reasoning_effort": "high",
           "compaction": {"thresholdRatio": 0.4, "retainTokens": 0}, "prompt": "steward.md",
           "presented": ["ask_owner", "close_question", "idle_dormant"],
           "forbidden": ["bash", "subagent", "todo_plan"], "presented_only": True}


# A valid image_retain (step 24.2c), the steward's committed values.
IMAGE_RETAIN = {"max_images": 4, "count_quantum": 4, "max_base64_bytes": 6291456, "byte_quantum": 1048576}


def _with_steward(table, **changes):
    table["steward"] = {**copy.deepcopy(STEWARD), **changes}


MALFORMED = {
    "unknown top-level key": lambda t: t.update(extra=1),
    "unknown role key": lambda t: t["worker"].update(models=[]),
    "unknown compaction key": lambda t: t["director"]["compaction"].update(retainRatio=0.1),
    "missing role key": lambda t: t["worker"].pop("forbidden"),
    "missing threshold": lambda t: t["director"]["compaction"].pop("thresholdRatio"),
    "bad version": lambda t: t.update(version=2),
    "boolean version": lambda t: t.update(version=True),
    "bad effort": lambda t: t["worker"].update(reasoning_effort="xhigh"),
    "empty route": lambda t: t["worker"].update(route=""),
    "ratio above one": lambda t: t["worker"]["compaction"].update(thresholdRatio=1.5),
    "negative retain": lambda t: t["director"]["compaction"].update(retainTokens=-1),
    "fractional retain": lambda t: t["director"]["compaction"].update(retainTokens=1.5),
    "prompt path": lambda t: t["worker"].update(prompt="../driver.md"),
    "duplicate tool": lambda t: t["worker"]["presented"].append("bash"),
    "presented and forbidden": lambda t: t["worker"]["presented"].append("subagent"),
    "bad tool name": lambda t: t["worker"]["forbidden"].append("not a name"),
    "unknown steward key": lambda t: _with_steward(t, models=[]),
    "missing steward key": lambda t: (_with_steward(t), t["steward"].pop("prompt")),
    "steward presents and forbids": lambda t: _with_steward(t, forbidden=["ask_owner"]),
    "non-boolean steward presented_only": lambda t: _with_steward(t, presented_only="yes"),
    "non-boolean director presented_only": lambda t: t["director"].update(presented_only=1),
    "null worker presented_only": lambda t: t["worker"].update(presented_only=None),
    # Step 24.1a: `catalog` is optional on any role and has one value.
    "unknown steward catalog": lambda t: _with_steward(t, catalog="dsh"),
    "boolean director catalog": lambda t: t["director"].update(catalog=True),
    # Step 24.2c: `child_route` (a route name) and `image_retain` (four positive integers,
    # exact keys) are optional on any role.
    "misspelt steward child_route key": lambda t: _with_steward(t, child_routes="steward"),
    "empty steward child_route": lambda t: _with_steward(t, child_route=""),
    "non-string director child_route": lambda t: t["director"].update(child_route=1),
    "null worker child_route": lambda t: t["worker"].update(child_route=None),
    "misspelt image_retain key": lambda t: _with_steward(t, image_retain={
        **IMAGE_RETAIN, "max_image": 4}),
    "missing image_retain key": lambda t: _with_steward(t, image_retain={
        k: v for k, v in IMAGE_RETAIN.items() if k != "byte_quantum"}),
    "image_retain not an object": lambda t: _with_steward(t, image_retain=4),
    "image_retain a list": lambda t: _with_steward(t, image_retain=[4, 4, 6291456, 1048576]),
    "zero count_quantum": lambda t: _with_steward(t, image_retain={**IMAGE_RETAIN, "count_quantum": 0}),
    "negative byte_quantum": lambda t: _with_steward(t, image_retain={**IMAGE_RETAIN, "byte_quantum": -1}),
    "zero max_images": lambda t: _with_steward(t, image_retain={**IMAGE_RETAIN, "max_images": 0}),
    "fractional max_images": lambda t: _with_steward(t, image_retain={**IMAGE_RETAIN, "max_images": 1.5}),
    "boolean max_images": lambda t: _with_steward(t, image_retain={**IMAGE_RETAIN, "max_images": True}),
    "string max_base64_bytes": lambda t: _with_steward(t, image_retain={
        **IMAGE_RETAIN, "max_base64_bytes": "6291456"}),
    "null director image_retain": lambda t: t["director"].update(image_retain=None),
    "missing director": lambda t: t.pop("director"),
}


def test_the_repository_table_loads_with_the_production_values():
    """The values moved into roles.json unchanged (measured before the move: root
    effort max - AITELIER_DSH_ROOT_REASONING_EFFORT is not set on the host unit -,
    children high, director compaction 0.25 / retainTokens 0, workers 0.5 with DSH's
    default tail, routes driver and worker)."""
    table = load_roles()
    assert table["director"]["route"] == "driver" and table["worker"]["route"] == "worker"
    assert table["director"]["reasoning_effort"] == "max" and table["worker"]["reasoning_effort"] == "high"
    assert table["director"]["compaction"] == {"thresholdRatio": 0.25, "retainTokens": 0}
    assert table["worker"]["compaction"] == {"thresholdRatio": 0.5}


@pytest.mark.parametrize("name", sorted(MALFORMED))
def test_the_python_loader_refuses_malformed_tables(name):
    with pytest.raises(ValueError, match="^roles: "):
        validate_roles(_variant(MALFORMED[name]))


@pytest.mark.skipif(NODE is None, reason="node is not installed in this environment")
def test_python_and_node_parse_roles_json_identically():
    assert _node_parse(ROLES_FILE) == ("ok", load_roles())


@pytest.mark.skipif(NODE is None, reason="node is not installed in this environment")
@pytest.mark.parametrize("name", sorted(MALFORMED))
def test_python_and_node_refuse_the_same_tables(tmp_path, name):
    path = tmp_path / "roles.json"
    path.write_text(json.dumps(_variant(MALFORMED[name])))
    python, node = _python_parse(path), _node_parse(path)
    assert python[0] == node[0] == "error", (python, node)
    # Same first complaint, word for word.
    assert python[1] == node[1]


@pytest.mark.skipif(NODE is None, reason="node is not installed in this environment")
def test_python_and_node_agree_on_an_accepted_variant(tmp_path):
    table = _variant(lambda t: (t["worker"].update(route="w-x", reasoning_effort="low"),
                                t["director"]["compaction"].update(retainTokens=7)))
    path = tmp_path / "roles.json"
    path.write_text(json.dumps(table))
    assert _python_parse(path) == _node_parse(path) == ("ok", table)


def test_steward_and_presented_only_are_optional_and_accepted_when_well_formed():
    """No steward: the repository table as it is (asserted above). With one, and
    `presented_only` on any role, the table parses unchanged."""
    without = _variant(lambda t: t.pop("steward", None))
    assert validate_roles(copy.deepcopy(without)) == without
    with_steward = _variant(lambda t: (_with_steward(t), t["worker"].update(presented_only=False),
                                       t["director"].update(presented_only=True)))
    assert validate_roles(copy.deepcopy(with_steward)) == with_steward
    assert with_steward["steward"] == STEWARD


def test_the_director_presents_media_call():
    """Step 24.2b (design-steward 7.8.12a "导演与管家的不同"): the repository's director presents
    media_call (its own media library's refs through its own media connection); the worker does not."""
    table = load_roles()
    assert "media_call" in table["director"]["presented"]
    assert "media_call" not in table["director"]["forbidden"]
    assert "media_call" not in table["worker"]["presented"]


@pytest.mark.skipif(NODE is None, reason="node is not installed in this environment")
def test_python_and_node_agree_on_the_repository_table():
    """The file itself, as both loaders read it - the director's media_call included."""
    verdict = _node_parse(ROLES_FILE)
    assert verdict == ("ok", load_roles())
    assert "media_call" in verdict[1]["director"]["presented"]


def test_the_steward_finds_its_tools_in_its_hosts_catalog():
    """Step 24.1a (design-steward 7.8.7): the repository's steward presents search_tools
    and call_tool, no longer forbids them, and searches only its host's catalog."""
    steward = load_roles()["steward"]
    assert steward["catalog"] == "host"
    assert {"search_tools", "call_tool"} <= set(steward["presented"])
    assert not {"search_tools", "call_tool"} & set(steward["forbidden"])


@pytest.mark.skipif(NODE is None, reason="node is not installed in this environment")
def test_python_and_node_agree_on_a_catalog_role(tmp_path):
    table = _variant(lambda t: (_with_steward(t, catalog="host"), t["worker"].update(catalog="host")))
    path = tmp_path / "roles.json"
    path.write_text(json.dumps(table))
    assert _python_parse(path) == _node_parse(path) == ("ok", table)


def test_the_steward_takes_its_own_route_for_children_and_bounds_its_seen_images():
    """Step 24.2c (design-steward 7.8.12b "九" 3, "八" 4): the repository's steward sends its
    children to the steward route and keeps at most 4 seen images / 6 MiB of base64; the
    byte cap is not below one request image (4 MiB raw = 5,592,408 base64 bytes)."""
    table = load_roles()
    steward = table["steward"]
    assert steward["child_route"] == "steward" == steward["route"]
    assert steward["image_retain"] == IMAGE_RETAIN
    assert steward["image_retain"]["max_base64_bytes"] >= -(-4 * 1024 * 1024 // 3) * 4 == 5_592_408
    for role in ("director", "worker"):
        assert "child_route" not in table[role] and "image_retain" not in table[role]


def test_child_route_and_image_retain_are_accepted_on_any_role():
    table = _variant(lambda t: (t["director"].update(child_route="d-children", image_retain=dict(IMAGE_RETAIN)),
                                t["worker"].update(image_retain={**IMAGE_RETAIN, "max_images": 1})))
    assert validate_roles(copy.deepcopy(table)) == table


@pytest.mark.skipif(NODE is None, reason="node is not installed in this environment")
def test_python_and_node_agree_on_child_route_and_image_retain(tmp_path):
    table = _variant(lambda t: (_with_steward(t, child_route="s-children", image_retain=dict(IMAGE_RETAIN)),
                                t["director"].update(child_route="d-children"),
                                t["worker"].update(image_retain={**IMAGE_RETAIN, "byte_quantum": 1})))
    path = tmp_path / "roles.json"
    path.write_text(json.dumps(table))
    assert _python_parse(path) == _node_parse(path) == ("ok", table)


@pytest.mark.skipif(NODE is None, reason="node is not installed in this environment")
@pytest.mark.parametrize("name", ["with steward", "without steward"])
def test_python_and_node_agree_on_steward_tables(tmp_path, name):
    if name == "with steward":
        table = _variant(lambda t: (_with_steward(t), t["director"].update(presented_only=False)))
    else:
        table = _variant(lambda t: t.pop("steward", None))
    path = tmp_path / "roles.json"
    path.write_text(json.dumps(table))
    assert _python_parse(path) == _node_parse(path) == ("ok", table)


def test_a_worker_only_ever_gets_the_tables_worker_route():
    table = load_roles()
    for model in ("driver", "worker", "deepseek/deepseek-flash", "anything-the-owner-set"):
        row = {"session_id": "root", "model": model}
        assert completion_route(row, "root", table) == model, "row['model'] overrides the director route"
        for child in ("child-a", "worker", "driver", model, "", "ROOT"):
            assert completion_route(row, child, table) == table["worker"]["route"]
    renamed = copy.deepcopy(table)
    renamed["worker"]["route"] = "w-renamed"
    assert completion_route({"session_id": "root", "model": "driver"}, "child", renamed) == "w-renamed"
    assert completion_route({"session_id": "root", "model": ""}, "root", table) == table["director"]["route"]


def test_the_completion_endpoint_takes_its_route_from_the_table():
    source = (REPO / "api" / "dsh_driver_routers.py").read_text()
    assert "route = completion_route(row, body.session_id)" in source
    assert not re.search(r'else "worker"', source)


def test_profile_compaction_and_subagent_route_follow_the_table(runtime, tmp_path, monkeypatch):
    monkeypatch.delenv("AITELIER_DSH_COMPACT_RATIO", raising=False)
    variant = _variant(lambda t: (t["worker"].update(route="w-variant", compaction={"thresholdRatio": 0.6}),
                                  t["director"].update(compaction={"thresholdRatio": 0.3, "retainTokens": 7})))
    path = tmp_path / "roles.json"
    path.write_text(json.dumps(variant))
    monkeypatch.setattr(dsh_driver, "ROLES_FILE", path)
    row = runtime._provision("alpha", "aitelier", "driver", "fixture")
    runtime._environment(row)
    patch = _patch(runtime, row)
    assert patch["compaction-basic"]["config"] == {"thresholdRatio": 0.6, "modelPolicies": [
        {"provider": "aitelier", "model": "driver", "thresholdRatio": 0.3, "retainTokens": 7},
        {"provider": "aitelier", "model": "w-variant", "thresholdRatio": 0.6}]}
    assert patch["tool-subagent"]["config"]["agentOptions"] == {"provider": "aitelier", "model": "w-variant"}


def test_row_model_never_changes_compaction_and_never_duplicates_a_policy(monkeypatch):
    monkeypatch.delenv("AITELIER_DSH_COMPACT_RATIO", raising=False)
    table = load_roles()
    expected_director = {"thresholdRatio": 0.25, "retainTokens": 0}
    for model in ("driver", "deepseek/deepseek-flash", "worker"):
        config = compaction_config({"provider": "aitelier", "model": model}, table)
        director = config["modelPolicies"][0]
        assert {k: director[k] for k in expected_director} == expected_director
        keys = [(p["provider"], p["model"]) for p in config["modelPolicies"]]
        assert len(keys) == len(set(keys)), "DSH refuses a duplicate provider/model policy at load"
        assert config["thresholdRatio"] == 0.5
    # The director's route IS the worker route: one policy, the director's (as before).
    assert compaction_config({"provider": "aitelier", "model": "worker"}, table)["modelPolicies"] == [
        {"provider": "aitelier", "model": "worker", "thresholdRatio": 0.25, "retainTokens": 0}]


def test_an_empty_row_model_gets_the_same_director_model_in_route_and_compaction(monkeypatch):
    """Review R2: the completion route already falls back to the table's director
    route for an empty row model; the compaction policy must name that same model,
    never a null one."""
    monkeypatch.delenv("AITELIER_DSH_COMPACT_RATIO", raising=False)
    table = load_roles()
    for empty in ("", None):
        row = {"provider": "aitelier", "model": empty, "session_id": "root"}
        route = completion_route(row, "root", table)
        policy = compaction_config(row, table)["modelPolicies"][0]
        assert route == table["director"]["route"] and policy["model"] == route, (route, policy)


def test_start_defaults_to_the_tables_director_route(runtime):
    async def go():
        try:
            return await runtime.start("alpha", actor="fixture")
        finally:
            await runtime.close()
    import asyncio
    asyncio.run(go())
    assert runtime.get("alpha")["model"] == load_roles()["director"]["route"]


def test_dsh_driver_keeps_no_route_or_compaction_literal():
    source = (REPO / "core" / "dsh_driver.py").read_text()
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
    for pattern in (r'"model":\s*"(worker|driver)"', r'model="(worker|driver)"', r'else "worker"',
                    r'"retainTokens":\s*\d', r'"thresholdRatio":\s*\d', r'\b0\.25\b', r'"0\.25"'):
        assert not re.search(pattern, code), pattern
