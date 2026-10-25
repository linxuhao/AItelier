"""Caller budgets through resolved host callbacks and the native executor payload.

UNRUN by the read-only Source author. Run only in Root's disposable CPU capsule.
The Docker/socket boundary is substituted; entry cases execute real tiny pytest
suites. These are Source/adapter checks, not normal service or game measurements.
"""
from functools import wraps
import importlib.metadata
import json
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest
import skillflow
from skillflow import PipelineGraph
import yaml

from core import addon_registry, pipeline_registry
from core.config_registry import ConfigRegistry
from core import repository_executor, repository_executor_entry as entry
from core import repository_host_launcher as launcher
from core.skillflow_host import AItelierSkillFlow
from core.tool_routes import AItelierToolLoader
from tests.unit.test_repository_host_launcher import _fixture as launcher_fixture

ROOT = Path(__file__).resolve().parents[2]
STAGES = ("5_test", "5_final_test")


def _rig(tmp_path, monkeypatch, outcome="pass"):
    # This test must not silently exercise a stale editable/local SDK.
    assert importlib.metadata.version("skillflow-py") == "1.5.99"
    cfg = launcher_fixture(tmp_path)
    cfg["executor_entry"] = str(Path(entry.__file__).resolve())
    repo = tmp_path / "projects" / "repo"
    package = repo / "src" / "budget_probe"
    package.mkdir(parents=True)
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "budget-probe"\nversion = "0.0.0"\n')
    (package / "__init__.py").write_text(
        "raise RuntimeError('BROKEN_BUDGET_IMPORT')\n"
        if outcome == "import" else "VALUE = 5\n")
    (repo / "test_probe.py").write_text(
        "from budget_probe import VALUE\n"
        "def test_value():\n"
        + ("    assert VALUE == 6\n" if outcome == "fail"
           else "    assert VALUE == 5\n"))
    monkeypatch.setenv("AITELIER_HOME", cfg["aitelier_home"])
    monkeypatch.setenv("AITELIER_GENERATED_CONFIGS_DIR", str(tmp_path / "configs"))
    actual_which = launcher.shutil.which
    monkeypatch.setattr(launcher.shutil, "which",
                        lambda name: "/controlled/docker" if name == "docker"
                        else actual_which(name))

    loader = AItelierToolLoader(Path(skillflow.__file__).parent / "tools")
    loader.add_tools_dir(ROOT / "aitelier" / "tools")
    sf = AItelierSkillFlow(
        str(tmp_path / "sf.db"), tool_loader=loader,
        workspace_base=str(tmp_path / "workspace"),
        projects_base=str(tmp_path / "engine-projects"),
        code_path_resolver=lambda project_id, run_id=None: repo)
    sf.register_capability(
        "stateful",
        context_provider=lambda name: {
            "state_dir": str(sf._workspace.state_dir(name))})
    sf.register_capability("game_assets",
                           tools=["gen_image_asset", "gen_audio_asset"])
    calls, native, measured = [], [], []
    load_fn = loader.load_fn

    def resolved(name):
        fn = load_fn(name)
        if name != "run_tests":
            return fn

        @wraps(fn)
        def invoke(**kwargs):
            calls.append(dict(kwargs))
            if kwargs.get("evidence_cycle_from"):
                # Admit the final suite using current upstream-green fixtures.
                # Its own report is never fabricated or reused from 5_test.
                root = Path(kwargs["out_dir"]).parent
                root.mkdir(parents=True, exist_ok=True)
                identity = {"run_id": kwargs["run_id"],
                            "evidence_cycle_id": "budget-cycle",
                            "evidence_generated_at": "test"}
                (root / ".evidence_cycle.json").write_text(json.dumps(
                    {"version": 1, **identity}))
                for stage, filename in (
                        ("5_test", "test_report.json"),
                        ("5_compile", "compile_report.json"),
                        ("5_compile", "playtest_report.json"),
                        ("5_vision", "vision_report.json")):
                    report = root / stage / filename
                    report.parent.mkdir(parents=True, exist_ok=True)
                    report.write_text(json.dumps({"passed": True, **identity}))
            return fn(**kwargs)
        return invoke

    monkeypatch.setattr(loader, "load_fn", resolved)

    def execute(root, args, timeout, **kwargs):
        # A fixture-owned binding exercises the real host request validator.
        # No real launcher, Docker, socket, provider, or backend is contacted.
        with sqlite3.connect(cfg["state_db"]) as conn:
            conn.execute("INSERT INTO run_isolation VALUES(?,?,?,?,?)",
                         (kwargs["run_id"], "worktree", str(repo), str(repo), "p"))
        request = {
            "op": "launch", "run_id": kwargs["run_id"], "repo": str(root),
            "args": args, "timeout": timeout,
            "writable_dirs": [str(p) for p in kwargs["writable_dirs"]],
            "import_module": kwargs["import_module"],
            "pytest_timeout": kwargs["pytest_timeout"],
        }
        fields = launcher.validate_request(cfg, request)
        argv = launcher.docker_command(cfg, fields, "aitelier-cpu-budget-probe")
        payload = json.loads(argv[-1])
        native.append((request, argv, payload))
        if outcome == "unavailable":
            raise repository_executor.IsolationUnavailable(
                "controlled launcher refusal: no measurement")
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(
                args, timeout, output=b"BUDGET_PARTIAL\n", stderr=b"diagnostic\n")
        result = entry.run(payload)
        measured.append(result)
        if result.get("timed_out"):
            raise subprocess.TimeoutExpired(
                args, timeout, result["stdout"], result["stderr"])
        return SimpleNamespace(**result)

    monkeypatch.setattr(repository_executor, "execute", execute)
    return SimpleNamespace(sf=sf, repo=repo, calls=calls, native=native,
                           measured=measured)


