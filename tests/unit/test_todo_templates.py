"""Plans come from repository templates: expansion, typing, refusals, and the list predicate.

The CLI is driven exactly as the director's tool will drive it (`create --template`),
against real git repositories; template defects are provoked with templates written
into a temporary directory selected by AITELIER_TODO_TEMPLATES.
"""
import hashlib
import json
import os
from pathlib import Path
import socket
import sys

import pytest

from core.todo_runtime import builtin_library
from core.todo_templates import check_declared_paths, load_template
# The CLI harness and fixtures are shared with the free-plan tests.
from tests.unit.test_todo_plan_cli import (  # noqa: F401
    Cli, T0, begin_done, base, cli, commit_all, deliver_results, git, item, plan, repo)

TEMPLATES = Path(__file__).resolve().parents[2] / "todo_templates"
SHIPPED = {"code_change", "test_fix", "docs_change", "investigate"}
VERSIONS = {"code_change": 1, "test_fix": 1, "docs_change": 1, "investigate": 2}   # investigate: scope required, freshness
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
    assert shape["test_fix"] == {"test_argv": ("list[str]", True, None), "scope": ("list[str]", True, None),
                                 "test_timeout": ("int", False, 1800)}
    assert shape["docs_change"] == {"paths": ("list[str]", True, None), "scope": ("list[str]", True, None)}
    assert shape["investigate"] == {"report_path": ("str", True, None), "require_keys": ("list[str]", True, None),
                                    "scope": ("list[str]", True, None)}
    assert by_name["code_change"]["items"] == ["implement", "test"]
    assert {n: t["version"] for n, t in by_name.items()} == VERSIONS


@pytest.mark.parametrize("name", sorted(SHIPPED))
def test_each_shipped_template_loads_and_validates_against_the_library(name):
    template = load_template(name, builtin_library(), TEMPLATES)
    assert template.name == name and template.version == VERSIONS[name]
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


def test_test_fix_bounds_its_test_item_and_a_plan_records_the_template_version(cli, repo, base):
    params = {"test_argv": PASS, "scope": ["app.py"]}
    assert create_from(cli, repo, base, "test_fix", params)[0] == 0
    items = {i["id"]: i for i in document(cli)["plan"]["items"]}
    assert items["test"]["predicates"][0]["args"]["timeout"] == 1800
    assert cli.ok("status", "--attempt", "att-1")["timeout_budget_seconds"] == {"fix": 0, "test": 1800, "deliver": 0}
    assert create_from(cli, repo, base, "test_fix", {**params, "test_timeout": 90}, attempt="att-2")[0] == 0
    assert cli.ok("status", "--attempt", "att-2")["finish_budget_seconds"] == 90
    assert create_from(cli, repo, base, "test_fix", {**params, "test_timeout": "90"}, attempt="att-3")[1]["refused"] == "bad_param"
    assert document(cli)["template"]["version"] == 1
    assert create_from(cli, repo, base, "investigate", INVESTIGATE, attempt="att-4")[0] == 0
    assert document(cli, "att-4")["template"]["version"] == 2, "the plan file says which meaning of investigate it was made with"


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


# -- the paths a plan requires to change ------------------------------------------------------
#
# An item whose predicates say "every listed path must differ from its state at the start of the
# step" compares a file's DIGEST with the one captured at `begin`. A declared path whose current
# state has no digest - an existing directory, a FIFO, a socket, a device - gives that comparison
# nothing to read, so the step cannot be SEEN to change it unless it first replaces it with a
# regular file. That is caught here, at freeze time, and the refusal is a POLICY: it names the
# regular file (or the files inside the directory) to declare instead. Without it the item is
# written, run, and fails at `done`, and the director only hears about it at delivery.

def test_a_declared_directory_is_refused_by_name_before_any_plan_file_exists(cli, repo, base):
    (repo / "out").mkdir()
    code, out = create_from(cli, repo, base, params={**CODE, "paths": ["app.py", "out"]})
    assert code == 2 and out["refused"] == "impossible_path", out
    assert "'out'" in out["reason"] and "DIRECTORY" in out["reason"], out["reason"]
    assert "The step cannot be seen to change this path unless it first replaces the directory" in out["reason"], out["reason"]
    assert (out["item"], out["predicate"], out["path"]) == ("implement", "files_changed", "out")
    assert not list(cli.store.glob("*.json")), "a refused create leaves no plan behind"


