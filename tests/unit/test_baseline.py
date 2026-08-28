"""Regression baselines for generated pipelines and addons.

The three forge gates and the smoke all run once, at generation; after that a
`gen_*` pipeline stays editable and nothing proved an edit kept what a real
test-drive had already verified. These tests pin the replay net: what a baseline
records, what counts as a regression, and — the risk of adding a file next to a
boot-scanned config — that the scan still ignores it.
"""
from __future__ import annotations

import copy
import json
import textwrap

import pytest
import yaml
from skillflow import SkillFlow

from core import baseline as bl
from core import pipeline_registry as pr
from core.config_registry import ConfigRegistry


GEN_YAML = textwrap.dedent("""
    name: placeholder
    description: A tiny generated pipeline
    begin: process
    end_conditions:
      combinator: or
      conditions:
        - type: node_reached
          node: done
          result: completed
    steps:
      - id: process
        step_type: agent
        agent_config: processor
        transitions:
          - to: done
      - id: done
        step_type: agent
        agent_config: summarizer
""")

BASE_GRAPH = {
    "name": "base_demo",
    "begin": "one",
    "anchors": {"post_one": "one"},
    "end_conditions": {"combinator": "or",
                       "conditions": [{"type": "node_reached", "node": "two",
                                       "result": "completed"}]},
    "steps": [
        {"id": "one", "step_type": "agent", "agent_config": "a",
         "transitions": [{"to": "two"}]},
        {"id": "two", "step_type": "agent", "agent_config": "b"},
    ],
}

ADDON_SPEC = {
    "name": "demo_addon",
    "base": "base_demo",
    "alias": "base_demo_x",
    "capabilities": ["game_assets"],
    "overlay": [{"insert_after": "two",
                 "steps": [{"id": "extra", "step_type": "tool",
                            "tool_name": "run_tests"}]}],
}


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #

class _FakeGraph:
    def __init__(self, d):
        self._d = d

    def to_dict(self):
        return copy.deepcopy(self._d)


class _FakeSF:
    """Enough skillflow for baseline: the graph cache and the overlay registry."""

    def __init__(self, graphs=None, overlays=None, smoke=None):
        self._graphs = {n: _FakeGraph(d) for n, d in (graphs or {}).items()}
        self._overlays = overlays or {}
        self._smoke = smoke
        if smoke is not None:
            self._tool_loader = _FakeLoader(smoke)

    def list_overlays(self):
        return [{"name": n, **s} for n, s in self._overlays.items()]


class _FakeLoader:
    def __init__(self, smoke):
        self._smoke = smoke

    def load_tool(self, name):                       # pragma: no cover - unused
        raise KeyError(name)

    def load_fn(self, name):
        if name != "forge_dryrun_smoke":
            raise KeyError(name)
        return lambda **kw: self._smoke


