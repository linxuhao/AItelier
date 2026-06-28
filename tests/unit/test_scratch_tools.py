# tests/unit/test_scratch_tools.py
# Scratch tools give agents a throwaway per-step workspace so they stop writing
# junk (e.g. _test_write.txt) into the project repo.
# aliased: importing it as `test_write` would make pytest collect the tool
# function itself as a test case.
from aitelier.tools.test_write.impl import test_write as scratch_write
from aitelier.tools.read_test_written.impl import read_test_written


def test_write_then_read_roundtrip():
    w = scratch_write("notes.txt", "hello scratch", step_id="t_impl", run_id="r1")
    assert w["ok"] is True
    assert "written" not in w          # must NOT be counted as a deliverable
    assert w["scratch_written"] == "notes.txt"
    r = read_test_written("notes.txt", step_id="t_impl", run_id="r1")
    assert r["content"] == "hello scratch"


def test_isolated_per_step():
    scratch_write("x.txt", "from step A", step_id="step_A", run_id="r1")
    # a different step must not see step_A's scratch
    r = read_test_written("x.txt", step_id="step_B", run_id="r1")
    assert "error" in r


def test_read_missing_returns_error():
    r = read_test_written("does_not_exist.txt", step_id="t_impl", run_id="r1")
    assert "error" in r


def test_path_traversal_rejected():
    w = scratch_write("../escape.txt", "nope", step_id="t_impl", run_id="r1")
    assert "error" in w
