"""Working-file validators never reach a file through a substituted parent.

An independent review measured the pinned read_snapshot resolver returning a
snapshot whose tracked directory had been replaced by a link to an outside
directory holding identical bytes: the verifier stat'ed and opened the outside
file (final-component O_NOFOLLOW only). These controls drive the actual
resolver, provisioning verifier, seed, reaper and raw Source status entry
points on real temporary repositories and observe every file descriptor the
process opens: a descriptor whose real target lies under the fixture's own
outside directory is recorded, including descriptor-relative opens. The
expected answer is an explicit refusal with no outside open, while the plain
snapshot (including a confined tracked directory link) stays useful and its
index unchanged.
"""

import builtins
import os
import shutil
from contextlib import contextmanager
from pathlib import Path

import pytest

from core import closed_git, run_isolation as ri
from skillflow.exceptions import IsolationUnavailable
from test_closed_backend_git import (  # noqa: F401 -- shared fixtures
    _ReapSF, add_filters, db, g, home, init, marker_program, markers, project, ran, sha256)


@contextmanager
def outside_opens(outside: Path):
    """Record every descriptor opened by this process whose real target is under *outside*."""
    real_os_open, real_open = os.open, builtins.open
    root = os.path.realpath(outside)
    seen = []

    def note(target):
        if target and (target == root or target.startswith(root + os.sep)):
            seen.append(target)

    def os_open(path, flags, *args, **kwargs):
        fd = real_os_open(path, flags, *args, **kwargs)
        try:
            note(os.readlink(f"/proc/self/fd/{fd}"))
        except OSError:
            pass
        return fd

    def py_open(file, *args, **kwargs):
        fh = real_open(file, *args, **kwargs)
        try:
            note(os.readlink(f"/proc/self/fd/{fh.fileno()}"))
        except (OSError, ValueError, AttributeError):
            pass
        return fh

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "open", os_open)
        mp.setattr(builtins, "open", py_open)
        yield seen


def _admin_index(w: Path) -> Path:
    return Path(g(w, "rev-parse", "--absolute-git-dir")) / "index"


def _snapshot(db, tmp_path, files, run_id="run-parent"):
    src = tmp_path / "src"
    base = init(src, files)
    project(db, "parent", src)
    rec = ri.ensure_for_run(db, run_id=run_id, project_id="parent",
                            config_name="review", repo_mode="none")
    assert rec["mode"] == "read_snapshot" and rec["base_sha"] == base
    return src, rec, Path(rec["worktree_path"])


def _outside_copy(tmp_path, rel_under: dict) -> Path:
    outside = tmp_path / "own-outside"
    for rel, data in rel_under.items():
        p = outside / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data)
    return outside


FILES = {"dir/file.txt": "same pinned fixture bytes\n", "a/b/c.txt": "deep pinned bytes\n",
         "keep.txt": "keep\n"}


def test_plain_snapshot_with_confined_directory_link_stays_useful(db, home, tmp_path):
    src = tmp_path / "src"
    init(src, FILES)
    os.symlink("a/b", src / "to-b")
    g(src, "add", "-A")
    g(src, "commit", "-qm", "confined directory link")
    project(db, "parent", src)
    rec = ri.ensure_for_run(db, run_id="run-plain", project_id="parent",
                            config_name="review", repo_mode="none")
    w = Path(rec["worktree_path"])
    before = sha256(_admin_index(w))
    with outside_opens(tmp_path / "own-outside") as seen:
        assert ri.resolve_for_resolver(db, "run-plain") == str(w)
        report = {}
        ri.verify_served_tree(w, rec["base_sha"], _report=report.update)
    assert report == {"commit_sha": rec["base_sha"], "tracked_entries": 4, "problems": []}
    assert os.readlink(w / "to-b") == "a/b" and (w / "to-b" / "c.txt").read_text() == "deep pinned bytes\n"
    assert sha256(_admin_index(w)) == before and seen == []


