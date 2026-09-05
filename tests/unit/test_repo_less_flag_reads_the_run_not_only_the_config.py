"""`repo_less` is a fact about a RUN, and deciding it from the config alone lied.

The dashboard buckets every run exactly once: authoring → generation, else
`repo_less` → the pipeline section, else a missing `repo_path` → orphans
(`web/src/views/UnifiedDashboard.svelte:bucketRuns`). Meanwhile `/api/repos`
builds its repository groups from a completely independent query: every row whose
`repo_path` is non-empty. Nothing reconciles the two, so a row that answers "I am
repo-less" *and* carries a real path renders in both places at once.

`repo_mode` is declared per CONFIG; owning a repository is per RUN. `against_project`
is precisely where they part company — `run_launcher` stamps `repo_type='none'` for a
`repo_mode: none` config but deliberately KEEPS the caller's `repo_path`, the
documented "emits no code, reads a real repo" shape (`core/workspace_manager.py`).
Judging on the manifest alone therefore mislabelled every such run.

This was latent for as long as no `repo_mode: none` config was ever pointed at a
repository, and went live the moment `code_review` declared `repo_mode: none`: a
review launched with `against_project` immediately rendered in the non-code section
while `/api/repos` still grouped it under its very real path.

Both halves of the predicate are load-bearing, so each gets its own claim below —
an `and` across two fields passes happily while one side is deleted, and a single
combined assertion would go green on half the fix.
"""
import api.run_routers as rr


class _Manifest:
    def __init__(self, repo_mode):
        self.repo_mode = repo_mode
        self.label = "L"
        self.has_task_loop = False
        self.registers_generated_pipeline = False
        self.registers_generated_addon = False


class _Registry:
    def __init__(self, mode):
        self._m = _Manifest(mode)

    def get(self, _cfg):
        return self._m


class _DB:
    def __init__(self, rows):
        self._rows = rows

    def list_projects_with_stats(self, owner_email=None):
        return self._rows


class _SF:
    def list_runs(self, project_id=None):
        return []


def _flag(monkeypatch, repo_mode, repo_path):
    """Run the real listing and return the single row's `repo_less`."""
    monkeypatch.setattr(rr, "get_skillflow", lambda: _SF())
    monkeypatch.setattr(rr, "enrich_project_status", lambda r: r)
    row = {"project_id": "p1", "config_name": "code_review",
           "repo_path": repo_path}
    out = rr._list_all_runs_uncached("o@e", _DB([row]), _Registry(repo_mode))
    return out[0]["repo_less"]


def test_a_repoless_config_with_no_repository_is_repo_less(monkeypatch):
    """The plain case: nothing was handed a repo, nothing claims one."""
    assert _flag(monkeypatch, "none", None) is True


def test_a_repoless_config_pointed_at_a_real_repo_is_not_repo_less(monkeypatch):
    """The `against_project` shape. Fails if the `not repo_path` half is dropped.

    Without this the run is bucketed into the non-code section while
    `/api/repos` groups it under `repo_path` — visible in two sections at once.
    """
    assert _flag(monkeypatch, "none", "/home/u/.AItelier/projects/real") is False


def test_a_code_config_is_never_repo_less_even_before_its_repo_exists(monkeypatch):
    """Fails if the manifest half is dropped and the flag reads `repo_path` alone.

    A code-producing config's row is written before `setup_workspace` fills in a
    path on some branches; inferring "repo-less" from the empty path would drag a
    real build out of the repository list.
    """
    assert _flag(monkeypatch, "code", None) is False
