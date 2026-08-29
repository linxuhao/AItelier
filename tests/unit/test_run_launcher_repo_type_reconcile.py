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


# ── A REFUSED launch must not rewrite the row ─────────────────────────────
#
# The reconcile is one-way: nothing ever puts `repo_type='none'` back. So a
# refusal that happens AFTER it has written has stripped the project id of its
# repo-less status permanently, while launching nothing.


def test_a_launch_refused_for_a_missing_cross_config_input_writes_nothing(
        monkeypatch):
    """`missing_cross_config_inputs` refuses before any workspace exists.

    Reconciling ahead of it would rewrite the row of a project that then gets no
    run, no `setup_workspace`, and no repository — and, the reconcile being
    one-way, no way back."""
    db, ws = MagicMock(), MagicMock()
    db.get_project.return_value = {"project_id": "p"}
    db.get_repo_info.return_value = {"repo_type": "none", "repo_path": None,
                                     "repo_url": None}
    _patch_registry(monkeypatch, _manifest("dpe_default"))
    import core.run_launcher as rl
    monkeypatch.setattr(
        rl, "missing_cross_config_inputs",
        lambda sf, cfg, pid: [{"config": "meta_conversation", "step": "finalize",
                               "output": "step1_goals.json", "reader": "1"}])

    res = start_config_run(db, ws, "dpe_default", "p", repo_type="new")

    assert res["status"] == "error", res
    assert _repo_type_writes(db) == [], (
        "a refused launch rewrote repo_type; the project id is now permanently "
        "off the repo-less path with nothing to put it back")
    assert ws.setup_workspace.call_count == 0


def test_a_launch_that_setup_workspace_rejects_writes_nothing(monkeypatch):
    """`repo_type='existing'` with no `repo_path` — `setup_workspace` raises.

    Same tail as above: the row flips, then nothing is built."""
    db, ws = MagicMock(), MagicMock()
    db.get_project.return_value = {"project_id": "p"}
    db.get_repo_info.return_value = {"repo_type": "none", "repo_path": None,
                                     "repo_url": None}
    ws.setup_workspace.side_effect = ValueError(
        "repo_path is required for repo_type='existing'")
    _patch_registry(monkeypatch, _manifest("dpe_default"))

    import pytest
    with pytest.raises(ValueError):
        start_config_run(db, ws, "dpe_default", "p", repo_type="existing")

    assert _repo_type_writes(db) == [], (
        "the row was reconciled to 'existing' for a repository that was never "
        "set up")


def test_the_reconcile_still_happens_on_the_brief_seeding_path(monkeypatch):
    """The DPE brief path returns before the generic one, so it needs its own
    call — otherwise the ordinary butler-driven build never reconciles."""
    db, ws = MagicMock(), MagicMock()
    db.get_project.return_value = {"project_id": "p"}
    db.get_repo_info.return_value = {"repo_type": "none", "repo_path": None,
                                     "repo_url": None}
    _patch_registry(monkeypatch,
                    _manifest("dpe_default", seed_file="project_brief.md"))
    import core.project_submit as ps
    monkeypatch.setattr(ps, "seed_and_trigger",
                        lambda *a, **k: {"status": "submitted"})

    start_config_run(db, ws, "dpe_default", "p", repo_type="new",
                     seed_inputs={"brief": {"goal": "x"}})

    assert _repo_type_writes(db) == ["new"]
    assert ws.setup_workspace.call_count == 1


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


# ── Every refusing path must precede the reconcile, not just the two named ────
#
# `_reconcile_repo_type` is one-way, so a launch that rewrites the row and then
# bails leaves that project id off the repo-less path with nothing to put it
# back. It used to sit immediately after `setup_workspace`, under a docstring
# claiming the two guards that can refuse both preceded it. A third did not, and
# the DPE brief branch ran it before `seed_and_trigger` — which refuses three
# more ways. The fix is positional: last on each branch.

def test_a_launch_refused_for_seed_text_with_no_seed_file_writes_nothing(
        monkeypatch):
    """The THIRD refusing guard, which used to run after the reconcile."""
    db, ws = MagicMock(), MagicMock()
    db.get_project.return_value = {"project_id": "p"}
    db.get_repo_info.return_value = {"repo_type": "none", "repo_path": None,
                                     "repo_url": None}
    _patch_registry(monkeypatch, _manifest("dpe_default", seed_file=""))

    res = start_config_run(db, ws, "dpe_default", "p", repo_type="new",
                           seed_text="build me a thing")

    assert res["status"] == "error", res
    assert _repo_type_writes(db) == [], (
        "a launch that refused seed_text still rewrote repo_type")


