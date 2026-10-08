"""Regression: a dirty tracked preview in a readonly snapshot must be refused.

Reproduces the measured admission of a dirty tracked preview at the unchanged
candidate B HEAD: the identity check (`HEAD == base_sha`) was satisfied, so the
reviewer was served edited bytes, the claim landed at epoch 1 and the run
advanced into review. A readonly review binds the served BYTES and MODES to the
recorded candidate, not only its commit identity.

The `world` fixture and the `git` helper are the existing exact-candidate
fixtures; independent execution of this file remains required, and authoring or
syntax parsing is not a passing CPU receipt.
"""
from pathlib import Path

import pytest

from core import run_isolation as ri

from tests.integration.test_readonly_review_candidate_binding import (
    git, launch, world)  # noqa: F401  (pytest fixtures)


def test_a_dirty_tracked_preview_at_the_candidate_is_refused(world):
    w = world
    result = launch(w)
    assert "error" not in result, result
    rid = result["run_id"]
    rec = ri.record(w.db, rid)
    assert rec["mode"] == ri.MODE_READ_SNAPSHOT and rec["base_sha"] == w.candidate
    drivers_before = list(w.drivers)
    steps_before = w.sf.get_steps(rid)
    canonical_head = git(w.source, "rev-parse", "HEAD")
    path = Path(rec["worktree_path"])

    # A tracked edit left in the served tree. HEAD still reads B, so a HEAD-only
    # binding admits it and serves the preview.
    (path / "changed.py").write_text('VALUE = "PREVIEW"\n')
    assert git(path, "rev-parse", "HEAD") == w.candidate, (
        "the tamper must leave the recorded identity intact")

    with pytest.raises(ri.IsolationUnavailable) as exc:
        ri.resolve_for_resolver(w.db, rid)
    assert "tracked" in str(exc.value)
    with pytest.raises(ri.IsolationUnavailable):
        w.sf.claim_next_step(rid)

    # No claim was handed out, no further driver, canonical bytes untouched, and
    # the preview is exactly as it was left: the check staged nothing.
    after = w.sf.get_steps(rid)
    assert [(s["step_id"], s["status"], s.get("claim_epoch")) for s in after] == [
        (s["step_id"], s["status"], s.get("claim_epoch")) for s in steps_before]
    assert w.drivers == drivers_before
    assert git(w.source, "rev-parse", "HEAD") == canonical_head
    assert (w.source / "changed.py").read_text() == 'VALUE = "A"\n'
    assert (path / "changed.py").read_text() == 'VALUE = "PREVIEW"\n'


def test_a_tracked_mode_change_at_the_candidate_is_refused(world):
    w = world
    result = launch(w)
    assert "error" not in result, result
    rid = result["run_id"]
    rec = ri.record(w.db, rid)
    path = Path(rec["worktree_path"])
    (path / "changed.py").chmod(0o755)
    assert git(path, "rev-parse", "HEAD") == w.candidate
    with pytest.raises(ri.IsolationUnavailable):
        ri.resolve_for_resolver(w.db, rid)


def test_a_staged_index_change_at_the_candidate_is_refused(world):
    w = world
    result = launch(w)
    assert "error" not in result, result
    rid = result["run_id"]
    rec = ri.record(w.db, rid)
    path = Path(rec["worktree_path"])
    (path / "changed.py").write_text('VALUE = "STAGED"\n')
    git(path, "add", "changed.py")
    with pytest.raises(ri.IsolationUnavailable):
        ri.resolve_for_resolver(w.db, rid)
