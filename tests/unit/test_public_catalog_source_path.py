"""The anonymous project catalog does not name a host path.

A project's source binding records `repo_path` and `common_dir`: directories on
THIS host (`/home/<user>/.AItelier/projects/<dir>`). The repository tree they name
is refused to an anonymous caller, so the path string must not be more visible
than that tree. An anonymous `project_catalog` keeps the public project and its
ids and counts, but blanks the two path values; a trusted writer keeps the full
binding, unchanged.

Real store, real `execute` handler; the caller's trust is the only thing that
changes between the two arms, as in `test_project_catalog_paging_agree.py`.
"""
from __future__ import annotations

import re

from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_service import StateService

PID = "demo-project"
REPO_PATH = "/home/somebody/.AItelier/projects/demo-repo"
COMMON_DIR = "/home/somebody/.AItelier/projects/demo-repo/.git"

# The same host-path shape the catalog must never carry to an anonymous caller.
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
                       actor="catalog-source-path", project_read_trusted=True)
    svc.create_project(PID, "Demo project")
    svc.store.add_nodes(PID, [{"key": "a", "goal": "A", "acceptance": [
        {"id": "c", "kind": "test", "description": "d"}]}])
    svc.portfolio.bind_source(PID, REPO_PATH, COMMON_DIR, 0)
    svc.open_project(PID)
    return svc


def _catalog_row(service, trusted):
    service.project_read_trusted = trusted
    answer = execute(service, "project_catalog", {"limit": 100})
    rows = [p for p in answer["projects"] if p["project_id"] == PID]
    assert len(rows) == 1, answer
    return answer, rows[0]


def test_anonymous_catalog_keeps_the_project_but_names_no_host_path(tmp_path):
    service = _service(tmp_path)
    _, writer_row = _catalog_row(service, trusted=True)
    answer, row = _catalog_row(service, trusted=False)

    # (a) The public project is still there, with the same ids and counts.
    assert row["project_id"] == PID
    assert row["node_count"] == writer_row["node_count"] == 1
    assert row["verified_count"] == writer_row["verified_count"]
    assert row["source"]["kind"] == writer_row["source"]["kind"] == "stable_binding"

    # (b) No string anywhere in the anonymous answer is a host path.
    leaked = [s for s in _strings(answer) if HOST_PATH.search(s)]
    assert leaked == [], f"anonymous catalog names a host path: {leaked}"


def test_trusted_writer_catalog_keeps_the_full_source_binding(tmp_path):
    service = _service(tmp_path)
    answer, row = _catalog_row(service, trusted=True)

    # The writer still sees the binding exactly as recorded.
    assert row["source"]["repo_path"] == REPO_PATH
    assert row["source"]["common_dir"] == COMMON_DIR
    assert row["source"]["kind"] == "stable_binding"
    assert any(HOST_PATH.search(s) for s in _strings(answer)), \
        "the writer catalog must still carry the path (the control)"


def test_host_path_matcher_has_a_positive_control():
    # The scan must be able to find the leak it guards against.
    assert HOST_PATH.search(REPO_PATH)
    assert HOST_PATH.search(f"x {COMMON_DIR}")
    assert not HOST_PATH.search("demo-repo")
