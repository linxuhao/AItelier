"""export_pipeline / import_pipeline — the closure has to survive the trip.

The interesting tests here are not "does a round trip work". They are the ways a
bundle can arrive looking installed and be broken:

  1. RENAME. Roles are stored namespaced and the graph points at those names.
     Re-prefixing without stripping gives `gen_b__gen_a__author`, the graph's ref
     no longer resolves, and every step silently falls back to a generic host
     prompt. The pipeline still runs. It just isn't the pipeline you shipped.
  2. TOMBSTONE. `archive_generated_pipeline` records the name in
     `_archived/archived.json`, which the boot scan consults. Files written back
     under an archived name work until the next restart.
  3. TOOL COLLISION. Generated tools live in ONE globally-registered directory
     keyed by name. Overwriting on import changes that tool for every pipeline
     already using it.
  4. NAMES FROM ELSEWHERE. A bundle is written by another machine, and its tool
     names, config name and role keys all land where THIS host resolves names —
     `tdir / tname`, `register_graph` (INSERT OR REPLACE), the global agent
     registry. Each is a way to overwrite something that isn't the import.
  5. HALF-INSTALLED. A check that runs after the writes leaves a broken pipeline
     on disk, which looks installed and survives every restart.

Each has a test that fails if the defence is removed.
"""

import json

import pytest
import yaml

from core import pipeline_bundle as pb
from core.pipeline_bundle import BundleError

GRAPH = {
    "name": "gen_alpha",
    # `begin` is not decoration: skillflow's own validate() reports "Graph begin
    # node is required" without it. The fixture used to omit it — and every import
    # test passed, because nothing validated the graph before writing it to disk.
    "begin": "write",
    "steps": [
        {"id": "write", "step_type": "agent", "agent_config": "gen_alpha__author",
         "validation": [{"files": ["out.md"], "tool": "file_exists"}],
         "transitions": [{"to": "score"}]},
        {"id": "score", "step_type": "tool", "tool_name": "custom_scorer",
         "transitions": [{"to": None}]},      # `to: null` is skillflow's terminal
    ],
}
ROLES = {"gen_alpha__author": {"model": "host", "tools": ["read_file", "write"],
                               "system_prompt": "You are the author."}}
