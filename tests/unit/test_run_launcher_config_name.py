"""A project run under a second config must be visible to the poller.

`get_next_active_project` filters `AND config_name IN (<scheduler-owned>)`, so a
project row still naming the butler-driven config that created it is invisible:
the run exists and is `running`, the poller reports `idle`, and nothing errors.
"""
from unittest.mock import MagicMock

from core.run_launcher import start_config_run


def _manifest(name, scheduler_owned, seed_file="seed.md", repo_mode="code"):
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
    monkeypatch.setattr(sched, "wake_scheduler", lambda *a, **k: None, raising=False)
    return sf


def test_scheduler_owned_config_claims_the_project_row(monkeypatch):
    db, ws = MagicMock(), MagicMock()
    db.get_project.return_value = {"project_id": "p", "config_name": "meta_conversation"}
    _patch_registry(monkeypatch, _manifest("dpe_game", True))

    start_config_run(db, ws, "dpe_game", "p")

    assert any(c.kwargs.get("config_name") == "dpe_game"
               for c in db.update_project.call_args_list), (
        "starting a scheduler-owned run left the project row naming the old config; "
        "get_next_active_project would never see it")


def test_butler_driven_config_does_not_steal_the_name(monkeypatch):
    db, ws = MagicMock(), MagicMock()
    db.get_project.return_value = {"project_id": "p", "config_name": "dpe_game"}
    _patch_registry(monkeypatch, _manifest("meta_conversation", False))

    start_config_run(db, ws, "meta_conversation", "p")

    assert not any("config_name" in c.kwargs for c in db.update_project.call_args_list), (
        "a butler-driven config overwrote the build config the poller must drive")


def test_new_project_still_gets_its_config_name(monkeypatch):
    db, ws = MagicMock(), MagicMock()
    db.get_project.return_value = None
    _patch_registry(monkeypatch, _manifest("dpe_game", True))

    start_config_run(db, ws, "dpe_game", "p")

    assert db.ensure_project.call_args.kwargs.get("config_name") == "dpe_game"
