"""Regression tests for the existing-repo "no-op floor" bug.

Root cause: a t_impl step whose only write was an ``edit`` (result key
``"edited"``, not ``"written"``) registered ZERO output, so the engine
(a) re-prompted "No output produced" — and because retries inherit the
message history, the agent re-applied its edit (duplicated/triplicated code),
and (b) eventually "floored" the ENTIRE repo into the draft via
``read_text(errors="replace")``, producing wholesale commits AND corrupting
binary files.

These tests lock in the two host-side guarantees of the fix:

1. ``_written_name`` counts ``written`` / ``edited`` / ``created`` — a single
   edit is recognised as output, so the no-op path never triggers after a real
   change.
2. ``clean_draft_dir`` clears ``{step}.tmp`` (so a fresh run can't inherit a
   prior task's staged files) while leaving ``{step}/`` intact (self-context).
"""

from core.dpe_pipeline import PipelineEngine
from core.workspace_manager import WorkspaceManager


def test_written_name_counts_edit_and_create_not_just_write():
    assert PipelineEngine._written_name({"written": "a.py"}) == "a.py"
    assert PipelineEngine._written_name({"edited": "b.py"}) == "b.py"
    assert PipelineEngine._written_name({"created": "c.py"}) == "c.py"
    # The bug: an edit result was previously read as "" → false no-op.
    assert PipelineEngine._written_name({"edited": "b.py"}) != ""
    # Non-write results register nothing.
    assert PipelineEngine._written_name({"error": "boom"}) == ""
    assert PipelineEngine._written_name({"content": "..."}) == ""


def test_clean_draft_dir_clears_tmp_but_preserves_final(tmp_path):
    ws = WorkspaceManager(str(tmp_path))
    pid, step, graph = "proj1", "t_impl", "dpe_default_v2"

    draft = ws._draft_dir(pid, step, graph)
    final = ws._final_dir(pid, step, graph)
    draft.mkdir(parents=True, exist_ok=True)
    final.mkdir(parents=True, exist_ok=True)

    # Stale file left in staging by a prior task, plus a legitimate final output.
    (draft / "stale_from_prior_task.py").write_text("# leftover\n")
    (final / "prior_output.py").write_text("# self-context\n")

    ws.clean_draft_dir(pid, step, graph)

    assert draft.exists()
    assert list(draft.iterdir()) == []                 # tmp wiped
    assert (final / "prior_output.py").exists()         # final untouched