TOOL_FILES = {"tool.yaml": "name: custom_scorer\ndescription: score it\n",
              "impl.py": "def custom_scorer(**kw):\n    return {'passed': True}\n"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolate both directories the bundle touches."""
    cfg, tools = tmp_path / "configs", tmp_path / "tools"
    cfg.mkdir(); tools.mkdir()
    monkeypatch.setenv("AITELIER_GENERATED_CONFIGS_DIR", str(cfg))
    monkeypatch.setattr(pb, "_generated_tools_dir", lambda: tools)
    return cfg, tools


def _install_alpha(home):
    cfg, tools = home
    (cfg / "gen_alpha.yaml").write_text(
        yaml.safe_dump(GRAPH, sort_keys=False), encoding="utf-8")
    (cfg / "gen_alpha.roles.json").write_text(
        json.dumps(ROLES, ensure_ascii=False), encoding="utf-8")
    d = tools / "custom_scorer"; d.mkdir()
    for k, v in TOOL_FILES.items():
        (d / k).write_text(v, encoding="utf-8")


class _Registry:
    def __init__(self): self.registered = {}
    def get(self, name): return self.registered.get(name)
    def register_one(self, sf, name, hint_overrides=None):
        self.registered[name] = hint_overrides or {}


class _SF:
    """Just the surface pipeline_registry actually touches during registration."""
    def __init__(self): self.graphs = []; self.agent_registry = {}
    def register_graph(self, g): self.graphs.append(g)
    def register_agent_config_from_dict(self, name, cfg):
        self.agent_registry[name] = cfg


# ── Closure discovery ────────────────────────────────────────────────────────

def test_every_place_a_tool_name_can_hide_is_searched():
    graph = {"steps": [
        {"id": "a", "step_type": "tool", "tool_name": "from_step"},
        {"id": "b", "validation": [{"tool": "from_validation"}]},
        {"id": "c", "context": [{"source": {"tool": "from_context"}}]},
        {"id": "d", "lifecycle": {"on_deliver": [{"tool": "from_lifecycle"}]}},
    ]}
    roles = {"r": {"tools": ["from_role"]}}
    assert pb.referenced_tool_names(graph, roles) == {
        "from_step", "from_validation", "from_context", "from_lifecycle", "from_role"}


def test_a_builtin_tool_is_not_bundled(home):
    """Shipping a copy of `read_file` would shadow the host's own with a frozen one."""
    _install_alpha(home)
    b = pb.export_pipeline("gen_alpha")
    assert "custom_scorer" in b["tools"]
    assert "read_file" not in b["tools"] and "file_exists" not in b["tools"]


def test_exporting_a_config_that_is_not_generated_is_refused(home):
    with pytest.raises(BundleError, match="not a generated pipeline"):
        pb.export_pipeline("dpe_default")


# ── Round trip ───────────────────────────────────────────────────────────────

def test_round_trip_restores_graph_roles_and_tools(home):
    cfg, tools = home
    _install_alpha(home)
    bundle = pb.export_pipeline("gen_alpha")

    # Wipe the machine, then import.
    (cfg / "gen_alpha.yaml").unlink()
    (cfg / "gen_alpha.roles.json").unlink()
    for f in (tools / "custom_scorer").iterdir():
        f.unlink()
    (tools / "custom_scorer").rmdir()

    result = pb.import_pipeline(_SF(), _Registry(), bundle)
    assert result["config_name"] == "gen_alpha"
    assert result["action"] == "created"
    assert result["tools_installed"] == ["custom_scorer"]
    assert json.loads((cfg / "gen_alpha.roles.json").read_text()) == ROLES
    assert (tools / "custom_scorer" / "impl.py").read_text() == TOOL_FILES["impl.py"]


def test_a_bundle_is_json_serialisable(home):
    """It travels as an MCP tool result, which is a string."""
    _install_alpha(home)
    assert json.loads(json.dumps(pb.export_pipeline("gen_alpha")))["config_name"] \
        == "gen_alpha"


# ── 1. Rename ────────────────────────────────────────────────────────────────

def test_renaming_moves_roles_and_the_graph_refs_together(home):
    cfg, _ = home
    _install_alpha(home)
    bundle = pb.export_pipeline("gen_alpha")

    result = pb.import_pipeline(_SF(), _Registry(), bundle, name="beta")
    assert result["config_name"] == "gen_beta"
    assert result["renamed_from"] == "gen_alpha"

    roles = json.loads((cfg / "gen_beta.roles.json").read_text())
    graph = yaml.safe_load((cfg / "gen_beta.yaml").read_text())
    ref = [s for s in graph["steps"] if s.get("agent_config")][0]["agent_config"]

    assert list(roles) == ["gen_beta__author"]
    assert ref == "gen_beta__author"
    # The failure this guards is SILENT: a mismatch drops the real prompt and the
    # step runs on a generic fallback, so assert the two sides actually agree.
    assert ref in roles
    assert "gen_alpha__" not in ref and "gen_beta__gen_" not in ref


def test_renaming_twice_does_not_stack_prefixes(home):
    _install_alpha(home)
    b1 = pb.export_pipeline("gen_alpha")
    pb.import_pipeline(_SF(), _Registry(), b1, name="beta")
    b2 = pb.export_pipeline("gen_beta")
    pb.import_pipeline(_SF(), _Registry(), b2, name="gamma")
    cfg, _ = home
    roles = json.loads((cfg / "gen_gamma.roles.json").read_text())
    assert list(roles) == ["gen_gamma__author"]


def test_importing_under_its_own_name_is_idempotent(home):
    _install_alpha(home)
    bundle = pb.export_pipeline("gen_alpha")
    pb.import_pipeline(_SF(), _Registry(), bundle)
    cfg, _ = home
    assert list(json.loads((cfg / "gen_alpha.roles.json").read_text())) \
        == ["gen_alpha__author"]


# ── 2. Tombstone ─────────────────────────────────────────────────────────────

def test_importing_clears_an_archive_tombstone(home, monkeypatch):
    """Files on disk under an archived name work until the next boot scan."""
    from core import pipeline_registry as pr
    _install_alpha(home)
    bundle = pb.export_pipeline("gen_alpha")

    cleared = []
    monkeypatch.setattr(pr, "_unarchive", lambda n: cleared.append(n) or True)
    pb.import_pipeline(_SF(), _Registry(), bundle)
    assert cleared == ["gen_alpha"]


# ── 3. Tool collision ────────────────────────────────────────────────────────

def test_a_differing_tool_of_the_same_name_is_refused(home):
    cfg, tools = home
    _install_alpha(home)
    bundle = pb.export_pipeline("gen_alpha")
    # Someone else's custom_scorer is already installed, with other code.
    (tools / "custom_scorer" / "impl.py").write_text(
        "def custom_scorer(**kw):\n    return {'passed': False}\n", encoding="utf-8")

    with pytest.raises(BundleError, match="already exist with DIFFERENT content"):
        pb.import_pipeline(_SF(), _Registry(), bundle)


def test_a_refused_import_writes_nothing_at_all(home):
    """Half-imported is worse than refused: it looks installed."""
    cfg, tools = home
    _install_alpha(home)
    bundle = pb.export_pipeline("gen_alpha")
    bundle["config_name"] = "gen_zeta"          # a name not on disk yet
    (tools / "custom_scorer" / "impl.py").write_text("def custom_scorer(**kw): ...\n",
                                                     encoding="utf-8")
    with pytest.raises(BundleError):
        pb.import_pipeline(_SF(), _Registry(), bundle)
    assert not (cfg / "gen_zeta.yaml").exists()
    assert not (cfg / "gen_zeta.roles.json").exists()


def test_an_identical_tool_is_not_a_collision(home):
    _install_alpha(home)
    bundle = pb.export_pipeline("gen_alpha")
    result = pb.import_pipeline(_SF(), _Registry(), bundle)
    assert result["tools_installed"] == []      # already there, byte-identical


def test_overwrite_tools_is_opt_in(home):
    cfg, tools = home
    _install_alpha(home)
    bundle = pb.export_pipeline("gen_alpha")
    (tools / "custom_scorer" / "impl.py").write_text("# theirs\n", encoding="utf-8")
    result = pb.import_pipeline(_SF(), _Registry(), bundle, overwrite_tools=True)
    assert result["tools_installed"] == ["custom_scorer"]
    assert (tools / "custom_scorer" / "impl.py").read_text() == TOOL_FILES["impl.py"]


# ── Malformed input ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("bundle,match", [
    ({}, "not an AItelier pipeline bundle"),
    ({pb.BUNDLE_KEY: 99, "graph_yaml": "x"}, "this host reads"),
    ({pb.BUNDLE_KEY: 1}, "no graph_yaml"),
    ({pb.BUNDLE_KEY: 1, "graph_yaml": "steps: ["}, "not valid YAML"),
    ({pb.BUNDLE_KEY: 1, "graph_yaml": "name: x\n"}, "no steps"),
    ({pb.BUNDLE_KEY: 1, "graph_yaml": "steps: [{id: a}]", "roles": []}, "not an object"),
])
def test_a_malformed_bundle_is_rejected_before_touching_disk(bundle, match):
    with pytest.raises(BundleError, match=match):
        pb.validate_bundle(bundle)


