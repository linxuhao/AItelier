"""Tests for godot_playtest_scenario — the single-scenario play-test probe.

The probe exists so an implementer can ask "is the scenario I was sent to repair
green yet?" without ending its step and waiting ~10 minutes for the full
26-scenario gate. These tests pin the three things that make the answer
trustworthy: it runs the scenario the caller named (and refuses a name it does
not have, rather than running the recognised remainder), it tests the caller's
STAGED edits rather than the code they are replacing, and it surfaces `observed`
for every failing assertion. The sidecar is mocked throughout.
"""

import json

import pytest
import yaml

from aitelier.tools.godot_playtest_scenario.impl import godot_playtest_scenario


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """The probe stages its throwaway tree under the AItelier data dir. Never
    the real one."""
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))


def _make_repo(root, scenarios=("alpha", "beta")):
    root.mkdir(parents=True, exist_ok=True)
    (root / "project.godot").write_text("config_version=5\n")
    (root / "scripts").mkdir()
    (root / "scripts" / "combat.gd").write_text("# old\n")
    d = root / "playtest"
    d.mkdir()
    (d / "_common.yaml").write_text(yaml.safe_dump(
        {"scene": "res://main.tscn", "scenario_order": list(scenarios)},
        sort_keys=False))
    for n in scenarios:
        (d / f"{n}.yaml").write_text(yaml.safe_dump(
            {"name": n, "timeline": [{"at": 10, "assert": {"x": "1"}}]},
            sort_keys=False))
    return root


def _fake_builder(monkeypatch, captured, behavior=None):
    class _R:
        def __init__(self, payload):
            self._p = payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(self._p).encode()

    def fake_urlopen(req, timeout=0):
        captured["body"] = json.loads(req.data.decode())
        return _R({"passed": True, "spec_used": True, "frames": 40,
                   "errors": [], "state": {}, "summary": "ran clean",
                   "behavior": behavior or {"all_passed": True, "scenarios": [
                       {"name": "alpha", "passed": True, "errors": [],
                        "asserts": [{"name": "x", "passed": True}]}]}})

    monkeypatch.setattr(
        "aitelier.tools.godot_playtest.impl.urllib.request.urlopen", fake_urlopen)


def test_it_sends_only_the_named_scenario(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path / "repo")
    captured = {}
    _fake_builder(monkeypatch, captured)
    out = godot_playtest_scenario(scenario="alpha", project_root=str(repo))
    sent = captured["body"]["spec"]
    assert [s["name"] for s in sent["scenarios"]] == ["alpha"]
    # the shared header rides along — without `scene` the probe boots the wrong scene
    assert sent["scene"] == "res://main.tscn"
    assert out["all_passed"] is True


def test_several_scenarios_comma_separated(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path / "repo")
    captured = {}
    _fake_builder(monkeypatch, captured)
    godot_playtest_scenario(scenario="beta, alpha", project_root=str(repo))
    assert [s["name"] for s in captured["body"]["spec"]["scenarios"]] == ["beta", "alpha"]


def test_an_unknown_name_is_refused_with_the_real_list(tmp_path, monkeypatch):
    """Running the recognised subset would let a typo read as "the scenario I
    asked about is green"."""
    repo = _make_repo(tmp_path / "repo")
    captured = {}
    _fake_builder(monkeypatch, captured)
    out = godot_playtest_scenario(scenario="alpha,typo", project_root=str(repo))
    assert "typo" in out["error"] and "alpha" in out["error"]
    assert "body" not in captured, "the sidecar must not be asked to run a subset"


def test_it_playtests_the_staged_edits_not_the_repo(tmp_path, monkeypatch):
    """A t_impl agent's change lives in staging until on_deliver repo_apply.
    Testing project_root directly would test the code being replaced and report
    on it as if it were the fix."""
    repo = _make_repo(tmp_path / "repo")
    staging = tmp_path / "stage"
    (staging / "scripts").mkdir(parents=True)
    (staging / "scripts" / "combat.gd").write_text("# NEW\n")
    captured = {}
    _fake_builder(monkeypatch, captured)
    out = godot_playtest_scenario(scenario="alpha", project_root=str(repo),
                                  step_tmp_dir=str(staging), step_id="t_impl")
    tested = captured["body"]["project_dir"]
    assert tested != str(repo), "the probe ran against the undelivered repo"
    assert out["staged_files_applied"] == ["scripts/combat.gd"]
    # …and the reader is told, in the report text, what was actually tested.
    assert "scripts/combat.gd" in out["report"]


