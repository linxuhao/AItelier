"""A warm-up pass must DERIVE each project once, whichever view builds first.

`api/dependencies._enrichment_memo_key` keys the enrichment memo on the row's own
change fields:

    (project_id, status, updated_at, current_project_step, config_name)

`api/project_routers._list_projects_uncached` hands it rows from
`db.list_projects_with_stats(...)`, which carries all five. The repos builder,
`api/repo_routers._build_repo_groups`, hands it rows straight out of

    SELECT project_id, name, status, updated_at FROM runs WHERE repo_path = ?

— so two of the five are ABSENT. `project.get("config_name")` is then always
None and the same project hashes to a different key depending on which view built
the row. A warm-up pass over both views (the startup prefetch builds `("repos",
None)` and `("projects", None)`) therefore pays for every derivation twice, and
an investigation on the live backend measured exactly that: 2893
`enrich_project_status` calls, 2699 real derivations, only 194 memo hits, for
1545 projects. The same omission has a second consequence:
`_derive_project_enrichment` resolves

    cfg = run.get("graph_name") or project.get("config_name") or "dpe_default_v2"

so for a run row without a `graph_name` the repos view falls through to the
default while the projects view uses the row's real config — the two views can
report different `config_name` for the same project.

What is witnessed here, in the order the acceptance asks for it:

* **one-derivation-per-project.** N projects, the repos builder then the
  projects builder (and the reverse order). The REAL derivation is counted by
  wrapping `_derive_project_enrichment` — the function the memo calls — because
  memo hits alone would not distinguish "derived once" from "never derived".
  Exactly N derivations are allowed: one per project. The earlier pass's help is
  the only thing that may make the second pass cheaper.
* **the row shape, not the assertions, is what makes that work.** Each raw row
  handed to the derivation is captured and its `_enrichment_memo_key` compared
  across the two views: same project, same key, whichever view built it.
* **both-views-agree.** A run row with NO `graph_name`: the two views must derive
  the same `config_name` (and the same decomposition of it).
* **response-shape-kept.** `GET /api/repos` keeps the shape its docstring
  documents: each group carries `repo_path, repo_name, repo_type, repo_url,
  project_count, representative_project_id, last_activity, projects`, and each
  `projects[]` entry still carries `project_id, name, status, updated_at`.

Nothing here is measured in seconds, and nothing is asserted about the memo's
internals: the count of `_derive_project_enrichment` calls is the whole witness.
"""
from __future__ import annotations

import pytest

import api.project_routers as project_routers
import api.repo_routers as repo_routers
from api import dependencies as deps
from api.dependencies import get_db_manager
from api.main import app

#: Small on purpose: the failure is per-project, not statistical, and a fixture
#: of a handful keeps the reason legible in the assertion message.
N_PROJECTS = 6

REPO_PATHS = ("/srv/repo-a", "/srv/repo-b")

#: The row's real config, deliberately NOT the `dpe_default_v2` fallback: the
#: disagreement this file witnesses is invisible when the fallback and the DB
#: happen to carry the same name.
CONFIG_NAME = "gen_repos_enrichment"


class _Engine:
    """A skillflow stand-in: run rows WITHOUT `graph_name`, plus the calls the
    two builders make around the derivation (`describe_config` through
    `core.addon_registry`, `list_runs` for the cache-stats pass)."""

    _trace_db_path = None

    def __init__(self, runs: dict[str, dict]):
        self.runs = dict(runs)
        self.steps = [{"step_id": "1", "status": "completed"},
                      {"step_id": "2", "status": "pending"}]

    def get_run_by_project(self, pid):
        return self.runs.get(pid)

    def list_runs(self, project_id=None, **_kwargs):
        # The repos builder's cache-stats pass asks this; answering empty keeps
        # the test out of the trace-database sidecar, and enrichment never
        # reaches it (the run lookup above answers first).
        return []

    def get_steps(self, run_id):
        return list(self.steps)

    def describe_config(self, name):
        return {"base": name, "addons": []}


def _run(pid: str) -> dict:
    """A run row with NO `graph_name` — the shape the second defect needs."""
    return {"id": f"run-{pid}", "project_id": pid, "status": "running",
            "current_node": "t_impl"}


@pytest.fixture(autouse=True)
def _fresh_memo():
    """The memo is process-global: one test's warm entries would be another's
    (wrong) head start."""
    deps.clear_enrichment_memo()
    yield
    deps.clear_enrichment_memo()


@pytest.fixture
def engine(monkeypatch):
    """The one engine both builders resolve, so the memo's engine-identity guard
    is not what makes a second pass cheap."""
    def _install(pids) -> _Engine:
        sf = _Engine({pid: _run(pid) for pid in pids})
        monkeypatch.setattr(deps, "get_skillflow", lambda: sf)
        # No real config registry in a unit test (`_derive_project_enrichment`
        # asks it for the manifest; `None` is the miss path it already handles).
        monkeypatch.setattr(deps, "get_config_registry", lambda: None)
        return sf
    return _install


