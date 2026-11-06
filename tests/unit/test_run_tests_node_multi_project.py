# tests/unit/test_run_tests_node_multi_project.py
# The node gate must run EVERY node project in the repository.
#
# Measured defect (2026-11-05): `_find_node_project` returned the root
# package.json if present, else the FIRST `*/package.json` one level deep. In
# this repository that is `web/`, so the driver suite at
# `integrations/dsh-driver` (fifteen files, a 30-second pinned end-to-end run)
# was never executed by the gate; main went red from it twice, the second time
# already merged. These tests pin the replacement: both projects are visited
# and reported separately, one project's red fails the aggregate, and a
# project that could not be measured is never counted as a pass.
import json
import subprocess

import aitelier.tools.run_tests.impl as rt


def _report(out_dir):
    return json.loads((out_dir / "test_report.json").read_text())


def _project(workdir):
    """The project a node command runs for, from its snapshot workdir."""
    path = str(workdir).replace("\\", "/")
    if "/src/" in path:
        return path.split("/src/", 1)[1]
    return "."


def _repo(tmp_path, projects=("web", "integrations/dsh-driver")):
    """A repository laid out like this one: `web/` first, tooling at depth 2."""
    repo = tmp_path / "repo"
    for rel in projects:
        directory = repo / rel
        directory.mkdir(parents=True)
        (directory / "package.json").write_text(json.dumps({
            "name": rel.replace("/", "-"), "version": "0.0.0",
            "scripts": {"build": "build-it", "test": "test-it"}}))
        (directory / "package-lock.json").write_text("{}")
    return repo


def _fake_npm(tmp_path, monkeypatch, exit_codes=None, refuse=None, inner=None):
    """Stand in for the bounded CPU transport; log (project, command) pairs.

    `exit_codes` maps ``(project, command-prefix)`` to a returncode, so a test
    can make exactly one project red. `refuse` is called with
    ``(project, profile, command)`` and, when true, makes that one launch
    unavailable — the "cannot be installed" case. `inner` serves the commands
    that are not node commands (the pytest phase) when a test wants both.
    """
    codes = exit_codes or {}
    calls = []

    def execute(pkg_dir, args, timeout, **kw):
        if not kw.get("profile"):
            if inner is not None:
                return inner(pkg_dir, args, timeout, **kw)
            raise rt.repository_executor.IsolationUnavailable("no Docker in this test")
        project = _project(kw.get("workdir"))
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