def test_a_staged_SCENARIO_is_the_one_evaluated_not_the_repo_baseline(tmp_path,
                                                                      monkeypatch):
    """Staging a scenario file must change the contract that gets evaluated.

    The probe passes the spec to the sidecar EXPLICITLY, so reading it from the
    repo while pointing project_dir at the overlaid tree ran the staged code
    against the BASELINE scenario — and still answered
    `staged_files_applied: [that scenario file]`. It only bites when the staged
    file IS a scenario, which is why the sibling test above missed it: that one
    stages a .gd, and code overlays worked fine.

    Live, jinyong-winnable 2026-08-24, first agent to use the tool in anger:
    "the sidecar listed the staged file as applied but the evaluated assert
    expressions were the repo-baseline (OLD) ones". Four of that round's five
    task cards edit scenario files.
    """
    repo = _make_repo(tmp_path / "repo")
    staging = tmp_path / "stage"
    (staging / "playtest").mkdir(parents=True)
    (staging / "playtest" / "alpha.yaml").write_text(yaml.safe_dump(
        {"name": "alpha",
         "timeline": [{"at": 99, "assert": {"REWRITTEN": "turns_taken == 2"}}]},
        sort_keys=False))
    captured = {}
    _fake_builder(monkeypatch, captured)

    out = godot_playtest_scenario(scenario="alpha", project_root=str(repo),
                                  step_tmp_dir=str(staging), step_id="t_impl")

    assert out["staged_files_applied"] == ["playtest/alpha.yaml"]
    sent = captured["body"]["spec"]["scenarios"]
    assert len(sent) == 1 and sent[0]["name"] == "alpha"
    row = sent[0]["timeline"][0]
    assert row["at"] == 99 and "REWRITTEN" in row["assert"], (
        "the sidecar was sent the repo-baseline scenario while the report "
        f"claimed the staged one was applied: {row}")


def test_staged_overlay_carries_the_untouched_repo_files_too(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path / "repo")
    (repo / "scripts" / "hud.gd").write_text("# hud\n")
    staging = tmp_path / "stage"
    (staging / "scripts").mkdir(parents=True)
    (staging / "scripts" / "combat.gd").write_text("# NEW\n")
    captured = {}
    seen = {}

    class _R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"passed": True, "behavior": {"scenarios": []},
                               "summary": ""}).encode()

    def fake_urlopen(req, timeout=0):
        body = json.loads(req.data.decode())
        captured["body"] = body
        from pathlib import Path as P
        d = P(body["project_dir"])
        # inspect the assembled tree WHILE it exists (it is cleaned up after)
        seen["combat"] = (d / "scripts" / "combat.gd").read_text()
        seen["hud"] = (d / "scripts" / "hud.gd").exists()
        seen["project_godot"] = (d / "project.godot").exists()
        return _R()

    monkeypatch.setattr(
        "aitelier.tools.godot_playtest.impl.urllib.request.urlopen", fake_urlopen)
    godot_playtest_scenario(scenario="alpha", project_root=str(repo),
                            step_tmp_dir=str(staging), step_id="t_impl")
    assert seen["combat"] == "# NEW\n"
    assert seen["hud"] and seen["project_godot"]


def test_the_deletions_manifest_never_reaches_the_tested_tree(tmp_path, monkeypatch):
    """`_deletions.json` is a control file the step's repo_apply ignores; it is
    not project content."""
    repo = _make_repo(tmp_path / "repo")
    staging = tmp_path / "stage"
    staging.mkdir()
    (staging / "_deletions.json").write_text("[]")
    captured = {}
    _fake_builder(monkeypatch, captured)
    out = godot_playtest_scenario(scenario="alpha", project_root=str(repo),
                                  step_tmp_dir=str(staging), step_id="t_impl")
    assert out["staged_files_applied"] == []
    # nothing to overlay → the real repo is tested directly, no copy made
    assert captured["body"]["project_dir"] == str(repo)


def test_failing_assertions_report_the_observed_value(tmp_path, monkeypatch):
    """`actual` on a comparison assert is `false` — it says the assert did not
    hold and nothing about what broke it. `observed` is the number that does."""
    repo = _make_repo(tmp_path / "repo")
    captured = {}
    _fake_builder(monkeypatch, captured, behavior={"all_passed": False, "scenarios": [
        {"name": "alpha", "passed": False, "errors": [],
         "asserts": [{"name": "East_Heretic.turns_taken", "passed": False,
                      "frame": 1200, "expr": "turns_taken == 1",
                      "actual": False, "observed": 2}]}]})
    out = godot_playtest_scenario(scenario="alpha", project_root=str(repo))
    assert out["all_passed"] is False
    assert out["scenarios"] == [{"name": "alpha", "passed": False, "ok": 0, "total": 1}]
    assert "observed=2" in out["report"]
    assert "turns_taken == 1" in out["report"]


