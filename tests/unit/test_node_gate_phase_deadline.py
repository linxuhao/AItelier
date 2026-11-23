# tests/unit/test_node_gate_phase_deadline.py
#
# THE DEFECT: the node gate's project phase had no enclosing deadline.
# `_run_node_checks` loops over up to NODE_PROJECT_CAP projects and each project
# runs up to four commands charged to NODE_INSTALL_TIMEOUT (fetch, install) and
# NODE_SCRIPT_TIMEOUT (build, test). Nothing bounded that loop: eight projects
# whose commands all hang can burn 8 * 4 * 600 = 19200 s, while the bound the
# gate advertises, REPO_GATE_TIMEOUT = 12600 s, is passed only to the repo-gate
# script and the relay upstream — never to `_run_node_checks`. A multi-project
# hang therefore outlived its caller, and a later run found node-gate tickets
# nobody was waiting for.
#
# THE FIX: NODE_PHASE_TIMEOUT (7200 s) is read once at the phase start from a
# monotonic clock and checked BEFORE each project starts.
#
# WHAT THIS FILE MEASURES, on the returned node dict and never on log text:
#   (1) three declared projects, a deadline that expires after project 1
#       finishes: project 1 measured, projects 2 and 3 never started, both named
#       in `unmeasured` with a reason naming the deadline and its value,
#       `skipped` true, and `passed` reflecting project 1 alone (both poles: a
#       green project 1 passes, a red project 1 fails, the unmeasured projects
#       in neither case turning the answer);
#   (2) the other order: the deadline passes while a project is IN FLIGHT —
#       that project finishes, keeps every check it ran, and is counted as
#       measured; only the one after it is skipped;
#   (3) an unexpired deadline produces exactly the base commit's fields, summary
#       wording and `skipped` value — asserted against the same fixtures
#       (`_repo` + `_fake_npm`) the base node-gate suite uses, not a second fake
#       world;
#   (4) the chosen value sits between the two numbers the comment in
#       `aitelier/tools/run_tests/impl.py` names: at least one project's whole
#       command budget, and strictly under the phase's unbounded worst case.
#
# The clock is a seam the test controls (`rt._node_phase_clock`), so nothing
# here sleeps and no test depends on how fast the runner is.
import json

import aitelier.tools.run_tests.impl as rt
# The producer mints this kind itself (`aitelier/tools/run_tests/impl.py`, beside
# `NODE_PHASE_TIMEOUT`): the bound it names is this module's own, and the only
# reader that branches on `kind` treats a project-kind entry correctly without
# being taught a new word.
ABSENCE_PHASE_DEADLINE = rt.ABSENCE_PHASE_DEADLINE
# The base node-gate suite's own fixtures: one repository layout this project
# really has (`web/` first, tooling at depth 2) and the bounded-transport fake.
# Reused rather than re-invented so claim (3) compares against the world the
# existing green test measures.
from tests.unit.test_run_tests_node_multi_project import _fake_npm, _repo

PROJECTS = ("web", "integrations/one", "packages/two")


class Clock:
    """The monotonic clock the phase deadline reads, moved by the test."""

    def __init__(self, now: float = 0.0):
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


def _declared_three(tmp_path):
    """Three node projects, DECLARED in the order the gate visits them.

    Declared rather than left to inference so no `capped`/`undeclared` absence
    can appear beside the deadline's own entries: every project in `unmeasured`
    below is there because of the deadline and nothing else.
    """
    repo = _repo(tmp_path, PROJECTS)
    (repo / rt.NODE_PROJECTS_DECLARATION).write_text(
        json.dumps({"projects": list(PROJECTS)}))
    return repo


