# tests/unit/test_node_gate_measured_semantics.py
# NON-AUTHOR REVIEW PROBE, adopted into the suite (#
# Every claim below is driven through `run_tests` -> `_run_node_checks` ->
# `_run_node_project` -> `_apply_baseline`, i.e. the real entry. The ONLY seam
# is `repository_executor.execute` (the process boundary where npm/pytest
# would be launched): the node commands are served in-process per
# (project, command), and the pytest leg is served by the project's own
# `tests/gate_fixture.install_entry_executor`, which runs the real
# `core.repository_executor_entry.run`. No assertion here calls
# `_aggregate_node_projects`, `Executed`, or `_baseline_dir`.
import json
import subprocess
from pathlib import Path

import aitelier.tools.run_tests.impl as rt
from aitelier.gate_evidence import report_state


def _report(out_dir):
    return json.loads((Path(out_dir) / "test_report.json").read_text())


def _project(directory, scripts=True):
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {"name": directory.name, "version": "0.0.0"}
    if scripts:
        manifest["scripts"] = {"build": "build-it", "test": "test-it"}
    (directory / "package.json").write_text(json.dumps(manifest))
    (directory / "package-lock.json").write_text("{}")


def _repo(tmp_path, projects=(("web", True), ("integrations/dsh", False))):
    """This repository's own layout: `web/` with tests, `integrations/dsh` with
    a package.json and no scripts at all."""
    repo = tmp_path / "repo"
    for rel, scripts in projects:
        _project(repo / rel, scripts)
    # One passing Python test, so the pytest leg MEASURES something: without it
    # the leg reports `no_tests_collected`, which is unrelated to the node
    # section under review and would make every `report_state` here "skipped".
    (repo / "tests").mkdir(parents=True, exist_ok=True)
    (repo / "tests" / "test_probe_ok.py").write_text(
        "def test_probe_ok():\n    assert True\n")
    return repo


def _project_of(workdir):
    path = str(workdir).replace("\\", "/")
    if "/src/" in path:
        return path.split("/src/", 1)[1]
    return "."


def _serve(tmp_path, monkeypatch, exit_codes=None, refuse=None):
    """Serve npm in-process, everything else really run.

    `exit_codes` maps (project, command-prefix) to a returncode; `refuse` is
    called with (project, profile, command) and models a project whose runner
    cannot be provisioned. The pytest leg goes through the real executor entry
    (tests/gate_fixture), so the report's absolute `passed` is decided by the
    node section.
    """
    from tests.gate_fixture import install_entry_executor

    install_entry_executor(monkeypatch)
    inner = rt.repository_executor.execute
    codes = exit_codes or {}
    calls = []

    def execute(repo_dir, args, timeout, **kw):
        if not kw.get("profile"):
            return inner(repo_dir, args, timeout, **kw)
        project = _project_of(kw.get("workdir"))
        command = " ".join(args[1:])
        calls.append({"dir": project, "profile": kw["profile"], "command": command})
        if refuse is not None and refuse(project, kw["profile"], command):
            raise rt.repository_executor.IsolationUnavailable(
                f"offline install refused for {project} (fixture)")
        code = 0
        for (want_dir, want_command), value in codes.items():
            if project == want_dir and command.startswith(want_command):
                code = value
        return subprocess.CompletedProcess(args, code, "", "")

    monkeypatch.setattr(rt.repository_executor, "execute", execute)
    return calls


def _baseline_files(state_dir):
    """The baseline on disk, found without asking the tool where it put it."""
    return sorted(p for p in Path(state_dir).rglob("*")
                  if p.is_file() and p.suffix == ".json")


def _baseline(state_dir):
    files = _baseline_files(state_dir)
    assert len(files) == 1, [str(p) for p in files]
    return sorted(json.loads(files[0].read_text())["failures"])


def _run(tmp_path, monkeypatch, repo, name, state_dir=None, **kw):
    calls = _serve(tmp_path, monkeypatch, **kw)
    out = tmp_path / name
    result = rt.run_tests(project_root=str(repo), out_dir=str(out),
                          repo_gate=False, state_dir=str(state_dir or (tmp_path / "state")))
    return result, _report(out), calls


def _evidence_schema():
    """The `test_evidence` inline schema `configs/coding_impl.yaml` validates
    `test_report.json` against before routing, read from the shipped config."""
    import yaml

    config = yaml.safe_load(
        (Path(rt.__file__).resolve().parents[3] / "configs" / "coding_impl.yaml")
        .read_text(encoding="utf-8"))
    for step in config["steps"]:
        if step.get("id") == "test_evidence":
            return step["tool_params"]["inline_schema"]
    raise AssertionError("test_evidence step not found")


