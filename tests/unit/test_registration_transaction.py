"""Fault injection for every host graph-publication entry point."""

from pathlib import Path
import json
import threading

import pytest
import skillflow
import yaml
from skillflow import SkillFlow
from skillflow.tool_loader import ToolLoader

from core import pipeline_registry as pr
from core.addon_registry import register_addon_combo
from core.config_registry import ConfigRegistry
from core.pipeline_bundle import (
    BUNDLE_KEY, BUNDLE_VERSION, BundleError, import_pipeline)


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "evidence/output-target-migration-20260911"
          / "generated-configs/gen_dpe_state_game.yaml")
NAME = "gen_dpe_state_game"


class _FailingRegistry(ConfigRegistry):
    def register_one(self, *_args, **_kwargs):
        raise RuntimeError("injected manifest failure")


def _runtime(root, monkeypatch):
    config_dir = root / "configs"
    monkeypatch.setenv("AITELIER_HOME", str(root / "home"))
    monkeypatch.setenv("AITELIER_GENERATED_CONFIGS_DIR", str(config_dir))
    loader = ToolLoader(
        Path(skillflow.__file__).parent / "tools", ROOT / "aitelier" / "tools")
    sf = SkillFlow(
        str(root / "skillflow.db"), tool_loader=loader,
        workspace_base=str(root / "workspace"),
        projects_base=str(root / "projects"))
    sf.register_capability(
        "game_assets", tools=["gen_image_asset", "gen_audio_asset"])
    return sf, config_dir


def _assert_absent(sf, name):
    assert name not in sf._graphs
    assert name not in sf._resolvers
    for table in ("skillflow_graphs", "skillflow_graph_versions"):
        assert sf._conn.execute(
            f"SELECT count(*) FROM {table} WHERE name=?", (name,)
        ).fetchone()[0] == 0


def _write_forge_verifier_role(emit):
    (emit / "templates").mkdir(exist_ok=True)
    (emit / "templates" / "final_verifier.md").write_text(
        "Final verifier writes reports only.")
    (emit / "role_table.yaml").write_text(yaml.safe_dump({
        "final_verifier": {
            "tools": ["list_tree"],
            "template": "templates/final_verifier.md",
        },
    }))


def _write_boot_verifier_role(config_dir):
    document = yaml.safe_load(SOURCE.read_text())
    role = next(step["agent_config"] for step in document["steps"]
                if step["id"] == "5")
    path = config_dir / f"{NAME}.roles.json"
    path.write_text(json.dumps({
        role: {"tools": ["list_tree"],
               "system_prompt": "Final verifier writes reports only."},
    }))
    return path, path.read_bytes()


def test_direct_registration_commits_the_roles_it_preflighted(tmp_path,
                                                               monkeypatch):
    sf, _ = _runtime(tmp_path, monkeypatch)
    document = yaml.safe_load(SOURCE.read_text())
    role = next(step["agent_config"] for step in document["steps"]
                if step["id"] == "5_design")
    sf.register_agent_config_from_dict(
        role, {"model": "host", "tools": ["run_tests"],
               "system_prompt": "unsafe live"})

    pr._register_text(
        sf, ConfigRegistry(), NAME,
        yaml.safe_dump(document, sort_keys=False),
        roles={role: {"model": "host", "tools": ["read_file"],
                      "system_prompt": "safe supplied"}})

    bound = sf.agent_registry.get(role)
    assert bound.tools == ["read_file"]
    assert set(bound.tool_schemas) == {"read_file"}


def test_snapshot_entry_failure_releases_lock_without_mutation(
        tmp_path, monkeypatch):
    from core import registration_transaction as transaction

    sf, config_dir = _runtime(tmp_path, monkeypatch)
    source = tmp_path / "source.yaml"
    source.write_bytes(SOURCE.read_bytes())
    monkeypatch.setattr(
        "skillflow.plugins.skill_converter.get_output_file",
        lambda _sf, _run: str(source))
    monkeypatch.setattr(
        transaction, "_capture",
        lambda _path: (_ for _ in ()).throw(OSError("snapshot failed")))

    result = pr.register_generated_pipeline(
        sf, ConfigRegistry(), "run", "dpe state game")
    acquired = []

    def contender():
        got = sf._lock.acquire(timeout=0.25)
        acquired.append(got)
        if got:
            sf._lock.release()

    thread = threading.Thread(target=contender)
    thread.start()
    thread.join(1)
    assert "snapshot failed" in result["error"]
    assert acquired == [True]
    _assert_absent(sf, NAME)
    assert not (config_dir / f"{NAME}.yaml").exists()