def test_a_declared_directory_is_refused_even_when_it_has_files_in_it(cli, repo, base):
    # A directory WITH content is no more digestible than an empty one: `_digest` asks `is_file`,
    # and a refusal that only fired for empty directories would announce this one as "existing".
    (repo / "out").mkdir()
    (repo / "out" / "keep.md").write_text("# not empty\n")
    code, out = create_from(cli, repo, base, params={**CODE, "paths": ["app.py", "out"]})
    assert code == 2 and out["refused"] == "impossible_path", out
    assert "'out'" in out["reason"] and "DIRECTORY" in out["reason"], out["reason"]
    assert (out["item"], out["predicate"], out["path"]) == ("implement", "files_changed", "out")
    assert not list(cli.store.glob("*.json")), "a refused create leaves no plan behind"


def test_a_declared_symlink_to_a_directory_is_refused_as_a_directory(cli, repo, base):
    # `_digest` follows the link, so a symlink to a directory has no digest either; a refusal that
    # skipped symlinks would announce it as a satisfiable path.
    (repo / "real").mkdir()
    (repo / "link").symlink_to("real", target_is_directory=True)
    code, out = create_from(cli, repo, base, params={**CODE, "paths": ["app.py", "link"]})
    assert code == 2 and out["refused"] == "impossible_path", out
    assert "'link'" in out["reason"] and "DIRECTORY" in out["reason"], out["reason"]
    assert (out["item"], out["predicate"], out["path"]) == ("implement", "files_changed", "link")
    assert not list(cli.store.glob("*.json")), "a refused create leaves no plan behind"


def test_a_declared_path_under_an_existing_file_is_refused_not_announced_for_creation(cli, repo, base):
    # `app.py/x/y.txt` cannot be created while app.py is a file, so announcing it as "the step will
    # have to CREATE it" promises a change the item can never deliver.
    code, out = create_from(cli, repo, base, params={**CODE, "paths": ["app.py", "app.py/x/y.txt"],
                                                     "scope": ["app.py"]})
    assert code == 2 and out["refused"] == "impossible_path", out
    assert "'app.py/x/y.txt'" in out["reason"] and "'app.py'" in out["reason"], out["reason"]
    assert "a regular file, where a directory is required" in out["reason"], out["reason"]
    assert "The step cannot be seen to change it unless it first replaces 'app.py' with a directory" in out["reason"], out["reason"]
    assert (out["item"], out["predicate"], out["path"]) == ("implement", "files_changed", "app.py/x/y.txt")
    assert not list(cli.store.glob("*.json")), "a refused create leaves no plan behind"
    # The audit runs BEFORE the plan's lock is taken, so a refusal does not even leave the sidecar
    # file: an audit that ran inside the lock would refuse with an empty `<attempt>.lock` behind.
    assert not list(cli.store.glob("*.lock")), "a refused create leaves no lock file either"


def test_a_declared_path_under_a_fifo_is_refused_the_same_way(cli, repo, base):
    os.mkfifo(repo / "pipe")
    code, out = create_from(cli, repo, base, params={**CODE, "paths": ["app.py", "pipe/x.txt"],
                                                     "scope": ["app.py"]})
    assert code == 2 and out["refused"] == "impossible_path", out
    assert "'pipe'" in out["reason"] and "a FIFO, where a directory is required" in out["reason"], out["reason"]
    assert out["path"] == "pipe/x.txt" and out["predicate"] == "files_changed"
    assert not list(cli.store.glob("*.json")), "a refused create leaves no plan behind"


def test_a_declared_fifo_is_refused_rather_than_announced_as_existing(cli, repo, base):
    # `_digest` reads None for anything that is not a regular file, so `files_changed` cannot see
    # a FIFO change unless the step replaces it with a regular file; listing it under "existing"
    # (as a path the step will change in place) claimed the opposite.
    os.mkfifo(repo / "pipe")
    code, out = create_from(cli, repo, base, params={**CODE, "paths": ["app.py", "pipe"]})
    assert code == 2 and out["refused"] == "impossible_path", out
    assert "'pipe'" in out["reason"] and "exists and is a FIFO" in out["reason"], out["reason"]
    assert "The step cannot be seen to change it unless it first replaces it with a regular file" in out["reason"], out["reason"]
    assert (out["item"], out["predicate"], out["path"]) == ("implement", "files_changed", "pipe")
    assert not list(cli.store.glob("*.json")), "a refused create leaves no plan behind"


