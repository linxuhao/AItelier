"""Which routes can be sent an image: the table's `_input` (step 24.2b).

`_input` is keyed by CONCRETE candidate. A route takes images only when every
candidate it resolves to lists `image`: a bad request is not failed over, so a
failover onto a text-only candidate would make that turn a 400 (the owner's
whole-route reading, design pending 18 (a)). A bad `_input` must fail the load
like any other bad table, not be skipped as a comment.
"""
import json
from pathlib import Path

import pytest

from core import model_registry as reg
from core import model_routes
from core.model_routes import ModelRoutes

ROOT = Path(__file__).resolve().parents[2]

ROUTES = {
    "_comment": ["notes"],
    "seeing": ["alpha/vis-1", "beta/vis-1"],
    "mixed": ["alpha/vis-1", "beta/txt-1"],
    "rotating": {"rotate": ["alpha/vis-1", "beta/vis-1"],
                 "fallback": ["gamma/txt-2"],
                 "effort": {"alpha/vis-1": "high"}},
}
INPUT = {
    "alpha/vis-1": ["text", "image"],
    "beta/vis-1": ["text", "image"],
    "beta/txt-1": ["text"],
    "delta/vis-9": ["text", "image"],
}


def _table(tmp_path, doc) -> ModelRoutes:
    p = tmp_path / "model_routes.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return ModelRoutes(p)


# ── input_for ────────────────────────────────────────────────────────────────

def test_a_candidate_missing_from_input_takes_text_only(tmp_path):
    t = _table(tmp_path, {**ROUTES, "_input": INPUT})
    assert t.input_for("gamma/txt-2") == ["text"]
    assert t.input_for("alpha/vis-1") == ["text", "image"]
    assert t.input_for("beta/txt-1") == ["text"]


def test_input_for_hands_out_a_copy(tmp_path):
    t = _table(tmp_path, {**ROUTES, "_input": INPUT})
    t.input_for("gamma/txt-2").append("image")
    t.input_for("alpha/vis-1").clear()
    assert t.input_for("gamma/txt-2") == ["text"]
    assert t.input_for("alpha/vis-1") == ["text", "image"]


# ── accepts_image: the whole route ───────────────────────────────────────────

def test_a_route_takes_images_only_when_every_candidate_does(tmp_path):
    t = _table(tmp_path, {**ROUTES, "_input": INPUT})
    assert t.accepts_image("seeing") is True
    # One text-only candidate is enough to make the whole route text-only.
    assert t.accepts_image("mixed") is False


def test_a_dict_route_counts_its_fallback_tail_too(tmp_path):
    t = _table(tmp_path, {**ROUTES, "_input": INPUT})
    assert t.accepts_image("rotating") is False
    doc = {**ROUTES, "_input": {**INPUT, "gamma/txt-2": ["text", "image"]}}
    assert _table(tmp_path, doc).accepts_image("rotating") is True


def test_a_concrete_project_model_is_its_own_candidate(tmp_path):
    t = _table(tmp_path, {**ROUTES, "_input": INPUT})
    # Not a route, not in `_input` -> text only.
    assert t.accepts_image("epsilon/unknown-1") is False
    # Not a route, listed with image -> image.
    assert t.accepts_image("delta/vis-9") is True
    # Listed text-only -> text only.
    assert t.accepts_image("beta/txt-1") is False


def test_a_bare_name_that_is_no_route_still_raises(tmp_path):
    t = _table(tmp_path, {**ROUTES, "_input": INPUT})
    with pytest.raises(RuntimeError, match="Known routes"):
        t.accepts_image("seeng")


# ── malformed `_input` fails the load ────────────────────────────────────────

