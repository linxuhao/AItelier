"""Repeated scope refusal: actual gateway/response/receipt boundaries (UNRUN).

These tests drive the real AIGateway.generate final-request hook, the real
per-request closure, the real receipt on disk and fresh engine instances that
reload it (cross-instance). No opportunity boolean is injected.
"""
import json

import litellm
import pytest
from tenacity import wait_none

from core import ai_router, model_routes
from core.agents import DPEAgent
from core.ai_router import AIGateway
from core.dpe_pipeline import (PipelineEngine, RepeatedScopeRefusal,
                               ScopeObservationUnavailable, MaxRetriesExceeded,
                               SCOPE_PROGRESS_TERMINAL)
from core.write_scope import WriteScope

FORBIDDEN = {"tool": "write", "params": {"file": "forbidden.py", "content": "x"}}
ALLOWED = {"tool": "write", "params": {"file": "owned/ok.py", "content": "x"}}


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_LLM_STREAM", "0")
    monkeypatch.setenv("OWN_ENDPOINT_KEY", "offline-key")
    model_routes.reset_cache()
    ai_router.reset_endpoint_cooldowns()
    providers = tmp_path / "providers.json"
    providers.write_text(json.dumps({p: {"base_url": "https://offline.invalid/x",
                                          "api_key_env": "OWN_ENDPOINT_KEY"}
                                     for p in ("one", "two")}))
    routes = tmp_path / "routes.json"
    routes.write_text(json.dumps({"pool": {"rotate": ["one/model"],
                                           "fallback": ["two/model"]}}))
    monkeypatch.setattr(AIGateway.generate.retry, "wait", wait_none())
    g = AIGateway("pool", config_path=str(providers), routes_path=str(routes))
    yield g
    model_routes.reset_cache()
    ai_router.reset_endpoint_cooldowns()


def _reply(action):
    content = json.dumps({"thoughts": "t", "actions": [action]})
    return litellm.ModelResponse(model="offline", choices=[{
        "index": 0, "finish_reason": "stop",
        "message": {"role": "assistant", "content": content}}],
        usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5})


def _engine(artifact, instance, epoch=1):
    engine = PipelineEngine.__new__(PipelineEngine)
    engine._write_scope = WriteScope("card-a", ("owned/",))
    engine._scope_policy = True
    engine._scope_mode = "json-tools"
    engine._scope_position = None
    engine._output_fixed = {}
    engine._output_target = "code"
    engine._artifact_dir = str(artifact)
    engine._run_id = "run"
    engine._current_step = "t_impl"
    engine._write_scope_step_id = "t_impl"
    engine._step_instance_id = instance
    engine._claim_epoch = epoch
    engine._emit = lambda *a, **k: None
    engine._trace = lambda *a, **k: None
    engine._load_write_scope_receipt()
    return engine


def _turn(engine, gateway, monkeypatch, action, calls, prompt="work"):
    """One real JSON request/response through the host closure."""
    monkeypatch.setattr(litellm, "completion",
                        lambda **kw: calls.append(kw) or _reply(action))
    agent = DPEAgent(gateway, "system json")
    prompt, closure = engine._scope_json_request(agent, prompt)
    response = engine._run_json_agent(agent, prompt, closure)
    payload = json.loads(response)
    engine._scope_json_commit(closure, response, payload)
    return closure


def test_typed_causes_are_nonretryable_max_retries_subtypes():
    assert issubclass(RepeatedScopeRefusal, MaxRetriesExceeded)
    assert issubclass(ScopeObservationUnavailable, MaxRetriesExceeded)
    assert set(SCOPE_PROGRESS_TERMINAL) == {RepeatedScopeRefusal, ScopeObservationUnavailable}


def test_first_denial_is_structured_and_cross_instance_repeat_is_typed(
        tmp_path, gateway, monkeypatch):
    artifact = tmp_path / "item"
    calls = []
    a = _engine(artifact, instance=1)
    first = a._write_scope_refusal("write", FORBIDDEN["params"])
    assert first["scope_violation"] is True
    receipt = json.loads((artifact / "write_scope_receipt.json").read_text())
    assert receipt["scope_cause"]["denied_targets"] == ["forbidden.py"]
    assert receipt["violations"][0]["error"] == first["error"]

    # A different producer instance (review loopback) reloads the same item.
    b = _engine(artifact, instance=2, epoch=7)
    assert b._scope_cause["hash"] == receipt["scope_cause"]["hash"]
    closure = _turn(b, gateway, monkeypatch, FORBIDDEN, calls)
    assert len(calls) == 1
    # The actual final sanitized request contained the complete carry block.
    assert any(closure.carry in str(m.get("content")) for m in calls[0]["messages"])
    witness = json.loads((artifact / "write_scope_receipt.json").read_text())["response_witness"]
    assert witness["carry_match"] is True and witness["cause"]["hash"] == b._scope_cause["hash"]
    with pytest.raises(RepeatedScopeRefusal):
        b._write_scope_refusal("write", FORBIDDEN["params"])
    assert len(calls) == 1, "typed failure must not trigger another model turn"
    final = json.loads((artifact / "write_scope_receipt.json").read_text())
    assert len(final["violations"]) == 2  # first raw error stays visible