def test_a_declared_socket_is_refused_by_the_same_no_digest_rule_as_a_fifo(cli, repo, base):
    # The rule is "a path that is not a regular file has no digest", not "a FIFO has no digest":
    # `_digest` asks `is_file`, so a socket is in exactly the same class. A refusal narrowed to
    # `is_fifo()` passed every shipped case and announced this one as a satisfiable path.
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(repo / "sock"))
        code, out = create_from(cli, repo, base, params={**CODE, "paths": ["app.py", "sock"]})
    assert code == 2 and out["refused"] == "impossible_path", out
    assert "'sock'" in out["reason"] and "exists and is a socket" in out["reason"], out["reason"]
    assert (out["item"], out["predicate"], out["path"]) == ("implement", "files_changed", "sock")
    assert not list(cli.store.glob("*.json")), "a refused create leaves no plan behind"


def test_a_declared_symlink_to_a_regular_file_still_changes_with_its_target(cli, repo, base):
    # The other side of the two checks above: `_digest` follows a link, so this path has a digest and
    # the item can pass. Refusing symlinks outright would be a regression, not a stricter audit.
    (repo / "link.py").symlink_to("app.py")
    code, out = create_from(cli, repo, base, params={**CODE, "paths": ["app.py", "link.py"],
                                                     "scope": ["app.py", "link.py"]})
    assert code == 0, out
    audit = out["paths"]
    assert (audit["existing"], audit["not_yet_existing"], audit["notes"]) == (["app.py", "link.py"], [], []), audit
    cli.ok("begin", "--attempt", "att-1", "--item", "implement")
    (repo / "app.py").write_text("x = 1  # changed\n")
    files = next(r for r in cli.ok("done", "--attempt", "att-1", "--item", "implement")["results"]
                 if r["predicate"] == "files_changed")
    assert files["outcome"] == "pass", files


def test_a_fifo_replaced_by_a_regular_file_satisfies_files_changed(cli, repo, base):
    # This is what makes the non-regular-file refusal a POLICY, not an impossibility. The refusal says
    # a path that is not a regular file has no digest, so the step cannot be seen to change it unless
    # it replaces it with a regular file. Measured here through the CLI: `begin` captures the FIFO's
    # state as None (`_digest` answers None for anything that is not a regular file), the same FIFO
    # left alone fails the item, and replacing it with a regular file passes it. `create` refuses an
    # existing FIFO at freeze time, so the FIFO appears after the plan is frozen and before `begin`:
    # that is the state `begin` captures and the item is judged from.
    code, out = create_from(cli, repo, base, params={**CODE, "paths": ["app.py", "pipe"], "scope": ["app.py", "pipe"]})
    assert code == 0, out
    os.mkfifo(repo / "pipe")
    began = cli.ok("begin", "--attempt", "att-1", "--item", "implement")
    assert began["pre_state"]["pipe"] is None, began["pre_state"]
    assert len(began["pre_state"]["app.py"]) == 64, "a regular file's captured state is a real sha256 digest"

    (repo / "app.py").write_text("x = 1\n# changed\n")
    files = next(r for r in cli.ok("done", "--attempt", "att-1", "--item", "implement")["results"]
                 if r["predicate"] == "files_changed")
    assert files["outcome"] == "fail" and "pipe" in files["detail"], files
    assert "app.py" not in files["detail"], "the FIFO is the path that fails, not the file that was edited"

    (repo / "pipe").unlink()
    (repo / "pipe").write_text("# changed\n")
    out = cli.ok("done", "--attempt", "att-1", "--item", "implement")
    files = next(r for r in out["results"] if r["predicate"] == "files_changed")
    assert files["outcome"] == "pass" and out["outcome"] == "pass", out


def test_a_directory_symlink_replaced_by_a_regular_file_satisfies_files_changed(cli, repo, base):
    # The same policy for the directory refusal. `_digest` follows a symlink, so a symlink to a
    # directory has no digest (captured as None); replacing it with a regular file gives one, and
    # the item passes. This is what makes the directory refusal a POLICY, not an impossibility: the
    # refusal names "declare a regular file" as the way through, and this test measures that it works.
    # The symlink appears after the plan is frozen, for the same reason as the FIFO above.
    code, out = create_from(cli, repo, base, params={**CODE, "paths": ["app.py", "link"], "scope": ["app.py", "link"]})
    assert code == 0, out
    (repo / "real").mkdir()
    (repo / "link").symlink_to("real", target_is_directory=True)
    began = cli.ok("begin", "--attempt", "att-1", "--item", "implement")
    assert began["pre_state"]["link"] is None, began["pre_state"]

    (repo / "app.py").write_text("x = 1\n# changed\n")
    (repo / "link").unlink()
    (repo / "link").write_text("# changed\n")
    out = cli.ok("done", "--attempt", "att-1", "--item", "implement")
    files = next(r for r in out["results"] if r["predicate"] == "files_changed")
    assert files["outcome"] == "pass" and out["outcome"] == "pass", out


