"""The anonymous project overview does not name a host path.

`project_overview` is a public read, and its `source` block is the same source
binding the anonymous catalog already blanks: `repo_path` and `common_dir` are
directories on THIS host (`/home/<user>/.AItelier/projects/<dir>`), and the
repository tree they name is refused to an anonymous caller. An anonymous
overview keeps the public project, its nodes, issue work and counts, and the
shape of the `source` block, but blanks the two path values; a trusted writer
keeps the full binding, unchanged.

Real store, real `execute` handler; the caller's trust is the only thing that
changes between the two arms, as in `test_public_catalog_source_path.py`.
"""
from __future__ import annotations

import re

from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_service import StateService

PID = "demo-project"
REPO_PATH = "/home/somebody/.AItelier/projects/demo-repo"
COMMON_DIR = "/home/somebody/.AItelier/projects/demo-repo/.git"
CRIT = [{"id": "fixed", "kind": "test", "description": "the reported input is no longer dropped"}]

# The same host-path shape the overview must never carry to an anonymous caller.
HOST_PATH = re.compile(r"(^|\s)/(home|Users|srv|opt|app|var)/")


def _strings(value):
    """Every string value in a JSON-like answer, at any depth."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _service(tmp_path):
    svc = StateService(StateDatabase(str(tmp_path / "state.sqlite")),
                       actor="overview-source-path", project_read_trusted=True)
    svc.create_project(PID, "Demo project")
    svc.store.add_nodes(PID, [{"key": "a", "goal": "A", "acceptance": [
        {"id": "c", "kind": "test", "description": "d"}]}])
    svc.portfolio.bind_source(PID, REPO_PATH, COMMON_DIR, 0)
    svc.open_project(PID)
    # One open issue work item, so `issue_work` is non-empty in the answer.
    issue = execute(svc, "report_issue", {"project_id": PID, "request_key": "r1", "kind": "defect",
                                          "title": "input dropped", "body": "measured: key press is discarded",
                                          "director_identity": "wuxia"}, allow_write=True)
    execute(svc, "start_issue_work", {"project_id": PID, "issue_id": issue["issue_id"],
                                      "expected_version": issue["version"], "acceptance": CRIT,
                                      "director_identity": "wuxia"}, allow_write=True)
    return svc


def _overview(service, trusted):
    service.project_read_trusted = trusted
    return execute(service, "project_overview", {"project_id": PID})


def test_anonymous_overview_keeps_the_project_but_names_no_host_path(tmp_path):
    service = _service(tmp_path)
    writer = _overview(service, trusted=True)
    answer = _overview(service, trusted=False)

    # (a) The public project is still there: its nodes, issue work and counts.
    assert answer["project"]["project_id"] == PID
    assert answer["nodes"] and answer["issue_work"] and answer["counts"]
    assert [n["node_key"] for n in answer["nodes"]] == [n["node_key"] for n in writer["nodes"]] == ["a"]
    assert answer["counts"] == writer["counts"] == {"OPEN": 1}
    assert len(answer["issue_work"]) == len(writer["issue_work"]) == 1

    # (b) No string anywhere in the anonymous answer is a host path.
    leaked = [s for s in _strings(answer) if HOST_PATH.search(s)]
    assert leaked == [], f"anonymous overview names a host path: {leaked}"

    # (c) The source block is still a dict with the same keys: only the values go.
    assert isinstance(answer["source"], dict)
    assert set(answer["source"]) == set(writer["source"])
    assert answer["source"]["kind"] == writer["source"]["kind"] == "stable_binding"


def test_trusted_writer_overview_keeps_the_full_source_binding(tmp_path):
    service = _service(tmp_path)
    answer = _overview(service, trusted=True)

    # The writer still sees the binding exactly as recorded.
    assert answer["source"]["repo_path"] == REPO_PATH
    assert answer["source"]["common_dir"] == COMMON_DIR
    assert answer["source"]["kind"] == "stable_binding"
    assert any(HOST_PATH.search(s) for s in _strings(answer)), \
        "the writer overview must still carry the path (the control)"


def test_host_path_matcher_has_a_positive_control():
    # The scan must be able to find the leak it guards against.
    assert HOST_PATH.search(REPO_PATH)
    assert HOST_PATH.search(f"x {COMMON_DIR}")
    assert not HOST_PATH.search("demo-repo")
