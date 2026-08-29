"""The code-path resolver's three answers, and why there are three.

skillflow asks the host where a project's code lives. Two answers were never
enough: a path ("this repo"), and None ("no opinion, use your default layout").
A run created with `repo_type: none` owns no repository at all —
`setup_workspace` deliberately creates nothing for it — but None made skillflow
invent `projects_base/<id>` anyway, and the read surface attached that invented
path as a `repo` source on an `is_dir()` check. Whether such a run could read a
repository therefore depended on whether a directory happened to exist.

False is the third answer: "there is no code repository."

The second half of this file is the other side of the same question. The host
answers it TWICE — once for skillflow (`_existing_repo_code_path`) and once for
itself (`WorkspaceManager.get_code_path`, whose result it then hands skillflow as
`project_root`) — and the two must agree, predicate for predicate, or a run has a
repository for its write tools and none for its read tools.
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


def test_a_row_holding_both_answers_keeps_its_repo(db):
    """`repo_type='none'` AND a `repo_path` is a real row, not a contradiction.

    `core/run_launcher.py` overwrites a caller-supplied `repo_type="existing"`
    with `"none"` whenever the config declares `repo_mode: none`, and keeps the
    caller's `repo_path`. The `against_project` path (`api/mcp_router.py`,
    `core/meta_agent.py:_tool_start_config_run`) produces exactly that: a run
    that emits no code of its own but was pointed at a real repository to READ.

    Testing `repo_type` first answered False for those and took the repository
    away — from the one run shape whose entire purpose is to look at it.

    Also the counter-example to "a `repo_mode: none` run gets no repo layer at
    all": it gets one whenever a `repo_path` is recorded, whatever `repo_type`
    says. `core/pipeline_registry.py`'s `_REPO_TOOLS` comment reasons about that
    claim, so keep the two in step.
    """
    db({"repo_type": "none", "repo_path": "/repos/jinyong-assets",
        "repo_url": None})
    assert deps._existing_repo_code_path("p") == "/repos/jinyong-assets", (
        "a recorded repo_path no longer wins over repo_type='none' — the "
        "against_project shape just lost its repository, and the comment in "
        "core/pipeline_registry.py is now wrong the other way")


# ── The host's own answer must match the one it gives skillflow ───────────

class _WSDB:
    def __init__(self, info):
        self._info = info

    def get_repo_info(self, project_id):
        if self._info is None:
            raise ValueError(f"Project {project_id} not found")
        return self._info


@pytest.fixture
def ws(tmp_path, monkeypatch):
    from core.workspace_manager import WorkspaceManager

    def _use(info):
        monkeypatch.setattr(deps, "get_db_manager", lambda: _WSDB(info))
        m = WorkspaceManager(base_path=str(tmp_path / "ws"))
        m.projects_base = tmp_path / "projects"
        m.projects_base.mkdir(parents=True, exist_ok=True)
        return m
    return _use


def test_the_host_gives_a_repoless_project_no_code_path_either(ws, tmp_path):
    """`get_code_path` is what the host hands skillflow as `project_root` for
    every agent-invoked tool (`core/dpe_pipeline.py:_exec_tool`). It never read
    `repo_type`, so on a repo-less run the host handed over a repository while
    skillflow — asking the resolver above — believed there was none:
    `read`/`list`/`search` saw no repo and `create`/`edit` had a baseline
    pointing at one. The two halves have to answer the same question the same
    way.
    """
    m = ws({"repo_type": "none", "repo_path": None, "repo_url": None})
    assert m.get_code_path("p") is None


def test_no_directory_is_materialised_for_a_project_that_wants_no_repo(ws,
                                                                       tmp_path):
    """The unconditional `mkdir` is what put the empty directories under
    `~/.AItelier/projects/` there — and an existing directory is precisely what
    made skillflow's `is_dir()` repo layer attach itself to a repo-less run."""
    m = ws({"repo_type": "none", "repo_path": None, "repo_url": None})
    m.get_code_path("p")
    assert not (tmp_path / "projects" / "p").exists()


def test_the_host_control_still_creates_and_returns_the_default(ws, tmp_path):
    """Same code, `repo_type: new` — the default layout still applies and the
    directory is still created. Without this the tests above would pass on a
    build that had simply broken `get_code_path` for everyone."""
    m = ws({"repo_type": "new", "repo_path": None, "repo_url": None})
    assert m.get_code_path("p") == tmp_path / "projects" / "p"
    assert (tmp_path / "projects" / "p").is_dir()


def test_the_host_agrees_with_the_resolver_on_a_row_holding_both_answers(
        ws, tmp_path):
    """Same predicate as `_existing_repo_code_path`, and it must stay the same:
    an `against_project` run reads the repo it was pointed at."""
    linked = tmp_path / "jinyong-assets"
    linked.mkdir()
    m = ws({"repo_type": "none", "repo_path": str(linked), "repo_url": None})
    assert m.get_code_path("p") == linked.resolve()


def test_an_unreadable_row_still_gets_the_default_not_no_repo(ws, tmp_path):
    """Failing to read the row is not a declaration. Answering None there would
    strip the repository from a project that has one every time the lookup
    hiccups — the same asymmetry the resolver above observes."""
    m = ws(None)
    assert m.get_code_path("p") == tmp_path / "projects" / "p"


def test_the_host_never_hands_skillflow_the_string_none_as_project_root(
        monkeypatch):
    """`str(None)` is `"None"` — a truthy RELATIVE path, not an absent one.

    `_exec_tool` forwards the host's code path to `sf.execute_tool` as
    `project_root`. Once `get_code_path` can answer None, a naive `str(...)`
    sends the literal "None", which skillflow's `if not kwargs.get(...)` reads as
    supplied and never replaces — so the repo tools resolve "None" against the
    process CWD, which is exactly the failure the empty-value guards were added
    to stop. "" is the right wire value: it means "no opinion from the host", and
    skillflow then asks its own resolver.
    """
    from core.dpe_pipeline import PipelineEngine

    seen = {}

    class _SF:
        def execute_tool(self, name, params, **kw):
            seen.update(kw)
            return {}

    monkeypatch.setattr("api.dependencies.get_skillflow", lambda: _SF())

    eng = PipelineEngine.__new__(PipelineEngine)
    eng._current_step = "t_impl"
    eng._code_path = None                       # a repo-less run
    eng._exec_tool({"tool": "read", "params": {}})

    assert seen["project_root"] == "", \
        f"the host sent project_root={seen['project_root']!r} to skillflow"
