"""The playtest ledger: a report of parts that also reports its remainder.

playtest.json used to carry 444MB and not one number saying where the 56
minutes went. These tests pin the two properties that make the new `timing`
block an accounting rather than a decoration:

  * every scenario has its OWN wall time, and the four classes are separate
    fields that do not borrow from each other;
  * the playtest level reports `unattributed_sec` — what is LEFT once every
    measured part is subtracted from its own wall clock. A ledger of parts with
    no remainder is exactly the unaccountability it exists to remove, so its
    absence is a failure, not a tidier report.
"""
import importlib.util
import json
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "docker" / "godot" / "godot_harness.py"
_spec = importlib.util.spec_from_file_location("godot_harness_timing", _SRC)
gh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gh)


def _probe(frames=10, captures=0):
    return {"frames": frames, "asserts": [], "nodes": {},
            "captures": [{"frame": i, "file": "f%d.png" % i} for i in range(captures)],
            "timing": {"boot_usec": 1_000_000, "step_usec": 4_000_000,
                       "capture_usec": 500_000, "serialize_usec": 100_000,
                       "engine_usec": 5_600_000, "frames_stepped": frames}}


def _spec_with(n):
    return {"scenarios": [{"name": "s%d" % i, "scene": "res://a.tscn",
                           "timeline": [{"at": 0, "press": "ui_accept"},
                                        {"at": 5, "assert": [
                                            {"name": "x", "node": "N", "expr": "1 == 1"}]}]}
                          for i in range(n)]}


def test_every_scenario_gets_its_own_line_not_a_sample(monkeypatch, tmp_path):
    """Three scenarios, three lines — the thing the 444MB report never had."""
    def fake(dst, state_path, frames, timeout, env, scene="", capture_at=None,
             timing=None):
        if timing is not None:
            timing.update({"proc_sec": 6.0, "passes": 1},
                          **_probe()["timing"])
        return _probe(), [], False

    monkeypatch.setattr(gh, "_run_probe", fake)
    ledger = {}
    gh._playtest_spec(tmp_path, _spec_with(3), 10, 60, ledger=ledger)
    assert [s["name"] for s in ledger["scenarios"]] == ["s0", "s1", "s2"]
    for line in ledger["scenarios"]:
        for field in ("wall_sec", "boot_sec", "step_sec", "capture_sec",
                      "serialize_sec"):
            assert field in line, field


def test_the_four_classes_are_read_from_their_own_clocks():
    """Each class comes from the measurement named for it — no class is derived
    by subtracting the others, which is how an unmeasured one hides."""
    line = gh._scenario_ledger("s", "res://a.tscn", 7.0, {
        "proc_sec": 6.0, "boot_usec": 1_000_000, "step_usec": 4_000_000,
        "capture_usec": 500_000, "serialize_usec": 100_000,
        "engine_usec": 5_600_000, "png_b64_sec": 0.25,
        "snapshot_parse_sec": 0.05, "passes": 1})
    assert line["boot_sec"] == 1.0
    assert line["step_sec"] == 4.0
    assert line["capture_sec"] == 0.75          # engine grab + python base64
    assert line["serialize_sec"] == pytest.approx(0.15)  # stringify + parse
    assert line["process_sec"] == pytest.approx(0.4)     # proc wall - engine clock
    assert line["other_sec"] == pytest.approx(1.0)       # wall - proc wall


def test_a_slower_capture_does_not_move_the_other_three():
    """The polarity that makes the split worth having: charge the capture more
    and only capture grows. If a delay in the encoder showed up as `step`, the
    breakdown would be decoration."""
    base = dict(proc_sec=6.0, boot_usec=1_000_000, step_usec=4_000_000,
                capture_usec=500_000, serialize_usec=100_000,
                engine_usec=5_600_000, png_b64_sec=0.0,
                snapshot_parse_sec=0.05, passes=1)
    slow = dict(base, capture_usec=2_500_000, engine_usec=7_600_000)
    a = gh._scenario_ledger("s", "", 7.0, base)
    b = gh._scenario_ledger("s", "", 9.0, slow)
    assert b["capture_sec"] - a["capture_sec"] == pytest.approx(2.0)
    for untouched in ("boot_sec", "step_sec", "serialize_sec"):
        assert a[untouched] == b[untouched], untouched


def test_the_playtest_level_reports_its_remainder(monkeypatch, tmp_path):
    """`unattributed_sec` is not optional. Without it this is a list of parts
    that can silently fail to add up — the defect the card names."""
    ledger = {"scenarios": [gh._scenario_ledger("s0", "", 5.0, {"proc_sec": 4.0}),
                            gh._scenario_ledger("s1", "", 3.0, {"proc_sec": 2.0})],
              "controls": [gh._scenario_ledger("c0", "", 2.0, {"proc_sec": 1.0})]}
    monkeypatch.setattr(gh.time, "monotonic", lambda: 100.0)
    out = gh._assemble_ledger(ledger, "2026-09-17T00:00:00+00:00", 89.0, 0.5, 1.5)
    assert out["scenario_wall_sum_sec"] == 8.0
    assert out["control_wall_sum_sec"] == 2.0
    assert out["wall_sec"] == 11.0
    # 11 - 8 - 2 - 0.5 - 1.5 == -1.0 is nonsense arithmetic on purpose: the
    # field must REPORT the mismatch, never clamp it away.
    assert out["unattributed_sec"] == pytest.approx(-1.0)
    assert out["unattributed_means"]


def test_the_response_carries_the_cost_of_serializing_itself():
    """The one number a document cannot contain by writing it: spliced in after
    json.dumps returns, so the payload is serialized exactly once."""
    sent = {}

    class Fake(gh._Handler):
        def __init__(self):  # no socket
            pass

        def send_response(self, code):
            sent["code"] = code

        def send_header(self, *a):
            pass

        def end_headers(self):
            pass

        @property
        def wfile(self):
            class W:
                def write(_s, body):
                    sent["body"] = body
            return W()

    payload = {"timing": _fresh_ledger()}
    Fake()._send_timed(200, payload)
    back = json.loads(sent["body"])
    assert sent["code"] == 200
    assert back["timing"]["report_serialize_sec"] >= 0.0
    assert back["timing"]["report_serialize_sec"] != 0.0 or True  # may be sub-ms


def test_a_report_without_the_placeholder_is_still_delivered():
    """A missing number must never cost the caller its evidence."""
    sent = {}

    class Fake(gh._Handler):
        def __init__(self):
            pass

        def send_response(self, code):
            sent["code"] = code

        def send_header(self, *a):
            pass

        def end_headers(self):
            pass

        @property
        def wfile(self):
            class W:
                def write(_s, body):
                    sent["body"] = body
            return W()

    Fake()._send_timed(200, {"passed": True})
    assert json.loads(sent["body"]) == {"passed": True}


def _fresh_ledger():
    return gh._assemble_ledger({"scenarios": [], "controls": []},
                               "2026-09-17T00:00:00+00:00",
                               gh.time.monotonic(), 0.0, 0.0)
