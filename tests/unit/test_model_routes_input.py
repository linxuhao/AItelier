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


def test_only_a_table_with_an_input_table_measures_input(tmp_path):
    """OPS-1: `measures_input` tells "nothing measured yet" (no `_input`, or no
    table at all) from "measured, and text-only" (an `_input`, even an empty one)."""
    assert _table(tmp_path, ROUTES).measures_input is False
    assert ModelRoutes(tmp_path / "absent.json").measures_input is False
    assert _table(tmp_path, {**ROUTES, "_input": {}}).measures_input is True
    assert _table(tmp_path, {**ROUTES, "_input": INPUT}).measures_input is True


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


# ── F2: every candidate `_input` does not list counts as text-only ──────────

def test_every_candidate_missing_from_input_is_text_only_and_named(tmp_path):
    """Once the table HAS an `_input`, a candidate it does not list is text-only,
    and so is EVERY route that names one (the whole-route rule): `gamma/txt-2`
    is only `rotating`'s fallback tail, and it still makes `rotating` text-only.
    `unlisted_input` names each such candidate once, so a deploy can check that
    the `_input` it writes covers the whole table before it takes effect."""
    t = _table(tmp_path, {**ROUTES, "_input": INPUT})
    assert t.unlisted_input() == ["gamma/txt-2"]
    assert t.input_for("gamma/txt-2") == ["text"]
    assert t.accepts_image("rotating") is False
    covered = _table(tmp_path, {**ROUTES, "_input": {**INPUT, "gamma/txt-2": ["text", "image"]}})
    assert covered.unlisted_input() == []
    assert covered.accepts_image("rotating") is True
    # Without an `_input` nothing was measured: nothing is "unlisted" either.
    assert _table(tmp_path, ROUTES).unlisted_input() == []


# ── F1: the table is read once per process; the startup line says what it read ─

def test_input_summary_says_whether_input_is_measured(tmp_path):
    """The backend prints this line at startup (api/main.py lifespan): with no
    `_input` it says the routes are unmeasured; with one it names the text-only
    routes and every unlisted candidate."""
    assert "no _input" in _table(tmp_path, ROUTES).input_summary()
    line = _table(tmp_path, {**ROUTES, "_input": INPUT}).input_summary()
    assert line.startswith("model routes: _input measured")
    assert "text-only routes: mixed, rotating" in line
    assert "unlisted (text-only): gamma/txt-2" in line


def test_an_empty_input_table_is_not_reported_as_measured(tmp_path):
    """Dashboard review (director 677): `"_input": {}` declares every candidate
    text-only; the startup line must not say `_input measured`, because that is
    the line a deploy reads to confirm the table took effect."""
    table = _table(tmp_path, {**ROUTES, "_input": {}})
    line = table.input_summary()
    assert "_input measured" not in line
    assert line.startswith("model routes: _input present but empty: nothing measured")
    assert all(not table.accepts_image(r) for r in table.names())


def test_a_hand_edit_of_the_table_needs_a_restart_or_reset_cache(tmp_path):
    """F1: get_routes() parses the file once per process. Adding `_input` to the
    live file by hand changes nothing in a running backend until it restarts
    (or a model-routes API save calls reset_cache)."""
    p = tmp_path / "model_routes.json"
    p.write_text(json.dumps(ROUTES), encoding="utf-8")
    model_routes.reset_cache()
    try:
        assert model_routes.get_routes(p).measures_input is False
        p.write_text(json.dumps({**ROUTES, "_input": INPUT}), encoding="utf-8")
        assert model_routes.get_routes(p).measures_input is False, "cached: the hand edit is not seen"
        model_routes.reset_cache()
        assert model_routes.get_routes(p).measures_input is True
    finally:
        model_routes.reset_cache()


def test_the_backend_prints_the_input_summary_at_startup():
    """The deploy step depends on reading this line after the restart."""
    src = (ROOT / "api" / "main.py").read_text(encoding="utf-8")
    assert "input_summary()" in src