def test_a_brief_path_launch_that_seed_and_trigger_refuses_writes_nothing(
        monkeypatch):
    """`seed_and_trigger` turns away a failed project (and two more shapes).

    On the brief branch the reconcile ran BEFORE it, so the row flipped for a
    build that was then told to use POST /retry instead.
    """
    db, ws = MagicMock(), MagicMock()
    db.get_project.return_value = {"project_id": "p"}
    db.get_repo_info.return_value = {"repo_type": "none", "repo_path": None,
                                     "repo_url": None}
    _patch_registry(monkeypatch,
                    _manifest("dpe_default", seed_file="project_brief.md"))
    import core.project_submit as ps
    monkeypatch.setattr(ps, "seed_and_trigger",
                        lambda *a, **k: {"status": "error",
                                         "message": "project has a failed run"})

    res = start_config_run(db, ws, "dpe_default", "p", repo_type="new",
                           seed_inputs={"brief": {"goal": "x"}})

    assert res["status"] == "error", res
    assert _repo_type_writes(db) == [], (
        "a refused brief submit rewrote repo_type")


def test_an_already_planned_brief_submit_writes_nothing(monkeypatch):
    db, ws = MagicMock(), MagicMock()
    db.get_project.return_value = {"project_id": "p"}
    db.get_repo_info.return_value = {"repo_type": "none", "repo_path": None,
                                     "repo_url": None}
    _patch_registry(monkeypatch,
                    _manifest("dpe_default", seed_file="project_brief.md"))
    import core.project_submit as ps
    monkeypatch.setattr(ps, "seed_and_trigger",
                        lambda *a, **k: {"status": "already_planned"})

    start_config_run(db, ws, "dpe_default", "p", repo_type="new",
                     seed_inputs={"brief": {"goal": "x"}})

    assert _repo_type_writes(db) == []


# ── One effective repo_type: the workspace and the row must agree ────────────

def test_the_brief_path_builds_the_workspace_as_the_type_it_records(
        monkeypatch):
    """`setup_workspace` took `seed_inputs["repo_type"]` while the row was
    stamped from `eff_repo_type`, so a caller supplying both got a row
    describing a repository the workspace was not built as."""
    db, ws = MagicMock(), MagicMock()
    db.get_project.return_value = {"project_id": "p"}
    db.get_repo_info.return_value = {"repo_type": "none", "repo_path": None,
                                     "repo_url": None}
    _patch_registry(monkeypatch,
                    _manifest("dpe_default", seed_file="project_brief.md"))
    import core.project_submit as ps
    monkeypatch.setattr(ps, "seed_and_trigger",
                        lambda *a, **k: {"status": "submitted"})

    start_config_run(db, ws, "dpe_default", "p", repo_type="new",
                     repo_path="/repos/theirs",
                     seed_inputs={"brief": {"goal": "x"},
                                  "repo_type": "existing"})

    built = ws.setup_workspace.call_args.kwargs["repo_type"]
    recorded = _repo_type_writes(db)
    assert built == "existing", built
    assert recorded == ["existing"], (
        f"the workspace was built as {built!r} and the row records {recorded!r}")


def test_a_new_project_row_records_the_type_the_workspace_is_built_as(
        monkeypatch):
    """The same disagreement at CREATION: `ensure_project` used `eff_repo_type`
    derived from the `repo_type` argument only."""
    db, ws = MagicMock(), MagicMock()
    db.get_project.return_value = None
    _patch_registry(monkeypatch,
                    _manifest("dpe_default", seed_file="project_brief.md"))
    import core.project_submit as ps
    monkeypatch.setattr(ps, "seed_and_trigger",
                        lambda *a, **k: {"status": "submitted"})

    start_config_run(db, ws, "dpe_default", "p", repo_type="new",
                     repo_path="/repos/theirs",
                     seed_inputs={"brief": {"goal": "x"},
                                  "repo_type": "existing"})

    created = db.ensure_project.call_args.kwargs["repo_type"]
    built = ws.setup_workspace.call_args.kwargs["repo_type"]
    assert created == built == "existing", (created, built)


def test_a_repoless_config_still_overrides_a_caller_supplied_type(monkeypatch):
    """`repo_mode: none` is the config's declaration and outranks the caller."""
    db, ws = MagicMock(), MagicMock()
    db.get_project.return_value = {"project_id": "p"}
    db.get_repo_info.return_value = {"repo_type": "none", "repo_path": None,
                                     "repo_url": None}
    _patch_registry(monkeypatch,
                    _manifest("pipeline_forge", repo_mode="none"))

    start_config_run(db, ws, "pipeline_forge", "p", repo_type="new",
                     seed_inputs={"repo_type": "existing"})

    assert ws.setup_workspace.call_args.kwargs["repo_type"] == "none"
    assert _repo_type_writes(db) == []
