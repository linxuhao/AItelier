"""Editing the routing tables through an API, without being able to break them.

The read side is easy. What these tests are for is the REFUSALS: every one of
them stands between an edit and a failure that would surface at some later
step's first LLM call, with an error pointing at the route table rather than at
the edit that caused it.
"""
import json

import pytest

from core import model_registry as reg
from core import model_routes


@pytest.fixture
def tables(tmp_path, monkeypatch):
    """Point the registry at throwaway copies of the two files."""
    providers = {"alpha": {"base_url": "https://a.test/v1",
                           "api_key_env": "ALPHA_KEY"},
                 "beta": {"base_url": "https://b.test/v1",
                          "api_key_env": "BETA_KEY"}}
    routes = {"_comment": ["ignored"],
              "fast": ["alpha/m-1", "beta/m-1"],
              "solo": ["alpha/m-2"]}
    (tmp_path / "llm_providers.json").write_text(json.dumps(providers))
    (tmp_path / "model_routes.json").write_text(json.dumps(routes))
    monkeypatch.setattr(reg, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(reg, "config_or_example",
                        lambda name: str(tmp_path / name))
    model_routes.reset_cache()
    yield tmp_path
    model_routes.reset_cache()


def _routes(tables):
    return json.loads((tables / "model_routes.json").read_text())


def _providers(tables):
    return json.loads((tables / "llm_providers.json").read_text())


# ── read ─────────────────────────────────────────────────────────────────────

def test_available_models_says_whether_each_endpoint_can_actually_serve(tables):
    out = reg.list_models()
    fast = next(m for m in out["models"] if m["model"] == "fast")
    first = fast["endpoints"][0]
    assert first["endpoint"] == "alpha/m-1"
    assert first["provider_registered"] is True
    assert first["api_key_env"] == "ALPHA_KEY"
    # Listed is not the same as usable — these three columns are the difference.
    assert set(first) >= {"provider_registered", "key_present", "cooldown_seconds"}


def test_an_unregistered_provider_shows_as_such_rather_than_being_hidden(tables):
    doc = _routes(tables)
    doc["fast"].append("ghost/m-9")
    (tables / "model_routes.json").write_text(json.dumps(doc))
    model_routes.reset_cache()
    fast = next(m for m in reg.list_models()["models"] if m["model"] == "fast")
    ghost = next(c for c in fast["endpoints"] if c["endpoint"] == "ghost/m-9")
    assert ghost["provider_registered"] is False


# ── providers ────────────────────────────────────────────────────────────────

def test_add_provider_records_the_key_NAME_and_says_the_key_is_a_file(tables):
    out = reg.add_provider("gamma", "https://g.test/v1", "GAMMA_KEY")
    assert _providers(tables)["gamma"]["api_key_env"] == "GAMMA_KEY"
    # The one thing an operator will get wrong: expecting this to store the key.
    assert "aitelier-secrets/GAMMA_KEY" in out["next_step"]
    assert "secrets:" in out["next_step"]        # and that compose needs it too


def test_a_provider_name_that_would_break_candidate_parsing_is_refused(tables):
    with pytest.raises(reg.RegistryError, match="alphanumeric"):
        reg.add_provider("bad/name", "https://x.test/v1")


def test_deleting_a_provider_a_model_still_uses_is_refused(tables):
    """It would reach litellm as a bare provider/model it cannot place — a
    client-side BadRequestError, which is deliberately NOT a failover error, so
    the gateway dies on it with healthy candidates still queued behind."""
    with pytest.raises(reg.RegistryError) as ei:
        reg.delete_provider("alpha")
    assert "fast" in str(ei.value) and "solo" in str(ei.value)
    assert "alpha" in _providers(tables)


def test_a_provider_nothing_uses_can_be_deleted(tables):
    reg.add_provider("gamma", "https://g.test/v1")
    reg.delete_provider("gamma")
    assert "gamma" not in _providers(tables)


# ── models ───────────────────────────────────────────────────────────────────

def test_a_model_with_no_endpoints_is_refused(tables):
    with pytest.raises(reg.RegistryError, match="at least one endpoint"):
        reg.add_model("empty", [])


def test_an_endpoint_whose_provider_is_not_registered_is_refused(tables):
    with pytest.raises(reg.RegistryError) as ei:
        reg.add_model("new", ["ghost/m-1"])
    assert "not registered" in str(ei.value)
    assert "new" not in _routes(tables)


def test_an_internal_name_shaped_like_an_endpoint_is_refused(tables):
    """`ark/deepseek-v4-flash` as an INTERNAL name would read as a concrete
    endpoint everywhere it appears, which is exactly the confusion the two
    layers exist to remove."""
    with pytest.raises(reg.RegistryError, match="bare name"):
        reg.add_model("ark/deepseek-v4-flash", ["alpha/m-1"])


def test_map_appends_by_default_and_position_inserts(tables):
    reg.map_model("fast", "beta/m-2")
    assert _routes(tables)["fast"] == ["alpha/m-1", "beta/m-1", "beta/m-2"]
    reg.map_model("fast", "beta/m-3", position=0)
    assert _routes(tables)["fast"][0] == "beta/m-3"


def test_mapping_the_same_endpoint_twice_is_refused(tables):
    with pytest.raises(reg.RegistryError, match="already an endpoint"):
        reg.map_model("fast", "alpha/m-1")


def test_unmapping_the_last_endpoint_is_refused(tables):
    """A model that resolves to nothing does not fail here — it fails at the
    first call of whatever step still names it."""
    with pytest.raises(reg.RegistryError, match="only endpoint"):
        reg.unmap_model("solo", "alpha/m-2")
    assert _routes(tables)["solo"] == ["alpha/m-2"]


def test_deleting_a_model_something_references_is_refused(tables, monkeypatch):
    monkeypatch.setattr(reg, "model_consumers",
                        lambda name: ["agent_configs/dpe_default.yaml"]
                        if name == "fast" else [])
    with pytest.raises(reg.RegistryError) as ei:
        reg.delete_model("fast")
    assert "dpe_default" in str(ei.value)
    assert "fast" in _routes(tables)


def test_an_unreferenced_model_can_be_deleted(tables, monkeypatch):
    monkeypatch.setattr(reg, "model_consumers", lambda name: [])
    reg.delete_model("solo")
    assert "solo" not in _routes(tables)


def test_comment_keys_survive_a_write(tables):
    """The tables are hand-edited; an API write must not eat the operator's
    notes on its way through."""
    reg.map_model("fast", "beta/m-2")
    assert _routes(tables)["_comment"] == ["ignored"]


def test_a_write_lands_on_the_real_file_not_the_example(tmp_path, monkeypatch):
    """An operator editing through the API is making THIS deployment's choice;
    the example is committed content a `git pull` would overwrite."""
    (tmp_path / "llm_providers.example.json").write_text(json.dumps(
        {"alpha": {"base_url": "https://a.test/v1"}}))
    monkeypatch.setattr(reg, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        reg, "config_or_example",
        lambda name: str(tmp_path / name) if (tmp_path / name).is_file()
        else str(tmp_path / name.replace(".json", ".example.json")))
    model_routes.reset_cache()
    reg.add_provider("gamma", "https://g.test/v1")
    assert (tmp_path / "llm_providers.json").is_file()
    assert "gamma" not in json.loads(
        (tmp_path / "llm_providers.example.json").read_text())


# ── the two surfaces agree ───────────────────────────────────────────────────

def test_rest_and_mcp_expose_the_same_operations():
    """A refusal only one surface enforces is not a refusal. Both call
    core/model_registry, and this pins that neither grows a private path."""
    import inspect

    from api import mcp_router, model_routers

    rest = inspect.getsource(model_routers)
    mcp = inspect.getsource(mcp_router._register_model_tools)
    for fn in ("list_models", "list_providers", "add_provider",
               "update_provider", "delete_provider", "add_model",
               "map_model", "unmap_model", "delete_model"):
        assert f"reg.{fn}" in rest, f"REST does not call {fn}"
        assert f"reg.{fn}" in mcp, f"MCP does not call {fn}"


def test_every_mutating_mcp_tool_is_declared_write():
    """Declared `read`, a mutation would sail past api/mcp_router._authorize —
    the MCP path's only gate, since every call arrives as a POST to /mcp and the
    HTTP middleware cannot classify it."""
    from api.mcp_router import _TOOL_KIND, build_mcp

    build_mcp()
    for name in ("add_provider", "update_provider", "delete_provider",
                 "add_model", "map_model", "unmap_model", "delete_model"):
        assert _TOOL_KIND[name] == "write", name
    for name in ("get_available_models", "list_providers"):
        assert _TOOL_KIND[name] == "read", name


def test_the_tool_dependency_guard_still_matches_the_real_source():
    """`delete_model` is only safe if `model_consumers` actually sees the tool.

    Both delete tests monkeypatch `model_consumers`, so nothing else exercises
    the detection against the shipped file. If `godot_vision`'s `_ROUTE`
    assignment drifts far enough that the pattern misses, this fails loudly
    instead of silently permitting the deletion of a model the gate resolves.
    """
    assert reg.model_consumers("vision") == [
        "aitelier/tools/godot_vision/impl.py"]
    assert reg.model_consumers("definitely-not-a-route") == []
