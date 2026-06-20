# tests/unit/test_run_launcher.py
# Phase 5: generic config-run launcher.

from core.run_launcher import generate_run_id, start_config_run


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
