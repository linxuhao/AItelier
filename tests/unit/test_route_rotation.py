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
