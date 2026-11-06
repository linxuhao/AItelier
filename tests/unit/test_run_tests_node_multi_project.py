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


def test_project_that_cannot_be_measured_never_counts_as_a_pass(tmp_path, monkeypatch):
    """WITNESS: `web/` cannot install offline, the driver project is red.

    The unmeasured project keeps its own entry and reason, the measured
    project keeps its real (red) result, and the aggregate is not a pass —
    even though an unmeasured project's own entry reads `passed: True,
    skipped: True` exactly as it always has.
    """
    repo = _repo(tmp_path)

    def refuse(project, profile, command):
        return project == "web" and profile == "node_run"

    _fake_npm(tmp_path, monkeypatch, refuse=refuse,
              exit_codes={("integrations/dsh-driver", "test"): 1})

    node = rt._run_node_checks(repo)

    web = {p["dir"]: p for p in node["projects"]}["web"]
    assert web["skipped"] is True
    assert "offline install refused for web" in web["unmeasured_reason"]
    driver = {p["dir"]: p for p in node["projects"]}["integrations/dsh-driver"]
    assert not driver.get("skipped")
    assert driver["checks"]["test"]["passed"] is False
    # The aggregate cannot be read as a pass, and it says which project was not
    # measured; that project's half-run contributes no red and no green.
    assert node["passed"] is False
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
    assert any(f.startswith("node gate unavailable for web:")
               for f in report["failures"]), report["failures"]
    assert any(f.startswith("node:integrations/dsh-driver:test failed")
               for f in report["failures"]), report["failures"]


def test_unmeasured_beside_a_green_project_is_not_a_pass(tmp_path, monkeypatch):
    """`all(passed)` over every project entry would call this state a pass.

    The unmeasured project's own entry says `passed: True, skipped: True`, so
    the aggregate's rule has to be "measured AND passed" per project, not
    "passed".
    """
    repo = _repo(tmp_path)
    _fake_npm(tmp_path, monkeypatch,
              refuse=lambda project, profile, command: project == "web"
              and profile == "node_run")

    node = rt._run_node_checks(repo)

    assert all(p["passed"] for p in node["projects"])   # the trap
    assert node["passed"] is False                      # the rule
    assert node["measured_dirs"] == ["integrations/dsh-driver"]
    assert [e["dir"] for e in node["unmeasured"]] == ["web"]

    out = tmp_path / "out"
    rt.run_tests(project_root=str(repo), out_dir=str(out))
    report = _report(out)
    assert report["passed"] is False
    # Nothing was measured red and something was not measured: an absence.
    assert report["skipped"] is True
    assert report["evidence_state"] == "infrastructure_unavailable"


def test_projects_past_the_cap_are_reported_not_dropped(tmp_path, monkeypatch):
    """The cap keeps one gate run bounded; it may not turn an unrun project
    into a pass."""
    projects = tuple(f"pkg-{index}" for index in range(rt.NODE_PROJECT_CAP + 2))
    repo = _repo(tmp_path, projects)
    calls = _fake_npm(tmp_path, monkeypatch)

    node = rt._run_node_checks(repo)

    assert len(node["dirs"]) == rt.NODE_PROJECT_CAP
    assert [e["dir"] for e in node["unmeasured"]] == list(projects[rt.NODE_PROJECT_CAP:])
    assert all("not visited" in e["reason"] for e in node["unmeasured"])
    assert node["passed"] is False
    assert {call["dir"] for call in calls} == set(projects[:rt.NODE_PROJECT_CAP])


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
