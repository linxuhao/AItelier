"""A play-test that evaluated zero assertions is not a pass.

Live 2026-09-04 23:08-23:12: a builder image change left Godot's HOME
root-owned, the engine could not write its user data, and every sweep came
back with the full scenario count, ZERO assertions, ZERO captures and
`passed: true`. Three trees "passed" while measuring nothing. The scenario
count comes from the spec we send; only the assertions come from the game.
"""
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from aitelier.tools.godot_playtest import impl  # noqa: E402


def _run(monkeypatch, tmp_path, report):
    proj = tmp_path / "proj"; (proj / "playtest").mkdir(parents=True)
    (proj / "project.godot").write_text("[application]\n")
    monkeypatch.setattr(impl, "read_spec",
                        lambda repo: ({"scenarios": [{"name": "a"}]},
                                      {"source": "playtest/", "notes": [], "errors": []}))
    monkeypatch.setattr(impl, "post_playtest", lambda payload, timeout=3600: dict(report))
    out = tmp_path / "out"
    impl.godot_playtest(project_root=str(proj), out_dir=str(out))
    return json.loads((out / "playtest_report.json").read_text())


BLIND = {"passed": True, "spec_used": True, "errors": [], "captures": [],
         "behavior": {"scenarios": [{"name": "a", "passed": True, "asserts": []},
                                    {"name": "b", "passed": True, "asserts": []}]}}


def test_zero_assertions_is_not_a_pass(monkeypatch, tmp_path):
    r = _run(monkeypatch, tmp_path, BLIND)
    assert r["passed"] is False
    assert r["blind_builder"] is True
    assert "ZERO assertions" in r["summary"]


def test_the_summary_says_how_little_was_measured(monkeypatch, tmp_path):
    r = _run(monkeypatch, tmp_path, BLIND)
    assert "2 scenario(s)" in r["summary"] and "0 captures" in r["summary"]


def test_a_real_run_is_untouched(monkeypatch, tmp_path):
    good = {"passed": True, "spec_used": True, "errors": [], "captures": [{"f": 1}],
            "behavior": {"scenarios": [{"name": "a", "passed": True,
                                        "asserts": [{"passed": True}]}]}}
    r = _run(monkeypatch, tmp_path, good)
    assert r["passed"] is True and not r.get("blind_builder")


def test_a_skipped_gate_is_not_relabelled(monkeypatch, tmp_path):
    # spec_used False → the guard must not fire; the skip keeps its own shape.
    skipped = {"passed": True, "spec_used": False, "errors": [], "captures": [],
               "behavior": None, "gate_skipped": True}
    r = _run(monkeypatch, tmp_path, skipped)
    assert r.get("gate_skipped") is True and not r.get("blind_builder")
