"""A blind readability gate must be handed to someone who can see, not looped.

The gate says `passed: false` in two situations that call for opposite
responses — the game IS unreadable (fix it) and NOBODY LOOKED (find someone who
can). Both used to route back to the PM. On 2026-08-26 the judge dropped after
4 of 47 scenarios, all at full HP; the PM was handed that as the readability
verdict, planned a health-bar rewrite citing an injured-frame answer no model
had ever produced, and the round died on `Cycle limit exceeded` — because no
amount of re-planning makes an unreachable judge answer.
"""
import json

import pytest
import yaml

from pathlib import Path

from aitelier.tools.vision_human_pass.impl import vision_human_pass

ROOT = Path(__file__).resolve().parents[2]
ADDON = yaml.safe_load(
    (ROOT / "configs" / "addons" / "game_harness.yaml").read_text(encoding="utf-8"))


def _steps():
    out = {}
    for op in ADDON.get("overlay") or []:
        for s in (op.get("steps") or []):
            out[s["id"]] = s
    return out


def _blind_report(tmp_path, step="5_vision", blind=True):
    d = tmp_path / "dpe_game" / step
    d.mkdir(parents=True)
    (d / "vision_report.json").write_text(json.dumps({
        "passed": False, "blind": blind, "blind_reason": "endpoint_unreachable",
        "summary": "The frames were NOT judged. Vision gate NOT run.",
        "calls": 4, "scenarios": 47,
    }), encoding="utf-8")
    return d / "vision_report.json"


# ── the routing ──────────────────────────────────────────────────────────────

def test_a_blind_gate_routes_to_a_checkpoint_not_onward():
    v = _steps()["5_vision"]
    blind_edge = [t for t in v["transitions"]
                  if (t.get("match") or {}).get("field") == "blind"]
    assert blind_edge, "5_vision no longer branches on `blind`"
    assert blind_edge[0]["match"]["value"] is True
    assert _steps()[blind_edge[0]["to"]].get("checkpoint") is True


def test_a_gate_that_could_see_is_not_sent_to_a_human():
    """The checkpoint is for absence of evidence, not for a failing grade."""
    v = _steps()["5_vision"]
    default = [t for t in v["transitions"] if "match" not in t]
    assert default and default[0]["to"] == "5_knowledge"


def test_rejecting_the_checkpoint_goes_back_to_the_planner():
    assert _steps()["5_vision_human"]["checkpoint_reject_to"] == "3"


def test_the_reviewer_is_shown_the_gate_output_they_are_judging():
    """The checkpoint modal reads the checkpoint step's OWN dir."""
    s = _steps()["5_vision_human"]
    assert s["tool_name"] == "restage"
    assert "5_vision" in s["tool_params"]["from_steps"]


def test_the_verdict_is_stamped_only_after_approval():
    """A step's tool runs BEFORE that step pauses, so the stamp needs its own."""
    s = _steps()["5_vision_human"]
    approved = [t for t in s["transitions"]
                if (t.get("match") or {}).get("from") == "checkpoint"]
    assert approved and approved[0]["match"]["value"] == "approved"
    assert _steps()[approved[0]["to"]]["tool_name"] == "vision_human_pass"


# ── the stamp ────────────────────────────────────────────────────────────────

def test_approval_turns_the_gate_green(tmp_path):
    rp = _blind_report(tmp_path)
    res = vision_human_pass(workspace_root=str(tmp_path), config_name="dpe_game",
                            out_dir=str(tmp_path / "out"))
    assert res["passed"] is True and res["judged_by"] == "human"
    assert json.loads(rp.read_text())["passed"] is True


def test_it_never_erases_the_fact_that_the_model_was_blind(tmp_path):
    """'A person judged instead' must not become 'the gate was green all along'."""
    rp = _blind_report(tmp_path)
    vision_human_pass(workspace_root=str(tmp_path), config_name="dpe_game")
    got = json.loads(rp.read_text())
    assert got["blind"] is True
    assert got["blind_reason"] == "endpoint_unreachable"
    assert got["judged_by"] == "human"


def test_it_refuses_to_overwrite_a_verdict_the_model_actually_reached(tmp_path):
    """Routed here by mistake, it must not launder a real failure into a pass."""
    rp = _blind_report(tmp_path, blind=False)
    res = vision_human_pass(workspace_root=str(tmp_path), config_name="dpe_game")
    assert "error" in res
    assert json.loads(rp.read_text())["passed"] is False


def test_a_missing_report_is_an_error_not_a_fabricated_pass(tmp_path):
    (tmp_path / "dpe_game" / "5_vision").mkdir(parents=True)
    res = vision_human_pass(workspace_root=str(tmp_path), config_name="dpe_game")
    assert "error" in res and "no vision_report.json" in res["error"]


def test_downstream_readers_see_one_verdict(tmp_path):
    """5_review reads {step: 5_vision} as a whole dir — the amend lands there."""
    rp = _blind_report(tmp_path)
    out = tmp_path / "out"
    vision_human_pass(workspace_root=str(tmp_path), config_name="dpe_game",
                      out_dir=str(out))
    assert json.loads(rp.read_text())["passed"] is True
    assert json.loads((out / "vision_report.json").read_text())["passed"] is True
