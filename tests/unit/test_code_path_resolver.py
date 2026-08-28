"""The code-path resolver's three answers, and why there are three.

skillflow asks the host where a project's code lives. Two answers were never
enough: a path ("this repo"), and None ("no opinion, use your default layout").
A run created with `repo_type: none` owns no repository at all —
`setup_workspace` deliberately creates nothing for it — but None made skillflow
invent `projects_base/<id>` anyway, and the read surface attached that invented
path as a `repo` source on an `is_dir()` check. Whether such a run could read a
repository therefore depended on whether a directory happened to exist.

False is the third answer: "there is no code repository."
"""
from __future__ import annotations

import pytest

import api.dependencies as deps


class _DB:
    def __init__(self, info):
        self._info = info

    def get_repo_info(self, project_id):
        if self._info is None:
            raise ValueError(f"Project {project_id} not found")
        return self._info


@pytest.fixture
def db(monkeypatch):
    def _use(info):
        monkeypatch.setattr(deps, "db_instance", _DB(info))
    return _use


def test_a_declared_repoless_run_answers_false(db):
    db({"repo_type": "none", "repo_path": None, "repo_url": None})
    assert deps._existing_repo_code_path("p") is False


def test_an_existing_repo_answers_with_its_path(db):
    db({"repo_type": "existing", "repo_path": "/repos/jinyong-assets",
        "repo_url": None})
    assert deps._existing_repo_code_path("p") == "/repos/jinyong-assets"


def test_new_and_clone_still_answer_none(db):
    """None = "no opinion": skillflow's default projects_base/<id> IS correct
    for these, which is why it must stay distinguishable from False."""
    for rt in ("new", "clone"):
        db({"repo_type": rt, "repo_path": None, "repo_url": None})
        assert deps._existing_repo_code_path("p") is None


def test_false_is_not_merely_falsy_it_is_the_specific_answer(db):
    """`is False`, not `not x`. Callers branch on the difference: None falls
    through to the default layout, False must not."""
    db({"repo_type": "none", "repo_path": None, "repo_url": None})
    got = deps._existing_repo_code_path("p")
    assert got is False and got is not None


def test_an_unreadable_row_stays_no_opinion_not_no_repo(db):
    """A lookup failure must not be reported as "this run owns no repo" — that
    would silently strip repo access from a project that has one."""
    db(None)
    assert deps._existing_repo_code_path("p") is None
