"""Per-step rotation over subscription pools, pay-as-you-go pinned last.

Three plans each carry their own 5h/weekly window; the sticky gateway bound
every step to the first candidate, so the other plans' windows expired unused.
Rotating PER CALL would gut the prefix cache (26:1 prefill:decode lives inside
a step's tool loop); rotating per resolve(rotate=True) — one gateway per step —
spreads the quota at the one granularity the cache does not care about.
"""
import json

import pytest

from core.model_routes import ModelRoutes
import core.model_routes as mr


@pytest.fixture()
def table(tmp_path):
    p = tmp_path / "routes.json"
    p.write_text(json.dumps({
        "flash": {"rotate": ["a/m1", "b/m2", "c/m3"], "fallback": ["payg/m9"]},
        "solo": {"rotate": ["a/m1"], "fallback": ["payg/m9"]},
        "plain": ["a/m1", "payg/m9"],
    }))
    mr._rot_counters.clear()
    return ModelRoutes(p)


def test_rotation_advances_per_call_and_keeps_fallback_last(table):
    seq = [table.resolve("flash", rotate=True) for _ in range(4)]
    assert seq[0] == ["a/m1", "b/m2", "c/m3", "payg/m9"]
    assert seq[1] == ["b/m2", "c/m3", "a/m1", "payg/m9"]
    assert seq[2] == ["c/m3", "a/m1", "b/m2", "payg/m9"]
    assert seq[3] == seq[0]                      # full cycle
    for s in seq:
        assert s[-1] == "payg/m9"                # money never rotates forward


def test_default_resolve_is_deterministic(table):
    """external_deps derives required keys from candidates[0]; the vision judge
    panel is ordered on purpose. Neither passes rotate=True; neither may move."""
    table.resolve("flash", rotate=True)          # advance the counter…
    assert table.resolve("flash") == ["a/m1", "b/m2", "c/m3", "payg/m9"]


def test_single_member_pool_never_rotates(table):
    assert table.resolve("solo", rotate=True) == ["a/m1", "payg/m9"]
    assert table.resolve("solo", rotate=True) == ["a/m1", "payg/m9"]


def test_plain_list_routes_are_untouched_by_the_flag(table):
    assert table.resolve("plain", rotate=True) == ["a/m1", "payg/m9"]
    assert table.resolve("plain", rotate=True) == ["a/m1", "payg/m9"]


def test_malformed_rotation_form_is_refused(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"x": {"rotate": [], "fallback": ["a/b"]}}))
    with pytest.raises(RuntimeError, match="rotate"):
        ModelRoutes(p)
    p.write_text(json.dumps({"x": {"rotate": ["a/b"], "surprise": []}}))
    with pytest.raises(RuntimeError, match="unknown key"):
        ModelRoutes(p)


def test_registry_refuses_to_blind_edit_a_rotation_route(tmp_path, monkeypatch):
    """map/unmap assume list order IS the policy; on the dict form they must
    say what to do, not report the route as missing."""
    import core.model_registry as reg
    routes = tmp_path / "model_routes.json"
    provs = tmp_path / "llm_providers.json"
    routes.write_text(json.dumps(
        {"flash": {"rotate": ["a/m1", "b/m2"], "fallback": []}}))
    provs.write_text(json.dumps({"a": {"base_url": "u", "api_key_env": "K"},
                                 "b": {"base_url": "u", "api_key_env": "K"}}))
    monkeypatch.setattr(reg, "ROUTES_FILE", routes)
    monkeypatch.setattr(reg, "PROVIDERS_FILE", provs)
    with pytest.raises(reg.RegistryError, match="dict form"):
        reg.map_model("flash", "a/m3")


