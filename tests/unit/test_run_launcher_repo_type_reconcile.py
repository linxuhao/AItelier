"""A project id whose FIRST run declared `repo_mode: none` must not be stuck.

`start_config_run` computes `eff_repo_type` from the config's declared
`repo_mode` and then hands it only to `ensure_project`, which no-ops when the
row already exists. So the first config to use a project id set `repo_type` for
every later run on that id.

That was cosmetic until `WorkspaceManager.get_code_path` learned to answer None
for `repo_type='none'`: a later `dpe_default` run on the same id then gets no
code path, no repo read layer, and dies at its first `on_deliver: repo_apply`
(the guard refuses → `passed=False` → the hook-level policy for a LIST spec
resolves to "fail") — while `setup_workspace` has git-init'd a real repository
at exactly the path the row refuses to name.

The reconciliation is ONE-WAY on purpose; the second half of this file pins that.
"""
from unittest.mock import MagicMock

from core.run_launcher import start_config_run


def _manifest(name, scheduler_owned=True, seed_file="seed.md",
              repo_mode="code"):
    m = MagicMock()
    m.config_name = name
    m.scheduler_owned = scheduler_owned
    m.seed_file = seed_file
    m.repo_mode = repo_mode
    return m


def _patch_registry(monkeypatch, manifest):
    import api.dependencies as deps
    reg = MagicMock()
    reg.get.return_value = manifest
    monkeypatch.setattr(deps, "get_config_registry", lambda: reg, raising=False)
    sf = MagicMock()
    sf.get_or_create_run.return_value = "run-1"
    sf.get_run.return_value = {"status": "running"}
    monkeypatch.setattr(deps, "get_skillflow", lambda: sf, raising=False)
    import core.scheduler as sched
    monkeypatch.setattr(sched, "wake_scheduler", lambda *a, **k: None,
                        raising=False)
    return sf


def _repo_type_writes(db):
    return [c.kwargs["repo_type"] for c in db.update_project.call_args_list
            if "repo_type" in c.kwargs]


def test_a_code_run_reclaims_a_project_id_stamped_repoless(monkeypatch):
    db, ws = MagicMock(), MagicMock()
    db.get_project.return_value = {"project_id": "p", "config_name": "pipeline_forge"}
    db.get_repo_info.return_value = {"repo_type": "none", "repo_path": None,
                                     "repo_url": None}
    _patch_registry(monkeypatch, _manifest("dpe_default"))

    start_config_run(db, ws, "dpe_default", "p", repo_type="new")

    assert _repo_type_writes(db) == ["new"], (
        "the stored repo_type stayed 'none'; get_code_path will answer None and "
        "the first repo_apply will fail the run")


def test_the_reconciled_row_also_names_where_the_code_will_be(monkeypatch):
    """`setup_workspace` is about to create `projects_dir()/<id>` — the row has
    to name it, exactly as the creation path does."""
    db, ws = MagicMock(), MagicMock()
    db.get_project.return_value = {"project_id": "p"}
    db.get_repo_info.return_value = {"repo_type": "none", "repo_path": None,
                                     "repo_url": None}
    _patch_registry(monkeypatch, _manifest("dpe_default"))

    start_config_run(db, ws, "dpe_default", "p", repo_type="new")

    from core.datadir import projects_dir
    paths = [c.kwargs["repo_path"] for c in db.update_project.call_args_list
             if "repo_path" in c.kwargs]
    assert paths == [str(projects_dir() / "p")], paths


def test_an_existing_repo_run_keeps_the_path_it_was_given(monkeypatch):
    db, ws = MagicMock(), MagicMock()
    db.get_project.return_value = {"project_id": "p"}
    db.get_repo_info.return_value = {"repo_type": "none", "repo_path": None,
                                     "repo_url": None}
    _patch_registry(monkeypatch, _manifest("dpe_default"))

    start_config_run(db, ws, "dpe_default", "p", repo_type="existing",
                     repo_path="/repos/jinyong-assets")

    kw = [c.kwargs for c in db.update_project.call_args_list
          if "repo_type" in c.kwargs]
    assert kw and kw[0]["repo_type"] == "existing"
    assert kw[0]["repo_path"] == "/repos/jinyong-assets"


# ── ONE-WAY: a repo-less run never takes a repository away ────────────────

def test_a_repoless_run_does_not_stamp_none_onto_a_code_project(monkeypatch):
    """The reverse direction is the `against_project` shape — a run that emits
    no code of its own but was pointed at a real repository to read. Stamping
    'none' there would take the repository from the build that owns the id."""
    db, ws = MagicMock(), MagicMock()
    db.get_project.return_value = {"project_id": "p"}
    db.get_repo_info.return_value = {"repo_type": "new",
                                     "repo_path": "/projects/p",
                                     "repo_url": None}
    _patch_registry(monkeypatch, _manifest("code_review", scheduler_owned=False,
                                           repo_mode="none"))

    start_config_run(db, ws, "code_review", "p")

    assert _repo_type_writes(db) == [], (
        "a repo-less run rewrote the repo_type of a project that has code")


def test_a_project_already_declaring_code_is_left_alone(monkeypatch):
    """No write when there is nothing to reconcile — the row already agrees."""
    db, ws = MagicMock(), MagicMock()
    db.get_project.return_value = {"project_id": "p"}
    db.get_repo_info.return_value = {"repo_type": "new",
                                     "repo_path": "/projects/p",
                                     "repo_url": None}
    _patch_registry(monkeypatch, _manifest("dpe_default"))

    start_config_run(db, ws, "dpe_default", "p", repo_type="new")

    assert _repo_type_writes(db) == []


def test_an_unreadable_row_is_not_reconciled_on_a_guess(monkeypatch):
    """`get_repo_info` raising is not evidence of anything; writing on it would
    overwrite a repo_type nobody read."""
    db, ws = MagicMock(), MagicMock()
    db.get_project.return_value = {"project_id": "p"}
    db.get_repo_info.side_effect = ValueError("Project p not found")
    _patch_registry(monkeypatch, _manifest("dpe_default"))

    start_config_run(db, ws, "dpe_default", "p", repo_type="new")

    assert _repo_type_writes(db) == []


# ── …and the write must reach the row ─────────────────────────────────────

def test_update_project_actually_persists_repo_type_and_path(db_manager):
    """The tests above assert the CALL; this one asserts the column. Before this
    change `update_project` had no repo_type/repo_path parameter at all, so a
    caller passing them would have been a silent TypeError-free no-op under a
    MagicMock and a hard TypeError in production."""
    db_manager.ensure_project("p", repo_type="none", repo_path=None)
    assert db_manager.get_repo_info("p")["repo_type"] == "none"

    assert db_manager.update_project("p", repo_type="new",
                                     repo_path="/projects/p") is True

    info = db_manager.get_repo_info("p")
    assert info["repo_type"] == "new"
    assert info["repo_path"] == "/projects/p"
