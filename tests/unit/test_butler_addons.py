"""Regression tests: the butler can discover addons + pick the game pipeline."""

import core.meta_agent as m


def test_list_pipeline_addons_tool_registered():
    names = {t["function"]["name"] for t in m.TOOL_DEFINITIONS}
    assert "list_pipeline_addons" in names
    assert "list_pipeline_addons" in m._TOOL_HANDLERS


def test_list_pipeline_addons_handler_finds_game_pipeline():
    class _Fake:
        pass
    r = m.MetaAgent._tool_list_pipeline_addons(_Fake(), {"query": "game"})
    names = {(a["name"], a["alias"]) for a in r["addons"]}
    assert ("game_harness", "dpe_game") in names


def test_list_pipeline_addons_base_filter():
    class _Fake:
        pass
    r = m.MetaAgent._tool_list_pipeline_addons(_Fake(), {"base": "dpe_default_v2"})
    assert r["count"] >= 1
    r2 = m.MetaAgent._tool_list_pipeline_addons(_Fake(), {"base": "no_such_base"})
    assert r2["count"] == 0


def test_approve_tool_accepts_config_name():
    ap = next(t for t in m.TOOL_DEFINITIONS if t["function"]["name"] == "approve_project_brief")
    props = ap["function"]["parameters"]["properties"]
    assert "config_name" in props
    # the prompt teaches the butler to pick the game pipeline
    assert "dpe_game" in ap["function"]["description"] or "list_pipeline_addons" in ap["function"]["description"]