def test_allowed_correction_after_presented_feedback_executes(tmp_path, gateway, monkeypatch):
    artifact = tmp_path / "item"
    _engine(artifact, 1)._write_scope_refusal("write", FORBIDDEN["params"])
    b = _engine(artifact, 2)
    _turn(b, gateway, monkeypatch, ALLOWED, [])
    assert b._write_scope_refusal("write", ALLOWED["params"]) is None


def test_different_denied_target_is_ordinary_correction_not_repeat(tmp_path, gateway, monkeypatch):
    artifact = tmp_path / "item"
    _engine(artifact, 1)._write_scope_refusal("write", FORBIDDEN["params"])
    b = _engine(artifact, 2)
    other = {"tool": "write", "params": {"file": "elsewhere.py", "content": "x"}}
    _turn(b, gateway, monkeypatch, other, [])
    refusal = b._write_scope_refusal("write", other["params"])
    assert refusal["scope_violation"] is True
    assert b._scope_cause["denied_targets"] == ["elsewhere.py"]


def test_same_batch_second_denial_is_not_a_repeat(tmp_path):
    a = _engine(tmp_path / "item", 1)
    a._scope_batch = {"cause": None, "witness": None, "created": False}
    assert a._write_scope_refusal("write", FORBIDDEN["params"])["scope_violation"]
    again = a._write_scope_refusal("write", FORBIDDEN["params"])
    assert again["scope_violation"] is True  # ordinary error, no typed stop


def test_missing_carry_in_final_request_is_feedback_delivery_failure(
        tmp_path, gateway, monkeypatch):
    artifact = tmp_path / "item"
    _engine(artifact, 1)._write_scope_refusal("write", FORBIDDEN["params"])
    b = _engine(artifact, 2)
    monkeypatch.setattr(litellm, "completion", lambda **kw: _reply(FORBIDDEN))
    agent = DPEAgent(gateway, "system json")
    _prompt, closure = b._scope_json_request(agent, "work")
    # Request WITHOUT the host carry block: presentation is never inferred.
    response = b._run_json_agent(agent, "work without carry", closure)
    with pytest.raises(ScopeObservationUnavailable):
        b._scope_json_commit(closure, response, json.loads(response))
    assert json.loads((artifact / "write_scope_receipt.json").read_text()
                      ).get("response_witness") is None


def test_provider_failure_gives_no_opportunity(tmp_path, gateway, monkeypatch):
    artifact = tmp_path / "item"
    _engine(artifact, 1)._write_scope_refusal("write", FORBIDDEN["params"])
    b = _engine(artifact, 2)
    def boom(**kw):
        raise litellm.exceptions.BadRequestError("bad", "m", "p")
    monkeypatch.setattr(litellm, "completion", boom)
    agent = DPEAgent(gateway, "system json")
    prompt, closure = b._scope_json_request(agent, "work")
    with pytest.raises(Exception):
        b._run_json_agent(agent, prompt, closure)
    assert closure.completed == [] and closure.presented is None
    assert b._scope_cause is not None  # same cause retained, no fresh budget


def test_damaged_receipt_is_observation_failure_not_clean_history(tmp_path):
    artifact = tmp_path / "item"
    artifact.mkdir()
    (artifact / "write_scope_receipt.json").write_text("{broken")
    engine = _engine(artifact, 2)
    assert engine._scope_history_error
    assert (artifact / "write_scope_receipt.json").read_text() == "{broken"


def test_trusted_authority_change_is_not_a_repeat(tmp_path, gateway, monkeypatch):
    artifact = tmp_path / "item"
    _engine(artifact, 1)._write_scope_refusal("write", FORBIDDEN["params"])
    b = _engine(artifact, 2)
    b._write_scope = WriteScope("card-a", ("owned/", "forbidden.py"))
    _turn(b, gateway, monkeypatch, FORBIDDEN, [])
    assert b._write_scope_refusal("write", FORBIDDEN["params"]) is None


def test_gateway_json_hook_runs_only_after_successful_completion(gateway, monkeypatch):
    seen = []
    gateway.on_messages_presented = lambda m: seen.append(m)
    monkeypatch.setattr(litellm, "completion", lambda **kw: _reply(ALLOWED))
    gateway.generate("sys json", "user", is_json_mode=True)
    assert len(seen) == 1 and seen[0][-1]["content"] == "user"
