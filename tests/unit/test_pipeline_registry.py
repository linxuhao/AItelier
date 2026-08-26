"""Unit tests for core.pipeline_registry — making converter-generated pipelines
runnable: namespacing, host-agent auto-registration, live register + manifest,
in-place update, boot-time load, and graceful failures."""

import json
import textwrap
from pathlib import Path

import pytest
import yaml
from skillflow import SkillFlow

from core import pipeline_registry as pr
from core.config_registry import ConfigRegistry

# A generated-style pipeline: invented agent roles, no registered agent configs.
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


@pytest.fixture
def sf():
    return SkillFlow(":memory:")


@pytest.fixture
def registry():
    return ConfigRegistry()


@pytest.fixture
def gdir(tmp_path, monkeypatch):
    d = tmp_path / "gen_configs"
    monkeypatch.setenv("AITELIER_GENERATED_CONFIGS_DIR", str(d))
    return d


def _patch_output(monkeypatch, path):
    monkeypatch.setattr(
        "skillflow.plugins.skill_converter.get_output_file",
        lambda _sf, _rid: path)


def test_name_is_namespaced_and_never_a_core_config():
    assert pr.config_name_for("My Cool Pipeline") == "gen_my_cool_pipeline"
    for core in ("dpe_default_v2", "meta_conversation", "skill_converter"):
        n = pr.config_name_for(core)
        assert n.startswith("gen_") and n != core


def test_register_text_adds_host_agents_graph_and_manifest(sf, registry):
    pr._register_text(sf, registry, "gen_demo", GEN_YAML)
    # invented roles auto-registered as host agents (else register_graph rejects)
    assert "processor" in sf.agent_registry
    assert "summarizer" in sf.agent_registry
    # graph is live under the forced namespaced name
    assert any(g["name"] == "gen_demo" for g in sf.list_graphs())
    # manifest present + carries generated-pipeline hints: SCHEDULER-driven (so a
    # run advances whoever started it — the butler is not the only starter any
    # more) with a seed file (so seed_text reaches step 1).
    m = registry.get("gen_demo")
    assert m is not None
    assert m.scheduler_owned is True
    assert m.seed_file == "seed_input.md"
    assert "process" in m.steps
    # a converted skill self-describes in the butler's pipeline catalog
    # (advertised as a layer-3 offload target) — carries a generic input_hint
    # so the butler knows how to feed it.
    assert "seed_text" in m.input_hint
    cat = {e["config_name"]: e for e in registry.catalog(full=True)}["gen_demo"]
    # `drive` follows scheduler_owned: the catalog tells the butler who advances
    # this pipeline, and a generated one is now the poller's, not the starter's.
    assert cat["drive"] == "background" and cat["input_hint"] == m.input_hint


def test_generated_roles_namespaced_and_dont_clobber_globals(sf, registry, gdir,
                                                             tmp_path, monkeypatch):
    """A generated role that collides with a real (global) agent name must NOT bind
    to or overwrite that agent — it's namespaced per-config."""
    # a pre-existing GLOBAL agent (mimics DPE's 'researcher')
    sf.register_agent_config_from_dict(
        "researcher", {"model": "deepseek/real", "system_prompt": "DPE researcher"})
    yml = GEN_YAML.replace("agent_config: processor", "agent_config: researcher")
    src = tmp_path / "p.yaml"
    src.write_text(yml, encoding="utf-8")
    _patch_output(monkeypatch, src)

    res = pr.register_generated_pipeline(sf, registry, "r1", "My Cool Pipeline")
    cn = res["config_name"]
    ns = f"{cn}__researcher"
    # global 'researcher' untouched (still the DPE agent)
    assert sf.agent_registry.get("researcher").model == "deepseek/real"
    # generated step registered under a namespaced host agent
    assert ns in sf.agent_registry
    assert sf.agent_registry.get(ns).model == "host"
    # persisted YAML uses the namespaced role, never the bare global name
    persisted = yaml.safe_load((gdir / f"{cn}.yaml").read_text())
    roles = [s.get("agent_config") for s in persisted["steps"]
             if s.get("step_type") == "agent"]
    assert ns in roles and "researcher" not in roles