def _validate(report):
    import jsonschema

    jsonschema.validate(report, _evidence_schema())


# --------------------------------------------------------------------- claim 1
def test_PROBE_no_tests_is_named_through_the_real_entry(tmp_path, monkeypatch):
    """Every measured project that runs no test is named, and nothing parks."""
    repo = _repo(tmp_path)
    result, report, calls = _run(tmp_path, monkeypatch, repo, "out")

    node = report["node"]
    assert {c["dir"] for c in calls} == {"web", "integrations/dsh"}
    assert node["no_tests"] == ["integrations/dsh"], node.get("no_tests")
    # And said where a reader looks: a claim that it is "named in the summary" was
    # once made in a commit message while the summary did not name it.
    assert "integrations/dsh" in node["summary"] and "no test" in node["summary"], \
        node["summary"]
    assert node["unmeasured"] == []
    assert node["skipped"] is False
    assert node["passed"] is True
    assert node["measured_dirs"] == ["web", "integrations/dsh"]
    # Its install IS a measurement and stays in checks; what it must not do is
    # stand in for a test run it never had.
    assert set(node["checks"]) == {"web:install", "web:build", "web:test",
                                   "integrations/dsh:install"}
    # Nothing parks: no absence key, the report reads as a pass, and the
    # shipped `test_evidence` schema accepts the file.
    assert "node_unmeasured" not in report
    assert report["passed"] is True and result["passed"] is True, report["failures"]
    assert report_state(report) == "passed", report_state(report)
    _validate(report)


def test_PROBE_no_tests_names_the_reachable_case_exactly(tmp_path, monkeypatch):
    """`integrations/dsh/package.json` for real: no scripts, no lockfile, no
    dependencies. Its checks are {"install"} and nothing else."""
    repo = _repo(tmp_path, projects=(("web", True), ("integrations/dsh", False)))
    (repo / "integrations" / "dsh" / "package-lock.json").unlink()
    _result, report, calls = _run(tmp_path, monkeypatch, repo, "out")
    node = report["node"]
    dsh_calls = [c["command"] for c in calls if c["dir"] == "integrations/dsh"]
    assert dsh_calls == ["install --offline"], dsh_calls   # no lockfile, no fetch
    assert node["projects"][1]["checks"] and set(node["projects"][1]["checks"]) == {"install"}
    assert node["no_tests"] == ["integrations/dsh"]
    assert node["unmeasured"] == [] and node["skipped"] is False
    assert report_state(report) == "passed"


def test_PROBE_no_tests_never_names_an_unmeasured_project(tmp_path, monkeypatch):
    """A project that was NOT measured is an absence, not a "no tests" project."""
    repo = _repo(tmp_path, projects=(("web", True), ("integrations/dsh", False),
                                     ("integrations/dsh-driver", True)))
    result, report, calls = _run(
        tmp_path, monkeypatch, repo, "out",
        refuse=lambda project, profile, command: (
            project == "integrations/dsh-driver" and profile == "node_run"))

    node = report["node"]
    assert node["no_tests"] == ["integrations/dsh"], node.get("no_tests")
    assert [e["dir"] for e in node["unmeasured"]] == ["integrations/dsh-driver"]
    assert node["skipped"] is True
    assert node["passed"] is True
    assert report_state(report) == "partial"
    # What the shipped schema does with this mixed run (measured-nothing is not
    # this change's business, but it must be stated, not implied).
    try:
        _validate(report)
        schema_verdict = "accepted"
    except Exception as exc:  # noqa: BLE001 - the verdict is the observation
        schema_verdict = f"rejected: {type(exc).__name__}: {exc}".splitlines()[0]
    assert schema_verdict.startswith("rejected"), schema_verdict
    print("MIXED-RUN test_evidence verdict:", schema_verdict)


# --------------------------------------------------------------------- claim 2
def test_PROBE_multiproject_prune_follows_the_check_that_ran(tmp_path, monkeypatch):
    """SEED `node:web:test` red, then run it GREEN: the key must be PRUNED."""
    repo = _repo(tmp_path)
    state = tmp_path / "state"
    _first, seeded, _calls = _run(
        tmp_path, monkeypatch, repo, "seed", state_dir=state,
        exit_codes={("web", "test"): 1})
    assert seeded["baseline_state"] == "seeded", seeded.get("baseline_state")
    assert _baseline(state) == ["node:web:test"], _baseline(state)
    assert len(seeded["failures"]) == 1, seeded["failures"]
    assert seeded["failures"][0].startswith("node:web:test failed"), seeded["failures"]

    # The same check now runs and passes.
    _second, green, _calls = _run(tmp_path, monkeypatch, repo, "green", state_dir=state)
    assert green["passed"] is True, green["failures"]
    assert "node:web:test" not in (green.get("baseline_kept_unproven") or []), (
        "the check RAN and passed; it may not be kept as unproven")
    assert _baseline(state) == [], (
        "node:web:test ran and passed; its known-red key must be pruned, "
        f"but the baseline still holds {_baseline(state)}")