def _fake_projects(monkeypatch, clock, step, red=()):
    """Run each project in fake time: it lasts `step` seconds and is measured.

    Returns the list of projects that actually STARTED — the claim that a
    skipped project was never begun is asserted on it, not inferred from the
    report.
    """
    started = []

    def project_run(repo, pkg_dir, *, run_id=""):
        rel = str(pkg_dir.relative_to(repo)) or "."
        started.append(rel)
        clock.advance(step)                       # the project's own work
        passed = rel not in red
        code = 0 if passed else 1
        return {"dir": rel, "passed": passed,
                "checks": {"install": {"passed": passed, "returncode": code,
                                       "output": ""},
                           "test": {"passed": passed, "returncode": code,
                                    "output": ""}}}

    monkeypatch.setattr(rt, "_run_node_project", project_run)
    return started


def _deadline_entries(node):
    return [e for e in node["unmeasured"] if e.get("kind") == ABSENCE_PHASE_DEADLINE]


def test_the_deadline_expiring_after_a_project_names_every_project_it_skipped(
        tmp_path, monkeypatch):
    """(1) The deadline expires after project 1 finishes.

    Project 1 is measured; projects 2 and 3 are never started, and each is
    named with the deadline and its value. `skipped` is true and `passed`
    reflects project 1 alone — measured in both poles, so the unmeasured
    projects can neither be read as a pass nor turned into a red.
    """
    repo = _declared_three(tmp_path)

    # Both poles in one place: the deadline expires at the same moment either
    # way, so the only difference between them is whether project 1 passed.
    for red, expected in (((), True), (("web",), False)):
        clock = Clock()
        monkeypatch.setattr(rt, "_node_phase_clock", clock)
        # Project 1 is charged exactly the whole deadline, so the pre-check
        # before project 2 sees it fully spent — and project 1, which started
        # at 0, is measured regardless.
        started = _fake_projects(monkeypatch, clock, step=rt.NODE_PHASE_TIMEOUT,
                                 red=red)

        node = rt._run_node_checks(repo)

        # Project 1 was measured, and nothing after it was even started.
        assert started == ["web"], started
        assert node["dirs"] == ["web"]
        assert node["measured_dirs"] == ["web"]
        # ...and `passed` is project 1's verdict, not the run's.
        assert node["passed"] is expected, (
            f"passed={node['passed']} with project 1 red={bool(red)}: an "
            "unmeasured project must not decide the aggregate either way")

        skipped = _deadline_entries(node)
        assert [e["dir"] for e in skipped] == ["integrations/one", "packages/two"]
        assert node["skipped"] is True
        for entry in skipped:
            # Its own reason, naming the deadline and the value it had.
            assert "deadline" in entry["reason"], entry["reason"]
            assert str(rt.NODE_PHASE_TIMEOUT) in entry["reason"], entry["reason"]
            assert "NODE_PHASE_TIMEOUT" in entry["reason"], entry["reason"]
            assert entry["reason"].startswith("not visited"), entry["reason"]

        # Nothing pretends the skipped projects ran, and the summary names them.
        assert set(node["checks"]) == {"web:install", "web:test"}
        assert "1 of 3 node project(s) measured" in node["summary"]
        for directory in ("integrations/one", "packages/two"):
            assert directory in node["summary"], node["summary"]


def test_a_project_in_flight_when_the_deadline_passes_is_still_measured(
        tmp_path, monkeypatch):
    """(2) The deadline lands DURING a project, not between two of them.

    Each project costs 3601 s of the 7200 s phase, so the second project starts
    below the deadline and finishes above it: it is reported as measured with
    every check it ran, and only the third project is skipped.
    """
    repo = _declared_three(tmp_path)
    clock = Clock()
    monkeypatch.setattr(rt, "_node_phase_clock", clock)
    step = rt.NODE_PHASE_TIMEOUT // 2 + 1          # crosses the deadline in #2
    started = _fake_projects(monkeypatch, clock, step=step)

    node = rt._run_node_checks(repo)

    assert step < rt.NODE_PHASE_TIMEOUT             # project 2 really started
    assert 2 * step >= rt.NODE_PHASE_TIMEOUT        # ...and ended past it
    assert started == ["web", "integrations/one"], started
    assert node["measured_dirs"] == ["web", "integrations/one"]
    assert [e["dir"] for e in _deadline_entries(node)] == ["packages/two"]
    # The in-flight project kept the checks it ran: it was not cut, and it is
    # not reported as an absence.
    assert {"integrations/one:install", "integrations/one:test"} <= set(node["checks"])
    assert node["passed"] is True
    assert node["skipped"] is True
    assert "2 of 3 node project(s) measured" in node["summary"]