def _registered(rig, variant):
    sf = rig.sf
    graph = PipelineGraph.from_yaml(ROOT / "configs" / "dpe_default.yaml")
    pipeline_registry.ensure_host_agents(sf, graph)
    sf.register_graph(graph)
    if variant == "base":
        return graph.name
    addon_registry.declare_addons(sf)
    game = addon_registry.register_addon_combo(
        sf, None, graph.name, ["game_harness"])
    if variant == "game":
        return game

    # Use the host's real generated namespacing, seed rewrite, persistence,
    # validation, registration and resolver rather than merely relabel a step.
    name = "gen_dpe_budget_probe"
    data = sf._graphs[game].to_dict()
    pipeline_registry._rewrite_self_config_refs(data, name)
    data["name"] = name
    pipeline_registry._namespace_agents(data, name)
    pipeline_registry._inject_seed_context(data, name)
    path = pipeline_registry.generated_configs_dir() / (name + ".yaml")
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    registry = ConfigRegistry()
    assert pipeline_registry.reload_generated_pipeline(
        sf, registry, name) == {"config_name": name}
    assert registry.get(name).seed_file == "seed_input.md"
    return name


def _dispatch(rig, name, stage, run_id=None):
    sf = rig.sf
    run_id = run_id or sf.create_run(name, project_id="p")
    node = sf._get_resolver_for_run(run_id).get_node(stage)
    result = sf._execute_tool_inline(node, run_id=run_id, graph_name=name)
    kwargs = rig.calls[-1]
    report = json.loads((Path(kwargs["out_dir"]) / "test_report.json").read_text())
    return node, result, kwargs, report


CASES = [("base", "5_test"), ("game", "5_test"), ("game", "5_final_test"),
         ("generated", "5_test"), ("generated", "5_final_test")]


