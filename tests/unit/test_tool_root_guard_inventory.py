"""Which AItelier tools refuse a missing root, and which resolve to the CWD.

`core/dpe_pipeline.py:_exec_tool` sends `project_root=""` to skillflow meaning
"no opinion". Whether that reaches a tool as an omitted argument or as "" depends
on the installed engine version, so the only durable protection is the TOOL's own
guard.

That comment used to carry the list itself, claiming to cover "every AItelier
tool that resolves a root". It named eleven and there are twenty-seven; four of
the omissions (`capability_declarations_known`, `gdscript_check`,
`user_stories_present`, `tasks_manifest_complete`) fall back to the process CWD.
A list like that is worth exactly as much as its accuracy: a reader who guards
the tools it names and skips the ones it omits inherits the omissions.

So the list lives here and is COMPLETE BY CONSTRUCTION. `test_every_tool_that_
takes_a_root_is_classified` enumerates every tool whose entry point declares
`project_root` or `workspace_root` and fails until each appears in exactly one
of the three tables below. A new tool that takes a root cannot be omitted; it can
only be classified.

This file covers `aitelier/tools/` ONLY. skillflow's native tools are the other
half and cannot be classified here at all: their guards ship in the `skillflow-py`
wheel, so what the running engine does with a missing root depends on the version
`pip install` resolved, not on anything in this checkout. That half is
`test_a_capability_grant_survives_the_deployed_engine.py`, which forbids granting
a native root-resolving tool rather than trusting a guard it cannot deploy.

The UNGUARDED table is CHARACTERIZATION, not endorsement — pinned so the hazard
is visible, and so that adding a guard is a deliberate change with a failing test
rather than a silent one. The behavioural spot-checks further down are the proof
for a handful of them; the tables are the inventory.
"""
import ast
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).resolve().parents[2] / "aitelier" / "tools"


# ── The inventory ─────────────────────────────────────────────────────────
#
# GUARDED: with no usable root the tool refuses (raises / returns an error) or
# resolves to None. It never reaches the process CWD.
GUARDED = {
    "apply_state",              # raises unless BOTH roots are absolute
    "closeout_gate",            # refuses a non-absolute project_root (depth: deep)
    "completed_cards",          # _project_id: relative_to() outside the workspaces
                                # dir raises -> "" -> no query, empty list. A bad
                                # root can never become a path it reads.
    "semantic_search",          # refuses a non-absolute project_root (error + hint)
    "emit_project_artifacts",   # refuses a non-absolute workspace_root
    "gen_audio_asset",          # _target_root: `if cand and Path(cand).is_dir()`
    "gen_image_asset",          # same
    "git_push_post",            # `Path(project_root) … if project_root else None`
    "knowledge_sync",           # `… if project_root else None`
    "loop_items_implemented",   # _graph_dir: every branch is `if <root> and …`
    "repo_delete",              # refuses a non-absolute project_root
    "restage",                  # raises on a missing root
    "run_tests",                # refuses a non-absolute project_root
    "scaffold",                 # `… if (project_root or workspace_root) else None`
    "scaffold_bible",           # raises unless the chosen root is absolute
    "task_budget_check",        # _graph_dir: every branch is `if <root> and …`
    "vision_human_pass",        # errors when workspace_root is not injected
}

# UNGUARDED: with both roots empty the tool resolves the process CWD —
# `Path(workspace_root or ".")`, or `Path("")` which IS `Path(".")`.
UNGUARDED = {
    "capability_declarations_known",   # Path(workspace_root or ".")
    "continuity_check",                # project_root or workspace_root or "."
    "gdscript_check",                  # Path(workspace_root or ".").resolve()
    "godot_compile",                   # Path(project_root or workspace_root)
    "godot_playtest",                  # same
    "godot_playtest_scenario",         # same
    "godot_vision",                    # … or "."
    "state_probe",                     # project_root or workspace_root or "."
    "tasks_manifest_complete",         # Path(workspace_root or step_dir or … or ".")
    "user_stories_present",            # Path(workspace_root or ".")
}

# DECLARES BUT DOES NOT RESOLVE: the parameter is accepted (every tool call
# supplies it) and never turned into a path. Nothing to guard.
INERT = {
    "list_pipeline_addons",
    "web_fetch",
    "web_search",
}


