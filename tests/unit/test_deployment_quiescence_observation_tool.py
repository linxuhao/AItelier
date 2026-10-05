"""Focused management observation tests; behavioral execution is separately authorized."""
import copy
import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api import mcp_router
from core import datadir, deployment_quiescence as dq

NAME = "observe_deployment_quiescence"


@pytest.fixture
def observation_tool(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_router, "_EXTERNAL_TOKEN", "")
    monkeypatch.setattr(datadir, "semantic_index_control_dir", lambda: tmp_path / "semantic")
    monkeypatch.setattr(mcp_router.authz, "gate_enabled", lambda: True)
    monkeypatch.setattr(mcp_router.authz, "request_can_write", lambda request: True)
    monkeypatch.setattr(mcp_router, "_request_from", lambda ctx: object())
    mcp = mcp_router.build_mcp()
    monkeypatch.setattr(mcp, "get_context", lambda: object())
    assert mcp_router._TOOL_KIND[NAME] == "write"
    return mcp._tool_manager.get_tool(NAME).fn


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["module", "skillflow", "db"])
async def test_uninitialized_observation_never_imports_or_initializes(
        monkeypatch, observation_tool, missing):
    import builtins

    forbidden = Mock(side_effect=AssertionError("runtime initialization is forbidden"))
    dependencies = SimpleNamespace(
        _skillflow_instance=object(), db_instance=object(), get_skillflow=forbidden)
    if missing == "module":
        monkeypatch.delitem(sys.modules, "api.dependencies", raising=False)
    else:
        setattr(dependencies, "_skillflow_instance" if missing == "skillflow"
                else "db_instance", None)
        monkeypatch.setitem(sys.modules, "api.dependencies", dependencies)
    monkeypatch.setattr(dq, "measure", forbidden)
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        assert name != "api.dependencies", "composition root must not be imported"
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    result = await observation_tool()
    assert result["error"] == "observation_unavailable"
    assert "already initialized" in result["reason"]
    assert "observation" not in result
    forbidden.assert_not_called()


@pytest.mark.asyncio
async def test_normal_measure_preserves_mixed_blockers_errors_and_original_digest(
        monkeypatch, observation_tool, tmp_path):
    command = "123 1 evaluator --session private-session password=private-password"
    sf = Mock()
    sf.list_runs.return_value = [
        {"id": "run-live", "project_id": "project-one", "status": "running"},
        {"id": "run-failed-audit", "project_id": "project-two", "status": "completed"},
    ]

    def audit(run_id):
        if run_id == "run-live":
            return {"lost": ["atomic-lost"], "unknown": ["native-unknown"], "alive": 1}
        raise RuntimeError(command)

    sf.audit_operation_owners.side_effect = audit
    db = object()
    lazy = Mock(side_effect=AssertionError("must use existing runtime"))
    monkeypatch.setitem(sys.modules, "api.dependencies", SimpleNamespace(
        _skillflow_instance=sf, db_instance=db, get_skillflow=lazy))
    monkeypatch.setattr(datadir, "semantic_index_control_dir", lambda: tmp_path / "semantic")
    lease = {"run_id": "run-live", "canonical_checkout": "/owned/canonical"}
    admission = {"id": 7, "owner": "writer-one", "canonical_checkout": "/owned/canonical",
                 "detail": command}
    registered = {"attempt_id": "attempt-live", "project_id": "project-one",
                  "status": "active"}
    db_probe = Mock(return_value=([lease], [admission], [registered], [
        "external owner registry status mismatch for attempt-live: password=private-password"]))
    monkeypatch.setattr(dq, "_db_rows", db_probe)
    sidecar = {"run_id": "run-live", "root": "/owned/run", "source": "run",
               "desired": "ready", "outcome": "pending", "revision": 2,
               "done_revision": 1, "activity_at": 1.0, "error": command}
    monkeypatch.setattr(dq, "_sidecar_rows", lambda path: ([sidecar], [
        "semantic project operation is active or unknown: " + command]))
    godot = {"owner_id": "render-one", "status": "owner_lost", "generation": 1}
    monkeypatch.setattr(dq, "_godot_rows", lambda path: ([godot], []))

    def runner(args):
        return subprocess.CompletedProcess(
            args, 0, stdout=command + "\n" if args[0] == "ps" else "", stderr="")

    original_measure = dq.measure
    captured = {}

    def measure(**kwargs):
        assert kwargs == {"skillflow": sf, "db": db,
                          "sidecar_db": tmp_path / "semantic" / "control.sqlite3"}
        raw = original_measure(**kwargs, command_runner=runner)
        captured["raw"] = copy.deepcopy(raw)
        return raw

    monkeypatch.setattr(dq, "measure", measure)
    result = await observation_tool()
    raw, projected = captured["raw"], result["observation"]
    assert result["status"] == "observed"
    assert raw["quiescent"] is False and projected["quiescent"] is False
    expected_categories = {"active_runs", "active_operations", "checkout_leases",
                           "sidecar_owners", "external_active",
                           "registered_external_owners", "godot_render_owners"}
    assert set(raw["blockers"]) == set(projected["blockers"]) == expected_categories
    assert all(raw["blockers"][name] for name in expected_categories)
    assert {name: len(rows) for name, rows in projected["blockers"].items()} == {
        name: len(rows) for name, rows in raw["blockers"].items()}
    assert projected["runs"][0]["audit"] == {
        "lost": ["atomic-lost"], "unknown": ["native-unknown"], "alive": 1}
    assert projected["runs"][0]["active_operations"] == 3
    assert projected["registered_external_owners"][0]["attempt_id"] == "attempt-live"
    assert projected["godot_render_owners"][0] == godot
    assert len(raw["errors"]) >= 4
    assert len(projected["errors"]) == len(raw["errors"])
    for original, transported in zip(raw["errors"], projected["errors"]):
        assert transported.startswith(original.partition(": ")[0])
    assert result["original_observation_digest"] == raw["digest"]
    assert raw["digest"] == dq._observation_digest(raw)
    assert "digest" not in projected
    assert result["transport_projection"]["digest_scope"] == "original_unprojected_observation"
    assert result["transport_projection"]["authorization_input"] is False
    serialized = json.dumps(result)
    assert command not in serialized
    assert "private-session" not in serialized
    assert "private-password" not in serialized
    db_probe.assert_called_once_with(db)
    assert sf.audit_operation_owners.call_count == 2
    lazy.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["missing", "unauthenticated", "unauthorized"])