@pytest.mark.parametrize("swap", ["dir", "a", "a/b"])
def test_resolver_refuses_a_tracked_parent_replaced_by_a_link(db, home, tmp_path, swap):
    src, rec, w = _snapshot(db, tmp_path, FILES)
    index_before = sha256(_admin_index(w))
    source_index_before = sha256(src / ".git" / "index")
    # The outside directory serves the SAME pinned bytes under the substituted parent.
    below = {rel[len(swap) + 1:]: data for rel, data in FILES.items() if rel.startswith(swap + "/")}
    outside = _outside_copy(tmp_path, below)
    shutil.rmtree(w / swap)
    os.symlink(outside, w / swap)
    with outside_opens(outside) as seen:
        with pytest.raises(IsolationUnavailable, match="differs from its candidate"):
            ri.resolve_for_resolver(db, rec["run_id"])
        with pytest.raises(IsolationUnavailable, match="does not serve candidate"):
            ri.verify_served_tree(w, rec["base_sha"])
    assert seen == []
    assert sha256(_admin_index(w)) == index_before
    assert sha256(src / ".git" / "index") == source_index_before
    for rel, data in below.items():
        assert (outside / rel).read_text() == data


def test_resolver_refuses_a_linked_snapshot_root_or_gitfile(db, home, tmp_path):
    src, rec, w = _snapshot(db, tmp_path, FILES)
    gitfile_text = (w / ".git").read_text()
    # A gitfile served through a link, naming the very same admin directory.
    outside = tmp_path / "own-outside"
    outside.mkdir()
    (outside / "gitfile").write_text(gitfile_text)
    (w / ".git").unlink()
    os.symlink(outside / "gitfile", w / ".git")
    with outside_opens(outside) as seen:
        with pytest.raises(IsolationUnavailable, match="not a live worktree"):
            ri.resolve_for_resolver(db, rec["run_id"])
    assert seen == []
    (w / ".git").unlink()
    (w / ".git").write_text(gitfile_text)
    assert ri.resolve_for_resolver(db, rec["run_id"]) == str(w)
    # The whole snapshot moved outside the managed root and linked back.
    moved = tmp_path / "own-outside" / "moved-snapshot"
    os.rename(w, moved)
    os.symlink(moved, w)
    with outside_opens(outside) as seen:
        with pytest.raises(IsolationUnavailable, match="not a live worktree"):
            ri.resolve_for_resolver(db, rec["run_id"])
        with pytest.raises(IsolationUnavailable):
            ri.verify_served_tree(w, rec["base_sha"])
    assert seen == []


def test_seed_never_reads_through_a_substituted_parent(db, home, tmp_path):
    src = tmp_path / "src"
    init(src, FILES)
    project(db, "seedp", src)
    rec = ri.ensure_for_run(db, run_id="run-seedp", project_id="seedp",
                            config_name="coding_impl", repo_mode="code")
    w = Path(rec["worktree_path"])
    outside = _outside_copy(tmp_path, {"file.txt": FILES["dir/file.txt"]})
    index_before = sha256(src / ".git" / "index")
    adm = ri.admit_write(db, src, kind="coding_workspace", detail="seed parent")
    try:
        # Owned tree side: a tracked parent replaced by a link is not its untouched base.
        shutil.rmtree(w / "dir")
        os.symlink(outside, w / "dir")
        with outside_opens(outside) as seen:
            with pytest.raises(IsolationUnavailable, match="no longer its untouched base"):
                ri.seed_worktree_from_source(db, "run-seedp", admission=adm)
        assert seen == [] and os.path.islink(w / "dir")
        os.unlink(w / "dir")
        (w / "dir").mkdir()
        (w / "dir" / "file.txt").write_text(FILES["dir/file.txt"])
        # Source side: the same substitution in the Source checkout is an unconfined link.
        shutil.rmtree(src / "dir")
        os.symlink(outside, src / "dir")
        with outside_opens(outside) as seen:
            with pytest.raises(IsolationUnavailable, match="leaves the repository"):
                ri.seed_worktree_from_source(db, "run-seedp", admission=adm)
        assert seen == []
        assert (w / "dir" / "file.txt").read_text() == FILES["dir/file.txt"]
    finally:
        ri.retire_write(db, adm["id"])
    assert sha256(src / ".git" / "index") == index_before


