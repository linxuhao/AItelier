"""Real SQLite/process exclusion for the one exclusive admission kind."""
import multiprocessing
from pathlib import Path
import subprocess

import pytest

from core.db_manager import DBManager
from core import run_isolation as ri


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "source"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "nested").mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    return DBManager(str(tmp_path / "host.db")), root, alias


@pytest.mark.parametrize("ordinary", ["bash", "edit_file", "repo_commit", "future_kind"])
@pytest.mark.parametrize("coding_first", [True, False])
def test_both_arrival_orders_refuse_without_a_partial_row(checkout, ordinary, coding_first):
    db, root, alias = checkout
    first, second = ("coding_workspace", ordinary) if coding_first else (ordinary, "coding_workspace")
    admitted = ri.admit_write(db, alias / "nested", kind=first, detail="same-run source handoff")
    before = ri.write_admissions(db)
    with pytest.raises(ri.CheckoutLeased) as refused:
        ri.admit_write(db, root / "nested" / "..", kind=second, detail="same-run source handoff")
    assert str(admitted["id"]) in str(refused.value) and first in str(refused.value)
    assert ri.write_admissions(db) == before
    assert admitted["canonical_checkout"] == str(root.resolve())
    ri.retire_write(db, admitted["id"])
    replacement = ri.admit_write(db, root, kind=second)
    assert replacement["id"] > admitted["id"]
    ri.retire_write(db, replacement["id"])
    assert ri.write_admissions(db) == []


def test_exclusive_kind_is_not_reentrant_in_the_same_process(checkout):
    db, root, alias = checkout
    one = ri.admit_write(db, root, kind="coding_workspace", detail="same-run")
    before = ri.write_admissions(db)
    with pytest.raises(ri.CheckoutLeased):
        ri.admit_write(db, alias, kind="coding_workspace", detail="same-run")
    assert ri.write_admissions(db) == before
    assert before[0]["owner"] == one["owner"]


def test_generic_writers_remain_compatible_and_retire_only_their_row(checkout):
    db, root, alias = checkout
    generic = [ri.admit_write(db, root, kind=kind) for kind in ("bash", "edit_file", "bash")]
    assert len({row["id"] for row in generic}) == 3
    with pytest.raises(ri.CheckoutLeased):
        ri.admit_write(db, alias, kind="coding_workspace")
    ri.retire_write(db, generic[1]["id"])
    assert [row["id"] for row in ri.write_admissions(db)] == [generic[0]["id"], generic[2]["id"]]
    with pytest.raises(ri.CheckoutLeased):
        ri.admit_write(db, root, kind="coding_workspace")


def test_direct_lease_interoperability_is_unchanged_in_both_orders(checkout):
    db, root, alias = checkout
    canonical = ri.canonical_checkout(alias)
    ri._acquire_lease(db, canonical, "same-run", "owned-fixture")
    with pytest.raises(ri.CheckoutLeased):
        ri.admit_write(db, root, kind="coding_workspace", detail="same-run")
    assert ri.write_admissions(db) == []
    assert ri.lease_holder(db, canonical)["run_id"] == "same-run"
    ri.release(db, "same-run", "owned fixture settled")
    one = ri.admit_write(db, alias, kind="coding_workspace", detail="same-run")
    before = ri.write_admissions(db)
    with pytest.raises(ri.CheckoutLeased):
        ri._acquire_lease(db, canonical, "same-run", "owned-fixture")
    assert ri.lease_holder(db, canonical) is None
    assert ri.write_admissions(db) == before
    ri.retire_write(db, one["id"])
    ri._acquire_lease(db, canonical, "same-run", "owned-fixture")
    ri.release(db, "same-run", "owned fixture settled")


def _contender(db_path, root, kind, barrier, release, results):
    try:
        db = DBManager(db_path)
        barrier.wait(15)
        if kind == "direct-lease":
            ri._acquire_lease(db, ri.canonical_checkout(root), "owned-direct", "fixture")
            row = {"kind": kind, "run_id": "owned-direct"}
        else:
            row = ri.admit_write(db, root, kind=kind, detail="owned concurrent process")
        results.put(("admitted", row))
        assert release.wait(15), "parent did not settle the owned fixture"
        if kind == "direct-lease":
            ri.release(db, "owned-direct", "owned concurrent fixture settled")
        else:
            ri.retire_write(db, row["id"])
    except ri.CheckoutLeased as exc:
        results.put(("refused", str(exc)))
    except BaseException as exc:
        results.put(("error", repr(exc)))