def test_namespacing_and_seed_handle_omitted_step_type(sf, registry, gdir,
                                                       tmp_path, monkeypatch):
    """skillflow defaults step_type to 'agent' when omitted; an agent step that
    leaves step_type out must still be namespaced AND seeded."""
    sf.register_agent_config_from_dict(
        "researcher", {"model": "deepseek/real", "system_prompt": "DPE"})
    yml = textwrap.dedent("""
        name: placeholder
        begin: process
        end_conditions:
          combinator: or
          conditions:
            - {type: node_reached, node: done, result: completed}
        steps:
          - id: process
            agent_config: researcher
            transitions:
              - to: done
          - id: done
            agent_config: summarizer
    """)  # NOTE: no step_type on either step
    src = tmp_path / "p.yaml"
    src.write_text(yml, encoding="utf-8")
    _patch_output(monkeypatch, src)
    res = pr.register_generated_pipeline(sf, registry, "n1", "No Type")
    cn = res["config_name"]
    assert "error" not in res
    # collision-prone role got namespaced despite missing step_type
    assert sf.agent_registry.get("researcher").model == "deepseek/real"  # global intact
    assert f"{cn}__researcher" in sf.agent_registry
    data = yaml.safe_load((gdir / f"{cn}.yaml").read_text())
    roles = [s.get("agent_config") for s in data["steps"]]
    assert f"{cn}__researcher" in roles and "researcher" not in roles
    # begin step (no step_type) still got the seed source
    bstep = next(s for s in data["steps"] if s["id"] == data["begin"])
    srcs = [(c.get("source", c) or {}) for c in bstep.get("context", [])]
    assert any(x.get("output") == "seed_input.md" for x in srcs)


def test_gen_config_seeds_first_step_and_is_butler_driven(sf, registry, gdir,
                                                          tmp_path, monkeypatch):
    src = tmp_path / "p.yaml"
    src.write_text(GEN_YAML, encoding="utf-8")
    _patch_output(monkeypatch, src)
    res = pr.register_generated_pipeline(sf, registry, "s1", "Seedy")
    cn = res["config_name"]
    m = registry.get(cn)
    assert m.scheduler_owned is True
    assert m.seed_file == "seed_input.md"
    # begin step now reads the seed (so start_config_run's seed_text reaches it)
    data = yaml.safe_load((gdir / f"{cn}.yaml").read_text())
    bstep = next(s for s in data["steps"] if s["id"] == data["begin"])
    srcs = [(c.get("source", c) or {}) for c in bstep.get("context", [])]
    assert any(x.get("config") == cn and x.get("output") == "seed_input.md"
               for x in srcs)


def test_update_overwrites_in_place(sf, registry):
    pr._register_text(sf, registry, "gen_demo", GEN_YAML)
    data = yaml.safe_load(GEN_YAML)
    data["steps"][1]["transitions"] = [{"to": "extra"}]
    data["steps"].append({"id": "extra", "step_type": "agent",
                          "agent_config": "checker"})
    pr._register_text(sf, registry, "gen_demo", yaml.safe_dump(data))
    # exactly one manifest, reflecting the NEW graph (manifest reads live + lazy)
    assert [m.config_name for m in registry.list()].count("gen_demo") == 1
    assert "extra" in registry.get("gen_demo").steps


def test_register_generated_pipeline_persists_and_updates(sf, registry, gdir,
                                                          tmp_path, monkeypatch):
    src = tmp_path / "skill_pipeline.yaml"
    src.write_text(GEN_YAML, encoding="utf-8")
    _patch_output(monkeypatch, src)

    res = pr.register_generated_pipeline(sf, registry, "run1", "My Cool Pipeline")
    assert res["config_name"] == "gen_my_cool_pipeline"
    assert res["action"] == "created"
    persisted = gdir / "gen_my_cool_pipeline.yaml"
    assert persisted.exists()
    # persisted YAML carries the namespaced name so boot re-scan agrees
    assert yaml.safe_load(persisted.read_text())["name"] == "gen_my_cool_pipeline"
    assert registry.get("gen_my_cool_pipeline") is not None

    # same name again → update in place
    res2 = pr.register_generated_pipeline(sf, registry, "run2", "My Cool Pipeline")
    assert res2["action"] == "updated"