def test_it_falls_back_to_the_monolithic_spec(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "project.godot").write_text("config_version=5\n")
    (repo / "playtest_spec.yaml").write_text(
        "scene: res://main.tscn\nscenarios:\n  - name: alpha\n    timeline: []\n")
    captured = {}
    _fake_builder(monkeypatch, captured)
    out = godot_playtest_scenario(scenario="alpha", project_root=str(repo))
    assert [s["name"] for s in captured["body"]["spec"]["scenarios"]] == ["alpha"]
    assert "playtest_spec.yaml" in out["report"]


def test_a_non_godot_project_is_an_error_not_a_pass(tmp_path):
    out = godot_playtest_scenario(scenario="alpha", project_root=str(tmp_path))
    assert "not a Godot project" in out["error"]


def test_a_repo_with_no_contract_is_an_error(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "project.godot").write_text("config_version=5\n")
    out = godot_playtest_scenario(scenario="alpha", project_root=str(repo))
    assert "No play-test contract" in out["error"]


def test_an_unreachable_builder_is_an_error_not_a_green_probe(tmp_path, monkeypatch):
    """The full gate degrades an unreachable sidecar to a LOUD gate_skipped
    PASS so infra never stalls a run. A probe has no such duty: answering
    "nothing failed" when nothing ran is the worst thing it could do."""
    repo = _make_repo(tmp_path / "repo")

    def boom(req, timeout=0):
        raise OSError("connection refused")

    monkeypatch.setattr(
        "aitelier.tools.godot_playtest.impl.urllib.request.urlopen", boom)
    out = godot_playtest_scenario(scenario="alpha", project_root=str(repo))
    assert "unreachable" in out["error"]


# The tool used to take a scenario NAME only, so forcing `observed` values out
# meant writing a throwaway scenario into playtest/ — the deliverable directory
# — and remembering to delete it. jinyong-endgame 2026-08-24: four of six cards
# shipped or re-shipped probe scaffolding that way (one across three
# rejections, one delivering nothing else), and because the loader runs unlisted
# scenario files, a forgotten probe reddens the WHOLE gate. inline_scenario
# removes the file from the loop.

def test_inline_scenario_runs_without_touching_the_repo(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path / "repo")
    before = sorted(p.name for p in (repo / "playtest").iterdir())
    captured = {}
    _fake_builder(monkeypatch, captured)

    out = godot_playtest_scenario(
        project_root=str(repo),
        inline_scenario="timeline:\n- at: 7\n  assert:\n    Foo.bar: bar == -1\n")

    assert "error" not in out, out
    sent = captured["body"]["spec"]["scenarios"]
    assert len(sent) == 1 and sent[0]["timeline"][0]["at"] == 7
    assert sent[0]["name"] == "inline_probe"          # named for you
    # The shared header still comes from the project's _common.yaml.
    assert captured["body"]["spec"]["scene"] == "res://main.tscn"
    # And nothing was written into the contract directory.
    assert sorted(p.name for p in (repo / "playtest").iterdir()) == before


def test_inline_scenario_accepts_the_scenarios_wrapper(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path / "repo")
    captured = {}
    _fake_builder(monkeypatch, captured)
    out = godot_playtest_scenario(
        project_root=str(repo),
        inline_scenario=("scenarios:\n- name: probe_a\n  timeline:\n"
                         "  - at: 1\n    assert: {A.b: 'b == -1'}\n"))
    assert "error" not in out, out
    assert [s["name"] for s in captured["body"]["spec"]["scenarios"]] == ["probe_a"]


def test_inline_scenario_without_a_timeline_is_refused(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path / "repo")
    _fake_builder(monkeypatch, {})
    out = godot_playtest_scenario(project_root=str(repo),
                                  inline_scenario="name: nope\n")
    assert "timeline" in out.get("error", ""), out


def test_neither_scenario_nor_inline_is_refused(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path / "repo")
    _fake_builder(monkeypatch, {})
    out = godot_playtest_scenario(project_root=str(repo))
    assert "inline_scenario" in out.get("error", ""), out


def test_both_scenario_and_inline_is_refused(tmp_path, monkeypatch):
    """Ambiguity here would silently pick one and report on the other."""
    repo = _make_repo(tmp_path / "repo")
    _fake_builder(monkeypatch, {})
    out = godot_playtest_scenario(project_root=str(repo), scenario="alpha",
                                  inline_scenario="timeline: []\n")
    assert "not both" in out.get("error", ""), out