@pytest.mark.parametrize("bad", ["/etc/passwd", "../outside.py", "sub/../../escape.py"])
def test_a_declared_path_outside_the_workspace_is_refused_before_any_plan_file_exists(cli, repo, base, bad):
    code, out = create_from(cli, repo, base, params={**CODE, "paths": ["app.py", bad]})
    assert code == 2 and out["refused"] == "invalid_plan", out
    assert "relative path inside the workspace" in out["reason"] and bad in out["reason"]
    assert not list(cli.store.glob("*.json")), "a refused create leaves no plan behind"


@pytest.mark.parametrize("paths", [["app.py", "app.py"], ["app.py", "./app.py"],
                                   ["docs/x.md", "docs/x.md"], ["app.py", "app.py/"]])
def test_a_declared_path_repeated_in_one_list_is_refused(cli, repo, base, paths):
    code, out = create_from(cli, repo, base, params={**CODE, "paths": paths})
    assert code == 2 and out["refused"] == "impossible_path", out
    assert "twice" in out["reason"] and "not declared at all" in out["reason"], out["reason"]
    assert out["path"] == paths[0].rstrip("/") and out["item"] == "implement"
    assert not list(cli.store.glob("*.json")), "a refused create leaves no plan behind"


def test_a_plan_whose_paths_all_exist_refuses_nothing_and_announces_nothing(cli, repo, base):
    code, out = create_from(cli, repo, base)
    assert code == 0, out
    assert out["paths"] == {"workspace": str(repo), "workspace_is_a_directory": True,
                            "declared": ["app.py", "other.py"], "existing": ["app.py", "other.py"],
                            "not_yet_existing": [], "notes": []}


def test_a_path_that_does_not_exist_yet_is_legal_and_announced_in_the_plan_result(cli, repo, base):
    params = {**CODE, "paths": ["app.py", "docs/new.md"], "scope": ["app.py", "docs/"]}
    code, out = create_from(cli, repo, base, params=params)
    assert code == 0, out
    audit = out["paths"]
    assert audit["workspace_is_a_directory"] is True
    assert audit["declared"] == ["app.py", "docs/new.md"]
    assert (audit["existing"], audit["not_yet_existing"]) == (["app.py"], ["docs/new.md"])
    note = audit["notes"][0]
    assert "docs/new.md" in note and "will have to CREATE" in note and "cannot pass" in note, note
    assert document(cli)["plan"]["items"][0]["predicates"][0]["args"]["paths"] == ["app.py", "docs/new.md"]


def test_a_path_that_does_not_exist_yet_still_has_to_be_created_for_the_item_to_pass(cli, repo, base):
    params = {**CODE, "paths": ["app.py", "docs/new.md"], "scope": ["app.py", "docs/"]}
    assert create_from(cli, repo, base, params=params)[0] == 0
    cli.ok("begin", "--attempt", "att-1", "--item", "implement")
    (repo / "app.py").write_text("x = 1  # changed\n")
    files = next(r for r in cli.ok("done", "--attempt", "att-1", "--item", "implement")["results"]
                 if r["predicate"] == "files_changed")
    assert files["outcome"] == "fail" and "docs/new.md" in files["detail"], "announced, not excused"
    (repo / "docs").mkdir()
    (repo / "docs" / "new.md").write_text("# changed\n")
    assert cli.ok("done", "--attempt", "att-1", "--item", "implement")["outcome"] == "pass"


def test_investigate_announces_the_report_it_has_to_create(cli, repo, base):
    code, out = create_from(cli, repo, base, "investigate", INVESTIGATE)
    assert code == 0, out
    assert out["paths"]["declared"] == ["out/report.json"] and out["paths"]["not_yet_existing"] == ["out/report.json"]
    assert "out/report.json" in out["paths"]["notes"][0]


