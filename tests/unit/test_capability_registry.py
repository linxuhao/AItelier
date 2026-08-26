"""Capability definitions: the invariants, and the migration they exist for.

The measurement behind all of this: `gen_image_asset` + `gen_audio_asset` were
granted to every DPE implementer, game run or not — 55% of that step's tool
schema budget, re-sent on all 3,527 turns, and called ZERO times in 6,996 traced
tool calls across 22 workspaces. They are now the `game_assets` capability,
granted per task card.
"""

import json
from pathlib import Path

import pytest
import yaml

from core import capability_registry as caps


class _FakeLoader:
    def __init__(self, known):
        self.known = set(known)

    def load_schema(self, name):
        if name not in self.known:
            raise KeyError(name)
        return {"name": name}


class _FakeGraph:
    def __init__(self, capabilities):
        self.capabilities = list(capabilities)


class _FakeSF:
    def __init__(self, known_tools=(), graphs=None):
        self._tool_loader = _FakeLoader(known_tools)
        self._capabilities = {}
        self._graphs = graphs or {}

    def register_capability(self, name, *, tools=(), context_provider=None,
                            briefing="", owner="host"):
        prev = self._capabilities.get(name)
        if prev is not None and prev.get("owner") != owner:
            raise ValueError(f"capability {name!r} is already registered by "
                             f"{prev.get('owner')!r}")
        self._capabilities[name] = {"tools": list(tools), "briefing": briefing,
                                    "owner": owner,
                                    "context_provider": context_provider}


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolate the data root. NO importlib.reload: datadir resolves
    `$AITELIER_HOME` at call time precisely so an override needs none, and
    reloading it here left the module object swapped for the rest of the
    session — two unrelated tests failed only when the full suite ran."""
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path))
    return tmp_path


def test_a_grant_of_tools_that_do_not_resolve_is_refused(home):
    """Half-registering is the quiet failure: the config reads as though the
    step was handed something, and the step is handed nothing."""
    sf = _FakeSF(known_tools=["write"])
    r = caps.define(sf, "broken", tools=["write", "does_not_exist"])
    assert "error" in r and "does_not_exist" in r["error"]
    assert "broken" not in sf._capabilities


def test_same_owner_edits_and_another_owner_conflicts(home):
    sf = _FakeSF(known_tools=["a", "b"])
    assert caps.define(sf, "x", tools=["a"], owner="addon:g")["ok"]
    assert caps.define(sf, "x", tools=["a", "b"], owner="addon:g")["ok"]
    r = caps.define(sf, "x", tools=["a"], owner="gen:other")
    assert "error" in r and "already registered" in r["error"]


def test_archive_is_refused_while_a_pipeline_still_offers_it(home):
    """Invariant 2 — otherwise an offer list names something that is gone, and
    every card declaring it silently grants nothing."""
    sf = _FakeSF(known_tools=["a"], graphs={"dpe_game": _FakeGraph(["x"])})
    caps.define(sf, "x", tools=["a"], owner="host")
    r = caps.archive(sf, "x")
    assert "error" in r and "dpe_game" in r["error"]
    assert "x" in sf._capabilities


def test_archive_unregisters_live_not_just_on_disk(home):
    """Deleting the file alone leaves the running process still granting it —
    behaviour would differ before and after a restart. That is the zombie
    hazard `archive_generated_pipeline` exists for."""
    sf = _FakeSF(known_tools=["a"])
    caps.define(sf, "x", tools=["a"], owner="gen:s", persist=True)
    assert (caps.capabilities_dir() / "x.json").is_file()
    assert caps.archive(sf, "x")["ok"]
    assert "x" not in sf._capabilities
    assert not (caps.capabilities_dir() / "x.json").is_file()
    assert "x" in caps.archived_names()


def test_an_archived_capability_is_not_resurrected_by_the_boot_scan(home):
    sf = _FakeSF(known_tools=["a"])
    caps.define(sf, "x", tools=["a"], owner="gen:s", persist=True)
    caps.archive(sf, "x")
    fresh = _FakeSF(known_tools=["a"])
    assert caps.load_generated(fresh) == []
    assert "x" not in fresh._capabilities


def test_palette_intersects_offers_and_shows_what_is_missing(home):
    """A capability a pipeline offers but this deployment never registered is a
    deployment gap. Showing an empty row would hide it."""
    sf = _FakeSF(known_tools=["a"],
                 graphs={"p": _FakeGraph(["known", "never_installed"])})
    caps.define(sf, "known", tools=["a"], owner="host")
    caps.define(sf, "not_offered", tools=["a"], owner="host")
    pal = caps.palette(sf, "p")
    assert [c["name"] for c in pal["capabilities"]] == ["known"]
    assert pal["offered_but_not_registered"] == ["never_installed"]


def test_persisted_definitions_come_back_on_boot(home):
    sf = _FakeSF(known_tools=["a"])
    caps.define(sf, "gen_thing", tools=["a"], owner="gen:s", briefing="b",
                persist=True)
    fresh = _FakeSF(known_tools=["a"])
    assert caps.load_generated(fresh) == ["gen_thing"]
    assert fresh._capabilities["gen_thing"]["briefing"] == "b"


# ── the migration itself ──────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent.parent


def test_the_implementer_no_longer_carries_the_asset_tools():
    cfg = yaml.safe_load((ROOT / "agent_configs" / "dpe_default.yaml").read_text())
    tools = cfg["task_implementer"]["tools"]
    assert "gen_image_asset" not in tools
    assert "gen_audio_asset" not in tools
    # …and the escape hatch stays, so an under-granted task can say so instead
    # of substituting a ColorRect and reporting success.
    assert "capabilities_available" in tools


def test_the_game_addon_offers_the_capability_and_ships_its_briefing():
    addon = yaml.safe_load(
        (ROOT / "configs" / "addons" / "game_harness.yaml").read_text())
    assert addon["capabilities"] == ["game_assets"]
    brief = ROOT / "configs" / "addons" / "game_harness" / "game_assets_briefing.md"
    assert brief.is_file() and "transparent=true" in brief.read_text()


def test_the_asset_discipline_left_the_role_template():
    """The teaching travels with the capability now. Leaving a copy behind means
    every non-asset game task keeps paying for it."""
    impl = (ROOT / "configs" / "addons" / "game_harness"
            / "implementer.md").read_text()
    assert "gen_image_asset" not in impl


def test_the_implementing_step_declares_the_card_as_its_source():
    graph = yaml.safe_load((ROOT / "configs" / "dpe_default.yaml").read_text())
    step = next(s for s in graph["steps"] if s["id"] == "t_impl")
    assert step["capability"]["from_item"] == "capabilities"
    assert "$current_task" in step["capability"]["card"]


def test_the_planner_sees_the_palette_and_is_gated_on_it():
    graph = yaml.safe_load((ROOT / "configs" / "dpe_default.yaml").read_text())
    pm = next(s for s in graph["steps"] if s["id"] == "3")
    assert {"tool": "capability_palette"} in [c["source"] for c in pm["context"]]
    assert "capability_declarations_known" in [v.get("tool") for v in pm["validation"]]


def test_the_card_gate_rejects_a_capability_the_pipeline_does_not_offer(tmp_path,
                                                                       monkeypatch):
    from aitelier.tools.capability_declarations_known.impl import (
        capability_declarations_known as check)
    cards = tmp_path / "tasks"
    cards.mkdir()
    (cards / "a.json").write_text(json.dumps({"id": "a",
                                              "capabilities": ["game_assets"]}))
    (cards / "b.json").write_text(json.dumps({"id": "b",
                                              "capabilities": ["robot_arm"]}))

    sf = _FakeSF(known_tools=["gen_image_asset"],
                 graphs={"dpe_game": _FakeGraph(["game_assets"])})
    caps.define(sf, "game_assets", tools=["gen_image_asset"], owner="addon:g")
    monkeypatch.setitem(__import__("sys").modules, "api.dependencies",
                        type("M", (), {"get_skillflow": staticmethod(lambda: sf)}))

    r = check(files=["tasks/*.json"], workspace_root=str(tmp_path),
              config_name="dpe_game")
    assert r["all_passed"] is False
    errs = " ".join(x["error"] for x in r["results"])
    assert "robot_arm" in errs and "game_assets" not in errs.split("robot_arm")[0]


# ── review-round regressions ─────────────────────────────────────────────
def test_a_stale_skillflow_is_a_legible_error_not_a_boot_crash(home):
    """The dev box runs an editable checkout; the container installs from PyPI.
    A host calling a contract the pinned version lacks therefore fails ONLY in
    the container — as a TypeError out of get_skillflow(), 500ing every request.
    It has to come back as a message instead."""
    class _Old:
        _tool_loader = _FakeLoader(["a"])
        _capabilities = {}
        _graphs = {}

        def register_capability(self, name, *, tools=(), context_provider=None):
            raise TypeError("unexpected keyword argument 'briefing'")

    r = caps.define(_Old(), "x", tools=["a"], owner="host")
    assert "error" in r and "1.5.45" in r["error"]


def test_redefining_an_archived_capability_lifts_the_tombstone(home):
    """Otherwise it is live now, on disk now, and gone after the next restart."""
    sf = _FakeSF(known_tools=["a"])
    caps.define(sf, "x", tools=["a"], owner="gen:s", persist=True)
    caps.archive(sf, "x")
    caps.define(sf, "x", tools=["a"], owner="gen:s", persist=True)
    fresh = _FakeSF(known_tools=["a"])
    assert caps.load_generated(fresh) == ["x"]


def test_purge_after_archive_removes_the_tombstone_too(home):
    sf = _FakeSF(known_tools=["a"])
    caps.define(sf, "x", tools=["a"], owner="gen:s", persist=True)
    caps.archive(sf, "x")
    assert caps.archive(sf, "x", purge=True)["ok"]
    assert "x" not in caps.archived_names()
    assert not (caps.capabilities_dir() / "_archived" / "x.json").is_file()


def test_the_card_gate_passes_when_it_has_no_pipeline_to_check_against(tmp_path,
                                                                      monkeypatch):
    """It used to check against an empty offer list and reject every CORRECT
    declaration — a gate that passes only when the planner declares nothing."""
    from aitelier.tools.capability_declarations_known.impl import (
        capability_declarations_known as check)
    cards = tmp_path / "tasks"
    cards.mkdir()
    (cards / "a.json").write_text(json.dumps({"id": "a",
                                              "capabilities": ["game_assets"]}))
    sf = _FakeSF(known_tools=["gen_image_asset"],
                 graphs={"dpe_game": _FakeGraph(["game_assets"])})
    monkeypatch.setitem(__import__("sys").modules, "api.dependencies",
                        type("M", (), {"get_skillflow": staticmethod(lambda: sf)}))
    r = check(files=["tasks/*.json"], workspace_root=str(tmp_path))
    assert r["all_passed"] is True


def test_the_planner_is_told_when_to_declare_the_capability():
    """Enforcement without teaching costs a rework round per run, forever."""
    pm = (ROOT / "configs" / "addons" / "game_harness" / "pm.md").read_text()
    assert "game_assets" in pm and "capability_palette" in pm


def test_the_briefing_has_no_dangling_cross_document_reference():
    brief = (ROOT / "configs" / "addons" / "game_harness"
             / "game_assets_briefing.md").read_text()
    assert "受上面的块顺序约束" not in brief


def test_the_palette_carries_a_purpose_line_not_the_briefing(home):
    """A planner reads this every run; the discipline belongs with the grant."""
    sf = _FakeSF(known_tools=["a"], graphs={"p": _FakeGraph(["x"])})
    caps.define(sf, "x", tools=["a"], owner="host",
                briefing="# title\nmakes real art instead of placeholders\n"
                         + "detail " * 200)
    row = caps.palette(sf, "p")["capabilities"][0]
    assert row["purpose"] == "makes real art instead of placeholders"
    assert "briefing" not in row


def test_the_installed_skillflow_accepts_the_contract_this_host_calls():
    """Guard the cross-repo skew, not just our own logic.

    Every other test here runs on a fake SkillFlow, so they pass identically
    against a skillflow that has none of this — which is precisely the shape of
    the recorded skew incident: the consumer shipped before the producer, the
    dev box was green because it runs an editable checkout, and only the
    container (which installs from PyPI) could fail. This asserts against the
    INSTALLED package.
    """
    import inspect
    from skillflow.core import SkillFlow
    from skillflow.graph import PipelineGraph, StepNode

    params = inspect.signature(SkillFlow.register_capability).parameters
    assert "briefing" in params and "owner" in params, (
        "installed skillflow predates the briefing/owner contract — the host "
        "calls it at boot; see the pin in pyproject.toml")
    assert "capabilities" in {f.name for f in
                              __import__("dataclasses").fields(PipelineGraph)}, (
        "installed skillflow has no graph-level capability offer list")
    node = StepNode(id="x", step_type="agent",
                    capability={"from_item": "capabilities", "card": "3/c.json"})
    assert SkillFlow._declared_capability_names(
        node, {"capabilities": ["game_assets"]}) == ["game_assets"], (
        "installed skillflow does not resolve a card-declared capability")
