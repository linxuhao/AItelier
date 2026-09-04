"""A gate that declines to run must leave a trace that outlives the run.

skillflow's validator reads `passed`/`all_passed` and drops every other key,
so `gate_skipped: true` beside `passed: true` reaches no reader. Live
2026-09-04 23:08: a play-test sweep taken while the builder was being
recreated answered passed:true / gate_skipped:true / spec_used:false /
frames:0, and ~/.AItelier/logs/gate_skips.log had no entry for it — the gate
vanished as a pass, which is the defect gate_skip_log.py exists to prevent.
"""
import ast
import pathlib

TOOLS = pathlib.Path(__file__).resolve().parents[2] / "aitelier/tools"
SKIPPING_GATES = ("godot_playtest", "godot_compile", "gdscript_check")


def _src(tool):
    return (TOOLS / tool / "impl.py").read_text(encoding="utf-8")


def test_every_gate_that_can_skip_records_the_skip():
    for tool in SKIPPING_GATES:
        s = _src(tool)
        assert "gate_skipped" in s or "all_passed" in s, f"{tool}: not a skipping gate any more?"
        assert "log_gate_skip(" in s, (
            f"{tool} can return a skip but never calls log_gate_skip — the flag "
            f"is dropped by skillflow's validator, so the skip would vanish")


def test_the_skip_is_logged_before_the_return_that_passes():
    # Order matters: a `return` above the log means the log never runs.
    for tool in ("godot_playtest", "godot_compile"):
        s = _src(tool)
        i = s.index("log_gate_skip(")
        j = s.index("gate_skipped", i)
        assert j > i, f"{tool}: log_gate_skip must come before the gate_skipped return"


def test_the_call_names_the_gate_and_a_reason():
    tree = ast.parse(_src("godot_playtest"))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "log_gate_skip"]
    assert calls, "no log_gate_skip call parsed"
    for c in calls:
        assert len(c.args) >= 2, "log_gate_skip(gate, reason, **detail)"
        assert isinstance(c.args[0], ast.Constant) and c.args[0].value == "godot_playtest"