def test_a_workspace_that_is_not_a_directory_is_said_out_loud_not_silently_skipped(cli, repo, base, tmp_path):
    ghost = tmp_path / "not-a-workspace"
    code, out = cli.run("create", "--template", "code_change", "--params", json.dumps(CODE),
                        "--attempt", "att-1", "--node-key", "execution.demo", "--context-hash", "c" * 64,
                        "--base-sha", base, "--workspace", str(ghost), "--goal", "make the change")
    assert code == 0, out
    audit = out["paths"]
    assert audit["workspace_is_a_directory"] is False
    assert (audit["declared"], audit["existing"], audit["not_yet_existing"]) == (["app.py", "other.py"], [], []), \
        "nothing was invented about paths the audit could not look up"
    note = audit["notes"][0]
    assert str(ghost) in note and "could not be looked up" in note and "nothing was assumed" in note, note


def test_a_missing_workspace_still_refuses_a_duplicate_and_the_note_does_not_claim_otherwise(cli, repo, base, tmp_path):
    # The duplicate-spelling refusal reads the PLAN, so it fires with or without a workspace; a note
    # that listed it among the checks it had skipped claimed the opposite of what the code did.
    ghost = tmp_path / "not-a-workspace"

    def create(params):
        return cli.run("create", "--template", "code_change", "--params", json.dumps(params),
                       "--attempt", "att-1", "--node-key", "execution.demo", "--context-hash", "c" * 64,
                       "--base-sha", base, "--workspace", str(ghost), "--goal", "make the change")

    code, out = create({**CODE, "paths": ["out", "out"], "scope": ["out"]})
    assert code == 2 and out["refused"] == "impossible_path", out
    assert "twice" in out["reason"] and "not declared at all" in out["reason"], out["reason"]
    assert out["path"] == "out" and not list(cli.store.glob("*.json")), "a refused create leaves no plan behind"

    code, out = create(CODE)
    assert code == 0, out
    note = out["paths"]["notes"][0]
    assert "could not be looked up" in note and "nothing was assumed" in note, note
    assert "duplicate" in note and "still refused" in note, note
    assert "duplicate-spelling or existence check was made" not in note, note


def test_a_missing_workspace_note_names_every_refusal_that_still_ran(cli, repo, base, tmp_path):
    # TWO refusals read the PLAN and need no workspace: a duplicate spelling, and a path outside the
    # workspace. The note named only the first, so a director read the second as one of the checks
    # the missing directory had cost them - and only a workspace-dependent check may be said to cost.
    ghost = tmp_path / "not-a-workspace"

    def create(params):
        return cli.run("create", "--template", "code_change", "--params", json.dumps(params),
                       "--attempt", "att-1", "--node-key", "execution.demo", "--context-hash", "c" * 64,
                       "--base-sha", base, "--workspace", str(ghost), "--goal", "make the change")

    code, out = create({**CODE, "paths": ["app.py", "/etc/passwd"]})
    assert code == 2 and out["refused"] == "invalid_plan", out
    assert "/etc/passwd" in out["reason"] and "inside the workspace" in out["reason"], out["reason"]

    code, out = create(CODE)
    assert code == 0, out
    note = out["paths"]["notes"][0]
    assert "duplicate" in note and "outside the workspace" in note, note
    assert "still refused" in note and "neither needs a directory" in note, note
    assert "could not be looked up" in note and "nothing was assumed" in note, \
        "the workspace-dependent checks that did not run are still said out loud"


def test_the_free_plan_route_refuses_the_same_paths(cli, repo, base):
    (repo / "out").mkdir()
    directory = plan(repo, base, [item("edit", [{"predicate": "files_changed", "args": {"paths": ["out"]}}])])
    code, out = cli.create(directory)
    assert code == 2 and out["refused"] == "impossible_path" and out["path"] == "out", out
    assert not list(cli.store.glob("*.json")), "a refused create leaves no plan behind"


TWO_ITEMS = """\
name: t
version: 1
description: two items that each require the same file to change
params:
  scope:
    type: list[str]
    required: true
  paths:
    type: list[str]
    required: true
items:
  - id: first
    title: "first ${paths}"
    predicates:
      - predicate: files_changed
        args: {paths: $paths}
  - id: second
    title: "second ${paths}"
    needs: [first]
    predicates:
      - predicate: files_changed
        args: {paths: $paths}
"""


def test_the_same_path_in_two_items_is_not_a_duplicate(cli, repo, base, custom):
    write_template(custom, "t", TWO_ITEMS)
    code, out = create_from(cli, repo, base, "t", {"scope": ["app.py"], "paths": ["app.py"]})
    assert code == 0, out
    assert out["paths"]["declared"] == ["app.py"], "one entry per path, once"