def test_PROBE_install_failure_keeps_build_and_test_keys(tmp_path, monkeypatch):
    """SEED `node:web:test` red, then make web's INSTALL fail: the key is KEPT."""
    repo = _repo(tmp_path)
    state = tmp_path / "state"
    _first, seeded, _calls = _run(
        tmp_path, monkeypatch, repo, "seed", state_dir=state,
        exit_codes={("web", "test"): 1})
    assert _baseline(state) == ["node:web:test"]

    _second, broken, calls = _run(
        tmp_path, monkeypatch, repo, "broken", state_dir=state,
        exit_codes={("web", "ci --offline"): 1})
    node = broken["node"]
    web = {p["dir"]: p for p in node["projects"]}["web"]
    assert set(web["checks"]) == {"install"}, set(web["checks"])
    assert web["passed"] is False and node["passed"] is False
    assert not any(c["dir"] == "web" and c["command"].startswith(("test", "run build"))
                   for c in calls)
    assert _baseline(state) == ["node:web:test"], (
        "build/test never ran, so their known-red keys must be KEPT; "
        f"the baseline holds {_baseline(state)}")
    assert broken.get("baseline_kept_unproven") == ["node:web:test"], (
        broken.get("baseline_kept_unproven"))
    # Recorded, not excused: a project whose INSTALL failed is named "no_tests"
    # too, so the key does not distinguish "declares none" from "never got to
    # run the ones it declares".
    assert node["no_tests"] == ["integrations/dsh"], node.get("no_tests")


# ---------------------------------------- single-project: the unchanged path
def test_PROBE_single_project_prunes_a_bare_check_key(tmp_path, monkeypatch):
    """One project: `node:<check>` keys are still answered by `node_gate_ran`,
    and a check that RAN and passed is still pruned (the control that shows
    this harness can see a prune at all)."""
    repo = _repo(tmp_path, projects=(("web", True),))
    state = tmp_path / "state"
    _first, seeded, _calls = _run(
        tmp_path, monkeypatch, repo, "seed", state_dir=state,
        exit_codes={("web", "test"): 1})
    assert _baseline(state) == ["node:test"], _baseline(state)

    _second, green, _calls = _run(tmp_path, monkeypatch, repo, "green", state_dir=state)
    assert green["passed"] is True, green["failures"]
    assert _baseline(state) == [], (
        f"node:test ran and passed; the baseline holds {_baseline(state)}")


def test_PROBE_single_project_bare_keys_follow_the_gate_not_the_check(
        tmp_path, monkeypatch):
    """`node_gate_ran` means the gate RAN, not that it passed. So a bare
    `node:<check>` key is pruned as soon as its project was measured, whatever
    another check of the same project did - and is KEPT when nothing was
    measured. Both halves, through the entry."""
    repo = _repo(tmp_path, projects=(("web", True),))
    state = tmp_path / "state"
    _first, seeded, _calls = _run(
        tmp_path, monkeypatch, repo, "seed", state_dir=state,
        exit_codes={("web", "run build"): 1})
    assert _baseline(state) == ["node:build"], _baseline(state)

    _second, other_red, _calls = _run(
        tmp_path, monkeypatch, repo, "other-red", state_dir=state,
        exit_codes={("web", "test"): 1})
    assert other_red["passed"] is False
    assert _baseline(state) == [], (
        "build ran, so its key is pruned even though `test` is red: "
        f"the baseline holds {_baseline(state)}")


def test_PROBE_single_project_a_gate_that_measured_nothing_keeps_the_key(
        tmp_path, monkeypatch):
    """The other half: the runner is unavailable, nothing was measured, and no
    `node:` key may be pruned by an absence."""
    repo = _repo(tmp_path, projects=(("web", True),))
    state = tmp_path / "state"
    _first, seeded, _calls = _run(
        tmp_path, monkeypatch, repo, "seed", state_dir=state,
        exit_codes={("web", "test"): 1})
    assert _baseline(state) == ["node:test"], _baseline(state)

    _second, absent, _calls = _run(
        tmp_path, monkeypatch, repo, "absent", state_dir=state,
        refuse=lambda project, profile, command: profile == "node_run")
    assert absent["node"]["skipped"] is True
    assert absent["passed"] is False
    assert absent["baseline_kept_unproven"] == ["node:test"], (
        absent.get("baseline_kept_unproven"))
    assert _baseline(state) == ["node:test"], _baseline(state)