def test_registry_reads_see_dict_form_routes(tmp_path, monkeypatch):
    """The read side must flatten the dict form, or delete_provider's guard
    goes blind for providers named only inside a rotate pool — found live:
    opencodego was deletable while flash/pro still rotated through it, and
    /api/models had silently stopped listing both primary routes."""
    import json as _json
    import core.model_registry as reg
    routes = tmp_path / "model_routes.json"
    provs = tmp_path / "llm_providers.json"
    routes.write_text(_json.dumps(
        {"flash": {"rotate": ["a/m1", "b/m2"], "fallback": ["payg/m9"]},
         "plain": ["a/m1"]}))
    provs.write_text(_json.dumps({
        "a": {"base_url": "u", "api_key_env": ""},
        "b": {"base_url": "u", "api_key_env": ""},
        "payg": {"base_url": "u", "api_key_env": ""}}))
    monkeypatch.setattr(reg, "ROUTES_FILE", routes)
    monkeypatch.setattr(reg, "PROVIDERS_FILE", provs)

    assert reg.provider_consumers("b") == ["flash"]        # rotate member
    assert reg.provider_consumers("payg") == ["flash"]     # fallback member
    listed = {m["model"] for m in reg.list_models()["models"]}
    assert listed == {"flash", "plain"}
    flash = next(m for m in reg.list_models()["models"] if m["model"] == "flash")
    assert [e["endpoint"] for e in flash["endpoints"]] == ["a/m1", "b/m2", "payg/m9"]
    with pytest.raises(reg.RegistryError, match="still"):
        reg.delete_provider("b")


def test_next_usable_skips_a_keyless_candidate(tmp_path, monkeypatch):
    """With rotation, a pool member becomes the FIRST bound endpoint on ~1/n of
    steps; binding one whose declared key has no value buys a guaranteed
    AuthenticationError + failover on every such step. Missing key file means
    'unused' — the gateway must honor that reading too."""
    import json as _json
    from core.ai_router import AIGateway
    provs = tmp_path / "llm_providers.json"
    provs.write_text(_json.dumps({
        "nokey": {"base_url": "u", "api_key_env": "NOKEY_API_KEY"},
        "haskey": {"base_url": "u", "api_key_env": "HAS_API_KEY"},
    }))
    monkeypatch.setenv("HAS_API_KEY", "x")
    monkeypatch.delenv("NOKEY_API_KEY", raising=False)
    gw = AIGateway.__new__(AIGateway)
    gw._config_path = str(provs)
    gw._candidates = ["nokey/m1", "haskey/m2"]
    assert gw._next_usable(0) == 1
    # every candidate keyless → degrade to "try it anyway", never brick
    gw._candidates = ["nokey/m1", "nokey/m2"]
    assert gw._next_usable(0) == 0


def test_degrade_prefers_a_keyed_candidate_over_a_keyless_one(tmp_path, monkeypatch):
    """When everything usable is parked, 'try it anyway' must try something
    that CAN answer: a parked endpoint might (parking is prose-parsed
    guesswork), a keyless one cannot."""
    import json as _json
    import core.ai_router as ar
    provs = tmp_path / "llm_providers.json"
    provs.write_text(_json.dumps({
        "nokey": {"base_url": "u", "api_key_env": "NOKEY_API_KEY"},
        "parked": {"base_url": "u", "api_key_env": "PARKED_API_KEY"},
    }))
    monkeypatch.setenv("PARKED_API_KEY", "x")
    monkeypatch.delenv("NOKEY_API_KEY", raising=False)
    monkeypatch.setattr(ar, "_endpoint_available", lambda c: False)  # 全部停靠
    gw = ar.AIGateway.__new__(ar.AIGateway)
    gw._config_path = str(provs)
    gw._candidates = ["nokey/m1", "parked/m2"]
    assert gw._next_usable(0) == 1      # 有 key 的停靠者,而不是 start=0 的无 key 者


def test_string_form_route_is_visible_to_registry_reads(tmp_path, monkeypatch):
    """ModelRoutes coerces '"flash": "ark/m"' to a one-item list; the registry
    flatten must accept the same shape or the blindness bug returns one value
    shape over."""
    import json as _json
    import core.model_registry as reg
    routes = tmp_path / "model_routes.json"
    provs = tmp_path / "llm_providers.json"
    routes.write_text(_json.dumps({"flash": "a/m1"}))
    provs.write_text(_json.dumps({"a": {"base_url": "u", "api_key_env": ""}}))
    monkeypatch.setattr(reg, "ROUTES_FILE", routes)
    monkeypatch.setattr(reg, "PROVIDERS_FILE", provs)
    assert reg.provider_consumers("a") == ["flash"]