def test_the_audit_is_one_function_over_the_frozen_plan(tmp_path):
    from core.todo_plan import Refusal, TodoPlan

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.py").write_text("x = 1\n")
    (workspace / "dir").mkdir()
    data = {"attempt_id": "att-1", "node_key": "execution.demo", "context_hash": "c" * 64, "base_sha": "0" * 40,
            "workspace": str(workspace), "scope": ["a.py"], "goal": "g",
            "items": [item("edit", [{"predicate": "files_changed", "args": {"paths": ["a.py", "later.py"]}}])]}
    audit = check_declared_paths(TodoPlan.from_json(data, builtin_library()))
    assert (audit.declared, audit.existing, audit.not_yet_existing) == (("a.py", "later.py"), ("a.py",), ("later.py",))
    assert audit.measured is True and audit.workspace == str(workspace)

    data["items"] = [item("edit", [{"predicate": "files_changed", "args": {"paths": ["dir"]}}])]
    with pytest.raises(Refusal) as caught:
        check_declared_paths(TodoPlan.from_json(data, builtin_library()))
    assert caught.value.code == "impossible_path" and caught.value.extra["path"] == "dir"


@pytest.mark.parametrize("shape, paths, named", [
    # Each case earns its place by being reddened by a named mutant, measured against the
    # candidate's `core/todo_templates.py` (mutant A: `is_dir() and not is_symlink()`; mutant B:
    # `is_dir() and not any(iterdir())`; mutant C: the no-digest refusal narrowed to `is_fifo()`):
    #   full_directory      - reddened by B (a non-empty directory is not refused)
    #   symlinked_directory - reddened by A (a symlink to a directory is not refused)
    #   socket              - reddened by C (a socket is not refused)
    # The other three (file_parent, fifo_parent, fifo) are not reddened by A, B or C: they are refusals
    # that were already on main at c2f2a51c2b, and they are reddened by that commit's pre-image
    # (c2f2a51c2b^), which lacks them. An empty directory is NOT a case here: no named mutant changes its refusal, so
    # it pinned nothing; the CLI test `test_a_declared_directory_is_refused_by_name_before_any_plan_file_exists`
    # still pins it through the route.
    ("full_directory", ["app.py", "out"], "DIRECTORY"),
    ("symlinked_directory", ["app.py", "link"], "DIRECTORY"),
    ("file_parent", ["app.py", "app.py/x/y.txt"], "a regular file, where a directory is required"),
    ("fifo_parent", ["app.py", "pipe/x.txt"], "a FIFO, where a directory is required"),
    ("fifo", ["app.py", "pipe"], "exists and is a FIFO"),
    ("socket", ["app.py", "sock"], "exists and is a socket"),
])
def test_every_no_digest_shape_is_refused_by_the_audit_function_itself(tmp_path, shape, paths, named):
    """The CLI cases above pin the route a director uses; this pins the FUNCTION that route calls.

    Each case asserts the refusal CODE, the path it names (the one with no digest for
    `files_changed` to compare, not the regular file that precedes it in the list) and the KIND it
    says it found, so a refusal that fired for another reason - or one narrowed to a single file
    kind - cannot pass here by accident.
    """
    from core.todo_plan import Refusal, TodoPlan

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "app.py").write_text("x = 1\n")
    listener = None
    if shape == "full_directory":
        (workspace / "out").mkdir()
        (workspace / "out" / "keep.md").write_text("# not empty\n")
    elif shape == "symlinked_directory":
        (workspace / "real").mkdir()
        (workspace / "link").symlink_to("real", target_is_directory=True)
    elif shape in ("fifo", "fifo_parent"):
        os.mkfifo(workspace / "pipe")
    elif shape == "socket":
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(workspace / "sock"))
    try:
        data = {"attempt_id": "att-1", "node_key": "execution.demo", "context_hash": "c" * 64,
                "base_sha": "0" * 40, "workspace": str(workspace), "scope": ["app.py"], "goal": "g",
                "items": [item("edit", [{"predicate": "files_changed", "args": {"paths": paths}}])]}
        with pytest.raises(Refusal) as caught:
            check_declared_paths(TodoPlan.from_json(data, builtin_library()))
    finally:
        if listener is not None:
            listener.close()
    assert caught.value.code == "impossible_path", caught.value.reason
    assert (caught.value.extra["item"], caught.value.extra["predicate"]) == ("edit", "files_changed")
    assert caught.value.extra["path"] == paths[-1], "the refusal names the no-digest path, not the one before it"
    assert named in caught.value.reason, caught.value.reason
