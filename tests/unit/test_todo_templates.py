"""Plans come from repository templates: expansion, typing, refusals, and the list predicate.

The CLI is driven exactly as the director's tool will drive it (`create --template`),
against real git repositories; template defects are provoked with templates written
into a temporary directory selected by AITELIER_TODO_TEMPLATES.
"""
import hashlib
import json
from pathlib import Path
import sys

import pytest

from core.todo_runtime import builtin_library
from core.todo_templates import load_template
# The CLI harness and fixtures are shared with the free-plan tests.
from tests.unit.test_todo_plan_cli import (  # noqa: F401
    Cli, T0, begin_done, base, cli, commit_all, deliver_results, git, repo)

TEMPLATES = Path(__file__).resolve().parents[2] / "todo_templates"
SHIPPED = {"code_change", "test_fix", "docs_change", "investigate"}
PASS = [sys.executable, "-c", "pass"]
CODE = {"paths": ["app.py", "other.py"], "scope": ["app.py", "other.py"], "test_argv": PASS, "marker": "changed"}


def create_from(cli: Cli, repo: Path, base: str, template: str = "code_change", params=CODE,
                attempt: str = "att-1", **kw):
    return cli.run("create", "--template", template, "--params", params if isinstance(params, str) else json.dumps(params),
                   "--attempt", attempt, "--node-key", "execution.demo", "--context-hash", "c" * 64,
                   "--base-sha", base, "--workspace", str(repo), "--goal", "make the change", **kw)


def document(cli: Cli, attempt: str = "att-1") -> dict:
    return json.loads((cli.store / f"{attempt}.json").read_text())


def write_template(directory: Path, name: str, body: str) -> None:
    directory.mkdir(exist_ok=True)
    (directory / f"{name}.yaml").write_text(body)


GOOD = """\
name: {name}
version: 1
description: a template under test
params:
  scope:
    type: list[str]
    required: true
  target:
    type: str
    required: true
items:
  - id: one
    title: "Change ${{target}}"
    predicates:
      - predicate: {predicate}
        args: {args}
"""


