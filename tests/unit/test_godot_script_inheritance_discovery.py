"""CPU SOURCE checks of actual /script discovery; never start Godot."""
import importlib.util
import os
from pathlib import Path

import pytest


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setenv("GODOT_LIFECYCLE_DB", str(tmp_path / "owners.sqlite3"))
    source = Path(os.environ.get("HARNESS_SOURCE", Path(__file__).resolve().parents[2]
                                 / "docker/godot/godot_harness.py"))
    spec = importlib.util.spec_from_file_location("gh_inheritance_discovery", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def project(tmp_path, files):
    (tmp_path / "project.godot").write_text('[application]\nconfig/name="source-check"\n')
    for name, source in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    return tmp_path


@pytest.mark.parametrize("source", [
    "extends SceneTree\n",
    "extends\tSceneTree # true base\n",
    "class_name TestLoop extends SceneTree\n",
    "@tool\nclass_name TestLoop\nextends SceneTree\n",
    "@tool class_name TestLoop extends SceneTree\n",
    "# extends RefCounted\n" * 300 + "extends SceneTree\n",
    'const NOTE = """\nextends RefCounted\n"""\nextends SceneTree\n',
    "extends MainLoop\n",
])
def test_positive_actual_mainloop_declaration(harness, tmp_path, source):
    root = project(tmp_path, {"tests/entry.gd": source})
    assert harness._discover_entry_points(root) == ["res://tests/entry.gd"]
    assert harness._validate_script_selection(str(root), ["res://tests/entry.gd"], True) == []


@pytest.mark.parametrize("source", [
    "extends RefCounted\n## NO extends SceneTree; owned by aggregate run().\n",
    'extends Node\nconst NOTE = "extends SceneTree"\n',
    'extends Control\nconst NOTE = """\nextends SceneTree\n"""\n',
    "extends Resource\nconst NOTE = '''\nextends SceneTree\n'''\n",
    "## extends SceneTree\nstatic func run(): return true\n",
    "extends RefCounted\nclass Decoy extends SceneTree:\n\tpass\n",
])
def test_inverse_comments_strings_inner_classes_are_not_file_bases(harness, tmp_path, source):
    root = project(tmp_path, {"tests/helper.gd": source, "tests/runner.gd": "extends SceneTree\n"})
    assert harness._discover_entry_points(root) == ["res://tests/runner.gd"]
    assert harness._validate_script_selection(str(root), ["res://tests/helper.gd"], True)


@pytest.mark.parametrize("base", ['"res://support/base.gd"', '"../support/base.gd"', "BaseLoop"])
def test_positive_file_and_global_ancestry(harness, tmp_path, base):
    root = project(tmp_path, {"tests/entry.gd": "extends " + base + "\n",
                              "support/base.gd": "class_name BaseLoop\nextends MainLoop\n"})
    assert harness._discover_entry_points(root) == ["res://tests/entry.gd"]


@pytest.mark.parametrize("base", ['"res://support/base.gd".Inner', "BaseLoop.Inner"])
def test_positive_named_inner_ancestry(harness, tmp_path, base):
    root = project(tmp_path, {"tests/entry.gd": "extends " + base + "\n",
                              "support/base.gd": "class_name BaseLoop\nextends RefCounted\n"
                              "class Inner extends SceneTree:\n\tpass\n"})
    assert harness._discover_entry_points(root) == ["res://tests/entry.gd"]


def test_positive_nested_inner_and_local_base(harness, tmp_path):
    root = project(tmp_path, {"tests/entry.gd": 'extends "res://support/base.gd".Inner.Deep\n',
                              "support/base.gd": "extends RefCounted\n"
                              "class Local extends SceneTree:\n\tpass\n"
                              "class Inner:\n\tclass Deep:\n\t\textends Local\n"})
    assert harness._discover_entry_points(root) == ["res://tests/entry.gd"]


@pytest.mark.parametrize("source,other", [
    ("extends MissingBase\n", {}),
    ('extends "missing.gd"\n', {}),
    ("extends\n", {}),
    ("class_name Bad extends\n", {}),
    ("extends SceneTree\nextends Node\n", {}),
    ('extends "loop.gd"\n', {"tests/loop.gd": 'extends "entry.gd"\n'}),
    ("extends Duplicate\n", {"support/a.gd": "class_name Duplicate extends SceneTree\n",
                              "support/b.gd": "class_name Duplicate extends SceneTree\n"}),
    ('extends "res://support/base.gd".Absent\n', {"support/base.gd": "extends Node\n"}),
])
def test_inverse_unknown_malformed_ambiguous_cycles_refused_before_admission(harness, tmp_path, source, other):
    root = project(tmp_path, {"tests/entry.gd": source, **other})
    errors = harness._validate_script_selection(str(root), [], True)
    assert errors and "discovery failed" in errors[0]


def test_roster_helper_successor_keeps_aggregate_run(harness, tmp_path):
    root = project(tmp_path, {
        "tests/roster.gd": "extends RefCounted\n## NO extends SceneTree\n"
                            "static func run() -> bool: return false\n",
        "tests/unit_test_runner.gd": "extends SceneTree\n"
            'const TESTS = ["res://tests/roster.gd"]\n',
    })
    assert harness._discover_entry_points(root) == ["res://tests/unit_test_runner.gd"]
    assert '"res://tests/roster.gd"' in (root / "tests/unit_test_runner.gd").read_text()
    assert "return false" in (root / "tests/roster.gd").read_text()
