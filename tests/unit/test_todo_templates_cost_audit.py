"""The plugin's generic `cost_audit` template works for any project, out of the box.

* It ships in the plugin's DEFAULT template set (integrations/dsh-director/todo/todo_templates), so
  the package alone, with no host hooks and no template setting, lists it and drives it.
* It binds only predicates the library already has, and expands for a project in any language: the
  measuring commands are data (a node or go command, or none at all), never a hard-coded runner.
* The repository's own template set (todo_templates/) carries the same file through a symlink, so
  there is one source of truth.
* director.md, which every install ships, names the template, and the template exists.
"""
import hashlib
import json
import os
from pathlib import Path

import pytest

from core.todo_plan import TodoPlan
from core.todo_runtime import builtin_library
from core.todo_templates import load_template, plan_data, resolve_params
from tests.unit.test_dsh_todo_package import _GIT_ENV, REPO_ROOT, TODO_ROOT, git, standalone

PLUGIN_TEMPLATES = TODO_ROOT / "todo_templates"
KEYS = ["measured_commands", "baseline", "costliest", "findings", "recommendations"]
REPORT = {
    "measured_commands": [{"command": "npm test", "seconds": 12.5, "exit": 0}],
    "baseline": {"total_seconds": 12.5, "previous_total_seconds": None},
    "costliest": [{"item": "test/render.test.js", "seconds": 9.1}],
    "findings": [{"category": "performance", "where": "test/render.test.js", "cost_seconds": 8.0,
                  "evidence": "rebuilds the fixture world in every test", "proposed_change": "build it once per file",
                  "still_guarded_by": "the same assertions run against the shared fixture", "risk": "low"}],
    "recommendations": ["share the fixture world"],
}


def expand(params: dict) -> dict:
    """The expanded `audit` item: its title and {predicate: args}, as the frozen plan holds them."""
    library = builtin_library()
    template = load_template("cost_audit", library, PLUGIN_TEMPLATES)
    plan = TodoPlan.from_json(plan_data(template, resolve_params(template, params), attempt_id="a", node_key="n",
                                        context_hash="c" * 64, base_sha="0" * 40, workspace="/w", goal="audit"),
                              library)
    audit = next(i for i in plan.items if i.id == "audit")
    return {"title": audit.title,
            "predicates": {b.predicate: json.loads(json.dumps(dict(b.args))) for b in audit.predicates}}


def test_it_loads_and_binds_only_predicates_the_library_has():
    library = builtin_library()
    template = load_template("cost_audit", library, PLUGIN_TEMPLATES)
    used = {b["predicate"] for item in template.items for b in item["predicates"]}
    assert used == {"files_changed", "report_json"} and used <= set(library.names())
    assert template.version == 1 and [i["id"] for i in template.items] == ["audit"]
    assert not template.params["measure_commands"].required, "a project with no test command can still be audited"


@pytest.mark.parametrize("commands", [["npm test"], ["go test ./...", "go vet ./..."], None],
                         ids=["node", "go", "no-test-command"])
def test_it_expands_for_any_project_with_its_commands_as_data(commands):
    params = {"report_path": "audit/cost.json", "scope": ["audit/"], "previous_report": "audit/old.json"}
    if commands is not None:
        params["measure_commands"] = commands
    audit = expand(params)
    assert audit["predicates"] == {
        "files_changed": {"paths": ["audit/cost.json"]},
        "report_json": {"path": "audit/cost.json", "require_keys": KEYS}}
    assert ", ".join(commands or []) in audit["title"] and "audit/old.json" in audit["title"]


def test_its_defaults_alone_make_a_plan():
    predicates = expand({})["predicates"]
    assert predicates["files_changed"] == {"paths": ["cost-audit.json"]}
    assert predicates["report_json"] == {"path": "cost-audit.json", "require_keys": KEYS}


def test_the_package_alone_lists_it_and_drives_it_on_a_node_project(tmp_path):
    repo, store, out = tmp_path / "ws", tmp_path / "store", tmp_path / "out"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "package.json").write_text('{"name": "demo", "scripts": {"test": "node --test"}}\n')
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    # No hooks, no template setting: the package's own default template directory.
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), **_GIT_ENV}

    def ok(*args: str) -> dict:
        code, result = standalone(*args, cwd=tmp_path, env=env)
        assert code == 0, result
        return result

    listed = ok("templates")
    assert Path(listed["directory"]) == PLUGIN_TEMPLATES
    names = {t["name"]: t for t in listed["templates"]}
    assert {"change_files", "cost_audit"} <= set(names) and "error" not in names["cost_audit"]

    created = ok("create", "--store", str(store), "--template", "cost_audit",
                 "--params", json.dumps({"measure_commands": ["npm test"]}),
                 "--attempt", "att-1", "--node-key", "audit.cost", "--context-hash", "c" * 64,
                 "--base-sha", base, "--workspace", str(repo), "--goal", "audit what the checks cost")
    assert created["items"] == ["audit", "deliver"], created

    ok("begin", "--store", str(store), "--attempt", "att-1", "--item", "audit")
    assert ok("done", "--store", str(store), "--attempt", "att-1", "--item", "audit")["outcome"] == "fail"
    (repo / "cost-audit.json").write_text(json.dumps({k: v for k, v in REPORT.items() if k != "findings"}))
    lacking = ok("done", "--store", str(store), "--attempt", "att-1", "--item", "audit")
    assert lacking["outcome"] == "fail" and "findings" in json.dumps(lacking["results"])
    (repo / "cost-audit.json").write_text(json.dumps(REPORT))
    assert ok("done", "--store", str(store), "--attempt", "att-1", "--item", "audit")["outcome"] == "pass"
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "cost audit")
    ok("begin", "--store", str(store), "--attempt", "att-1", "--item", "deliver")
    assert ok("done", "--store", str(store), "--attempt", "att-1", "--item", "deliver")["outcome"] == "pass"
    assert ok("finish", "--store", str(store), "--attempt", "att-1", "--out", str(out))["candidate"] is True


def test_the_repository_template_set_carries_the_same_file():
    shared = REPO_ROOT / "todo_templates" / "cost_audit.yaml"
    assert shared.is_symlink() and shared.resolve() == (PLUGIN_TEMPLATES / "cost_audit.yaml").resolve()
    template = load_template("cost_audit", builtin_library(), REPO_ROOT / "todo_templates")
    assert template.sha256 == hashlib.sha256((PLUGIN_TEMPLATES / "cost_audit.yaml").read_bytes()).hexdigest()


def test_the_shipped_director_prompt_names_it():
    director = (REPO_ROOT / "integrations" / "dsh-director" / "director.md").read_text()
    assert "cost_audit template" in director and (PLUGIN_TEMPLATES / "cost_audit.yaml").is_file()
