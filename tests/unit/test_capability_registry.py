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

    def capabilities(self):
        """Mirrors the public accessor — the host reads through it now, so a
        double that lacks it fails loudly instead of returning an empty table."""
        return {n: {"tools": list(c.get("tools") or ()),
                    "briefing": c.get("briefing", ""),
                    "owner": c.get("owner", "host")}
                for n, c in self._capabilities.items()}

    def graph_capabilities(self, graph_name):
        return list(getattr(self._graphs.get(graph_name), "capabilities", []) or [])

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


# ── Batch 2: generation + management ─────────────────────────────────────
def test_the_forge_palette_lists_capabilities_live(home):
    """It was a hand-written list of two.

    A capability added anywhere else — an addon shipping one, the forge
    authoring one — was invisible to the next generation, and a maker that
    cannot see a capability writes the tool grant by hand onto a role, which is
    the thing capabilities exist to stop.
    """
    from aitelier.tools.forge_palette.impl import forge_palette
    md = forge_palette()["palette_markdown"]
    assert "{CAPABILITIES_SECTION}" not in md, "placeholder was never substituted"
    assert "## Capabilities" in md
    assert "game_assets" in md, "a live capability is missing from the palette"
    assert "from_item" in md, "the per-item declaration form is not taught"


def test_the_emit_gate_rejects_capability_mistakes():
    """`capability_known`, the same rule shape as `role_model_known`: a name
    outside the registry grants nothing at RUNTIME, silently."""
    from aitelier.tools.forge_registry_check.impl import _capability_known
    g = lambda **k: dict(name="g", begin="a", steps=[], **k)
    S3 = [{"id": "3"}]      # the step a card path points at must exist

    assert _capability_known(g(), [{"id": "s", "capability": "nope"}])
    assert _capability_known(g(capabilities=["ghost_cap_nobody_has"]), [])
    # from_item without a card grants nothing — a loop item is a NAME
    assert _capability_known(g(capabilities=["stateful"]),
                             S3 + [{"id": "s", "capability": {"from_item": "c"}}])
    # …and with no offer list the engine refuses every card-declared name
    assert _capability_known(g(), S3 + [{"id": "s", "capability":
                                        {"from_item": "c", "card": "3/c.json"}}])
    # the correct shapes are silent
    assert not _capability_known(g(capabilities=["stateful"]),
                                 [{"id": "s", "capability": "stateful"}])
    assert not _capability_known(
        g(capabilities=["game_assets"]),
        S3 + [{"id": "s", "capability": {"from_item": "capabilities",
                                         "card": "3/tasks/$t.json"}}])


def test_capability_known_is_in_the_taught_rule_table():
    """An enforced-but-untaught rule costs a rework round per generation,
    forever — the RULES table is what the palette renders."""
    from aitelier.tools.forge_registry_check.impl import RULES
    assert "capability_known" in {r.id for r in RULES}


def test_the_forge_can_define_a_capability_not_just_a_tool():
    """A tool that needs framework-chosen state has nowhere to attach without
    one, and a maker with no way to declare it writes the grant by hand."""
    import yaml
    from api.dependencies import get_skillflow
    grants = (get_skillflow()._capabilities.get("tool_creation") or {}).get("tools")
    assert "register_capability" in grants
    spec = yaml.safe_load(
        (ROOT / "aitelier" / "tools" / "register_capability" / "tool.yaml").read_text())
    assert spec["name"] == "register_capability"


def test_describe_pipeline_reports_the_offer_list():
    """A pipeline's offer list is part of its contract, like its seed shape."""
    from api.dependencies import get_config_registry
    got = {e["config_name"]: e.get("capabilities")
           for e in get_config_registry().describe("dpe_game")}
    assert got.get("dpe_game") == ["game_assets"]


def test_a_built_in_pipelines_offer_list_cannot_be_edited_at_runtime():
    """Runtime mutation of a repo config is drift no checkout can see. The
    supported way to give a built-in base a capability is an addon."""
    from api.dependencies import get_config_registry, get_skillflow
    from core.pipeline_registry import set_pipeline_capabilities
    r = set_pipeline_capabilities(get_skillflow(), get_config_registry(),
                                  "dpe_default_v2", add=["game_assets"])
    assert "error" in r and "addon" in r["error"]


def test_offering_something_unregistered_is_refused_at_the_edit(home):
    """Otherwise every card declaring it grants nothing, silently."""
    from core.pipeline_registry import set_pipeline_capabilities
    from api.dependencies import get_config_registry, get_skillflow
    r = set_pipeline_capabilities(get_skillflow(), get_config_registry(),
                                  "gen_dsh_code_review", add=["ghost_nobody_has"])
    assert "error" in r and "not registered" in r["error"]