def _tools_declaring_a_root() -> set[str]:
    found = set()
    for d in sorted(TOOLS_DIR.iterdir()):
        impl = d / "impl.py"
        if not impl.is_file():
            continue
        try:
            tree = ast.parse(impl.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a broken tool is its own bug
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name != d.name:
                continue          # the entry point is named after the tool dir
            args = node.args
            names = {a.arg for a in
                     args.posonlyargs + args.args + args.kwonlyargs}
            if names & {"project_root", "workspace_root"}:
                found.add(d.name)
    return found


def test_every_tool_that_takes_a_root_is_classified():
    """The completeness claim, made mechanical."""
    declared = _tools_declaring_a_root()
    classified = GUARDED | UNGUARDED | INERT

    unclassified = declared - classified
    assert not unclassified, (
        f"these tools take a root and are in none of the tables: "
        f"{sorted(unclassified)}. Read the tool: does it refuse a missing root "
        f"(GUARDED), resolve the CWD (UNGUARDED), or never turn it into a path "
        f"(INERT)? Guard it before offering it to a `repo_mode: none` config.")

    gone = classified - declared
    assert not gone, (
        f"classified tools that no longer take a root: {sorted(gone)} — either "
        f"renamed, deleted, or the parameter was dropped. Update the tables.")


def test_the_three_tables_do_not_overlap():
    assert not (GUARDED & UNGUARDED)
    assert not (GUARDED & INERT)
    assert not (UNGUARDED & INERT)


# ── Behavioural spot-checks ───────────────────────────────────────────────
#
# The tables above are static classification; these run the tool.

# ── Guarded: no root injected → refuse, never the CWD ─────────────────────

def test_apply_state_refuses_when_neither_root_is_injected(tmp_path,
                                                           monkeypatch):
    """apply_state WRITES and git-commits a chapter, so this is the guard that
    matters most — and it is present."""
    from aitelier.tools.apply_state.impl import apply_state
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="absolute"):
        apply_state(project_root="", workspace_root="")


def test_restage_refuses_when_no_root_is_injected(tmp_path, monkeypatch):
    from aitelier.tools.restage.impl import restage
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="project_root"):
        restage(project_root="", workspace_root="", out_dir=str(tmp_path),
                from_repo=["README.md"])


def test_knowledge_sync_refuses_when_no_project_root_is_injected(
        tmp_path, monkeypatch):
    from aitelier.tools.knowledge_sync.impl import knowledge_sync
    monkeypatch.chdir(tmp_path)
    (tmp_path / "docs").mkdir()

    res = knowledge_sync(project_root="", workspace_root="")

    assert res.get("written") is not True, res


def test_run_tests_and_repo_delete_refuse_a_non_absolute_project_root(
        tmp_path, monkeypatch):
    from aitelier.tools.run_tests.impl import run_tests
    from aitelier.tools.repo_delete.impl import repo_delete
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "out"
    out.mkdir()

    assert run_tests(project_root="", workspace_root="",
                     out_dir=str(out))["passed"] is False

    # repo_delete's guard sits after the deletion manifest is read, so it needs
    # one to reach — a call with nothing queued returns early and deletes
    # nothing, which is safe but proves nothing.
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "_deletions.json").write_text('["x.py"]', encoding="utf-8")
    res = repo_delete(str(stage), project_root="", workspace_root="")
    assert "absolute" in (res.get("error") or ""), res
    assert res["committed"] is False


# ── Unguarded: no root injected → the process CWD ─────────────────────────

def test_state_probe_resolves_against_the_process_cwd(tmp_path, monkeypatch):
    """`base = project_root or workspace_root or "."`. With both empty the novel
    bible is looked for under the CWD — in the container, the AItelier checkout.

    Reachable only from a pipeline that both declares `repo_mode: none` and
    grants this tool; no shipped config does. Recorded, not fixed.
    """
    from aitelier.tools.state_probe.impl import state_probe
    monkeypatch.chdir(tmp_path)
    (tmp_path / "novel" / "bible").mkdir(parents=True)
    (tmp_path / "novel" / "bible" / "overview.md").write_text(
        "world", encoding="utf-8")

    res = state_probe(project_root="", workspace_root="")

    assert res["next_chapter"] == 1, (
        "state_probe no longer reads the CWD — it grew a guard; move it from "
        "UNGUARDED to GUARDED above")


def test_continuity_check_resolves_against_the_process_cwd(tmp_path,
                                                           monkeypatch):
    from aitelier.tools.continuity_check.impl import continuity_check
    monkeypatch.chdir(tmp_path)

    res = continuity_check(project_root="", workspace_root="")

    # It got far enough to look for a file, i.e. it resolved a root at all.
    assert res["passed"] is False
    assert "chapter_final.md" in res["error"], (
        "continuity_check no longer resolves the CWD; move it from UNGUARDED "
        "to GUARDED above")


def test_run_tests_refuses_a_relative_out_dir(tmp_path):
    """The root guard covered `project_root` and an EMPTY `out_dir`, not a
    relative one — and `out_dir` is agent-visible (`tool.yaml`, required: false)
    while `run_tests` is granted to a repo-less step by `tool_creation`. So
    `out_dir="reports"` on a run with no repo made `Path("reports").mkdir()`
    resolve against the process CWD, which in the container is /app, the
    bind-mounted AItelier checkout.
    """
    import importlib.util
    p = "/home/linxuhao/AItelier/aitelier/tools/run_tests/impl.py"
    s = importlib.util.spec_from_file_location("run_tests_probe", p)
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m)

    out = m.run_tests(project_root=str(tmp_path), out_dir="reports")

    assert out["passed"] is False
    assert "absolute" in out["error"] and "reports" in out["error"]
    assert not (Path.cwd() / "reports").exists(), \
        "run_tests created a directory under the process CWD"