def test_an_unexpired_deadline_changes_nothing_in_the_report(tmp_path, monkeypatch):
    """(3) No overrun: the report is the base commit's, field for field.

    Driven through the base suite's own fixtures (`_repo` for this repository's
    layout, `_fake_npm` for the transport), with the clock pinned so the
    deadline cannot fire. Every field, the summary wording and `skipped` are
    asserted against what the pre-existing green multi-project test pins.
    """
    repo = _repo(tmp_path)                         # web/, integrations/dsh-driver
    calls = _fake_npm(tmp_path, monkeypatch)
    clock = Clock(now=1000.0)                      # pinned: never advances
    monkeypatch.setattr(rt, "_node_phase_clock", clock)

    node = rt._run_node_checks(repo)

    # Not one field added, and none dropped: the phase deadline is not a new
    # key on the report, it is a bound on the loop.
    assert set(node) == {"dirs", "projects", "checks", "measured_dirs",
                         "unmeasured", "skipped", "passed", "summary"}, sorted(node)
    assert node["dirs"] == ["web", "integrations/dsh-driver"]
    assert [p["dir"] for p in node["projects"]] == node["dirs"]
    for project in node["projects"]:
        assert project["passed"] is True
        assert set(project["checks"]) == {"install", "build", "test"}
    assert node["measured_dirs"] == node["dirs"]
    assert node["unmeasured"] == []
    assert node["skipped"] is False
    assert node["passed"] is True
    assert node["summary"] == "2 of 2 node project(s) measured"
    assert set(node["checks"]) == {
        "web:install", "web:build", "web:test",
        "integrations/dsh-driver:install", "integrations/dsh-driver:build",
        "integrations/dsh-driver:test"}
    assert _deadline_entries(node) == []
    assert {call["dir"] for call in calls} == {"web", "integrations/dsh-driver"}
    assert clock.now == 1000.0, "the report path moved the clock it was given"


def test_the_deadline_sits_between_the_two_numbers_it_must():
    """(4) The value is derived from the budgets, not picked from the air.

    Read off the REAL constants, so the guard moves with them: at least one
    project's whole command budget (a project may spend fetch, install, build
    and test and still finish), and strictly under the phase's unbounded worst
    case (or it encloses nothing).
    """
    from core import gate_deferral as gd

    one_project = (2 * rt.NODE_INSTALL_TIMEOUT + 2 * rt.NODE_SCRIPT_TIMEOUT)
    assert one_project == 4 * rt.NODE_SCRIPT_TIMEOUT      # both budgets are 600
    assert rt.NODE_PHASE_TIMEOUT >= max(4 * rt.NODE_SCRIPT_TIMEOUT, one_project), (
        f"NODE_PHASE_TIMEOUT={rt.NODE_PHASE_TIMEOUT}s cannot pay for one "
        f"project's four command budgets ({one_project}s): a project that "
        "spent them all would be cut")
    worst_case = rt.NODE_PROJECT_CAP * one_project
    assert rt.NODE_PHASE_TIMEOUT < worst_case, (
        f"NODE_PHASE_TIMEOUT={rt.NODE_PHASE_TIMEOUT}s is not below the phase's "
        f"unbounded worst case ({worst_case}s): it encloses nothing")
    # The phase's own worst case, deadline plus the one project that started
    # just under it, still fits the deferral episode that waits for the step.
    assert rt.NODE_PHASE_TIMEOUT + one_project <= gd.episode_max_seconds(), (
        f"the node phase can hold the scheduler for "
        f"{rt.NODE_PHASE_TIMEOUT + one_project}s, past the "
        f"{gd.episode_max_seconds()}s episode that waits for it")