# ── the review round: the two bugs that shipped were in code no test called ──
def test_a_generated_capability_cannot_impersonate_the_host(home):
    """`owner` was a tool PARAMETER, so the ownership invariant was a
    suggestion — and the refusal message names the string that defeats it, which
    is an LLM's most natural repair.

    Passing owner="host" overwrote a host capability: `stateful` lost its
    context_provider (state_dir injection dead deployment-wide, persisted, and
    re-applied by the boot scan on every restart), or `tool_creation` was
    rewritten to grant repo_apply/repo_delete to the very step that holds it.
    """
    import yaml
    from aitelier.tools.register_capability.impl import register_capability
    import inspect
    assert "owner" not in inspect.signature(register_capability).parameters
    spec = yaml.safe_load((ROOT / "aitelier" / "tools" / "register_capability"
                           / "tool.yaml").read_text())
    assert "owner" not in spec["parameters"], "the tool still advertises owner"

    sf = _FakeSF(known_tools=["write"])
    sf._capabilities["stateful"] = {"tools": [], "owner": "host",
                                    "briefing": "", "context_provider": object()}
    r = caps.define(sf, "stateful", tools=["write"], owner="host")
    assert "error" in r and "host" in r["error"]
    assert sf._capabilities["stateful"]["tools"] == [], "host definition overwritten"


def test_an_edit_does_not_silently_drop_a_context_provider(home):
    """An edit replaces the whole definition; a caller passing no provider is
    not asking to remove one."""
    sf = _FakeSF(known_tools=["write", "pytest"])
    sentinel = object()
    caps.define(sf, "x", tools=["write"], owner="gen:x",
                context_provider=sentinel)
    caps.define(sf, "x", tools=["write", "pytest"], owner="gen:x")
    assert sf._capabilities["x"]["context_provider"] is sentinel


def test_the_briefing_is_capped(home):
    """It rides the step's per-turn context and is shown truncated everywhere it
    is listed — unbounded, it is the same token leak this mechanism removed,
    wearing different clothes."""
    from aitelier.tools.register_capability.impl import register_capability
    r = register_capability(name="probe", tools=["read_file"], briefing="x" * 99999)
    assert r["registered"] is False and "limit" in r["error"]


def test_a_capability_name_must_be_a_safe_filename(home):
    """The name becomes a filename: a 300-char one came back as
    OSError(ENAMETOOLONG) out of a function documented to report, never raise."""
    sf = _FakeSF(known_tools=["write"])
    for bad in ("a" * 300, "a/b", ".hidden", "a\\b", "Caps"):
        assert "error" in caps.define(sf, bad, tools=["write"]), bad
    assert caps.define(sf, "good_name-2", tools=["write"])["ok"]


def test_a_rejected_edit_does_not_leave_a_broken_config_on_disk(tmp_path,
                                                               monkeypatch):
    """Write-then-reload left the bad file in place: the live registry still
    held the old graph, so nothing looked wrong until a restart skipped the
    invalid file and the pipeline VANISHED from the catalog.

    Reachable by the common case — adding an offer list to a graph that already
    declares a static `capability:`, which is what the forge emits.
    """
    import yaml
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path))
    from core import datadir, pipeline_registry
    d = datadir.configs_dir(); d.mkdir(parents=True, exist_ok=True)
    f = d / "gen_probe.yaml"
    graph = {"name": "gen_probe", "begin": "a", "steps": [
        {"id": "a", "step_type": "agent", "agent_config": "r",
         "capability": "stateful", "transitions": [{"to": "done"}]},
        {"id": "done", "step_type": "gate", "transitions": []}]}
    before = yaml.safe_dump(graph, sort_keys=False)
    f.write_text(before, encoding="utf-8")

    sf = _FakeSF(known_tools=["write"])
    caps.define(sf, "game_assets", tools=["write"], owner="host")
    r = pipeline_registry.set_pipeline_capabilities(sf, None, "gen_probe",
                                                    add=["game_assets"])
    assert "error" in r, "an offer list that outlaws a step's own capability"
    assert f.read_text(encoding="utf-8") == before, "the bad config was written"


def test_removing_a_capability_reports_what_still_declares_it():
    """`remove` has two silently different meanings.

    Dropping the LAST offered capability empties the list, and an empty list
    stops binding GRAPH-declared names at all — so "remove X" on a graph whose
    step statically declares X RE-GRANTS it. Dropping one a task card declares
    revokes it correctly, but only as a claim-time warning: the step quietly
    loses its tools. Either way the caller is told.
    """
    from core.pipeline_registry import _still_declared_statically
    graph = {"steps": [{"id": "a", "capability": "stateful"},
                       {"id": "b", "capability": ["other"]}]}
    warn = _still_declared_statically(graph, {"stateful"})
    assert "a:stateful" in warn and "unconditionally" in warn
    # nothing declares it statically → the other meaning, still reported
    assert "grants nothing" in _still_declared_statically(graph, {"ghost"})
    assert _still_declared_statically(graph, set()) == ""
