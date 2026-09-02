"""Every name a module references must be bound — checked by ruff F821.

On 2026-09-02 the dashboard's repo listing 500ed with
`NameError: name 'merge_stats' is not defined` (api/repo_routers.py:125): a
patch had added the call and "added the import" by replacing the first
occurrence of `compute_cache_stats_batch` — which was in a COMMENT, not the
function-local import. `python -c "import api.repo_routers"` passed (a
NameError only fires at call time) and the unit test that "checked the
routers" grepped source text. Both were green over an absence. ruff's F821
finds this class in milliseconds; this test makes it part of the suite.

The same scan also found two latent ones: core/scheduler.py used `logging`
in an except branch without importing it (a crash hidden inside error
handling), and core/llm_quota.py annotated a return type with a module it
never imported.
"""
import subprocess, sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def test_no_undefined_names_in_first_party_code():
    r = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--select", "F821",
         "--output-format", "concise", "api/", "core/", "aitelier/"],
        cwd=_ROOT, capture_output=True, text=True)
    assert r.returncode == 0, "undefined names:\n" + r.stdout + r.stderr


def test_the_repo_group_merge_actually_executes(db_manager, monkeypatch):
    """Execute the line that crashed, with data that reaches it.

    A bare `GET /api/repos` on an empty test DB returns 200 without ever
    entering the merge loop (no runs → no groups), which is exactly how the
    first version of this test stayed green over the missing import. So:
    seed one run row, stub skillflow's run list and the batch stats, and
    assert the merged number comes back — the merge has to RUN to produce it.
    """
    from api import repo_routers
    from api._cache_stats import _build_stats_dict
    with db_manager.get_connection() as c:
        c.execute("INSERT INTO runs (project_id, name, repo_path) VALUES (?, ?, ?)",
                  ("p1", "P1", "/tmp/repo-p1"))
        c.commit()

    class _SF:
        def list_runs(self, project_id=None, **kw):
            return [{"id": "run-x"}] if project_id == "p1" else []
    monkeypatch.setattr("api.dependencies.get_skillflow", lambda: _SF())
    monkeypatch.setattr("api._cache_stats.compute_cache_stats_batch",
                        lambda ids: {"run-x": _build_stats_dict(8, 2, 100, 10)})

    groups = repo_routers._build_repo_groups(db_manager)
    projects = [p for g in groups for p in g["projects"]]
    assert projects, "seed row did not produce a group"
    cs = projects[0]["cache_stats"]
    assert cs is not None and cs["total_tokens"] == 110 and cs["hit_ratio"] == 0.8