def test_edit_mode_prenamespaced_role_table_is_not_double_prefixed(
        tmp_path, registry, gdir):
    """EDIT-mode regression (the CAC40 e2e host bug): in edit mode the forge
    emitter echoes the baseline's already-namespaced role names into
    role_table.yaml. register_forge_pipeline must re-apply the ``<config>__``
    prefix IDEMPOTENTLY — a blind ``prefix + role`` double-prefixes
    (``gen_x__gen_x__role``), mismatching the (single-prefixed) graph
    agent_config refs, so the real emitted prompt is silently dropped to the
    generic host fallback."""
    import skillflow as _sk
    from skillflow import PipelineGraph
    from skillflow.tool_loader import ToolLoader
    loader = ToolLoader(Path(_sk.__file__).parent / "tools")
    sf = SkillFlow(str(tmp_path / "sf.db"), tool_loader=loader,
                   workspace_base=str(tmp_path / "ws"),
                   projects_base=str(tmp_path / "proj"))

    config_name = pr.config_name_for("cac40 daily")          # gen_cac40_daily
    pfx = config_name + pr._ROLE_SEP
    graph = {
        "name": "placeholder", "begin": "make",
        "end_conditions": {"combinator": "or", "conditions": [
            {"type": "node_reached", "node": "done", "result": "completed"}]},
        "steps": [
            {"id": "make", "step_type": "agent", "agent_config": pfx + "maker",
             "transitions": [{"to": "done"}]},
            {"id": "done", "step_type": "gate", "transitions": [{"to": None}]},
        ],
    }
    # A minimal graph named 'pipeline_forge' so create_run has something to bind
    # to (register_forge_pipeline only reads the run's project_id).
    forge_run_graph = {**graph, "name": "pipeline_forge"}
    sf.register_agent_config_from_dict(pfx + "maker", {"model": "host"})
    sf.register_graph(PipelineGraph._from_dict(forge_run_graph))
    run_id = sf.create_run("pipeline_forge", {"project_id": "p"})

    emit = sf._workspace.get_step_dir("p", "pipeline_forge", "emit_graph")
    emit.mkdir(parents=True, exist_ok=True)
    (emit / "pipeline.yaml").write_text(yaml.safe_dump(graph), encoding="utf-8")
    (emit / "role_table.yaml").write_text(
        yaml.safe_dump({pfx + "maker": {"tools": ["read_file", "write"],
                                        "template": f"templates/{pfx}maker.md"}}),
        encoding="utf-8")
    (emit / "templates").mkdir(exist_ok=True)
    (emit / "templates" / f"{pfx}maker.md").write_text(
        "REAL MAKER PROMPT", encoding="utf-8")

    res = pr.register_forge_pipeline(sf, registry, run_id, "cac40 daily")
    assert "error" not in res, res

    role_key = pfx + "maker"
    assert role_key in sf.agent_registry                     # single prefix
    assert (pfx + pfx + "maker") not in sf.agent_registry    # NOT double
    # the registered role kept the REAL emitted prompt, not the generic fallback
    assert sf.agent_registry.get(role_key).config["system_prompt"] == "REAL MAKER PROMPT"
    # graph ref stayed single-prefixed and matches the role key
    persisted = yaml.safe_load((gdir / f"{config_name}.yaml").read_text())
    refs = [s.get("agent_config") for s in persisted["steps"] if s.get("agent_config")]
    assert refs == [role_key]
    # persisted roles.json is single-prefixed too
    roles_json = json.loads((gdir / f"{config_name}.roles.json").read_text())
    assert set(roles_json) == {role_key}