@pytest.mark.parametrize("variant,stage", CASES)
def test_resolved_dpe_caller_reaches_bounded_native_payload_and_real_pytest(
        tmp_path, monkeypatch, variant, stage):
    rig = _rig(tmp_path, monkeypatch)
    name = _registered(rig, variant)
    node, result, kwargs, report = _dispatch(rig, name, stage)
    # Assert after dispatch so the baseline also reaches the actual callback.
    assert len(rig.calls) == len(rig.native) == len(rig.measured) == 1
    assert node.timeout_seconds == 4200, (kwargs, report, rig.native)
    assert kwargs["pytest_wall_seconds"] == 3600
    assert kwargs["repo_gate"] is False
    assert kwargs["project_root"] == str(rig.repo)
    assert kwargs["state_dir"] == str(rig.sf._workspace.state_dir(name))
    assert "$" not in kwargs["out_dir"]
    request, argv, payload = rig.native[0]
    assert request["timeout"] == payload["timeout"] == report["pytest_wall_seconds"] == 3600
    assert payload["repo"] == str(rig.repo)
    assert payload["import_module"] == "budget_probe"
    assert payload["pytest_timeout"] is True
    assert payload["args"][:4] == ["python3", "-m", "pytest", str(rig.repo)]
    for flag, value in (("--network", "none"), ("--cpus", "2"),
                        ("--memory", "2g"), ("--pids-limit", "512")):
        assert argv[argv.index(flag) + 1] == value
    assert "--rm" in argv and "--init" in argv
    assert "--privileged" not in argv
    assert rig.measured[0]["timed_out"] is False
    assert result["passed"] is True and report["returncode"] == 0
    assert "1 passed" in report["summary"]
    assert not report.get("measurement_not_taken")
    assert not report.get("skipped_because")


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("outcome", ["fail", "import", "timeout", "unavailable"])
def test_each_dpe_stage_keeps_failure_and_unmeasured_outcomes(
        tmp_path, monkeypatch, stage, outcome):
    rig = _rig(tmp_path, monkeypatch, outcome)
    name = _registered(rig, "game")
    _, result, _, report = _dispatch(rig, name, stage)
    assert len(rig.native) == 1, "final suite must execute, not short-circuit to green"
    assert rig.native[0][2]["timeout"] == report["pytest_wall_seconds"] == 3600
    assert result["passed"] is False and report["passed"] is False
    if outcome in {"timeout", "unavailable"}:
        assert rig.measured == []
        assert report["measurement_not_taken"] is True
        assert report["baseline_state"] == "unmeasured"
        assert result["passed_relative"] is False
        assert result["release_evidence"] == "unresolved"
        if outcome == "timeout":
            assert report["returncode"] is None and report["timed_out"] is True
            assert report["stdout"] == "BUDGET_PARTIAL\n"
            assert report["skipped_because"] == "pytest_timeout"
        else:
            assert report["infrastructure_unavailable"] is True
            assert report["skipped_because"] == "cpu_executor_unavailable"
    else:
        assert len(rig.measured) == 1 and report["returncode"] != 0
        if outcome == "import":
            assert "BROKEN_BUDGET_IMPORT" in report["import_error"]
        else:
            assert "1 failed" in report["summary"]


@pytest.mark.parametrize("stage", STAGES)
def test_saved_generated_budget_edit_requires_reload_and_preserves_old_pin(
        tmp_path, monkeypatch, stage):
    rig = _rig(tmp_path, monkeypatch, "unavailable")
    name = "gen_dpe_state_game"
    # The frozen repository fixture is copied; no foreign user config is read.
    source = (ROOT / "evidence/output-target-migration-20260911/"
              "generated-configs/gen_dpe_state_game.yaml")
    path = pipeline_registry.generated_configs_dir() / (name + ".yaml")
    path.write_bytes(source.read_bytes())
    registry = ConfigRegistry()
    assert pipeline_registry.reload_generated_pipeline(
        rig.sf, registry, name) == {"config_name": name}
    old_run = rig.sf.create_run(name, project_id="p")
    pin = rig.sf.get_run(old_run)["graph_version"]
    document = yaml.safe_load(path.read_text())
    for step in document["steps"]:
        if step["id"] in STAGES:
            step["timeout_seconds"] = 4200
            step["tool_params"]["pytest_wall_seconds"] = 3600
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    assert "pytest_wall_seconds" not in next(
        s for s in rig.sf._graphs[name].steps if s.id == stage).tool_params
    assert pipeline_registry.reload_generated_pipeline(
        rig.sf, registry, name) == {"config_name": name}
    new_run = rig.sf.create_run(name, project_id="p")
    old, _, _, old_report = _dispatch(rig, name, stage, old_run)
    new, _, _, new_report = _dispatch(rig, name, stage, new_run)
    assert rig.sf.get_run(old_run)["graph_version"] == pin
    assert rig.sf.get_run(new_run)["graph_version"] != pin
    assert old.timeout_seconds == 1200 and new.timeout_seconds == 4200
    assert "pytest_wall_seconds" not in old.tool_params
    assert new.tool_params["pytest_wall_seconds"] == 3600
    assert old_report["pytest_wall_seconds"] == 1800
    assert new_report["pytest_wall_seconds"] == 3600
    assert [row[2]["timeout"] for row in rig.native] == [1800, 3600]