@pytest.fixture
def custom(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "tpl"
    directory.mkdir()
    monkeypatch.setenv("AITELIER_TODO_TEMPLATES", str(directory))
    return directory


# -- listing and loading -------------------------------------------------------------

def test_templates_lists_the_four_repository_templates_with_their_params(cli):
    out = cli.ok("templates", store=False)
    assert Path(out["directory"]) == TEMPLATES
    by_name = {t["name"]: t for t in out["templates"]}
    assert set(by_name) == SHIPPED and all("error" not in t for t in by_name.values())
    shape = {name: {p: (spec["type"], spec["required"], spec["default"]) for p, spec in t["params"].items()}
             for name, t in by_name.items()}
    assert shape["code_change"] == {
        "paths": ("list[str]", True, None), "scope": ("list[str]", True, None),
        "test_argv": ("list[str]", True, None), "marker": ("str", False, None), "test_timeout": ("int", False, 1800)}
    assert shape["test_fix"] == {"test_argv": ("list[str]", True, None), "scope": ("list[str]", True, None)}
    assert shape["docs_change"] == {"paths": ("list[str]", True, None), "scope": ("list[str]", True, None)}
    assert shape["investigate"] == {"report_path": ("str", True, None), "require_keys": ("list[str]", True, None),
                                    "scope": ("list[str]", True, None)}
    assert by_name["code_change"]["items"] == ["implement", "test"] and all(t["version"] == 1 for t in by_name.values())


@pytest.mark.parametrize("name", sorted(SHIPPED))
def test_each_shipped_template_loads_and_validates_against_the_library(name):
    template = load_template(name, builtin_library(), TEMPLATES)
    assert template.name == name and template.version == 1
    assert template.sha256 == hashlib.sha256((TEMPLATES / f"{name}.yaml").read_bytes()).hexdigest()


# -- expansion ----------------------------------------------------------------------------

def test_create_from_code_change_expands_freezes_and_records_where_it_came_from(cli, repo, base):
    code, out = create_from(cli, repo, base)
    assert code == 0 and out["items"] == ["implement", "test", "deliver"], out
    stored = document(cli)
    items = {i["id"]: i for i in stored["plan"]["items"]}
    assert items["implement"]["predicates"] == [
        {"predicate": "files_changed", "args": {"paths": ["app.py", "other.py"], "contains": "changed"}},
        {"predicate": "changes_within", "args": {"scope": ["app.py", "other.py"]}}]
    assert items["test"]["needs"] == ["implement"]
    assert items["test"]["predicates"] == [
        {"predicate": "command_exit", "args": {"argv": PASS, "expect": 0, "timeout": 1800}}]
    assert items["implement"]["title"] == "Change in app.py, other.py".replace("Change in", "Implement the change in")
    assert items["deliver"]["needs"] == ["implement", "test"]
    assert items["deliver"]["predicates"][0] == {"predicate": "git_delivered", "args": {"base": base}}
    assert stored["template"] == {
        "name": "code_change", "version": 1,
        "sha256": hashlib.sha256((TEMPLATES / "code_change.yaml").read_bytes()).hexdigest()}
    assert stored["params"] == {**CODE, "test_timeout": 1800}, "the resolved params, defaults included"
    assert out["template"] == stored["template"] and out["plan_hash"] == stored["plan_hash"]
    status = cli.ok("status", "--attempt", "att-1")
    assert status["timeout_budget_seconds"] == {"implement": 0, "test": 1800, "deliver": 0}
    assert status["finish_budget_seconds"] == 1800, "the template's test_timeout default is what the runtime must wait for"


def test_an_optional_param_nobody_set_leaves_its_arg_out(cli, repo, base):
    without = {k: v for k, v in CODE.items() if k != "marker"}
    assert create_from(cli, repo, base, params={**without, "test_timeout": 60})[0] == 0
    items = {i["id"]: i for i in document(cli)["plan"]["items"]}
    assert items["implement"]["predicates"][0]["args"] == {"paths": ["app.py", "other.py"]}
    assert items["test"]["predicates"][0]["args"]["timeout"] == 60


def test_two_creates_from_one_template_differ_only_in_the_bound_args(cli, repo, base):
    other = {"paths": ["docs/x.md"], "scope": ["docs/**"], "test_argv": [sys.executable, "-c", "print(1)"], "test_timeout": 5}
    assert create_from(cli, repo, base, attempt="att-1")[0] == 0
    assert create_from(cli, repo, base, params=other, attempt="att-2")[0] == 0
    a, b = document(cli, "att-1"), document(cli, "att-2")
    assert a["plan_hash"] != b["plan_hash"]
    shape = lambda d: [(i["id"], i["needs"], [p["predicate"] for p in i["predicates"]]) for i in d["plan"]["items"]]  # noqa: E731
    assert shape(a) == shape(b), "same item ids, needs and predicate names"
    assert {k: a["template"][k] for k in ("name", "version", "sha256")} == b["template"]
    assert a["plan"]["items"][0]["predicates"][0]["args"] != b["plan"]["items"][0]["predicates"][0]["args"]


def test_create_from_a_template_is_idempotent_and_refuses_a_different_result(cli, repo, base):
    first = create_from(cli, repo, base)[1]
    again = create_from(cli, repo, base)[1]
    assert again["created"] is False and again["plan_hash"] == first["plan_hash"]
    code, out = create_from(cli, repo, base, params={**CODE, "marker": "other marker"})
    assert code == 2 and out["refused"] == "plan_differs"


INVESTIGATE = {"report_path": "out/report.json", "require_keys": ["passed"], "scope": ["out/"]}


def test_investigate_needs_an_explicit_scope(cli, repo, base):
    code, out = create_from(cli, repo, base, "investigate", {k: v for k, v in INVESTIGATE.items() if k != "scope"})
    assert code == 2 and out["refused"] == "missing_param" and "scope" in out["reason"]


def test_investigate_wants_a_report_written_during_the_step(cli, repo, base, tmp_path):
    assert create_from(cli, repo, base, "investigate", INVESTIGATE)[0] == 0
    cli.ok("begin", "--attempt", "att-1", "--item", "investigate")
    assert cli.ok("done", "--attempt", "att-1", "--item", "investigate")["outcome"] == "fail", "no report yet"
    (repo / "out").mkdir()
    (repo / "out" / "report.json").write_text(json.dumps({"passed": True}))
    assert cli.ok("done", "--attempt", "att-1", "--item", "investigate")["outcome"] == "pass"
    commit_all(repo)
    assert deliver_results(cli)["_outcome"] == "pass"
    assert cli.ok("finish", "--attempt", "att-1", "--out", str(tmp_path / "out"))["candidate"] is True


def test_a_report_that_already_existed_at_the_base_does_not_satisfy_investigate(cli, repo):
    (repo / "out").mkdir()
    (repo / "out" / "report.json").write_text(json.dumps({"passed": True}))
    old_base = commit_all(repo, "a stale report")
    assert create_from(cli, repo, old_base, "investigate", INVESTIGATE)[0] == 0
    cli.ok("begin", "--attempt", "att-1", "--item", "investigate")
    out = cli.ok("done", "--attempt", "att-1", "--item", "investigate")
    by = {r["predicate"]: r for r in out["results"]}
    assert out["outcome"] == "fail" and by["report_json"]["outcome"] == "pass"
    assert by["files_changed"]["outcome"] == "fail" and "out/report.json" in by["files_changed"]["detail"]
    (repo / "out" / "report.json").write_text(json.dumps({"passed": True, "rewritten": 1}))
    assert cli.ok("done", "--attempt", "att-1", "--item", "investigate")["outcome"] == "pass"


def test_a_delivery_with_the_base_trees_content_is_not_a_candidate(cli, repo, base, tmp_path):
    params = {"test_argv": PASS, "scope": ["app.py"]}                 # the "fix" is already green
    assert create_from(cli, repo, base, "test_fix", params)[0] == 0
    assert begin_done(cli, "fix")["outcome"] == "pass" and begin_done(cli, "test")["outcome"] == "pass"
    git(repo, "commit", "-q", "--allow-empty", "-m", "nothing")
    got = deliver_results(cli)
    assert got["_outcome"] == "fail" and "nothing was delivered" in got["git_delivered"]["detail"]
    refusal = cli.refused("finish", "--attempt", "att-1", "--out", str(tmp_path / "out"))
    assert refusal["refused"] == "unfinished" and {f["item"] for f in refusal["failing"]} == {"deliver"}


# -- refusals -----------------------------------------------------------------------------

@pytest.mark.parametrize("template,params,refused", [
    ("code_change", {k: v for k, v in CODE.items() if k != "test_argv"}, "missing_param"),
    ("code_change", {**CODE, "paths": "app.py"}, "bad_param"),                 # a string where list[str] is declared
    ("code_change", {**CODE, "paths": ["app.py", 3]}, "bad_param"),
    ("code_change", {**CODE, "test_timeout": "30"}, "bad_param"),
    ("code_change", {**CODE, "test_timeout": True}, "bad_param"),
    ("code_change", {**CODE, "marker": ["a"]}, "bad_param"),
    ("code_change", {**CODE, "surprise": 1}, "unknown_param"),
    ("no_such_template", CODE, "unknown_template"),
    ("../code_change", CODE, "unknown_template"),
    ("code_change", "[1, 2]", "bad_param"),
    ("code_change", "{not json", "bad_param"),
])
def test_create_refuses_bad_params_and_unknown_templates(cli, repo, base, template, params, refused):
    code, out = create_from(cli, repo, base, template, params)
    assert code == 2 and out["refused"] == refused, out
    assert not list(cli.store.glob("*.json")), "a refused create leaves no plan behind"


def test_create_needs_exactly_one_source_and_every_identity_flag(cli, repo, base, tmp_path):
    assert cli.refused("create")["refused"] == "usage"
    plan_file = tmp_path / "p.json"
    plan_file.write_text("{}")
    both = cli.refused("create", "--plan-json", str(plan_file), "--template", "code_change")
    assert both["refused"] == "usage"
    assert "--attempt" in cli.refused("create", "--template", "code_change", "--params", json.dumps(CODE))["reason"]


@pytest.mark.parametrize("body,refused,needle", [
    (GOOD.format(name="t", predicate="files_present", args="{pattern: $nope}"), "bad_template", "undeclared"),
    (GOOD.format(name="t", predicate="files_present", args="{pattern: $target}").replace("${target}", "${nope}"),
     "bad_template", "undeclared"),
    (GOOD.format(name="t", predicate="worker_says_done", args="{x: $target}"), "unknown_predicate", "worker_says_done"),
    (GOOD.format(name="t", predicate="files_present", args='{pattern: "pre-${target}"}'), "bad_template", "whole value"),
    (GOOD.format(name="t", predicate="files_present", args='{pattern: "src/$target"}'), "bad_template", "whole value"),
    (GOOD.format(name="t", predicate="command_exit", args='{argv: [ls, "-l/$target"]}'), "bad_template", "whole value"),
    (GOOD.format(name="t", predicate="command_exit", args="{argv: [ls, $nope]}"), "bad_template", "undeclared"),
    (GOOD.format(name="t", predicate="files_present", args="{pattern: $target}").replace(
        "    required: true\n  target", "    required: false\n  target"), "bad_template", "scope"),
    (GOOD.format(name="t", predicate="files_present", args="{glob: $target}"), "bad_template", "unknown argument"),
    (GOOD.format(name="t", predicate="command_exit", args="{argv: $target}"), "bad_template", "wrong type"),
    (GOOD.format(name="other", predicate="files_present", args="{pattern: $target}"), "bad_template", "name"),
    (GOOD.format(name="t", predicate="files_present", args="{pattern: $target}").replace("  scope:\n    type: list[str]\n    required: true\n", ""),
     "bad_template", "scope"),
    (GOOD.format(name="t", predicate="files_present", args="{pattern: $target}").replace("type: str", "type: float"),
     "bad_template", "param target"),
    ("name: t\nversion: 1\n", "bad_template", "keys"),
    (": not yaml: [", "bad_template", "YAML"),
])
def test_a_defective_template_is_refused_when_it_is_loaded(cli, repo, base, custom, body, refused, needle):
    write_template(custom, "t", body)
    code, out = create_from(cli, repo, base, "t", {"scope": ["**"], "target": "x"})
    assert code == 2 and out["refused"] == refused and needle in out["reason"], out
    listed = {t["name"]: t for t in cli.ok("templates", store=False)["templates"]}
    assert "error" in listed["t"], "`templates` shows the defect instead of hiding the template"


def test_a_broken_template_does_not_hide_the_others_from_the_listing(cli, repo, base, custom):
    write_template(custom, "broken", GOOD.format(name="broken", predicate="files_present", args="{pattern: $target}")
                   .replace("    required: true\n  target", "    required: false\n  target"))
    write_template(custom, "fine", GOOD.format(name="fine", predicate="files_present", args="{pattern: $target}"))
    listed = {t["name"]: t for t in cli.ok("templates", store=False)["templates"]}
    assert "error" in listed["broken"] and "scope" in listed["broken"]["error"]
    assert "error" not in listed["fine"] and listed["fine"]["version"] == 1


def test_a_list_argument_may_hold_whole_param_elements(cli, repo, base, custom):
    write_template(custom, "t", GOOD.format(name="t", predicate="files_present", args="{pattern: $target}").replace(
        "predicate: files_present\n        args: {pattern: $target}",
        "predicate: command_exit\n        args: {argv: [ls, -l, $target]}"))
    assert create_from(cli, repo, base, "t", {"scope": ["**"], "target": "app.py"})[0] == 0
    assert document(cli)["plan"]["items"][0]["predicates"][0]["args"]["argv"] == ["ls", "-l", "app.py"]


def test_a_well_formed_custom_template_is_usable_from_the_overridden_directory(cli, repo, base, custom):
    write_template(custom, "t", GOOD.format(name="t", predicate="files_present", args="{pattern: $target}"))
    code, out = create_from(cli, repo, base, "t", {"scope": ["**"], "target": "app.py"})
    assert code == 0 and out["template"]["name"] == "t"
    assert document(cli)["plan"]["items"][0]["title"] == "Change app.py"


# -- files_changed ------------------------------------------------------------------------

def test_files_changed_passes_only_when_every_listed_path_changed_and_names_the_first_that_did_not(cli, repo, base):
    create_from(cli, repo, base)
    cli.ok("begin", "--attempt", "att-1", "--item", "implement")
    (repo / "app.py").write_text("x = 1\n# changed\n")
    out = cli.ok("done", "--attempt", "att-1", "--item", "implement")
    files = next(r for r in out["results"] if r["predicate"] == "files_changed")
    assert out["outcome"] == "fail" and files["outcome"] == "fail" and "other.py" in files["detail"]
    assert "app.py" not in files["detail"], "the first UNCHANGED path is the one named"

    (repo / "other.py").write_text("o = 2\n")                          # changed, but without the marker
    files = next(r for r in cli.ok("done", "--attempt", "att-1", "--item", "implement")["results"]
                 if r["predicate"] == "files_changed")
    assert files["outcome"] == "fail" and "other.py" in files["detail"] and "changed" in files["detail"]

    (repo / "other.py").write_text("o = 2  # changed\n")
    assert cli.ok("done", "--attempt", "att-1", "--item", "implement")["outcome"] == "pass"


def test_a_template_plan_runs_end_to_end_to_a_candidate(cli, repo, base, tmp_path):
    create_from(cli, repo, base)
    cli.ok("begin", "--attempt", "att-1", "--item", "implement", "--session", "child-A")
    (repo / "app.py").write_text("x = 1\n# changed\n")
    (repo / "other.py").write_text("o = 2  # changed\n")
    assert cli.ok("done", "--attempt", "att-1", "--item", "implement")["outcome"] == "pass"
    assert begin_done(cli, "test")["outcome"] == "pass"
    commit_all(repo)
    assert deliver_results(cli)["_outcome"] == "pass"
    done = cli.ok("finish", "--attempt", "att-1", "--out", str(tmp_path / "out"))
    assert done["candidate"] is True and done["artifact"] == git(repo, "rev-parse", "HEAD")