def test_every_node_project_is_visited_and_reported_separately(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    calls = _fake_npm(tmp_path, monkeypatch)

    node = rt._run_node_checks(repo)

    # Both projects were visited, in a deterministic order, and each carries
    # its own install/build/test result.
    assert node["dirs"] == ["web", "integrations/dsh-driver"]
    assert [p["dir"] for p in node["projects"]] == node["dirs"]
    for project in node["projects"]:
        assert project["passed"] is True
        assert set(project["checks"]) == {"install", "build", "test"}
    assert {call["dir"] for call in calls} == {"web", "integrations/dsh-driver"}
    # The aggregate is a pass only because both passed and both were measured.
    assert node["passed"] is True
    assert node["measured_dirs"] == node["dirs"]
    assert node["unmeasured"] == []
    assert not node.get("skipped")
    # Checks are namespaced by project, so a red names the project it came from.
    assert set(node["checks"]) == {
        "web:install", "web:build", "web:test",
        "integrations/dsh-driver:install", "integrations/dsh-driver:build",
        "integrations/dsh-driver:test"}


def test_only_the_driver_project_red_fails_the_aggregate(tmp_path, monkeypatch):
    """WITNESS: the driver suite is red while `web/` is green and the gate fails.

    With the single-project finder this repository's gate never ran the driver
    suite at all, so this exact state reported `passed: true`.
    """
    repo = _repo(tmp_path)
    _fake_npm(tmp_path, monkeypatch,
              exit_codes={("integrations/dsh-driver", "test"): 1})

    node = rt._run_node_checks(repo)

    driver = {p["dir"]: p for p in node["projects"]}["integrations/dsh-driver"]
    web = {p["dir"]: p for p in node["projects"]}["web"]
    assert driver["checks"]["test"]["passed"] is False
    assert driver["passed"] is False
    assert web["passed"] is True                      # not hidden by the red
    assert node["passed"] is False                    # the aggregate is not a pass
    assert node["checks"]["integrations/dsh-driver:test"]["passed"] is False

    # And the run itself: the report names the project and fails.
    out = tmp_path / "out"
    res = rt.run_tests(project_root=str(repo), out_dir=str(out))
    report = _report(out)
    assert report["passed"] is False and res["passed"] is False
    assert any(f.startswith("node:integrations/dsh-driver:test failed")
               for f in report["failures"]), report["failures"]
    assert {p["dir"]: p for p in report["node"]["projects"]}["web"]["passed"] is True


def _composed(tmp_path, monkeypatch, **kw):
    """Node commands served by the fake transport, everything else really run.

    The pytest phase of `run_tests` is served by the project's own test seam
    (`tests/gate_fixture.install_entry_executor`), so a report's absolute
    `passed` here is decided by the node section and not by a missing pytest
    container.
    """
    from tests.gate_fixture import install_entry_executor

    install_entry_executor(monkeypatch)
    return _fake_npm(tmp_path, monkeypatch, inner=rt.repository_executor.execute, **kw)


def test_project_that_cannot_be_measured_never_counts_as_a_pass(tmp_path, monkeypatch):
    """WITNESS: `web/` cannot install offline and the driver project is red.

    The unmeasured project keeps its own entry and reason, contributes no red,
    and cannot hide the measured project's real red: the aggregate fails on the
    red, with the absence named beside it.
    """
    repo = _repo(tmp_path)
    _composed(tmp_path, monkeypatch,
              refuse=lambda project, profile, command: project == "web"
              and profile == "node_run",
              exit_codes={("integrations/dsh-driver", "test"): 1})

    node = rt._run_node_checks(repo)

    web = {p["dir"]: p for p in node["projects"]}["web"]
    assert web["skipped"] is True
    assert "offline install refused for web" in web["unmeasured_reason"]
    driver = {p["dir"]: p for p in node["projects"]}["integrations/dsh-driver"]
    assert not driver.get("skipped")
    assert driver["checks"]["test"]["passed"] is False
    # `passed` reflects only the measured projects: the driver is red, so it is
    # false; the unmeasured project is named and contributes no check.
    assert node["passed"] is False
    assert node["skipped"] is True                       # something was not measured
    assert node["measured_dirs"] == ["integrations/dsh-driver"]
    assert [e["dir"] for e in node["unmeasured"]] == ["web"]
    assert "offline install refused for web" in node["unmeasured"][0]["reason"]
    assert set(node["checks"]) == {
        "integrations/dsh-driver:install", "integrations/dsh-driver:build",
        "integrations/dsh-driver:test"}

    out = tmp_path / "out"
    rt.run_tests(project_root=str(repo), out_dir=str(out))
    report = _report(out)
    assert report["passed"] is False
    assert [e["dir"] for e in report["node_unmeasured"]] == ["web"]
    assert any(f.startswith("node:integrations/dsh-driver:test failed")
               for f in report["failures"]), report["failures"]
    # The absence stays an absence: no failure entry may read as a red.
    assert not any(f.startswith("node gate unavailable") for f in report["failures"])


def test_unmeasured_project_beside_a_green_one_is_neither_pass_nor_block(
        tmp_path, monkeypatch):
    """WITNESS, the rule this change turns on.

    Measured 2026-11-06: `integrations/dsh-driver`'s lockfile carries 687
    packages and the gate's empty ticket cache answers `npm ci --offline` with
    ENOTCACHED — unprovisionable from a code change (it needs an image build).
    If "not measured" meant "not a pass", every run of this repository would be
    red for everyone, which is worse than the defect. So it is the third state:
    `web/` green + driver not measurable -> the run PASSES, and the report says
    plainly that the driver was not measured and why.
    """
    repo = _repo(tmp_path)
    _composed(tmp_path, monkeypatch,
              refuse=lambda project, profile, command: project == "integrations/dsh-driver"
              and profile == "node_run")

    node = rt._run_node_checks(repo)

    assert node["skipped"] is True
    assert node["passed"] is True                        # every MEASURED project passed
    assert node["measured_dirs"] == ["web"]
    assert [e["dir"] for e in node["unmeasured"]] == ["integrations/dsh-driver"]
    assert "offline install refused for integrations/dsh-driver" in (
        node["unmeasured"][0]["reason"])
    # Nothing in `checks` pretends the driver ran, and `web` is really there.
    assert set(node["checks"]) == {"web:install", "web:build", "web:test"}
    assert "not measured" in node["summary"]

    out = tmp_path / "out"
    res = rt.run_tests(project_root=str(repo), out_dir=str(out))
    report = _report(out)
    assert report["passed"] is True and res["passed"] is True, (
        report["failures"], report["summary"])
    assert [e["dir"] for e in report["node_unmeasured"]] == ["integrations/dsh-driver"]
    assert report["node"]["skipped"] is True
    assert report["failures"] == []                      # an absence is not a red
    assert not any("dsh-driver" in f for f in report["failures"])


def test_projects_past_the_cap_are_reported_and_do_not_block(tmp_path, monkeypatch):
    """The cap keeps one gate run bounded: the projects past it are neither run
    nor silently treated as green — they are named, and they do not fail a run
    whose measured projects passed."""
    projects = tuple(f"pkg-{index}" for index in range(rt.NODE_PROJECT_CAP + 2))
    repo = _repo(tmp_path, projects)
    calls = _fake_npm(tmp_path, monkeypatch)

    node = rt._run_node_checks(repo)

    assert len(node["dirs"]) == rt.NODE_PROJECT_CAP
    assert [e["dir"] for e in node["unmeasured"]] == list(projects[rt.NODE_PROJECT_CAP:])
    assert all("not visited" in e["reason"] for e in node["unmeasured"])
    assert node["skipped"] is True
    assert node["passed"] is True                        # the measured ones passed
    assert {call["dir"] for call in calls} == set(projects[:rt.NODE_PROJECT_CAP])


def test_nothing_measurable_keeps_the_single_project_absence(tmp_path, monkeypatch):
    """When NO project can be measured, today's meaning is kept: an absence with
    `passed: False`, not a red — and still not a pass."""
    repo = _repo(tmp_path)
    _composed(tmp_path, monkeypatch,
              refuse=lambda project, profile, command: profile == "node_run")

    node = rt._run_node_checks(repo)

    assert node["skipped"] is True
    assert node["passed"] is True and node["measured_dirs"] == []
    assert [e["dir"] for e in node["unmeasured"]] == ["web", "integrations/dsh-driver"]
    assert node["unmeasured_reason"]
    assert node["checks"] == {}

    out = tmp_path / "out"
    rt.run_tests(project_root=str(repo), out_dir=str(out))
    report = _report(out)
    assert report["passed"] is False
    assert report["skipped"] is True
    assert report["infrastructure_unavailable"] is True
    assert report["evidence_state"] == "infrastructure_unavailable"
    assert any(f.startswith("node gate unavailable:")
               for f in report["failures"]), report["failures"]


def test_single_project_report_is_unchanged(tmp_path, monkeypatch):
    """A `web`-only repository sees exactly what it saw before this change."""
    repo = _repo(tmp_path, projects=("web",))
    _fake_npm(tmp_path, monkeypatch)

    node = rt._run_node_checks(repo)

    assert node["dir"] == "web"
    assert node["passed"] is True
    assert set(node["checks"]) == {"install", "build", "test"}
    assert not {"projects", "dirs", "measured_dirs", "unmeasured"} & set(node)

    out = tmp_path / "out"
    rt.run_tests(project_root=str(repo), out_dir=str(out))
    report = _report(out)
    assert report["node"]["dir"] == "web"
    assert report["node"]["checks"]["build"]["passed"] is True
    assert not {"projects", "dirs"} & set(report["node"])


def test_root_project_comes_first(tmp_path, monkeypatch):
    """A root package.json plus `web/`: both run, the root one first."""
    repo = _repo(tmp_path, projects=("web",))
    (repo / "package.json").write_text(json.dumps({
        "name": "root", "version": "0.0.0",
        "scripts": {"build": "build-it", "test": "test-it"}}))
    (repo / "package-lock.json").write_text("{}")
    calls = _fake_npm(tmp_path, monkeypatch)

    node = rt._run_node_checks(repo)

    assert node["dirs"] == [".", "web"]
    assert node["passed"] is True
    assert {call["dir"] for call in calls} == {".", "web"}


def test_the_gate_itself_fails_when_only_the_driver_project_is_red(tmp_path, monkeypatch):
    """End to end, with the pytest phase really run (no Python sources here, so
    it is "not applicable" and green): the SAME repository passes the gate when
    both projects are green and fails it when only the driver project is red.

    This is the state that shipped on 2026-11-05: `web/` green, the driver
    suite red, the gate reporting `passed: true`.
    """
    from tests.gate_fixture import install_entry_executor

    install_entry_executor(monkeypatch)          # serves the pytest phase
    pytest_phase = rt.repository_executor.execute
    repo = _repo(tmp_path)

    _fake_npm(tmp_path, monkeypatch, inner=pytest_phase)
    green = tmp_path / "green"
    res = rt.run_tests(project_root=str(repo), out_dir=str(green))
    green_report = _report(green)
    assert green_report["node"]["passed"] is True
    assert green_report["node"]["dirs"] == ["web", "integrations/dsh-driver"]
    assert green_report["passed"] is True and res["passed"] is True, (
        green_report["failures"], green_report["summary"])

    _fake_npm(tmp_path, monkeypatch, inner=pytest_phase,
              exit_codes={("integrations/dsh-driver", "test"): 1})
    red = tmp_path / "red"
    res = rt.run_tests(project_root=str(repo), out_dir=str(red))
    red_report = _report(red)
    assert red_report["node"]["passed"] is False
    assert red_report["passed"] is False and res["passed"] is False
    assert red_report["failures"], "a failing node check must produce a failure entry"
    assert all(f.startswith("node:integrations/dsh-driver:test failed")
               for f in red_report["failures"]
               if f.startswith("node:")), red_report["failures"]
    assert {p["dir"]: p["passed"] for p in red_report["node"]["projects"]} == {
        "web": True, "integrations/dsh-driver": False}


# ---------------------------------------------------------------- third state
# Three defects a non-author review measured in the first multi-project
# version, all of which made the gate look GREENER than it was: an absence no
# consumer rendered, baseline keys that stopped being pruned for projects that
# DID run, and a project counted as measured while running nothing.

def test_an_unmeasured_project_keeps_the_run_from_reading_as_passed():
    """The absence has to reach a consumer, not just a report key.

    `report_state` routes on passed/skipped/failures and never looked at the
    node section, so a run where one project's suite never ran came back
    "passed" to anything that asked.
    """
    from aitelier.gate_evidence import report_state

    green = {"passed": True, "failures": []}
    assert report_state(green) == "passed"
    with_absence = {"passed": True, "failures": [],
                    "node_unmeasured": [{"dir": "integrations/dsh-driver",
                                         "reason": "offline install refused"}]}
    assert report_state(with_absence) == "partial", (
        "a run that measured some projects and skipped others must not read "
        "as a pass to a consumer that never opens report['node']")
    # A measured red is still a red, presence or absence beside it.
    red_with_absence = {"passed": False, "failures": ["node:web:test failed"]}
    assert report_state(red_with_absence) != "passed"


def test_baseline_pruning_follows_the_project_that_ran():
    """One unmeasurable project must not stop pruning the measured ones.

    `node_gate_ran` is all-or-nothing. With it false, every `node:` baseline key
    was kept, so a node test that was fixed and then regressed again stayed
    forgiven for as long as any project in the tree could not install.
    """
    subjects = rt.Executed(node_gate_ran=False, node_ran=["web:install", "web:test"])
    assert subjects.ran("node:web:test") is True, (
        "web ran, so its known-red key must be pruned when it goes green")
    assert subjects.ran("node:integrations/dsh-driver:test") is False, (
        "the driver project did not run; keeping its key is the whole point")
    # Without the per-project information the old all-or-nothing answer stands.
    assert rt.Executed(node_gate_ran=True).ran("node:test") is True
    assert rt.Executed(node_gate_ran=False).ran("node:test") is False
    # Directory granularity was not enough: an install failure means build and
    # test never ran, so their known-red keys must be KEPT.
    install_only = rt.Executed(node_gate_ran=False, node_ran=["web:install"])
    assert install_only.ran("node:web:install") is True
    assert install_only.ran("node:web:test") is False, (
        "pruning a key for a check that never ran blames the next round for a "
        "red it did not cause")


def test_a_project_with_no_test_script_is_named_not_counted_as_evidence():
    """`integrations/dsh` has a package.json and no scripts.

    It is measured and green with nothing but its install check, which is not
    evidence that its tests passed. It must not be silent, and it must not be
    called unmeasured either - nothing was withheld, there is nothing to run,
    and an absence parks every run of this repository behind a human.
    """
    results = [
        {"dir": "web", "passed": True,
         "checks": {"install": {"passed": True}, "test": {"passed": True}}},
        {"dir": "integrations/dsh", "passed": True,
         "checks": {"install": {"passed": True}}},
    ]
    node = rt._aggregate_node_projects(results, [])
    assert node["no_tests"] == ["integrations/dsh"], (
        "a project measured with no test-like check must be named")
    assert node["unmeasured"] == [], (
        "nothing was withheld from a project that declares no tests")
    # Its install IS a real measurement and belongs in checks; what it must
    # not do is stand in for a test run it never had.
    assert set(node["checks"]) == {"web:install", "web:test",
                                   "integrations/dsh:install"}


def test_ran_checks_name_the_pairs_the_entry_itself_wrote():
    """The tokens `run_tests` hands the baseline must be the ones it can ask about.

    The first version prefixed an already-prefixed name (`web:web:test`), so no
    namespaced baseline key was ever pruned in a multi-project repository while
    every file in the suite stayed green. Both shapes are pinned here because the
    entry writes both.
    """
    multi = {"checks": {"web:install": {}, "web:test": {}}}
    assert rt._node_ran_checks(multi) == {"web:install", "web:test"}
    single = {"dir": "web", "checks": {"install": {}, "test": {}}}
    assert rt._node_ran_checks(single) == {"web:install", "web:test"}
    # And what the baseline is asked for is exactly what that set answers.
    executed = rt.Executed(node_gate_ran=False, node_ran=rt._node_ran_checks(multi))
    assert executed.ran("node:web:test") is True
    assert executed.ran("node:web:build") is False
