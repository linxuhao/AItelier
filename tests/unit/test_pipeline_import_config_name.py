"""import_pipeline applies the existing config-name contract to the FINAL name.

export / get / edit classify names with `core.tool_guards.bad_config_name`
(single ASCII name, 1-64 characters, no '..'). Import used to accept anything
that merely started with `gen_`, so a 65/68-character or separator-bearing name
was probed, written and registered although it could never be exported, read or
edited again.

These tests drive the real `import_pipeline` and the real guard (no copied
regex). Disk is compared before/after through an isolated home; the effect
boundaries (tool probe, registration commit, un-archive) are instrumented so a
refusal that happens too late is visible even when the later step would fail
anyway.
"""
import json
from pathlib import Path

import pytest
import yaml

from core import pipeline_bundle as pb, pipeline_registry as pr
from core.pipeline_bundle import BundleError
from core.tool_guards import bad_config_name, bad_tool_name
from tests.integration.test_nested_callback_seed import runtime  # noqa: F401  (fixture)

TOOL_FILES = {"tool.yaml": "name: custom_scorer\ndescription: score it\n",
              "impl.py": "def custom_scorer(**kw):\n    return {'passed': True}\n"}


def _bundle(config_name: str, *, with_tool: bool = True) -> dict:
    role = f"{config_name}__author"
    steps = [
        {"id": "write", "step_type": "agent", "agent_config": role,
         "validation": [{"files": ["out.md"], "tool": "file_exists"}],
         "transitions": [{"to": "score" if with_tool else None}]},
    ]
    if with_tool:
        steps.append({"id": "score", "step_type": "tool", "tool_name": "custom_scorer",
                      "transitions": [{"to": None}]})
    graph = {"name": config_name, "begin": "write", "steps": steps}
    return {
        pb.BUNDLE_KEY: pb.BUNDLE_VERSION, "config_name": config_name,
        "graph_yaml": yaml.safe_dump(graph, sort_keys=False),
        "roles": {role: {"model": "host", "tools": ["read_file", "write"],
                         "system_prompt": "You are the author."}},
        "tools": {"custom_scorer": dict(TOOL_FILES)} if with_tool else {},
    }


def _tree(root: Path) -> dict:
    return {str(p.relative_to(root)): (p.read_text(encoding="utf-8") if p.is_file() else None)
            for p in sorted(root.rglob("*"))}


class _Registry:
    def __init__(self):
        self.registered = {}

    def get(self, name):
        return self.registered.get(name)

    def register_one(self, sf, name, hint_overrides=None):
        self.registered[name] = hint_overrides or {}


class _SF:
    def __init__(self):
        self.graphs = []
        self.agent_registry = {}

    def register_graph(self, g):
        self.graphs.append(g)

    def register_agent_config_from_dict(self, name, cfg):
        self.agent_registry[name] = cfg


@pytest.fixture
def home(tmp_path, monkeypatch):
    cfg, tools = tmp_path / "configs", tmp_path / "tools"
    cfg.mkdir()
    tools.mkdir()
    monkeypatch.setenv("AITELIER_GENERATED_CONFIGS_DIR", str(cfg))
    monkeypatch.setattr(pb, "_generated_tools_dir", lambda: tools)
    return cfg, tools


@pytest.fixture
def boundaries(monkeypatch):
    """Count every effect boundary import_pipeline can cross after name selection."""
    calls = {"probe": [], "commit": [], "unarchive": []}
    real_probe = pb.tool_source_error
    real_commit = pr._commit_registration
    real_unarchive = pr._unarchive

    def probe(name, source):
        calls["probe"].append(name)
        return real_probe(name, source)

    def commit(*a, **kw):
        calls["commit"].append(a[2])
        return real_commit(*a, **kw)

    def unarchive(name):
        calls["unarchive"].append(name)
        return real_unarchive(name)

    monkeypatch.setattr(pb, "tool_source_error", probe)
    monkeypatch.setattr(pr, "_commit_registration", commit)
    monkeypatch.setattr(pr, "_unarchive", unarchive)
    return calls


SUFFIX_64 = "a" * 60            # "gen_" + 60 == exactly 64
INVALID = [
    pytest.param("gen_" + "a" * 61, id="length65"),
    pytest.param("gen_" + "a" * 64, id="length68"),
    pytest.param("gen_a/b", id="forward-slash"),
    pytest.param("gen_a\\b", id="backslash"),
    pytest.param("gen_../x", id="traversal-segment"),
    pytest.param("gen_a..b", id="double-dot"),
    pytest.param("gen_a\x00b", id="nul"),
    pytest.param("gen_a\tb", id="control-tab"),
    pytest.param("gen_a\n", id="final-newline"),
    pytest.param("gen_" + "a" * 60 + "\n", id="64-ascii-plus-newline65"),
    pytest.param("gen_caf\u00e9", id="non-ascii"),
    pytest.param("gen_a b", id="space"),
]


@pytest.mark.parametrize("name", INVALID)
def test_invalid_selected_name_is_refused_before_any_effect(home, boundaries, name):
    cfg, tools = home
    assert bad_config_name(name) is not None          # the REAL guard classifies it
    before = (_tree(cfg), _tree(tools))
    sf, registry = _SF(), _Registry()

    with pytest.raises(BundleError, match="invalid config name"):
        pb.import_pipeline(sf, registry, _bundle(name))

    assert boundaries == {"probe": [], "commit": [], "unarchive": []}
    assert (_tree(cfg), _tree(tools)) == before
    assert sf.graphs == [] and sf.agent_registry == {} and registry.registered == {}


