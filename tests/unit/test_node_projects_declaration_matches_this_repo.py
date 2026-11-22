# tests/unit/test_node_projects_declaration_matches_this_repo.py
# The witness for `issue.iss-fee9be424f9247ef`: this repository's own node gate
# could not MEASURE this repository.
#
# `node_projects.json` is the repository's coverage statement, and the gate is
# deliberately asymmetric about it (`_find_node_projects`): the file decides
# what RUNS, discovery decides what must be REPORTED. Every discovered node
# project the file omits becomes an `undeclared` absence, the report's
# `skipped` flag goes up, and `release_disposition` answers `unresolved` -- a
# permanent human park on every gate run of this repository. The park is not
# wrong; the file was. It had fallen behind the tree it speaks for.
#
# So this test recomputes discovery from the tree this file lives in and holds
# the declaration against it, exactly as the gate does. A node project may be
# left out of the declaration, but only by being named in EXCEPTIONS with the
# measurement that kept it out: an unmeasured omission is the defect this test
# exists to catch, and an EXCEPTIONS entry cannot be written without a number
# from a run -- a NON-ZERO one, because a directory whose suite passes belongs
# in the declaration, not beside it. The entries are measured in a throwaway
# container on the gate's own shape (`docker run --rm --network none --cpus 2
# --memory 2g`,
# an `npm ci`/`npm install --offline` install then `npm test` inside the gate's
# 300 s script budget);
# the log named in each entry is kept beside the run's report, not in the
# repository.
#
# Nothing here runs npm: the point is the DECLARATION against discovery, and a
# pytest that installed five node projects would be its own kind of park.
import re
from pathlib import Path

import aitelier.tools.run_tests.impl as impl


REPO_ROOT = Path(__file__).resolve().parents[2]

# Discovered directories that are deliberately NOT declared, each with the
# measurement that decided it. Both were measured in the gate's own shape; both
# fail there, so declaring either would turn a documented absence into a red
# gate -- which measures nothing and hides which suite is broken.
EXCEPTIONS = {
    "integrations/dsh-director": {
        "reason": (
            "its 23-file suite does not finish inside the gate's 300 s script "
            "budget, and not inside 900 s either: the memory-* files alone take "
            "hundreds of seconds in a 2-CPU container. It also has one test "
            "that walks the plugin directory and fails while sibling test files "
            "are still running there, so a declared run would be a timeout or a "
            "red on every gate"),
        "evidence": (
            "dsh-director.log: `npm test` rc 124 (timed out) after 302 s with "
            "test/memory-purge.test.mjs interrupted; dsh-director-diagnostic.log: "
            "the same command rc 124 after 900 s with 317 of 328 tests passed, "
            "10 cancelled and that one walk test failed (it passes alone: rc 0)"),
    },
}

# Evidence must carry a number from a run, not a feeling about the code -- and
# the number has to be one only a FAILING run produces. `rc 0` is a suite that
# passed, and an exception justified by a passing measurement keeps a project
# the gate could measure out of `node_projects.json` for as long as nobody looks
# again: the entry stays, the file stays silent, and the gate's report keeps
# calling a green directory an absence. That is the rot this table exists to
# prevent, so a zero code is not evidence.
_NONZERO = re.compile(r"\b(?:rc|exit code)\s*([1-9]\d*)")


def _measured_failure(evidence):
    """The exit code `evidence` cites if that code is a failure, else None.

    One landing point for "is this evidence a measurement of a red run?", asked
    both by the EXCEPTIONS assertion below and by the witness that pins the
    refusal of a zero code -- so relaxing the check cannot pass unfalsified.
    """
    match = _NONZERO.search(evidence)
    return int(match.group(1)) if match else None


def _relative(path):
    return str(Path(path).relative_to(REPO_ROOT)) or "."


def _discovered():
    """What the gate's own discovery finds in the tree this file lives in."""
    return [_relative(path) for path in impl._discovered_node_projects(REPO_ROOT)]