def test_a_tool_carrying_an_unexpected_file_is_refused(home):
    _install_alpha(home)
    bundle = pb.export_pipeline("gen_alpha")
    bundle["tools"]["custom_scorer"] = {**TOOL_FILES, "../../evil.py": "boom"}
    with pytest.raises(BundleError):
        pb.import_pipeline(_SF(), _Registry(), bundle, overwrite_tools=True)


# ── 4. The bundle is INPUT, not a set of local identifiers ───────────────────
#
# Everything above treats the bundle as if this machine wrote it. It didn't: a
# bundle is an artifact from another host, and every string in it — tool names,
# the config name, role keys — lands in a place where this host resolves names.

@pytest.mark.parametrize("tname", ["../escaped", "sub/dir", ".hidden"])
def test_a_tool_name_that_leaves_the_generated_dir_is_refused(home, tname):
    """`tdir / tname` is only safe while tname is one directory name.

    `../../AItelier/aitelier/tools/web_search` writes impl.py over a BUILT-IN
    tool the host imports and runs. Installing a shared pipeline is consent to
    add a tool, not to overwrite files outside the generated-tools directory.
    """
    cfg, tools = home
    _install_alpha(home)
    bundle = pb.export_pipeline("gen_alpha")
    bundle["tools"] = {tname: dict(TOOL_FILES)}
    with pytest.raises(BundleError, match="invalid tool name"):
        pb.import_pipeline(_SF(), _Registry(), bundle, overwrite_tools=True)
    assert not (tools.parent / "escaped").exists()      # ../ landed nowhere
    assert not (tools / "sub").exists()
    assert not (tools / ".hidden").exists()