def test_manifest_failure_rolls_back_every_publication_surface(tmp_path,
                                                               monkeypatch):
    sf, _ = _runtime(tmp_path / "direct", monkeypatch)
    with pytest.raises(RuntimeError, match="injected manifest failure"):
        pr._register_text(
            sf, _FailingRegistry(), NAME, SOURCE.read_text(), roles={})
    _assert_absent(sf, NAME)

    sf, config_dir = _runtime(tmp_path / "generated", monkeypatch)
    generated_source = tmp_path / "generated-source.yaml"
    generated_source.write_bytes(SOURCE.read_bytes())
    monkeypatch.setattr(
        "skillflow.plugins.skill_converter.get_output_file",
        lambda _sf, _run: str(generated_source))
    result = pr.register_generated_pipeline(
        sf, _FailingRegistry(), "run", "dpe state game")
    assert "injected manifest failure" in result["error"]
    _assert_absent(sf, NAME)
    assert not (config_dir / f"{NAME}.yaml").exists()

    sf, config_dir = _runtime(tmp_path / "forge", monkeypatch)
    sf.get_run = lambda _run: {"project_id": "forge-project"}
    emit = sf._workspace.get_step_dir(
        "forge-project", "pipeline_forge", "emit_graph")
    emit.mkdir(parents=True)
    (emit / "pipeline.yaml").write_bytes(SOURCE.read_bytes())
    _write_forge_verifier_role(emit)
    result = pr.register_forge_pipeline(
        sf, _FailingRegistry(), "run", "dpe state game")
    assert "injected manifest failure" in result["error"]
    _assert_absent(sf, NAME)
    assert not (config_dir / f"{NAME}.yaml").exists()

    sf, config_dir = _runtime(tmp_path / "boot", monkeypatch)
    config_dir.mkdir(parents=True)
    boot_source = config_dir / f"{NAME}.yaml"
    boot_source.write_bytes(SOURCE.read_bytes())
    boot_roles, original_boot_roles = _write_boot_verifier_role(config_dir)
    assert pr.load_generated_configs(sf, _FailingRegistry()) == []
    _assert_absent(sf, NAME)
    assert boot_source.read_bytes() == SOURCE.read_bytes()
    assert boot_roles.read_bytes() == original_boot_roles

    sf, config_dir = _runtime(tmp_path / "bundle", monkeypatch)
    document = yaml.safe_load(SOURCE.read_text())
    role = next(step["agent_config"] for step in document["steps"]
                if step["id"] == "5_design")
    bundle = {
        BUNDLE_KEY: BUNDLE_VERSION,
        "config_name": NAME,
        "graph_yaml": yaml.safe_dump(document, sort_keys=False),
        "roles": {role: {"model": "host", "tools": ["read_file"],
                         "system_prompt": "safe"}},
        "tools": {},
    }
    with pytest.raises(RuntimeError, match="injected manifest failure"):
        import_pipeline(sf, _FailingRegistry(), bundle)
    _assert_absent(sf, NAME)
    assert not (config_dir / f"{NAME}.yaml").exists()
    assert not (config_dir / f"{NAME}.roles.json").exists()

    sf, _ = _runtime(tmp_path / "addon", monkeypatch)
    pr._register_text(
        sf, ConfigRegistry(), NAME, SOURCE.read_text(), roles={})
    sf.register_overlay("safe_addon", {
        "name": "safe_addon", "base": NAME,
        "overlay": [{"add_tools": "5_design", "tools": ["read_file"]}],
    })
    target = f"{NAME}_safe_addon"
    with pytest.raises(RuntimeError, match="injected manifest failure"):
        register_addon_combo(
            sf, _FailingRegistry(), NAME, ["safe_addon"], name=target)
    _assert_absent(sf, target)


@pytest.mark.parametrize("surface", ["generated", "forge", "bundle"])
def test_persistence_failure_restores_files_and_live_state(
        tmp_path, monkeypatch, surface):
    sf, config_dir = _runtime(tmp_path / surface, monkeypatch)
    generated_source = tmp_path / f"{surface}-source.yaml"
    generated_source.write_bytes(SOURCE.read_bytes())
    monkeypatch.setattr(
        "skillflow.plugins.skill_converter.get_output_file",
        lambda _sf, _run: str(generated_source))

    def fail_after_config_write(_name):
        assert (config_dir / f"{NAME}.yaml").exists()
        raise OSError("injected persistence failure")

    monkeypatch.setattr(pr, "_unarchive", fail_after_config_write)
    if surface == "generated":
        result = pr.register_generated_pipeline(
            sf, ConfigRegistry(), "run", "dpe state game")
        assert "injected persistence failure" in result["error"]
    elif surface == "forge":
        sf.get_run = lambda _run: {"project_id": "forge-project"}
        emit = sf._workspace.get_step_dir(
            "forge-project", "pipeline_forge", "emit_graph")
        emit.mkdir(parents=True)
        (emit / "pipeline.yaml").write_bytes(SOURCE.read_bytes())
        _write_forge_verifier_role(emit)
        result = pr.register_forge_pipeline(
            sf, ConfigRegistry(), "run", "dpe state game")
        assert "injected persistence failure" in result["error"]
    else:
        document = yaml.safe_load(SOURCE.read_text())
        role = next(step["agent_config"] for step in document["steps"]
                    if step["id"] == "5_design")
        bundle = {
            BUNDLE_KEY: BUNDLE_VERSION, "config_name": NAME,
            "graph_yaml": yaml.safe_dump(document, sort_keys=False),
            "roles": {role: {"model": "host", "tools": ["read_file"],
                             "system_prompt": "safe"}},
            "tools": {},
        }
        with pytest.raises(BundleError, match="injected persistence failure"):
            import_pipeline(sf, ConfigRegistry(), bundle)
    _assert_absent(sf, NAME)
    assert not (config_dir / f"{NAME}.yaml").exists()
    assert not (config_dir / f"{NAME}.roles.json").exists()
