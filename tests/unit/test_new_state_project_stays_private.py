"""Plugin step 10: a State project created from the dashboard is NOT published.

The dashboard creates a project with the owner's session through the State
command route (`create_project`); publishing stays the separate, recorded
`open_project` decision. So right after creation an anonymous reader must not
see the project on any read face, while the owner (a trusted writer) does.
"""
from __future__ import annotations

import pytest

from core.state_commands import ProjectPrivate, execute
from core.state_service import StateService
from tests.support import state_canaries as C

NEW = "step10-fresh-project"


@pytest.fixture
def database(tmp_path):
    return C.new_database(tmp_path, "step10.sqlite")


@pytest.fixture
def owner(database):
    return StateService(database, actor="owner:someone@example.com", project_read_trusted=True)


@pytest.fixture
def anonymous(database):
    return StateService(database, actor="anonymous", project_read_trusted=False)


def _ids(answer):
    # list_projects answers a list of rows; project_catalog a page {projects: [...]}.
    rows = answer["projects"] if isinstance(answer, dict) else answer
    return {row["project_id"] for row in rows}


def test_created_project_is_private_until_opened(owner, anonymous):
    created = execute(owner, "create_project", {"project_id": NEW, "title": "Fresh idea"}, allow_write=True)
    assert created["project_id"] == NEW

    # The owner sees it on both listing faces.
    assert NEW in _ids(execute(owner, "list_projects", {}))
    assert NEW in _ids(execute(owner, "project_catalog", {}))

    # An anonymous reader sees it on none of them, and a by-id read is refused
    # as private (the same refusal as for a project that does not exist).
    assert NEW not in _ids(execute(anonymous, "list_projects", {}))
    assert NEW not in _ids(execute(anonymous, "project_catalog", {}))
    with pytest.raises(ProjectPrivate):
        execute(anonymous, "project_overview", {"project_id": NEW})


def test_opening_is_what_publishes_it(owner, anonymous):
    """Control: the same project becomes visible only through open_project."""
    execute(owner, "create_project", {"project_id": NEW, "title": "Fresh idea"}, allow_write=True)
    execute(owner, "open_project", {"project_id": NEW}, allow_write=True)
    assert NEW in _ids(execute(anonymous, "list_projects", {}))