@pytest.fixture
def gdir(tmp_path, monkeypatch):
    d = tmp_path / "gen_configs"
    monkeypatch.setenv("AITELIER_GENERATED_CONFIGS_DIR", str(d))
    return d


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Addon baselines hang off datadir, which follows AITELIER_HOME."""
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path))
    return tmp_path


def _use(monkeypatch, sf):
    monkeypatch.setattr("api.dependencies.get_skillflow", lambda: sf)


# --------------------------------------------------------------------------- #
# the file, and the boot scan that must keep ignoring it
# --------------------------------------------------------------------------- #

def test_the_boot_scan_still_ignores_a_baseline_sibling(gdir):
    """The whole risk of adding a file next to a boot-scanned config.

    The baseline written here holds a VALID pipeline graph, so a scan that ever
    widened its `gen_*.yaml` glob would register it as a second config and this
    assertion would catch it. A baseline of ordinary contents would not: the scan
    skips unparseable files with a logged warning, so the name list would look
    identical whether the glob was right or wrong.
    """
    gdir.mkdir(parents=True, exist_ok=True)
    data = yaml.safe_load(GEN_YAML)
    data["name"] = "gen_boot_demo"
    (gdir / "gen_boot_demo.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    # JSON is valid YAML, and this one would register cleanly if it were read.
    (gdir / "gen_boot_demo.baseline.json").write_text(json.dumps(data),
                                                      encoding="utf-8")

    names = pr.load_generated_configs(SkillFlow(":memory:"), ConfigRegistry())

    assert names == ["gen_boot_demo"]


def test_a_baseline_round_trips_to_its_own_namespace(gdir, home):
    bl.write(bl.KIND_PIPELINE, "gen_x", {"kind": "pipeline", "target": "gen_x"})
    bl.write(bl.KIND_ADDON, "demo_addon", {"kind": "addon", "target": "demo_addon"})

    assert bl.read(bl.KIND_PIPELINE, "gen_x")["target"] == "gen_x"
    assert bl.read(bl.KIND_ADDON, "demo_addon")["target"] == "demo_addon"
    # Different directories — an addon and a pipeline may share a name.
    assert bl.path_for(bl.KIND_PIPELINE, "x") != bl.path_for(bl.KIND_ADDON, "x")
    assert bl.read(bl.KIND_PIPELINE, "never_recorded") is None


def test_a_half_written_baseline_is_never_visible(gdir, monkeypatch):
    """Written through a temp file: a truncated JSON would read as a pile of
    false regressions, which is worse than having no baseline at all."""
    gdir.mkdir(parents=True, exist_ok=True)
    seen = {}
    real_replace = bl.os.replace

    def _spy(src, dst):
        seen["existed_before_replace"] = bl.path_for(bl.KIND_PIPELINE, "gen_x").exists()
        return real_replace(src, dst)

    monkeypatch.setattr(bl.os, "replace", _spy)
    bl.write(bl.KIND_PIPELINE, "gen_x", {"target": "gen_x"})
    assert seen["existed_before_replace"] is False


# --------------------------------------------------------------------------- #
# capture
# --------------------------------------------------------------------------- #

def test_declared_outputs_skip_globs():
    """A `*` pattern names no specific file, so it can neither be compared nor
    go missing — the same rule the stub runner applies when touching outputs."""
    step = {"id": "s", "output": {"fixed": {
        "report": {"file": "report.md"},
        "plain": "verdict.json",
        "many": {"file": "chunk_*.json"},
    }}}
    assert bl._declared_outputs(step) == ["report.md", "verdict.json"]


def test_shape_records_what_an_edit_can_break():
    shape = bl.capture_shape({
        "begin": "one",
        "capabilities": ["game_assets"],
        "steps": [{"id": "one", "step_type": "tool", "tool_name": "run_tests",
                   "output": {"fixed": {"r": {"file": "test_report.json"}}}}],
    })
    assert shape["begin"] == "one"
    assert shape["capabilities"] == ["game_assets"]
    assert shape["steps"]["one"]["tool_name"] == "run_tests"
    assert shape["steps"]["one"]["declared_outputs"] == ["test_report.json"]


def test_smoke_reduces_the_trail_to_the_set_of_reached_steps(monkeypatch):
    """Never a sequence. A real drive's loop iterates once per real task and the
    stub's over canned output, so the orders differ for reasons that are not
    regressions."""
    _use(monkeypatch, _FakeSF(smoke={"status": "completed", "passed": True,
                                     "trail": ["a", "[tool]b", "a", "c"]}))
    smoke = bl.capture_smoke({"name": "g", "steps": []})
    assert smoke["reached"] == ["a", "b", "c"]
    assert smoke["status"] == "completed"
    assert smoke["usable"] is True


def test_a_drive_that_never_booted_is_marked_unusable(monkeypatch):
    """Measured on the real `dpe_game`: a graph whose first step needs context from
    another config's run ("Required context source resolved to no content:
    finalize") cannot boot in the stub's empty workspace, and `dpe_default_v2`
    alone fails identically — so it is the base, not the addon.

    Recorded as an ordinary outcome, a replay would compare boot_error to
    boot_error and report a match, i.e. announce a clean bill of health for a
    check that never ran.
    """
    _use(monkeypatch, _FakeSF(smoke={"status": "boot_error", "passed": False,
                                     "trail": [],
                                     "error": "Required context source resolved "
                                              "to no content: finalize"}))
    assert bl.capture_smoke({"name": "g", "steps": []})["usable"] is False


def test_an_unloadable_smoke_tool_is_unusable_not_a_clean_result(monkeypatch):
    _use(monkeypatch, _FakeSF())          # no _tool_loader at all
    smoke = bl.capture_smoke({"name": "g", "steps": []})
    assert smoke["usable"] is False and smoke["reached"] == []


def test_observed_keeps_basenames_so_two_healthy_runs_agree(monkeypatch, tmp_path):
    """Loop-body output lands in per-item subdirectories whose names carry a hash
    of the item value, so full relative paths differ between runs of one graph."""

    class _WS:
        def get_final_path(self, pid, step, cfg):
            d = tmp_path / pid / cfg / step
            return d

    class _SF:
        def get_steps(self, run_id):
            return [{"step_id": "t_impl", "loop_item": "task-a"},
                    {"step_id": "t_impl", "loop_item": "task-b"}]

    for item in ("task_a_9f2c", "task_b_31aa"):
        d = tmp_path / "p1" / "gen_x" / "t_impl" / item
        d.mkdir(parents=True)
        (d / "patch.diff").write_text("x", encoding="utf-8")
        (d / "_snapshot.json").write_text("{}", encoding="utf-8")

    observed = bl.capture_observed(_SF(), _WS(), "run1", "p1", "gen_x", "seed")

    assert observed["steps"] == [{"step": "t_impl", "loop": True,
                                  "files": ["patch.diff"]}]


# --------------------------------------------------------------------------- #
# diff — what counts as a regression
# --------------------------------------------------------------------------- #

def _bl(steps, *, reached=None, caps=None, status="completed", observed=None):
    data = {
        "shape": {"begin": "one", "capabilities": caps or [], "steps": steps},
        "smoke": {"status": status, "reached": reached if reached is not None
                  else sorted(steps)},
    }
    if observed:
        data["observed"] = observed
    return data


def _step(**kw):
    base = {"step_type": "agent", "tool_name": None, "agent_config": "a",
            "capability": None, "declared_outputs": []}
    base.update(kw)
    return base


def test_a_deleted_step_is_reported_once_not_three_times():
    """A rename would otherwise produce step_removed + step_added + unreachable
    for one edit, and a findings list that inflates is a list people stop reading.
    """
    old = _bl({"one": _step(), "two": _step()})
    new = _bl({"one": _step()}, reached=["one"])

    findings = bl.diff(old, new)

    assert [f["finding"] for f in findings] == ["step_removed"]
    assert findings[0]["step"] == "two"


def test_an_added_step_is_reported():
    old = _bl({"one": _step()})
    new = _bl({"one": _step(), "two": _step()})
    assert [f["finding"] for f in bl.diff(old, new)] == ["step_added"]


def test_a_dropped_output_declaration_is_a_regression():
    old = _bl({"one": _step(declared_outputs=["tasks.json", "notes.md"])})
    new = _bl({"one": _step(declared_outputs=["notes.md"])})

    findings = bl.diff(old, new)

    assert findings[0]["finding"] == "output_undeclared"
    assert findings[0]["files"] == ["tasks.json"]


def test_a_step_that_changed_kind_is_a_regression():
    old = _bl({"one": _step(step_type="tool", tool_name="run_tests",
                            agent_config=None)})
    new = _bl({"one": _step(step_type="tool", tool_name="pytest",
                            agent_config=None)})
    findings = bl.diff(old, new)
    assert findings[0]["finding"] == "step_changed"
    assert findings[0]["field"] == "tool_name"


def test_a_dropped_capability_offer_is_a_regression():
    old = _bl({"one": _step()}, caps=["game_assets", "stateful"])
    new = _bl({"one": _step()}, caps=["stateful"])
    findings = bl.diff(old, new)
    assert findings[0]["finding"] == "capability_dropped"
    assert findings[0]["capabilities"] == ["game_assets"]


def test_a_step_that_survives_but_stops_being_reached_is_a_regression():
    """A rewired transition: the step is still in the graph, the drive no longer
    gets to it. Structurally invisible — only the stub drive sees it."""
    old = _bl({"one": _step(), "two": _step()}, reached=["one", "two"])
    new = _bl({"one": _step(), "two": _step()}, reached=["one"])

    findings = bl.diff(old, new)

    assert [f["finding"] for f in findings] == ["unreachable"]
    assert findings[0]["steps"] == ["two"]


def test_a_drive_that_stops_terminating_is_a_regression():
    old = _bl({"one": _step()}, status="completed")
    new = _bl({"one": _step()}, status="max_steps")
    assert [f["finding"] for f in bl.diff(old, new)] == ["smoke_status_changed"]


def test_an_unchanged_graph_produces_no_findings():
    old = _bl({"one": _step(declared_outputs=["a.json"])}, caps=["stateful"])
    assert bl.diff(old, copy.deepcopy(old)) == []


def test_a_file_a_real_drive_produced_may_not_quietly_stop_being_promised():
    """The half only a real drive can supply: `tasks.json` was declared AND
    actually written, and the edit dropped the declaration."""
    observed = {"test_seed": "s",
                "steps": [{"step": "one", "loop": False,
                           "files": ["tasks.json", "scratch.txt"]}]}
    old = _bl({"one": _step(declared_outputs=["tasks.json", "notes.md"])},
              observed=observed)
    new = _bl({"one": _step(declared_outputs=["notes.md"])})

    kinds = [f["finding"] for f in bl.diff(old, new)]

    assert "observed_output_undeclared" in kinds
    lost = next(f for f in bl.diff(old, new)
                if f["finding"] == "observed_output_undeclared")
    # scratch.txt was written but never promised — an agent step writes freely,
    # so that is its normal state, not a regression.
    assert lost["files"] == ["tasks.json"]


def test_a_step_that_promises_nothing_is_not_judged_on_what_it_wrote():
    observed = {"steps": [{"step": "one", "loop": False, "files": ["out.md"]}]}
    old = _bl({"one": _step(declared_outputs=[])}, observed=observed)
    new = _bl({"one": _step(declared_outputs=[])})
    assert bl.diff(old, new) == []


# --------------------------------------------------------------------------- #
# addons — recomposition IS the check
# --------------------------------------------------------------------------- #

def test_an_addon_baseline_is_taken_against_a_fresh_composition(monkeypatch, home):
    _use(monkeypatch, _FakeSF(graphs={"base_demo": BASE_GRAPH},
                              overlays={"demo_addon": ADDON_SPEC}))

    graph = bl.resolve_graph(bl.KIND_ADDON, "demo_addon")

    ids = [s["id"] for s in graph["steps"]]
    assert "extra" in ids                     # the overlay spliced its step in
    assert graph["name"] == "base_demo_x"     # under the blessed alias
    assert graph["capabilities"] == ["game_assets"]


def test_addon_shape_attributes_the_spliced_steps(monkeypatch, home):
    _use(monkeypatch, _FakeSF(graphs={"base_demo": BASE_GRAPH},
                              overlays={"demo_addon": ADDON_SPEC}))
    graph = bl.resolve_graph(bl.KIND_ADDON, "demo_addon")
    shape = bl.capture_shape(graph, base_ids=bl._base_step_ids(bl.KIND_ADDON,
                                                               "demo_addon"))
    assert shape["addon_steps"] == ["extra"]


def test_renaming_a_base_step_breaks_the_overlay_and_says_so(monkeypatch, home):
    """The documented risk: an overlay may target a RAW base step id (game_harness
    hangs off `5_knowledge`, `t_plan`, `t_impl`), and nothing today notices when
    the base renames one. Recomposition raises, which is the finding.
    """
    renamed = copy.deepcopy(BASE_GRAPH)
    renamed["steps"][1]["id"] = "TWO"
    renamed["steps"][0]["transitions"] = [{"to": "TWO"}]
    renamed["end_conditions"]["conditions"][0]["node"] = "TWO"
    _use(monkeypatch, _FakeSF(graphs={"base_demo": renamed},
                              overlays={"demo_addon": ADDON_SPEC}))

    from skillflow.compose import ComposeError
    with pytest.raises(ComposeError, match="'two' not found"):
        bl.resolve_graph(bl.KIND_ADDON, "demo_addon")


def test_an_addon_bound_to_an_unregistered_base_says_which(monkeypatch, home):
    _use(monkeypatch, _FakeSF(graphs={}, overlays={"demo_addon": ADDON_SPEC}))
    with pytest.raises(ValueError, match="base_demo"):
        bl.resolve_graph(bl.KIND_ADDON, "demo_addon")


def test_kind_is_resolved_from_what_is_actually_registered(monkeypatch, home):
    _use(monkeypatch, _FakeSF(graphs={"base_demo": BASE_GRAPH},
                              overlays={"demo_addon": ADDON_SPEC}))
    assert bl.resolve_kind("gen_anything") == bl.KIND_PIPELINE
    assert bl.resolve_kind("demo_addon") == bl.KIND_ADDON
    # Neither — the caller must say so rather than guess and fail later.
    assert bl.resolve_kind("dpe_default_v2") is None


# --------------------------------------------------------------------------- #
# lifecycle: the baseline travels with the files it describes
# --------------------------------------------------------------------------- #

def test_archiving_takes_the_baseline_with_it(gdir):
    """Left behind, it would describe a pipeline that is no longer in the catalog
    and would be compared against a REGENERATED one under the same name."""
    gdir.mkdir(parents=True, exist_ok=True)
    for suffix in (".yaml", ".roles.json", ".baseline.json"):
        (gdir / f"gen_demo{suffix}").write_text("{}", encoding="utf-8")

    out = pr.archive_generated_pipeline(SkillFlow(":memory:"), ConfigRegistry(),
                                        "gen_demo")

    assert "gen_demo.baseline.json" in out["moved"]
    assert not (gdir / "gen_demo.baseline.json").exists()
    assert (pr.archived_dir() / "gen_demo.baseline.json").exists()


def test_un_archiving_clears_the_stale_baseline_copy(gdir):
    """Same reason the yaml copy is cleared: the archive dir must stop lying
    about what is retired."""
    gdir.mkdir(parents=True, exist_ok=True)
    pr._exclusion_file().write_text(json.dumps(["gen_demo"]), encoding="utf-8")
    stale = pr.archived_dir() / "gen_demo.baseline.json"
    stale.write_text("{}", encoding="utf-8")

    assert pr._unarchive("gen_demo") is True
    assert not stale.exists()