def test_a_bundle_cannot_claim_a_builtin_config_name(home):
    """register_graph is INSERT OR REPLACE by name — the built-in graph is gone.

    The registry's whole no-blocklist design rests on gen_* and built-in names
    being disjoint; export, edit and archive all refuse a non-gen_ name and
    import was the door left open.
    """
    cfg, _ = home
    _install_alpha(home)
    bundle = pb.export_pipeline("gen_alpha")
    bundle["config_name"] = "dpe_default_v2"
    sf = _SF()
    with pytest.raises(BundleError, match="not a generated pipeline name"):
        pb.import_pipeline(sf, _Registry(), bundle)
    assert sf.graphs == []                              # nothing re-registered
    assert not (cfg / "dpe_default_v2.yaml").exists()


def test_renaming_is_the_way_to_install_such_a_bundle(home):
    """Refusal is not a dead end: `name` forces it into the generated keyspace."""
    cfg, _ = home
    _install_alpha(home)
    bundle = pb.export_pipeline("gen_alpha")
    bundle["config_name"] = "dpe_default_v2"
    result = pb.import_pipeline(_SF(), _Registry(), bundle, name="dpe_default_v2")
    assert result["config_name"] == "gen_dpe_default_v2"
    assert (cfg / "gen_dpe_default_v2.yaml").exists()


def test_bare_role_keys_are_namespaced_even_without_a_rename(home):
    """Roles are registered GLOBALLY by key, and namespacing only ran on rename.

    A bundle carrying `researcher` (hand-written, or from a host that never
    namespaced) therefore replaced DPE's own researcher agent config for the
    whole process — with the bundle author's prompt.
    """
    cfg, _ = home
    graph = {"name": "gen_alpha", "begin": "write",
             "steps": [{"id": "write", "agent_config": "researcher",
                        "transitions": [{"to": None}]}]}
    bundle = {pb.BUNDLE_KEY: pb.BUNDLE_VERSION, "config_name": "gen_alpha",
              "graph_yaml": yaml.safe_dump(graph),
              "roles": {"researcher": {"system_prompt": "not DPE's researcher"}},
              "tools": {}}
    sf = _SF()
    pb.import_pipeline(sf, _Registry(), bundle)

    assert "researcher" not in sf.agent_registry
    assert "gen_alpha__researcher" in sf.agent_registry
    # Both sides move together, or the step falls back to a generic host prompt.
    saved = yaml.safe_load((cfg / "gen_alpha.yaml").read_text())
    assert saved["steps"][0]["agent_config"] == "gen_alpha__researcher"


