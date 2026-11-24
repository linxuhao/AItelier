"""Guards for the shared provisioned baseline behind the steward channel tests.

The baseline is provisioned once; a world hands each test a private copy. These
tests are the proof that the copy is real: two tests cannot see one another's
State file or driver home directory, and a row written through a test's
StateService is absent from a freshly requested world.
"""
from __future__ import annotations

import pytest

from tests.unit.test_steward_channels import _build_world, world, world_baseline  # noqa: F401


@pytest.fixture(scope="module")
def _seen_worlds():
    """Facts from earlier tests in this module, to compare against this test's world."""
    return {"paths": set(), "token_files": set()}


@pytest.mark.parametrize("slot", (1, 2))
def test_each_world_owns_a_distinct_state_file(slot, world, world_baseline, _seen_worlds):
    assert slot in (1, 2)
    token_file = world.runtime.root / world.steward_row["driver_id"] / "token"
    assert world.db.db_path not in _seen_worlds["paths"], "two tests were handed the same State file"
    assert token_file not in _seen_worlds["token_files"], "two tests share a driver home directory"
    assert token_file.parent.parent != world_baseline.runtime.root, "a test wrote into the baseline's driver home"
    assert token_file.read_text().strip() == world.tokens["steward"]
    assert world.registry.db is world.db and world.runtime.db is world.db
    _seen_worlds["paths"].add(world.db.db_path)
    _seen_worlds["token_files"].add(token_file)


def test_a_row_written_in_one_world_is_absent_from_a_freshly_requested_world(
        world, world_baseline, tmp_path, monkeypatch):
    project = "guard-sentinel"
    world.owner.store.create_project(project, "Guard sentinel")
    with world.db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM state_projects WHERE project_id=?", (project,)
                            ).fetchone()[0] == 1

    fresh = _build_world(tmp_path / "fresh", world_baseline, monkeypatch)
    assert fresh.db.db_path != world.db.db_path
    with fresh.db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM state_projects WHERE project_id=?", (project,)
                            ).fetchone()[0] == 0
    assert fresh.owner.store.get_project("aitelier-home"), "the fresh copy still has the provisioned baseline"