def _declared():
    """What this repository says its coverage is, refusing an unusable file."""
    declared, problems = impl._declared_node_projects(REPO_ROOT)
    assert problems == [], problems
    assert declared is not None, "node_projects.json is missing or unreadable"
    return [_relative(path) for path in declared]


def test_every_discovered_node_project_is_declared_or_a_measured_exception():
    """The defect itself: discovery may not outrun the declaration silently."""
    discovered = _discovered()
    declared = _declared()
    # A tree with nothing to measure would pass every assertion below without
    # witnessing anything; say so instead of going quietly green.
    assert discovered, "discovery found no node project in this repository"

    missing = [d for d in discovered if d not in declared and d not in EXCEPTIONS]
    assert missing == [], (
        "discovered node project(s) neither declared in node_projects.json nor "
        "named with a measurement in this test's EXCEPTIONS: "
        + ", ".join(missing)
        + ". Measure each one in the gate's shape and then either declare it or "
        "add it to EXCEPTIONS with its log and exit code.")


def test_the_declaration_names_projects_that_can_be_measured():
    """A declaration is only coverage if each entry is a real node project."""
    declared = _declared()
    assert declared, "node_projects.json declares no project"
    for directory in declared:
        assert (REPO_ROOT / directory / "package.json").is_file(), (
            f"declared project {directory} has no package.json")


def test_each_exception_is_a_discovered_project_that_is_not_declared():
    """Both ways an exception rots: it stops existing, or it gets declared."""
    discovered = _discovered()
    declared = _declared()
    for directory, entry in EXCEPTIONS.items():
        assert directory in discovered, (
            f"EXCEPTIONS names {directory}, which discovery no longer finds: "
            "the directory moved or went away, and the entry describes nothing")
        assert directory not in declared, (
            f"EXCEPTIONS names {directory}, which node_projects.json now "
            "declares: the entry (and its failing measurement) is stale")
        assert entry["reason"].strip(), f"EXCEPTIONS[{directory}] has no reason"
        assert _measured_failure(entry["evidence"]) is not None, (
            f"EXCEPTIONS[{directory}] carries no measured FAILING exit code; "
            "evidence must name the log and a NON-ZERO code a run produced "
            "(`rc 0` is a suite that passed: declare the project, do not "
            "except it)")


def test_an_exception_measured_green_is_refused():
    """The other way this table rots: the suite went green and the entry stayed.

    `rc 0` is what a working suite reports, so evidence citing one is not
    evidence of the failure an exception exists to describe -- it is a project
    this repository can declare. The check must refuse it, and must keep
    accepting the non-zero codes a real exception rests on: 1 for a red suite,
    124 for the gate's own timeout.
    """
    green = "measured: rc 0 in 4 s -- it actually passed"
    assert _measured_failure(green) is None, (
        "an `rc 0` measurement was accepted as evidence: the suite passed, so "
        "the directory belongs in node_projects.json, not in EXCEPTIONS")
    for evidence, code in (
        ("dsh-channel-slack.log: `npm test` rc 1 in 2 s", 1),
        ("dsh-director.log: `npm test` rc 124 (timed out) after 302 s", 124),
        ("dsh-director-diagnostic.log: the same command exit code 124 "
         "after 900 s (it passes alone: rc 0)", 124),
    ):
        assert _measured_failure(evidence) == code, evidence


def test_the_gate_would_park_only_on_the_measured_exceptions():
    """The end this declaration serves: a gate run of THIS repository.

    `_find_node_projects` is the gate's own decision, not a restatement of
    discovery: it returns what the run visits and every project it will not.
    After this change the absences it reports are exactly the measured
    exceptions -- no undeclared project the repository never looked at.
    """
    visited, absent = impl._find_node_projects(REPO_ROOT)
    assert [_relative(path) for path in visited] == _declared()
    assert [entry["kind"] for entry in absent] == ["undeclared"] * len(absent), absent
    assert sorted(entry["dir"] for entry in absent) == sorted(EXCEPTIONS), absent