@pytest.fixture
def db(db_manager):
    """N real projects, in two repositories, with rows carrying every field the
    memo key reads — the shape `list_projects_with_stats` returns."""
    for i in range(N_PROJECTS):
        pid = f"proj-{i}"
        db_manager.ensure_project(
            pid, name=f"Project {i}", repo_type="existing",
            repo_path=REPO_PATHS[i % len(REPO_PATHS)], repo_url=None,
            config_name=CONFIG_NAME)
        db_manager.update_project(pid, status="running:3",
                                  current_project_step="3")
    return db_manager


def _count_derivations(monkeypatch) -> list[dict]:
    """Wrap the function the memo calls and keep every RAW row it was handed.

    The rows are kept, not just a counter: comparing the memo key of the rows
    the two builders produce is the direct statement of "same row shape", and it
    explains the count rather than merely reporting it.
    """
    real = deps._derive_project_enrichment
    handed: list[dict] = []

    def counting(project, sf):
        handed.append(dict(project))
        return real(project, sf)

    monkeypatch.setattr(deps, "_derive_project_enrichment", counting)
    return handed


def _build(order: str, db) -> None:
    if order == "repos-then-projects":
        repo_routers._list_repos_uncached(db)
        project_routers._list_projects_uncached(db, None)
    else:
        project_routers._list_projects_uncached(db, None)
        repo_routers._list_repos_uncached(db)


def _project_in(groups: list[dict], pid: str) -> dict:
    for group in groups:
        for row in group["projects"]:
            if row["project_id"] == pid:
                return row
    raise AssertionError(f"{pid} is in no repo group: "
                         f"{[g['repo_path'] for g in groups]!r}")


def _projects_view_row(rows: list[dict], pid: str) -> dict:
    for row in rows:
        if row["project_id"] == pid:
            return row
    raise AssertionError(f"{pid} is in no project row: "
                         f"{[r['project_id'] for r in rows]!r}")


class TestAWarmUpPassDerivesEachProjectOnce:
    @pytest.mark.parametrize("order", ["repos-then-projects",
                                       "projects-then-repos"])
    def test_two_views_over_one_fixture_derive_each_project_once(
            self, db, engine, monkeypatch, order):
        """N projects, both views: exactly N derivations, and each project seen
        once — the other view is served from the memo.

        On the tree before the fix this file was written against, this same
        fixture derives 2N: the repos rows lack `config_name` and
        `current_project_step`, so their memo key cannot match the projects
        rows' and the second view re-derives everything.
        """
        pids = [f"proj-{i}" for i in range(N_PROJECTS)]
        engine(pids)

        handed = _count_derivations(monkeypatch)
        _build(order, db)

        print(f"\n{order}: {len(handed)} real derivations for "
              f"{N_PROJECTS} projects: {[r['project_id'] for r in handed]}")
        assert len(handed) == N_PROJECTS, (
            f"{order}: {len(handed)} real derivations for {N_PROJECTS} "
            f"projects; a warm-up pass over both views must derive each project "
            f"once and serve the other view from the memo. The rows handed to "
            f"the derivation, in order: {[r['project_id'] for r in handed]}")
        assert sorted(r["project_id"] for r in handed) == sorted(pids), (
            "every project must be derived exactly once — no project may be "
            f"skipped and none twice: {[r['project_id'] for r in handed]}")

    def test_each_view_hands_the_memo_the_same_key(self, db, engine, monkeypatch):
        """The count above is a consequence of this: one memo key per project,
        whichever of the two builders produced the row.

        Each view is built against its OWN cold memo, so each really derives and
        the captured rows are the raw ones that builder selected, not the
        enriched copies. A view that selects a subset of the key fields shows up
        here as two different tuples for one project — the `None` being the
        absent column.
        """
        pids = [f"proj-{i}" for i in range(N_PROJECTS)]
        engine(pids)

        def _keys_for(build) -> dict[str, tuple]:
            deps.clear_enrichment_memo()
            handed = _count_derivations(monkeypatch)
            build()
            assert len(handed) == N_PROJECTS, (
                f"one view alone must derive every project once, got "
                f"{len(handed)}: {[r['project_id'] for r in handed]}")
            return {row["project_id"]: deps._enrichment_memo_key(row)
                    for row in handed}

        repos_keys = _keys_for(lambda: repo_routers._list_repos_uncached(db))
        projects_keys = _keys_for(
            lambda: project_routers._list_projects_uncached(db, None))

        assert set(repos_keys) == set(pids) == set(projects_keys), (
            f"derivations were captured for repos={sorted(repos_keys)}, "
            f"projects={sorted(projects_keys)}, expected {pids}")
        for pid in pids:
            print(f"\n{pid}: repos key={repos_keys[pid]!r} "
                  f"projects key={projects_keys[pid]!r}")
            assert repos_keys[pid] == projects_keys[pid], (
                f"{pid}: the repos builder and the projects builder hand the "
                f"memo DIFFERENT keys — repos={repos_keys[pid]!r}, "
                f"projects={projects_keys[pid]!r}. The memo key is "
                f"(project_id, status, updated_at, current_project_step, "
                f"config_name); a None in one of the last two is the column the "
                f"repos builder failed to select.")