def test_overlong_name_is_refused_even_with_overwrite_tools(home, boundaries):
    cfg, tools = home
    name = "gen_" + "a" * 64
    with pytest.raises(BundleError, match="invalid config name"):
        pb.import_pipeline(_SF(), _Registry(), _bundle(name), overwrite_tools=True)
    assert boundaries == {"probe": [], "commit": [], "unarchive": []}
    assert _tree(cfg) == {} and _tree(tools) == {}


def test_exact_64_name_imports_and_is_inspectable_and_exportable(home, boundaries):
    cfg, tools = home
    name = "gen_" + SUFFIX_64
    assert len(name) == 64 and bad_config_name(name) is None

    sf, registry = _SF(), _Registry()
    result = pb.import_pipeline(sf, registry, _bundle(name))

    assert result["config_name"] == name and result["action"] == "created"
    assert result["renamed_from"] is None
    assert boundaries["commit"] == [name] and boundaries["probe"] == ["custom_scorer"]
    assert (cfg / f"{name}.yaml").is_file() and (cfg / f"{name}.roles.json").is_file()
    assert registry.get(name) is not None and len(sf.graphs) == 1

    exported = pb.export_pipeline(name)                # ordinary entry accepts it
    assert exported["config_name"] == name
    assert yaml.safe_load(exported["graph_yaml"])["name"] == name
    assert list(json.loads((cfg / f"{name}.roles.json").read_text())) == [f"{name}__author"]
    assert sorted(exported["tools"]) == ["custom_scorer"]


def test_explicit_rename_gives_an_old_oversized_bundle_a_new_valid_name(home, boundaries):
    cfg, tools = home
    old = "gen_" + "a" * 64                             # invalid as a selected name
    sf, registry = _SF(), _Registry()

    result = pb.import_pipeline(sf, registry, _bundle(old), name="fresh copy")

    assert result["config_name"] == "gen_fresh_copy"
    assert result["renamed_from"] == old
    assert bad_config_name(result["config_name"]) is None
    assert boundaries["commit"] == ["gen_fresh_copy"]
    assert (cfg / "gen_fresh_copy.yaml").is_file()
    assert not (cfg / f"{old}.yaml").exists()
    roles = json.loads((cfg / "gen_fresh_copy.roles.json").read_text())
    assert list(roles) == ["gen_fresh_copy__author"]
    assert pb.export_pipeline("gen_fresh_copy")["config_name"] == "gen_fresh_copy"


def test_rename_with_unsafe_text_still_lands_on_a_safe_name(home, boundaries):
    cfg, _ = home
    result = pb.import_pipeline(_SF(), _Registry(), _bundle("gen_alpha"),
                                name="../../etc/passwd " + "x" * 200)
    assert bad_config_name(result["config_name"]) is None
    assert Path(result["path"]).parent == cfg


def test_builtin_namespace_guard_is_unchanged(home, boundaries):
    with pytest.raises(BundleError, match="not a generated pipeline name"):
        pb.import_pipeline(_SF(), _Registry(), _bundle("dpe_default_v2"))
    assert boundaries == {"probe": [], "commit": [], "unarchive": []}


# ── Real temporary DB / SDK / registry ───────────────────────────────────────

def _real_snapshot(sf, registry, name):
    cdir, tdir = pr.generated_configs_dir(), pb._generated_tools_dir()
    return {
        "configs": _tree(cdir), "tools": _tree(tdir),
        "graph_registered": name in getattr(sf, "_graphs", {}),
        "manifest": registry.get(name) is not None,
    }


@pytest.mark.parametrize("name", [
    pytest.param("gen_" + "b" * 61, id="length65"),
    pytest.param("gen_" + "b" * 64, id="length68"),
    pytest.param("gen_x/../y", id="traversal"),
    pytest.param("gen_a\n", id="final-newline"),
    pytest.param("gen_" + "a" * 60 + "\n", id="64-ascii-plus-newline65"),
])
def test_real_runtime_refusal_leaves_disk_sdk_and_registry_untouched(runtime, boundaries, name):
    sf, db, ws, registry, outbound = runtime
    before = _real_snapshot(sf, registry, name)
    assert before["graph_registered"] is False and before["manifest"] is False

    with pytest.raises(BundleError, match="invalid config name"):
        pb.import_pipeline(sf, registry, _bundle(name))

    assert boundaries == {"probe": [], "commit": [], "unarchive": []}
    assert _real_snapshot(sf, registry, name) == before
    assert outbound == []


def test_real_runtime_exact_64_import_registers_and_exports(runtime, boundaries):
    sf, db, ws, registry, outbound = runtime
    name = "gen_" + "c" * 60
    assert len(name) == 64 and bad_config_name(name) is None

    result = pb.import_pipeline(sf, registry, _bundle(name, with_tool=False))

    assert result["config_name"] == name
    assert name in sf._graphs
    manifest = registry.get(name)
    assert manifest is not None and manifest.config_name == name
    assert Path(result["path"]).is_file()
    assert pb.export_pipeline(name)["config_name"] == name
    assert outbound == []


@pytest.mark.parametrize("name", ["gen_a\n", "gen_" + "a" * 60 + "\n"])
def test_shared_config_and_tool_checker_consumes_the_entire_name(name):
    # A dollar anchor with match() can stop before a final newline. Both public
    # consumers must enforce the same unchanged whole-name grammar.
    assert bad_config_name(name) is not None
    assert bad_tool_name(name) is not None