async def test_existing_writer_auth_refuses_before_measurement(
        monkeypatch, observation_tool, identity):
    measure = Mock(side_effect=AssertionError("unauthorized observation"))
    monkeypatch.setattr(dq, "measure", measure)
    monkeypatch.setattr(mcp_router, "_request_from",
                        lambda ctx: None if identity == "missing" else object())
    monkeypatch.setattr(mcp_router.authz, "request_can_write", lambda request: False)
    reason = ("write_denied_not_authenticated" if identity == "unauthenticated"
              else "write_denied_not_a_writer")
    monkeypatch.setattr(mcp_router.authz, "write_denial_reason", lambda request: reason)
    result = await observation_tool()
    assert result["error"].startswith("denied:")
    assert "no verifiable identity" in result["error"] if identity == "missing" else (
        reason in result["error"])
    measure.assert_not_called()


@pytest.mark.asyncio
async def test_unexpected_measurement_failure_is_unavailable_not_quiet(
        monkeypatch, observation_tool):
    monkeypatch.setitem(sys.modules, "api.dependencies", SimpleNamespace(
        _skillflow_instance=object(), db_instance=object()))
    monkeypatch.setattr(dq, "measure", Mock(side_effect=RuntimeError(
        "evaluation --session private-session password=private-password")))
    result = await observation_tool()
    assert result == {"error": "observation_unavailable", "reason": "measurement failed",
                      "exception_type": "RuntimeError"}
    assert "observation" not in result
    assert "private" not in json.dumps(result)


@pytest.mark.asyncio
async def test_quiet_measurement_keeps_producer_digest_without_authorizing(
        monkeypatch, observation_tool):
    sf = Mock()
    sf.list_runs.return_value = []
    monkeypatch.setitem(sys.modules, "api.dependencies", SimpleNamespace(
        _skillflow_instance=sf, db_instance=object()))
    monkeypatch.setattr(dq, "_db_rows", lambda db: ([], [], [], []))
    monkeypatch.setattr(dq, "_sidecar_rows", lambda path: ([], []))
    monkeypatch.setattr(dq, "_godot_rows", lambda path: ([], []))
    original_measure = dq.measure
    observations = []

    def measure(**kwargs):
        result = original_measure(**kwargs, external_probe=list)
        observations.append(result)
        return result

    monkeypatch.setattr(dq, "measure", measure)
    result = await observation_tool()
    assert result["observation"]["quiescent"] is True
    assert result["observation"]["errors"] == []
    assert not any(result["observation"]["blockers"].values())
    assert result["original_observation_digest"] == observations[0]["digest"]
    assert result["transport_projection"]["authorization_input"] is False
    sf.audit_operation_owners.assert_not_called()