def test_load_generated_configs_on_boot(sf, registry, gdir):
    gdir.mkdir(parents=True, exist_ok=True)
    data = yaml.safe_load(GEN_YAML)
    data["name"] = "gen_boot_demo"
    (gdir / "gen_boot_demo.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    names = pr.load_generated_configs(sf, registry)
    assert "gen_boot_demo" in names
    assert registry.get("gen_boot_demo") is not None


def test_no_output_yaml_returns_error(sf, registry, gdir, monkeypatch):
    _patch_output(monkeypatch, None)
    res = pr.register_generated_pipeline(sf, registry, "x", "whatever")
    assert "error" in res


def test_invalid_graph_returns_error_and_persists_nothing(sf, registry, gdir,
                                                          tmp_path, monkeypatch):
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: x\nsteps: []\n", encoding="utf-8")  # no begin/end_conditions
    _patch_output(monkeypatch, bad)
    res = pr.register_generated_pipeline(sf, registry, "x", "bad one")
    assert "error" in res
    assert not (gdir / "gen_bad_one.yaml").exists()


def test_wrapper_importable():
    from api.dependencies import register_pipeline_from_run
    assert callable(register_pipeline_from_run)


# ── repo_mode derivation ───────────────────────────────────────────────────
# A generated pipeline gets its workspace shape DERIVED from its graph, not
# declared by the emitting agent. Asymmetric on purpose: any repo signal ⇒
# "code" (an unused empty repo is harmless), only a total absence ⇒ "none"
# (guessing "none" wrongly is a hard runtime failure).

def _graph_with(step_over: dict, roles: dict | None = None):
    from skillflow.graph import PipelineGraph
    data = yaml.safe_load(GEN_YAML)
    data["name"] = "gen_probe"
    data["steps"][0].update(step_over)
    return PipelineGraph._from_dict(data), roles


def test_repo_mode_none_when_graph_never_touches_a_repo():
    graph, roles = _graph_with({})
    assert pr.derive_repo_mode(graph, roles) == "none"


@pytest.mark.parametrize("over", [
    {"step_type": "tool", "tool_name": "repo_apply", "agent_config": ""},
    {"validation": [{"tool": "pytest", "files": []}]},
    {"context": [{"from": "repository", "path": "src/"}]},
])
def test_repo_signals_force_code_mode(over):
    graph, roles = _graph_with(over)
    assert pr.derive_repo_mode(graph, roles) == "code"


def test_agent_reaching_the_repo_through_its_role_tools_forces_code_mode():
    # The graph node itself is innocent — the signal is in the role's tool list.
    graph, roles = _graph_with({}, {"processor": {"tools": ["read_file", "repo_apply"]}})
    assert pr.derive_repo_mode(graph, roles) == "code"


def test_role_tools_are_found_when_both_sides_are_namespaced():
    """What production actually passes: register_forge_pipeline namespaces the graph's
    agent_config AND the roles dict keys. Matching only the bare name loses the signal
    entirely — a pipeline whose agents get pytest/repo_apply via their role would be
    called repo-less and fail at runtime against a repo that was never created."""
    graph, _ = _graph_with({"agent_config": "gen_probe__processor"})
    roles = {"gen_probe__processor": {"tools": ["read_file", "repo_apply"]}}
    assert pr.derive_repo_mode(graph, roles) == "code"


def test_namespaced_graph_with_a_bare_role_table_still_matches():
    """A role_table.yaml read straight off disk has bare keys — both must work."""
    graph, _ = _graph_with({"agent_config": "gen_probe__processor"})
    assert pr.derive_repo_mode(graph, {"processor": {"tools": ["run_tests"]}}) == "code"


def test_read_tools_alone_are_not_a_repo_signal():
    # skillflow's code-path resolution is lazy: a read against a missing repo
    # finds nothing, it does not fail. Only git-touching tools hard-depend.
    graph, roles = _graph_with({}, {"processor": {"tools": ["read_file", "list_tree"]}})
    assert pr.derive_repo_mode(graph, roles) == "none"


def test_registered_generated_pipeline_carries_the_derived_repo_mode(sf, registry, gdir):
    gdir.mkdir(parents=True, exist_ok=True)
    data = yaml.safe_load(GEN_YAML)
    data["name"] = "gen_repoless"
    (gdir / "gen_repoless.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    pr.load_generated_configs(sf, registry)
    assert registry.get("gen_repoless").repo_mode == "none"


# ── Role-table normalization (S16) ────────────────────────────────────────

class TestNormalizeRoleTable:
    def test_top_level_table_is_untouched(self):
        table = {"maker": {"model": "host"}, "reviewer": {"model": "host"}}
        roles, note = pr.normalize_role_table(table)
        assert roles == table and note == ""

    def test_entries_wrapper_is_unwrapped(self):
        roles, note = pr.normalize_role_table({"entries": {"maker": {"model": "host"}}})
        assert roles == {"maker": {"model": "host"}}
        assert "entries" in note

    @pytest.mark.parametrize("key", ["roles", "role_table", "agents", "Entries"])
    def test_other_wrapper_spellings(self, key):
        roles, note = pr.normalize_role_table({key: {"maker": {"model": "host"}}})
        assert roles == {"maker": {"model": "host"}} and note

    def test_a_single_real_role_is_not_unwrapped(self):
        """A one-role table looks structurally identical to a wrapper — only an
        explicit wrapper name may be unwrapped, or a lone role would vanish."""
        table = {"maker": {"model": "host", "template": "t.md"}}
        roles, note = pr.normalize_role_table(table)
        assert roles == table and note == ""

    def test_empty_and_junk_are_safe(self):
        assert pr.normalize_role_table({}) == ({}, "")
        assert pr.normalize_role_table(None) == ({}, "")
        assert pr.normalize_role_table({"entries": "not a mapping"})[0] == {"entries": "not a mapping"}


# ── Output-step derivation (S8) ───────────────────────────────────────────

def _graph_from(steps, terminal="done_gate"):
    from skillflow.graph import PipelineGraph
    return PipelineGraph._from_dict({
        "name": "gen_probe", "begin": steps[0]["id"],
        "end_conditions": {"combinator": "or", "conditions": [
            {"type": "node_reached", "node": terminal, "result": "completed"}]},
        "steps": steps})


class TestDeriveOutputStep:
    def test_picks_the_step_feeding_the_success_terminal(self):
        g = _graph_from([
            {"id": "make", "step_type": "agent", "agent_config": "m",
             "output": {"mode": "content", "fixed": {"r": {"file": "answer.md"}}},
             "transitions": [{"to": "done_gate"}]},
            {"id": "done_gate", "step_type": "gate", "transitions": [{"to": None}]},
        ])
        assert pr.derive_output_step(g) == "make"

    def test_ignores_a_give_up_branch(self):
        """The failed path also ends the run — it must not be mistaken for the result."""
        g = _graph_from([
            {"id": "verify", "step_type": "agent", "agent_config": "v",
             "output": {"mode": "content", "fixed": {"v": {"file": "review_verdict.json"}}},
             "transitions": [{"to": "success_answer", "match": {"passed": True}},
                             {"to": "give_up"}]},
            {"id": "success_answer", "step_type": "agent", "agent_config": "a",
             "output": {"mode": "content", "fixed": {"r": {"file": "final_answer.md"}}},
             "transitions": [{"to": "done_gate"}]},
            {"id": "give_up", "step_type": "agent", "agent_config": "a",
             "output": {"mode": "content", "fixed": {"r": {"file": "final_answer.md"}}},
             "transitions": [{"to": None}]},
            {"id": "done_gate", "step_type": "gate", "transitions": [{"to": None}]},
        ])
        assert pr.derive_output_step(g) == "success_answer"

    def test_prefers_a_real_output_over_a_verdict(self):
        g = _graph_from([
            {"id": "review", "step_type": "agent", "agent_config": "r",
             "output": {"mode": "content", "fixed": {"v": {"file": "review_verdict.json"}}},
             "transitions": [{"to": "done_gate"}]},
            {"id": "write_up", "step_type": "agent", "agent_config": "w",
             "output": {"mode": "content", "fixed": {"r": {"file": "report.md"}}},
             "transitions": [{"to": "done_gate"}]},
            {"id": "done_gate", "step_type": "gate", "transitions": [{"to": None}]},
        ])
        assert pr.derive_output_step(g) == "write_up"

    def test_falls_back_to_the_last_non_gate_step(self):
        g = _graph_from([{"id": "only", "step_type": "agent", "agent_config": "a",
                          "transitions": [{"to": None}]}], terminal="nonexistent")
        assert pr.derive_output_step(g) == "only"


def test_generated_manifest_carries_a_derived_output_step_and_label(sf, registry):
    yml = textwrap.dedent("""
        name: placeholder
        begin: make
        end_conditions:
          combinator: or
          conditions:
            - {type: node_reached, node: done_gate, result: completed}
        steps:
          - id: make
            step_type: agent
            agent_config: maker
            output: {mode: content, fixed: {r: {file: answer.md}}}
            transitions: [{to: done_gate}]
          - id: done_gate
            step_type: gate
            transitions: [{to: null}]
    """)
    pr._register_text(sf, registry, "gen_answers", yml)
    m = registry.get("gen_answers")
    assert m.output_step == "make"          # not the gate, not None
    assert m.label == "Answers"


# ── Declared outputs: BOTH legal `fixed:` forms (review follow-up) ────────

class TestDeclaredOutputFiles:
    @pytest.mark.parametrize("fixed,expected", [
        ({"answer": "final_answer.md"}, ["final_answer.md"]),                 # shorthand
        ({"answer": {"file": "final_answer.md"}}, ["final_answer.md"]),       # long form
        ({"a": "one.md", "b": {"file": "two.md"}}, ["one.md", "two.md"]),     # mixed
    ])
    def test_both_forms_are_read(self, fixed, expected):
        files, knowable = pr.declared_output_files({"output": {"mode": "content",
                                                              "fixed": fixed}})
        assert knowable and sorted(files) == sorted(expected)

    def test_write_mode_is_not_knowable(self):
        assert pr.declared_output_files({"output": {"mode": "write"}}) == ([], False)

    def test_no_output_block_is_not_knowable(self):
        assert pr.declared_output_files({"id": "x"}) == ([], False)

    def test_reads_a_parsed_step_node(self):
        from skillflow.graph import PipelineGraph
        g = PipelineGraph._from_dict({
            "name": "g", "begin": "a",
            "end_conditions": {"combinator": "or", "conditions": [
                {"type": "node_reached", "node": "a", "result": "completed"}]},
            "steps": [{"id": "a", "step_type": "agent", "agent_config": "m",
                       "output": {"mode": "content", "fixed": {"r": "out.md"}},
                       "transitions": [{"to": None}]}]})
        files, knowable = pr.declared_output_files(g.steps[0])
        assert knowable and files == ["out.md"]


def test_output_step_prefers_a_shorthand_writer_over_a_reviewer():
    """The shorthand `fixed: {answer: "report.md"}` is legal and used in this repo's
    own configs; reading only the long form points output_step at the verdict."""
    g = _graph_from([
        {"id": "review", "step_type": "agent", "agent_config": "r",
         "output": {"mode": "content", "fixed": {"v": {"file": "review_verdict.json"}}},
         "transitions": [{"to": "done_gate"}]},
        {"id": "write_up", "step_type": "agent", "agent_config": "w",
         "output": {"mode": "content", "fixed": {"answer": "report.md"}},
         "transitions": [{"to": "done_gate"}]},
        {"id": "done_gate", "step_type": "gate", "transitions": [{"to": None}]},
    ])
    assert pr.derive_output_step(g) == "write_up"


def test_a_validation_block_written_as_a_mapping_does_not_crash_derivation():
    """A generated graph is written by a MODEL, and one emitted `validation:` as a
    single mapping instead of a list of them.

    Iterating a dict yields its KEYS — bare strings — so `spec.get("tool")` raised
    `'str' object has no attribute 'get'`. Live, forge run 74833837: the graph was
    already registered by the time that raised, so the host ended up with a
    runnable config, no files on disk, and no roles.json — every step silently on
    the generic host prompt instead of its real one.

    The mapping is read as ONE spec, not skipped: derive_repo_mode is deliberately
    asymmetric (a wrong "none" is a hard runtime failure), so a lost repo signal is
    the expensive direction.
    """
    class _Step:
        id = "s"
        tool_name = ""
        agent_config = ""
        validation = {"tool": "repo_apply", "files": ["x.py"]}   # mapping, not list
        context = []

    class _Graph:
        steps = [_Step()]

    assert pr.derive_repo_mode(_Graph()) == "code"


def test_derivation_survives_junk_entries_without_losing_a_real_signal():
    class _Step:
        id = "s"
        tool_name = ""
        agent_config = ""
        validation = ["garbage", None, {"tool": "repo_apply"}]
        context = ["nonsense"]

    class _Graph:
        steps = [_Step()]

    assert pr.derive_repo_mode(_Graph()) == "code"


def test_registration_writes_nothing_when_hint_derivation_fails(sf, registry,
                                                                monkeypatch, tmp_path):
    """The order used to be roles → graph → register → hints. A hint failure then
    left the graph LIVE with no files ever written — a config that runs and has
    lost every real prompt. Deriving before mutating makes that failure clean."""
    monkeypatch.setenv("AITELIER_GENERATED_CONFIGS_DIR", str(tmp_path))
    monkeypatch.setattr(pr, "_gen_hints",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    import pytest as _pytest
    with _pytest.raises(RuntimeError, match="boom"):
        pr._register_text(sf, registry, "gen_probe", GEN_YAML, {})
    assert not any(tmp_path.iterdir()), "a failed registration left files behind"
    assert not any(g["name"] == "gen_probe" for g in sf.list_graphs()), \
        "a failed registration left the graph live"


def test_the_vision_judge_is_not_swept_into_a_provider_migration():
    """The `vision` route must only name endpoints that accept IMAGE input.

    This is the one capability a text smoke test cannot see. Verified against
    the Ark coding plan 2026-08-25: `deepseek-v4-flash-vision-exp` answers TEXT
    there (HTTP 200) but refuses an image — `Model do not support image input`.
    So a global `deepseek/... -> ark/...` sweep that also "tidied up" this route
    would leave the readability gate with a judge that 400s on every frame,
    while every text-only check stayed green.

    The judge used to be three env vars inside the tool and this guard read
    them; it now reads the route, because that is where the choice lives.
    Candidates verified to accept a real 960x704 play-test frame:
    `qwen/qwen3.8-max` and `deepseek/deepseek-v4-flash-vision-exp` (2026-08-26,
    both answered the same health-bar question correctly). Adding a candidate
    means POSTing it a real frame first — not reading a model card.
    """
    import json

    root = Path(__file__).resolve().parents[2]
    from core.model_routes import config_or_example
    route = json.loads(
        Path(config_or_example("model_routes.json")).read_text(encoding="utf-8"))["vision"]
    providers = {c.split("/", 1)[0] for c in route}

    assert not any(c.startswith("ark/") for c in route), (
        "an ark/ endpoint was added to the vision route — Ark's plan serves "
        "deepseek-v4-flash-vision-exp for TEXT and refuses image input, so this "
        "would 400 on every frame while text checks stayed green")
    assert any(c.startswith("deepseek/") for c in route), (
        "the vision route lost its DeepSeek judge — it is the pay-as-you-go one "
        "that cannot run out of plan quota, which is why it is last")

    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    from core.model_routes import ModelRoutes  # noqa: F401  (import guard)
    llm = json.loads(
        Path(config_or_example("llm_providers.json")).read_text(encoding="utf-8"))
    for prov in providers:
        assert prov in llm, f"vision route names unknown provider '{prov}'"
        key = llm[prov].get("api_key_env")
        if key:
            assert key in compose, (
                f"the vision judge '{prov}' needs {key}, which is not mounted "
                f"in docker-compose.yml's secrets — the gate would go blind")