@pytest.mark.parametrize("bad, needle", [
    (["alpha/vis-1"], "'_input' must map"),
    ("alpha/vis-1", "'_input' must map"),
    ({"alpha/vis-1": "image"}, "must be a list"),
    ({"alpha/vis-1": {"image": True}}, "must be a list"),
    ({"alpha/vis-1": ["text", "video"]}, "must be a list drawn from"),
    ({"alpha/vis-1": ["text", 1]}, "must be a list drawn from"),
    ({"seeing": ["text", "image"]}, "concrete 'provider/model'"),
])
def test_a_malformed_input_table_is_a_load_error(tmp_path, bad, needle):
    with pytest.raises(RuntimeError, match=needle):
        _table(tmp_path, {**ROUTES, "_input": bad})


# ── no `_input`: exactly as before ───────────────────────────────────────────

def test_a_table_without_input_loads_as_it_always_did(tmp_path):
    plain = _table(tmp_path, ROUTES)
    with_input = _table(tmp_path, {**ROUTES, "_input": INPUT})
    assert plain.names() == with_input.names() == ["mixed", "rotating", "seeing"]
    for name in plain.names():
        assert plain.resolve(name) == with_input.resolve(name)
    assert plain.effort_for("rotating", "alpha/vis-1") == "high"
    # Without the table nothing is image-capable.
    assert plain.input_for("alpha/vis-1") == ["text"]
    assert plain.accepts_image("seeing") is False


def test_the_shipped_example_carries_a_valid_input_table():
    """The example a clean checkout runs on loads with its `_input`, and only
    the measured vision judge takes images: `driver` and `worker` name
    unmeasured candidates, so they are text-only."""
    raw = json.loads((ROOT / "model_routes.example.json").read_text(encoding="utf-8"))
    assert isinstance(raw.get("_input"), dict) and raw["_input"]
    t = ModelRoutes(ROOT / "model_routes.example.json")
    assert t.accepts_image("vision") is True
    assert t.accepts_image("driver") is False
    assert t.accepts_image("worker") is False


# ── the registry rewrites the whole document and keeps `_input` ──────────────

@pytest.fixture
def tables(tmp_path, monkeypatch):
    providers = {"alpha": {"base_url": "https://a.test/v1",
                           "api_key_env": "ALPHA_KEY"},
                 "beta": {"base_url": "https://b.test/v1",
                          "api_key_env": "BETA_KEY"}}
    routes = {"fast": ["alpha/m-1", "beta/m-1"],
              "_input": {"alpha/m-1": ["text", "image"],
                         "beta/m-1": ["text", "image"]}}
    (tmp_path / "llm_providers.json").write_text(json.dumps(providers))
    (tmp_path / "model_routes.json").write_text(json.dumps(routes))
    monkeypatch.setattr(reg, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(reg, "config_or_example",
                        lambda name: str(tmp_path / name))
    model_routes.reset_cache()
    yield tmp_path
    model_routes.reset_cache()


def test_map_and_unmap_keep_the_input_table(tables):
    path = tables / "model_routes.json"
    before = json.loads(path.read_text())["_input"]

    reg.map_model("fast", "beta/m-2")
    doc = json.loads(path.read_text())
    assert doc["_input"] == before
    t = ModelRoutes(path)
    # The new candidate is unmeasured, so the route turned text-only.
    assert t.accepts_image("fast") is False

    reg.unmap_model("fast", "beta/m-2")
    doc = json.loads(path.read_text())
    assert doc["_input"] == before
    assert ModelRoutes(path).accepts_image("fast") is True


def test_a_registry_write_onto_a_bad_input_table_is_refused(tables):
    """The registry validates the proposed document the way production loads
    it, so a hand-broken `_input` blocks the write instead of being written
    on through and taking every agent down at the next start."""
    path = tables / "model_routes.json"
    doc = json.loads(path.read_text())
    doc["_input"] = {"alpha/m-1": ["text", "smell"]}
    path.write_text(json.dumps(doc))
    with pytest.raises(reg.RegistryError, match="_input"):
        reg.map_model("fast", "beta/m-2")
    assert json.loads(path.read_text())["fast"] == ["alpha/m-1", "beta/m-1"]
