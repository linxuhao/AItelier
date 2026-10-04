"""Bounded controls for the boundaries shared by the eleven source inputs."""
import asyncio
import sys

import pytest

from core import run_isolation as ri
from core.dpe_pipeline import MaxRetriesExceeded, NativeSideEffectsRetained
from core.state_graph import StateConflict
from tests.unit.test_json_delivery_failure import (
    EXTRA_BRACE, action, engine, response, successful_writes,
)
from tests.unit.test_native_direct_code import execute, host
from tests.unit.test_native_effect_recovery import (
    HostCrash, batch, crash_after_fence, owned_tools, response as native_response,
)
from tests.unit.test_scheduler_launch_isolation_failure import launch_world, _reserve


@pytest.mark.parametrize("repair", [True, False], ids=["corrected-delivery", "pending-write-refusal"])
def test_native_fallback_uses_bounded_json_correction_without_masking_writes(tmp_path, repair):
    replies = ([EXTRA_BRACE, successful_writes()] if repair else [
        response(action("gen_image_asset")), successful_writes(), EXTRA_BRACE,
        response(action("finish_step")),
    ])
    e = engine(tmp_path, replies, failed_writes=0 if repair else 2)
    e.factory.is_native = lambda _: True
    e.factory.get_fallback_to_json = lambda _: True
    e._report_read_accounting = lambda _: None

    def native_unavailable(*args, **kwargs):
        # No native effect occurred, so the existing fallback boundary permits
        # the real JSON turn loop. Only the native failure is scripted here.
        raise ValueError("owned native response-schema failure before effects")

    e._run_native_step = native_unavailable
    if repair:
        assert e.run_step(1, "implement", None, "fixture", agent_config_name="stub",
                          tool_schemas=e._tool_schemas) is True
        assert (tmp_path / "final/delivery.md").read_text() == "candidate"
        assert len(e.prompts) == 2
        assert "Failed to parse JSON" in e.prompts[1]
    else:
        with pytest.raises(MaxRetriesExceeded, match="retained first write failure"):
            e.run_step(1, "implement", None, "fixture", agent_config_name="stub",
                       tool_schemas=e._tool_schemas)
        assert sum(call["tool"] == "gen_image_asset" for call in e.calls) == 1
        assert not (tmp_path / "final/delivery.md").exists()
        assert not any(kind == "step_done" for kind, _ in e.events)
    assert sum(kind == "native_fallback" for kind, _ in e.events) == 1
    assert not any(call["tool"] == "read" for call in e.calls)


def test_unsettled_native_recovery_cannot_enter_enabled_json_correction(tmp_path, monkeypatch):
    sf, rid, claim, root, executions = owned_tools(tmp_path, monkeypatch)
    try:
        calls = batch(native_response("set_owned_state", value="A"),
                      native_response("remove_owned", file="baseline.py"))
        before, ws = host(sf, rid, claim, root, lambda **kw: calls)
        crash_after_fence(before, sf, rid, claim, "set_owned_state")
        with pytest.raises(HostCrash):
            execute(before, ws, rid, claim)
        fence_bytes = {p.name: p.read_bytes() for p in before._effect_fence_dir.glob("*.json")}
        claim.inputs["_tool_schemas"].pop("remove_owned")
        provider_calls, json_calls = [], []

        def provider(**kwargs):
            provider_calls.append(1)
            return native_response("finish_step")

        resumed, ws2 = host(sf, rid, claim, root, provider)
        resumed.factory.get_fallback_to_json = lambda _: True

        def json_agent(*args):
            json_calls.append(1)
            raise AssertionError("incomplete durable batch reached fresh JSON execution")

        resumed.factory.get_agent = json_agent
        with pytest.raises(NativeSideEffectsRetained, match="recovery remains incomplete"):
            execute(resumed, ws2, rid, claim)
        assert provider_calls == json_calls == []
        assert executions == [("state", "A")]
        assert (root / "baseline.py").read_text() == "baseline=True"
        assert {p.name: p.read_bytes() for p in before._effect_fence_dir.glob("*.json")} == fence_bytes
    finally:
        sf._conn.close()


def test_failed_launch_cannot_invent_relay_payload_or_external_owner(launch_world):
    w = launch_world
    attempt = _reserve(w, "interaction-missing-base")
    pid = attempt["execution_project_id"]
    ri.request_base(w.db, pid, "f" * 40, "owned unavailable source base")
    assert w.scheduler._get_or_create_skillflow_run(pid) is None
    rid = w.sf._conn.execute("SELECT id FROM skillflow_runs WHERE project_id=?", (pid,)).fetchone()[0]
    w.attempts.bind_run(attempt["attempt_id"], rid, w.sf)
    failed = w.service.reconcile_attempt(attempt["attempt_id"])
    assert failed["status"] == "failed"
    assert failed.get("relay_inventory") is None
    with pytest.raises(StateConflict, match="no crash-safe relay inventory"):
        w.service.disposition_failed_attempt(attempt["attempt_id"], "handoff-external",
            request_key="cannot-invent-handoff", relay_digest="0" * 64,
            instruction="preserve actual launch failure", harness="owned-test", external_id="owned/next")
    assert w.service.disposition_failed_attempt(attempt["attempt_id"], "leave-stopped")["automatic_retry"] is False
    assert w.sf.get_run(rid)["started_at"] is None
    assert w.sf.get_run(rid)["error_reason"] == failed["error"]
    assert w.sf._conn.execute("SELECT COUNT(*) FROM skillflow_runs WHERE project_id=?", (pid,)).fetchone()[0] == 1
    with w.db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM state_attempts WHERE project_id='owner'").fetchone()[0] == 1


async def test_existing_async_runner_executes_body_without_provisioning(tmp_path, monkeypatch):
    import aitelier.tools.run_tests.impl as rt

    def forbid_provisioning(**kwargs):
        raise AssertionError("healthy combined environment attempted installation")

    monkeypatch.setattr(rt.tempfile, "mkdtemp", forbid_provisioning)
    assert rt._resolve_pytest_python(tmp_path, {}) == (sys.executable, None)
    await asyncio.sleep(0)
    marker = tmp_path / "async-body-executed"
    marker.write_text("executed")
    assert marker.read_text() == "executed"