class TestTheTwoViewsAgreeOnTheConfig:
    @pytest.mark.parametrize("order", ["repos-then-projects",
                                       "projects-then-repos"])
    def test_a_run_row_without_a_graph_name_derives_the_same_config(
            self, db, engine, monkeypatch, order):
        """`cfg = run.get("graph_name") or project.get("config_name") or
        "dpe_default_v2"` — with no `graph_name`, whichever view builds the row
        must offer the same fallback, or the dashboard shows one project under
        two different configs.
        """
        pids = [f"proj-{i}" for i in range(N_PROJECTS)]
        sf = engine(pids)
        assert all("graph_name" not in run for run in sf.runs.values())

        if order == "repos-then-projects":
            repos_groups = repo_routers._list_repos_uncached(db)
            projects_rows = project_routers._list_projects_uncached(db, None)
        else:
            projects_rows = project_routers._list_projects_uncached(db, None)
            repos_groups = repo_routers._list_repos_uncached(db)

        for pid in pids:
            repo_row = _project_in(repos_groups, pid)
            project_row = _projects_view_row(projects_rows, pid)
            print(f"\n{pid}: repos={repo_row.get('config_name')!r} "
                  f"projects={project_row.get('config_name')!r}")
            assert repo_row["config_name"] == CONFIG_NAME, (
                f"{pid}: the repos view derived config_name "
                f"{repo_row['config_name']!r}; the run row has no graph_name, so "
                f"it must fall back to the project row's {CONFIG_NAME!r}")
            assert repo_row["config_name"] == project_row["config_name"], (
                f"{pid}: the two views disagree — repos="
                f"{repo_row['config_name']!r}, projects="
                f"{project_row['config_name']!r}")
            assert repo_row["config_base"] == project_row["config_base"], (
                f"{pid}: same config_name, different decomposition")
            assert repo_row["config_label"] == project_row["config_label"]


class TestTheReposResponseShapeIsKept:
    #: What `_build_repo_groups`' docstring documents, and what the SPA reads.
    GROUP_KEYS = {"repo_path", "repo_name", "repo_type", "repo_url",
                  "project_count", "representative_project_id", "last_activity",
                  "projects"}
    PROJECT_KEYS = {"project_id", "name", "status", "updated_at"}

    def test_get_api_repos_still_carries_the_documented_fields(
            self, client, engine):
        """Over HTTP, through the real route, on a fixture database: the group
        keys are all still there and so are the four project keys.

        Extra keys are not the concern (enrichment has always added fields); a
        MISSING documented key is.
        """
        db = app.dependency_overrides[get_db_manager]()
        for i in range(N_PROJECTS):
            pid = f"proj-{i}"
            db.ensure_project(pid, name=f"Project {i}", repo_type="existing",
                              repo_path=REPO_PATHS[i % len(REPO_PATHS)],
                              repo_url=None, config_name=CONFIG_NAME)
            db.update_project(pid, status="running:3",
                              current_project_step="3")
        engine([f"proj-{i}" for i in range(N_PROJECTS)])

        response = client.get("/api/repos")
        assert response.status_code == 200, response.text
        groups = response.json()

        assert len(groups) == len(REPO_PATHS), (
            f"expected one group per repository, got "
            f"{[g.get('repo_path') for g in groups]!r}")
        seen = set()
        for group in groups:
            missing = self.GROUP_KEYS - set(group)
            assert not missing, f"the group lost {sorted(missing)}: {group!r}"
            assert group["project_count"] == len(group["projects"]), group
            ids = [p["project_id"] for p in group["projects"]]
            assert group["representative_project_id"] in ids, group
            assert group["last_activity"] == max(
                p["updated_at"] for p in group["projects"]), group
            assert group["repo_name"] == group["repo_path"].rstrip("/").split("/")[-1]
            for project in group["projects"]:
                missing = self.PROJECT_KEYS - set(project)
                assert not missing, (
                    f"the repo group's project entry lost {sorted(missing)}: "
                    f"{project!r}")
                seen.add(project["project_id"])

        assert seen == {f"proj-{i}" for i in range(N_PROJECTS)}, (
            f"the response lost projects: {sorted(seen)!r}")
