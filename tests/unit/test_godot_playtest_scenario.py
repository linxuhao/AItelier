"""Tests for godot_playtest_scenario — the single-scenario play-test probe.

The probe exists so an implementer can ask "is the scenario I was sent to repair
green yet?" without ending its step and waiting ~10 minutes for the full
26-scenario gate. These tests pin the three things that make the answer
trustworthy: it runs the scenario the caller named (and refuses a name it does
not have, rather than running the recognised remainder), it tests the caller's
current worktree edits without a second copy of code, and it surfaces `observed`
for every failing assertion. The sidecar is mocked throughout.
"""

import json

import pytest
import yaml

from aitelier.tools.godot_playtest_scenario.impl import godot_playtest_scenario


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Never access a real data directory during tests."""
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


def test_it_playtests_uncommitted_code_in_the_same_worktree(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path / "repo")
    (repo / "scripts/combat.gd").write_text("# NEW\n")
    (repo / "scripts/hud.gd").write_text("# unchanged\n")
    captured = {}; _fake_builder(monkeypatch, captured)
    # Copying any code directory is now a regression, not how the test works.
    def no_copy(*a, **kw):
        raise AssertionError("playtest must not copy the code worktree")
    monkeypatch.setattr("shutil.copytree", no_copy)
    got = godot_playtest_scenario(scenario="alpha", project_root=str(repo), step_id="t_impl")
    assert captured["body"]["project_dir"] == str(repo)
    assert got["code_root"] == str(repo)
    assert (repo / "scripts/combat.gd").read_text() == "# NEW\n"
    assert (repo / "scripts/hud.gd").read_text() == "# unchanged\n"
    assert not (tmp_path / "home/scratch").exists()


def test_current_scenario_and_code_are_read_from_the_same_root(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path / "repo")
    (repo / "playtest/alpha.yaml").write_text(yaml.safe_dump(
        {"name": "alpha", "timeline": [{"at": 99, "assert": {"REWRITTEN": "turns_taken == 2"}}]},
        sort_keys=False))
    captured = {}; _fake_builder(monkeypatch, captured)
    got = godot_playtest_scenario(scenario="alpha", project_root=str(repo))
    assert got["code_root"] == captured["body"]["project_dir"] == str(repo)
    row = captured["body"]["spec"]["scenarios"][0]["timeline"][0]
    assert row["at"] == 99 and "REWRITTEN" in row["assert"]


@pytest.mark.parametrize("filename", ["scripts/combat.gd", "playtest/alpha.yaml", "_deletions.json"])
def test_legacy_code_drafts_are_refused_not_silently_tested_as_baseline(tmp_path, monkeypatch, filename):
    repo = _make_repo(tmp_path / "repo")
    legacy = tmp_path / "old-stage"; f = legacy / filename
    f.parent.mkdir(parents=True, exist_ok=True); f.write_text("pending old output")
    captured = {}; _fake_builder(monkeypatch, captured)
    got = godot_playtest_scenario(scenario="alpha", project_root=str(repo), step_tmp_dir=str(legacy), legacy_code_staging=True)
    assert "Legacy" in got["error"]
    assert "body" not in captured, "must not claim a green test of the wrong code"
    assert f.read_text() == "pending old output"


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
    assert "GODOT_BUILDER_URL" in out["error"]


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


def test_inline_scenario_with_a_repeated_key_is_refused_before_the_builder(
        tmp_path, monkeypatch):
    """An inline probe repeating `assert:` would otherwise run its LAST block
    only, and report that as the whole probe."""
    repo = _make_repo(tmp_path / "repo")
    captured = {}
    _fake_builder(monkeypatch, captured)
    out = godot_playtest_scenario(
        project_root=str(repo),
        inline_scenario=("timeline:\n- at: 7\n  assert:\n    Foo.bar: bar == -1\n"
                         "  assert:\n    Foo.bar: bar == bar\n"))
    assert "duplicate key" in out.get("error", ""), out
    assert "assert" in out["error"] and "inline_scenario" in out["error"]
    assert "body" not in captured, "the sidecar must not be asked to run it"



def test_artifact_drafts_are_not_mistaken_for_code_staging(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path / "repo")
    artifacts = tmp_path / "plan.tmp"
    artifacts.mkdir()
    (artifacts / "plan.md").write_text("An artifact, not a source overlay")
    captured = {}
    _fake_builder(monkeypatch, captured)
    result = godot_playtest_scenario(scenario="alpha", project_root=str(repo),
                                     step_tmp_dir=str(artifacts), output_target="artifact")
    assert "error" not in result, result
    assert captured["body"]["project_dir"] == str(repo)
    assert (artifacts / "plan.md").read_text() == "An artifact, not a source overlay"
