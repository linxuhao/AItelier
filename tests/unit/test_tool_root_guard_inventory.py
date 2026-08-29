"""Which AItelier tools refuse a missing root, and which resolve to the CWD.

`core/dpe_pipeline.py:_exec_tool` sends `project_root=""` to skillflow meaning
"no opinion". Whether that reaches a tool as an omitted argument or as "" depends
on the installed engine version, so the only durable protection is the TOOL's own
guard — and `_exec_tool` carries a comment listing which tools have one, with the
instruction "guard the tool before offering it to a `repo_mode: none` config".

A list like that is worth exactly as much as its accuracy: a reader who guards
the tools it names and skips the ones it omits inherits the omissions. This file
is that list, executable. When a tool moves between the two halves, fix the
comment in `_exec_tool` in the same change.

The unguarded half is CHARACTERIZATION, not endorsement — pinned so the hazard is
visible, and so that adding a guard is a deliberate change with a failing test
rather than a silent one.
"""
import pytest


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
    grants this tool; no shipped config does. Recorded, not fixed, this round.
    """
    from aitelier.tools.state_probe.impl import state_probe
    monkeypatch.chdir(tmp_path)
    (tmp_path / "novel" / "bible").mkdir(parents=True)
    (tmp_path / "novel" / "bible" / "overview.md").write_text(
        "world", encoding="utf-8")

    res = state_probe(project_root="", workspace_root="")

    assert res["next_chapter"] == 1, (
        "state_probe no longer reads the CWD — it grew a guard; move it to the "
        "guarded half here and in core/dpe_pipeline.py:_exec_tool")


def test_continuity_check_resolves_against_the_process_cwd(tmp_path,
                                                           monkeypatch):
    from aitelier.tools.continuity_check.impl import continuity_check
    monkeypatch.chdir(tmp_path)

    res = continuity_check(project_root="", workspace_root="")

    # It got far enough to look for a file, i.e. it resolved a root at all.
    assert res["passed"] is False
    assert "chapter_final.md" in res["error"], (
        "continuity_check no longer resolves the CWD; update both lists")