def test_PROBE_single_project_report_shape_is_unchanged(tmp_path, monkeypatch):
    """A one-project repository sees the shape it always saw; `no_tests` is a
    multi-project aggregate key and its absence here is recorded, not excused."""
    repo = _repo(tmp_path, projects=(("web", True),))
    _result, report, _calls = _run(tmp_path, monkeypatch, repo, "out")
    node = report["node"]
    assert node["dir"] == "web" and node["passed"] is True
    assert set(node["checks"]) == {"install", "build", "test"}
    assert not {"projects", "dirs", "measured_dirs", "unmeasured", "no_tests"} & set(node)
    assert not any("dir" in chk for chk in node["checks"].values())
    assert report_state(report) == "passed"
    _validate(report)

    # And the same repository with NO scripts: measured, green, and silent.
    bare = _repo(tmp_path / "bare", projects=(("web", False),))
    _result, report, _calls = _run(tmp_path / "bare", monkeypatch, bare, "out")
    assert set(report["node"]["checks"]) == {"install"}
    assert report["node"]["passed"] is True
    assert "no_tests" not in report["node"]


# ------------------------------------------------------- what the entry built
def test_the_real_entry_names_checks_the_way_the_baseline_asks_for_them(tmp_path, monkeypatch):
    """The tokens the entry supplies and the questions it asks must be the same shape.

    This started as a diagnosis: the entry prefixed an already-prefixed name
    (`web:web:test`), so no namespaced baseline key was ever pruned while the
    suite stayed green. It is now the standing assertion that they agree.
    """
    repo = _repo(tmp_path)
    state = tmp_path / "state"
    _first, _seeded, _calls = _run(
        tmp_path, monkeypatch, repo, "seed", state_dir=state,
        exit_codes={("web", "test"): 1})

    seen = {}
    real_init, real_ran = rt.Executed.__init__, rt.Executed.ran

    def init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        if self.node_ran is not None:
            seen["node_ran"] = sorted(self.node_ran)

    def ran(self, key):
        answer = real_ran(self, key)
        seen.setdefault("asked", []).append((key, answer))
        if self.node_ran is not None:
            seen["node_ran"] = sorted(self.node_ran)
        return answer

    monkeypatch.setattr(rt.Executed, "__init__", init)
    monkeypatch.setattr(rt.Executed, "ran", ran)
    _second, green, _calls = _run(tmp_path, monkeypatch, repo, "green", state_dir=state)

    print("node_ran =", seen.get("node_ran"))
    print("asked =", seen.get("asked"))
    print("kept_unproven =", green.get("baseline_kept_unproven"))
    assert seen.get("node_ran"), "the real entry supplied no per-check evidence"
    assert not [name for name in seen["node_ran"] if name.count(":") > 1], (
        f"a check was namespaced twice: {seen['node_ran']}")
    assert "web:test" in seen["node_ran"], seen["node_ran"]
    assert ("node:web:test", True) in seen.get("asked", []), (
        "web's test ran and passed, so the baseline must prune it: "
        f"{seen.get('asked')}")


# ---------------------------------------------------------------- the loop
def test_one_project_that_raises_does_not_cost_the_others_their_result(tmp_path, monkeypatch):
    """An exception in project 2 must not take the whole run's report with it.

    Before this, a RuntimeError raised inside `_run_node_project` propagated out of
    `run_tests` and NOTHING was written - the one path where one project could
    still take another's result down, and the whole run with it.
    """
    repo = _repo(tmp_path, projects=(("web", True),
                                     ("integrations/dsh-driver", True)))
    real = rt._run_node_project

    def explode(repo_path, project, *, run_id=""):
        if "dsh-driver" in str(project):
            raise RuntimeError("snapshot refused")
        return real(repo_path, project, run_id=run_id)

    monkeypatch.setattr(rt, "_run_node_project", explode)
    _result, report, _calls = _run(tmp_path, monkeypatch, repo, "boom")
    node = report["node"]
    assert node["dirs"], node
    reasons = {entry["dir"]: entry["reason"] for entry in node.get("unmeasured", [])}
    assert "integrations/dsh-driver" in reasons, reasons
    assert "RuntimeError" in reasons["integrations/dsh-driver"], reasons
    assert node["passed"] is True, "the measured project passed; the absence is not its red"
    assert report["node_unmeasured"]
