# tests/unit/test_run_launcher.py
# Phase 5: generic config-run launcher.

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core.run_launcher import generate_run_id, start_config_run


def _fake_manifest(**over):
    base = dict(config_name="c", seed_file="x", scheduler_owned=False,
                registers_generated_pipeline=False, registers_generated_addon=False)
    base.update(over)
    return SimpleNamespace(**base)


def _run_start(manifest):
    """Drive start_config_run with everything but repo resolution mocked,
    returning (repo_path, repo_type) passed to db.ensure_project and the
    repo_type passed to ws.setup_workspace."""
    db = MagicMock()
    db.get_project.return_value = None            # force ensure_project path
    ws = MagicMock()
    registry = MagicMock()
    registry.get.return_value = manifest
    sf = MagicMock()
    sf._workspace.get_config_path.return_value = MagicMock()
    sf.get_run.return_value = {"status": "running"}
    with patch("api.dependencies.get_skillflow", return_value=sf), \
         patch("api.dependencies.get_config_registry", return_value=registry), \
         patch("core.scheduler.wake_scheduler"):
        start_config_run(db, ws, "c", "pid_x")
    ep = db.ensure_project.call_args.kwargs
    return ep["repo_path"], ep["repo_type"], ws.setup_workspace.call_args.kwargs["repo_type"]


def test_authoring_run_gets_repoless_workspace():
    # skill_converter / addon_converter emit a config artifact, not a code repo —
    # no synthetic repo_path (else each surfaces as a "fake repo") AND repo_type
    # "none" so setup_workspace never git-inits a throwaway projects/<id> dir.
    for m in (_fake_manifest(registers_generated_addon=True),
              _fake_manifest(registers_generated_pipeline=True)):
        rp, ep_type, ws_type = _run_start(m)
        assert rp is None
        assert ep_type == "none" and ws_type == "none"


def test_plain_new_run_gets_a_repo_path():
    rp, ep_type, ws_type = _run_start(_fake_manifest())
    assert rp is not None and rp.endswith("pid_x")
    assert ep_type == "new" and ws_type == "new"


def test_generate_run_id_is_filesystem_safe():
    rid = generate_run_id("Some Weird/Config Name")
    assert all(c.isalnum() or c == "-" for c in rid)
    assert rid.startswith("some-weird-config-name-")
    # uniqueness suffix
    assert rid != generate_run_id("Some Weird/Config Name")


def test_start_config_run_unknown_config_is_error():
    # Unknown config short-circuits before touching db/ws/skillflow.
    result = start_config_run(None, None, "totally_unknown_config_xyz", "pid_x")
    assert result["status"] == "error"
    assert "Unknown config" in result["message"]
