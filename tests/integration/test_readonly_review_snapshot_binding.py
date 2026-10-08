"""Regression: a substituted readonly snapshot must be refused preclaim.

Reproduces the actual first-claim failure: the recorded snapshot path is
re-created as a live worktree of the SAME repository (same Git common dir,
which the existing resolver verifies) but detached at canonical HEAD A
instead of the recorded candidate B. The resolver must bind the tree's
actual HEAD to the recorded base_sha BEFORE any review effect.

No model/engine/provider is invoked. Independent execution of this file
remains required; authoring or syntax parsing is not a passing CPU receipt.
"""
from pathlib import Path
import pytest

from core import run_isolation as ri

from tests.integration.test_readonly_review_candidate_binding import (
    git, launch, world)  # noqa: F401  (pytest fixtures)


def test_substituted_snapshot_head_differs_from_recorded_candidate(world):
    w = world
    result = launch(w)
    assert "error" not in result, result
    rid = result["run_id"]
    rec = ri.record(w.db, rid)
    assert rec["mode"] == ri.MODE_READ_SNAPSHOT
    assert rec["base_sha"] == w.candidate
    # Freeze the witness BEFORE the tamper: a successful launch already started
    # its own driver (appended rid), so the post-refusal assertion is that the
    # tampered resolution produced NO ADDITIONAL effects — not an empty list.
    drivers_before = list(w.drivers)
    assert drivers_before == [rid], drivers_before
    canonical_head = git(w.source, "rev-parse", "HEAD")
    canonical_changed = (w.source / "changed.py").read_text()

    # Substitute the snapshot: same path, same repository, same common dir —
    # but HEAD is canonical A, not the recorded candidate B.
    path = rec["worktree_path"]
    git(w.source, "worktree", "remove", path)
    git(w.source, "worktree", "add", "--detach", path, w.canonical)

    with pytest.raises(ri.IsolationUnavailable):
        ri.resolve_for_resolver(w.db, rid)
    # No additional review effect landed: no driver beyond the launched one,
    # canonical checkout unchanged.
    assert w.drivers == drivers_before
    assert git(w.source, "rev-parse", "HEAD") == canonical_head
    assert (w.source / "changed.py").read_text() == canonical_changed == 'VALUE = "A"\n'


def test_valid_snapshot_still_serves_the_recorded_candidate(world):
    w = world
    result = launch(w)
    assert "error" not in result, result
    rec = ri.record(w.db, result["run_id"])
    served = ri.resolve_for_resolver(w.db, result["run_id"])
    assert served == rec["worktree_path"]
    assert git(Path(served), "rev-parse", "HEAD") == w.candidate
    assert (Path(served) / "changed.py").read_text() == 'VALUE = "B"\n'


def test_host_claim_ingress_refuses_a_substituted_snapshot_typed(world):
    """The REAL host entry point — not the resolver called directly.

    The actual FIRST failure was a reviewer claim that COMPLETED: SkillFlow's
    context resolver and read-tool registration both call the code-path resolver
    as best-effort, so the typed ``IsolationUnavailable`` raised there was
    swallowed, no read schema was registered, and the later read died on
    ImportError. This drives ``AItelierSkillFlow.claim_next_step`` itself and
    asserts the refusal is propagated BEFORE any claim exists — no claimed step
    row, no additional driver, canonical bytes untouched.
    """
    w = world
    result = launch(w)
    assert "error" not in result, result
    rid = result["run_id"]
    rec = ri.record(w.db, rid)
    assert rec["mode"] == ri.MODE_READ_SNAPSHOT and rec["base_sha"] == w.candidate
    drivers_before = list(w.drivers)
    steps_before = w.sf.get_steps(rid)
    assert all(step["status"] != "claimed" for step in steps_before), steps_before
    canonical_head = git(w.source, "rev-parse", "HEAD")

    # Substitute the snapshot: same path, same repository, same common dir —
    # but HEAD is canonical A, not the recorded candidate B.
    path = rec["worktree_path"]
    git(w.source, "worktree", "remove", path)
    git(w.source, "worktree", "add", "--detach", path, w.canonical)

    with pytest.raises(ri.IsolationUnavailable):
        w.sf.claim_next_step(rid)

    # No claim was handed out: same step rows, no further driver, canonical
    # checkout unchanged. The refusal is the host ingress, not a read error.
    after = w.sf.get_steps(rid)
    assert [(s["step_id"], s["status"], s.get("claim_epoch")) for s in after] == [
        (s["step_id"], s["status"], s.get("claim_epoch")) for s in steps_before]
    assert w.drivers == drivers_before
    assert git(w.source, "rev-parse", "HEAD") == canonical_head
    assert (w.source / "changed.py").read_text() == 'VALUE = "A"\n'