@pytest.mark.parametrize("mode", ["worktree", "read_snapshot"])
def test_reaper_retains_a_tree_whose_tracked_parent_became_a_link(db, home, tmp_path, mode):
    src = tmp_path / "src"
    init(src, FILES)
    project(db, "reapp", src)
    rid = f"run-reapp-{mode}"
    rec = ri.ensure_for_run(db, run_id=rid, project_id="reapp",
                            config_name="coding_impl" if mode == "worktree" else "review",
                            repo_mode="code" if mode == "worktree" else "none")
    w = Path(rec["worktree_path"])
    outside = _outside_copy(tmp_path, {"file.txt": FILES["dir/file.txt"]})
    shutil.rmtree(w / "dir")
    os.symlink(outside, w / "dir")
    ri.release(db, rid, disposition="discarded")
    with outside_opens(outside) as seen:
        ok, why = ri.is_disposable(db, rid, run_status="completed", admitted_ops=0)
        assert not ok and "uncommitted work" in why
        report = ri.reap_released_worktrees(db, _ReapSF({rid: {"status": "completed"}}),
                                            min_idle_seconds=0)
        ok, why = ri._remove_run_worktree(ri.record(db, rid))
    assert report["removed"] == [] and not ok and "not clean" in why
    assert seen == [] and os.path.islink(w / "dir")
    assert str(w) in g(src, "worktree", "list", "--porcelain")
    assert (outside / "file.txt").read_text() == FILES["dir/file.txt"]


def test_reaper_storage_absence_is_observed_without_following_links(db, home, tmp_path):
    src = tmp_path / "src"
    init(src, {"a.txt": "a\n"})
    project(db, "store", src)
    rec = ri.ensure_for_run(db, run_id="run-store", project_id="store",
                            config_name="review", repo_mode="none")
    w = Path(rec["worktree_path"])
    names = ("manifest.json", "files.zvec", "index.zvec")
    assert ri._index_storage_present(w, names) is False
    outside = tmp_path / "own-outside"
    outside.mkdir()
    os.symlink(outside, w / ".zvec-grep")  # a link where storage belongs: present, not absent
    with outside_opens(outside) as seen:
        assert ri._index_storage_present(w, names) is True
        assert ri._index_storage_present(w, ()) is True
    assert seen == []
    os.unlink(w / ".zvec-grep")
    (w / ".zvec-grep").mkdir()
    assert ri._index_storage_present(w, names) is False
    assert ri._index_storage_present(w, ()) is True
    (w / ".zvec-grep" / "files.zvec").write_text("x")
    assert ri._index_storage_present(w, names) is True


def test_source_attributes_file_is_not_read_through_a_linked_parent(db, home, tmp_path, markers):
    src = tmp_path / "src"
    init(src, {"a.txt": "a\n"})
    add_filters(src, markers, attributes=False)
    outside = tmp_path / "own-outside"
    outside.mkdir()
    (outside / "attrs").write_text("*.txt filter=evil\n")
    g(src, "config", "core.attributesFile", "attrdir/attrs")
    os.symlink(outside, src / "attrdir")
    (src / "a.txt").write_text("changed\n")
    index_before = sha256(src / ".git" / "index")
    with outside_opens(outside) as seen:
        with pytest.raises(closed_git.GitRefused, match="core.attributesFile"):
            closed_git.raw_status(src)
        with pytest.raises(closed_git.GitRefused, match="core.attributesFile"):
            closed_git.commit_all(src, "never through the link")
    assert seen == [] and ran(markers) == []
    assert sha256(src / ".git" / "index") == index_before
    # The same relative attributes file in a real directory is read as data.
    os.unlink(src / "attrdir")
    (src / "attrdir").mkdir()
    (src / "attrdir" / "attrs").write_text("*.txt filter=evil\n")
    raw = closed_git.raw_status(src)
    assert raw.changes["a.txt"] == " M"
    with pytest.raises(closed_git.GitRefused, match="filter drivers"):
        closed_git.commit_all(src, "filter selected through the attributes file")
    assert ran(markers) == []