# ── 5. Nothing is written until every check has passed ──────────────────────

def test_a_graph_that_fails_skillflow_validation_writes_nothing(home, monkeypatch):
    """`PipelineGraph._from_dict` only BUILDS; validate() is a separate call.

    It used to run first inside `sf.register_graph`, i.e. after the tools, the
    YAML, the roles.json and the un-archive had all landed — leaving a broken
    pipeline installed for every later boot scan to load, and raising a
    GraphValidationError that the MCP layer's `except BundleError` misses.
    """
    from core import pipeline_registry as pr
    cfg, tools = home
    _install_alpha(home)
    bundle = pb.export_pipeline("gen_alpha")
    bad = yaml.safe_load(bundle["graph_yaml"])
    bad["steps"][0]["transitions"] = [{"to": "nowhere"}]
    bundle["graph_yaml"] = yaml.safe_dump(bad)
    bundle["config_name"] = "gen_zeta"                  # a name not on disk yet
    for f in (tools / "custom_scorer").iterdir():
        f.unlink()
    (tools / "custom_scorer").rmdir()                   # so the tool is a fresh write

    cleared = []
    monkeypatch.setattr(pr, "_unarchive", lambda n: cleared.append(n) or True)
    with pytest.raises(BundleError, match="not a valid pipeline"):
        pb.import_pipeline(_SF(), _Registry(), bundle)

    assert not (cfg / "gen_zeta.yaml").exists()
    assert not (cfg / "gen_zeta.roles.json").exists()
    assert not (tools / "custom_scorer").exists()
    assert cleared == []                                # tombstone still standing


def test_a_tool_with_a_bad_filename_leaves_no_partial_tool_dir(home):
    """The filename check sat INSIDE the commit loop, so earlier tools (and the
    earlier files of this one) were already on disk when it fired. The debris
    then digest-mismatched on retry, bricking the import even with
    overwrite_tools=true."""
    cfg, tools = home
    _install_alpha(home)
    bundle = pb.export_pipeline("gen_alpha")
    for f in (tools / "custom_scorer").iterdir():
        f.unlink()
    (tools / "custom_scorer").rmdir()
    bundle["tools"] = {"custom_scorer": dict(TOOL_FILES),
                       "second_tool": {**TOOL_FILES, "notes.txt": "not a tool file"}}

    with pytest.raises(BundleError, match="unexpected file"):
        pb.import_pipeline(_SF(), _Registry(), bundle)
    assert not (tools / "custom_scorer").exists()       # the GOOD tool, untouched
    assert not (tools / "second_tool").exists()

    # And the fixed bundle imports cleanly — no debris to collide with.
    del bundle["tools"]["second_tool"]["notes.txt"]
    result = pb.import_pipeline(_SF(), _Registry(), bundle)
    assert result["tools_installed"] == ["custom_scorer", "second_tool"]


def test_a_graph_the_engine_rejects_is_reported_as_a_bundle_error(home):
    """`register_graph` raises GraphValidationError — not a BundleError.

    The MCP layer catches only BundleError, so this escaped raw to the caller,
    and it did so AFTER the tools, the YAML and the roles.json were written.
    Registration now happens before the commit, and its failure is translated.
    """
    from skillflow.exceptions import GraphValidationError

    class _RejectingSF(_SF):
        def register_graph(self, g):
            raise GraphValidationError(["the engine says no"])

    cfg, tools = home
    _install_alpha(home)
    bundle = pb.export_pipeline("gen_alpha")
    bundle["config_name"] = "gen_zeta"
    for f in (tools / "custom_scorer").iterdir():
        f.unlink()
    (tools / "custom_scorer").rmdir()

    with pytest.raises(BundleError, match="rejected"):
        pb.import_pipeline(_RejectingSF(), _Registry(), bundle)
    assert not (cfg / "gen_zeta.yaml").exists()
    assert not (tools / "custom_scorer").exists()
