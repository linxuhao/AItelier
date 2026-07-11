"""Regression tests for the pipeline-addon registry (core/addon_registry.py)."""

import pytest

from core import addon_registry as ar


def test_list_addons_discovers_game_harness():
    addons = ar.list_addons()
    gh = next((a for a in addons if a["name"] == "game_harness"), None)
    assert gh is not None
    assert gh["base"] == "dpe_default_v2"
    assert gh["alias"] == "dpe_game"
    assert gh["description"] and gh["when_to_use"]


def test_compose_alias_name_and_steps():
    name, graph, hints = ar.compose_addon_graph("dpe_default_v2", ["game_harness"], "dpe_game")
    assert name == "dpe_game"
    ids = {s.id for s in graph.steps}
    # the addon's steps are spliced in
    assert "5_compile" in ids and "gh_scaffold" in ids
    # base's own steps survive
    assert "1" in ids and "5_review" in ids
    # base hints carried onto the composed config
    assert hints.get("seed_file") == "project_brief.md"


def test_compose_emergent_name_for_no_result_name():
    name, _, _ = ar.compose_addon_graph("dpe_default_v2", ["game_harness"])
    assert name == "dpe_default_v2__game_harness"


def test_compose_rejects_base_mismatch():
    with pytest.raises(ValueError, match="binds to base"):
        ar.compose_addon_graph("meta_conversation", ["game_harness"])


def test_compose_rejects_unknown_base():
    with pytest.raises(ValueError, match="unknown base"):
        ar.compose_addon_graph("does_not_exist", ["game_harness"])


def test_compose_rejects_unknown_addon():
    with pytest.raises(ValueError, match="unknown addon"):
        ar.compose_addon_graph("dpe_default_v2", ["nope_addon"])


def test_describe_config_decomposes_alias_emergent_and_base():
    assert ar.describe_config("dpe_game") == {"base": "dpe_default_v2", "addons": ["game_harness"]}
    assert ar.describe_config("dpe_default_v2") == {"base": "dpe_default_v2", "addons": []}
    assert ar.describe_config("dpe_default_v2__game_harness+mobile") == {
        "base": "dpe_default_v2", "addons": ["game_harness", "mobile"]}


def test_read_fragments_resolves_addon_files():
    frags = ar.read_fragments(["game_harness/architect.md"])
    assert len(frags) == 1
    label, content = next(iter(frags.items()))
    assert "game_harness/architect.md" in label
    assert "Godot" in content


def test_read_fragments_ignores_missing_and_escapes():
    # missing file → dropped; path traversal outside addons dir → dropped
    frags = ar.read_fragments(["game_harness/nope.md", "../../etc/passwd"])
    assert frags == {}