@pytest.mark.parametrize("kinds,separate,admitted_count", [
    (("coding_workspace", "bash"), False, 1),
    (("coding_workspace", "coding_workspace"), False, 1),
    (("coding_workspace", "direct-lease"), False, 1),
    (("bash", "edit_file"), False, 2),
    (("coding_workspace", "coding_workspace"), True, 2),
])
def test_simultaneous_processes_arbitrate_real_sqlite(checkout, tmp_path, kinds, separate, admitted_count):
    db, root, alias = checkout
    second = alias
    if separate:
        second = tmp_path / "other-source"
        subprocess.run(["git", "init", "-q", str(second)], check=True)
    ctx = multiprocessing.get_context("spawn")
    barrier, release, results = ctx.Barrier(3), ctx.Event(), ctx.Queue()
    children = [ctx.Process(target=_contender, args=(db.db_path, str(path), kind, barrier, release, results))
                for path, kind in zip((root, second), kinds)]
    try:
        for child in children:
            child.start()
        barrier.wait(15)
        observed = [results.get(timeout=20), results.get(timeout=20)]
        assert sum(state == "admitted" for state, _ in observed) == admitted_count, observed
        assert sum(state == "refused" for state, _ in observed) == 2 - admitted_count, observed
        held = ri.write_admissions(db)
        expected_ids = sorted(row["id"] for state, row in observed if state == "admitted" and row["kind"] != "direct-lease")
        assert [row["id"] for row in held] == expected_ids
        assert all(row["admitted_at"] and row["owner"] for row in held)
    finally:
        release.set()
        for child in children:
            child.join(20)
            if child.is_alive():
                child.terminate()
                child.join(5)
        results.close()
    assert all(child.exitcode == 0 for child in children)
    assert ri.write_admissions(db) == []
    assert ri.lease_holder(db, ri.canonical_checkout(root)) is None


def _leave_unsettled_admission(db_path, root, results):
    db = DBManager(db_path)
    results.put(ri.admit_write(db, root, kind="coding_workspace", detail="owned unknown effects"))


def test_process_exit_and_pending_marker_preserve_exact_row_until_explicit_retire(checkout):
    db, root, alias = checkout
    ctx = multiprocessing.get_context("spawn")
    results = ctx.Queue()
    child = ctx.Process(target=_leave_unsettled_admission, args=(db.db_path, str(root), results))
    child.start()
    try:
        row = results.get(timeout=20)
    finally:
        child.join(20)
        if child.is_alive():
            child.terminate()
            child.join(5)
        results.close()
    assert child.exitcode == 0
    reopened = DBManager(db.db_path)
    before = ri.write_admissions(reopened)[0]
    assert before["id"] == row["id"] and before["owner"] == row["owner"]
    ri.mark_write_admission_pending(reopened, row["id"], "fixture child exited; effect settlement still unknown")
    pending = ri.write_admissions(reopened)[0]
    assert {k: v for k, v in pending.items() if k != "detail"} == {k: v for k, v in before.items() if k != "detail"}
    assert before["detail"] in pending["detail"] and "PENDING WRITERS" in pending["detail"]
    for kind in ("bash", "coding_workspace"):
        with pytest.raises(ri.CheckoutLeased):
            ri.admit_write(reopened, alias, kind=kind)
    assert ri.write_admissions(reopened) == [pending]
    ri.retire_write(reopened, row["id"] + 1)
    assert ri.write_admissions(reopened) == [pending]
    ri.retire_write(reopened, row["id"])
    assert ri.write_admissions(reopened) == []


def test_owned_source_identity():
    assert Path(__file__).resolve().parents[2] == Path("/src")
    assert Path(ri.__file__).resolve() == Path("/src/core/run_isolation.py")
