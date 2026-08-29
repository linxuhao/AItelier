"""A version number an agent cannot act on is just a bigger integer.

`pipeline_versions` exists so an agent can answer "what did this run actually
execute" and "how has this pipeline drifted since I last verified it". The
second question needs the DELTA, not the count — so each version carries what
changed at it, reported in `core/baseline.py:diff`'s vocabulary rather than a
second one invented here.
"""

from unittest.mock import MagicMock

import pytest

from core.meta_agent import MetaAgent


def _graph(steps):
    return {"name": "gen_x", "begin": steps[0]["id"], "steps": steps,
            "capabilities": []}


def _step(sid, outputs=None, tool=None):
    s = {"id": sid, "step_type": "tool" if tool else "agent"}
    if tool:
        s["tool_name"] = tool
    else:
        s["agent_config"] = "host"
    if outputs:
        # `output.fixed`, the shape `to_dict()` emits — NOT the `output_fixed`
        # attribute name on StepNode. `_declared_outputs` reads the former, and
        # a fixture using the latter silently declares nothing.
        s["output"] = {"fixed": {k: k for k in outputs}}
    return s


V = {
    1: _graph([_step("plan"), _step("impl", outputs=["out.md", "notes.md"])]),
    2: _graph([_step("plan"), _step("impl", outputs=["out.md"])]),   # dropped one
    3: _graph([_step("plan")]),                                      # step gone
}


@pytest.fixture
def agent(monkeypatch):
    sf = MagicMock()
    sf.list_graph_versions.return_value = [
        {"version": 3, "digest": "sha256:c", "created_at": "t3"},
        {"version": 2, "digest": "sha256:b", "created_at": "t2"},
        {"version": 1, "digest": "sha256:a", "created_at": "t1"},
    ]
    sf.get_graph_version.side_effect = (
        lambda name, v: {"graph": V[v]} if v in V else None)
    import api.dependencies as deps
    monkeypatch.setattr(deps, "get_skillflow", lambda: sf, raising=False)
    return MetaAgent.__new__(MetaAgent)


async def test_each_version_carries_what_changed_at_it(agent):
    out = await agent._tool_pipeline_versions({"config_name": "gen_x"})

    assert out["latest"] == 3
    by_v = {v["version"]: v for v in out["versions"]}

    assert [f["finding"] for f in by_v[3]["changed"]] == ["step_removed"]
    assert by_v[3]["changed"][0]["step"] == "impl"

    assert [f["finding"] for f in by_v[2]["changed"]] == ["output_undeclared"]
    assert by_v[2]["changed"][0]["files"] == ["notes.md"]

    assert by_v[1]["changed"] == [], "v1 is the beginning, not a change"


async def test_one_version_can_be_read_on_its_own(agent):
    out = await agent._tool_pipeline_versions(
        {"config_name": "gen_x", "version": 1})

    assert out["version"] == 1
    assert set(out["shape"]["steps"]) == {"plan", "impl"}
    assert "changed" not in out


async def test_a_version_that_never_existed_says_which_do(agent):
    out = await agent._tool_pipeline_versions(
        {"config_name": "gen_x", "version": 9})
    assert "error" in out and "[3, 2, 1]" in out["error"]


async def test_the_delta_window_is_bounded_and_says_what_it_dropped(
        agent, monkeypatch):
    """The history of a config under active editing is long and its tail is
    rarely the question; silently truncating it would read as "that's all"."""
    from api.dependencies import get_skillflow
    sf = get_skillflow()
    sf.list_graph_versions.return_value = [
        {"version": v, "digest": f"d{v}", "created_at": "t"}
        for v in range(20, 0, -1)]
    monkeypatch.setattr(MetaAgent, "_VERSION_DELTA_WINDOW", 3)

    out = await agent._tool_pipeline_versions({"config_name": "gen_x"})

    assert [v["version"] for v in out["versions"]] == [20, 19, 18]
    assert out["older"][0] == 17 and len(out["older"]) == 17
    assert "17 older version(s)" in out["note"]


async def test_an_engine_without_the_history_says_so(monkeypatch):
    """The container tracks PyPI while the host runs an editable checkout, so
    the tool must name that rather than raising AttributeError."""
    import api.dependencies as deps
    monkeypatch.setattr(deps, "get_skillflow",
                        lambda: MagicMock(spec=[]), raising=False)

    out = await MetaAgent.__new__(MetaAgent)._tool_pipeline_versions(
        {"config_name": "gen_x"})
    assert "error" in out and "skillflow older than" in out["error"]
