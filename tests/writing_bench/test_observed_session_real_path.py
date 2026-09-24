"""The observed-review session must be installed on the REAL step path.

The previous round got the coverage logic right and never started it in
production: `_run_native_step` called `begin_observed_review(self)` ABOVE
`self._current_step = step_id`, and `aitelier/runner.py` builds a fresh
PipelineEngine for every step, so the field it read was `None` for every step.
No host certificate was ever written, and the downstream `literary_check` had
nothing to accept. Every earlier test built its ReviewSession by hand, which is
why the suite stayed green.

This test drives the production entry point instead: the real AgentStepRunner
builds a real PipelineEngine per step of the real `novel_writing_bench_v2`
graph, and the real model-step loop executes `literary_review` and
`ledger_audit`. Only the model call itself is a double; the graph, the runner,
the engine, the tools and the host session are the shipped ones.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from aitelier.runner import AgentStepRunner
from aitelier.writing_bench.reading import ReviewSession, load_certificate
from aitelier.writing_bench.storage import decode, encode
from core.db_manager import DBManager
from core.workspace_manager import WorkspaceManager
from test_bench import bench, request, verdict  # noqa: F401 (fixtures re-exported)
from test_workflow import Session


ROLES = {"bench_literary_v2": "literary", "bench_ledger_auditor_v2": "ledger"}


def _call(name, call_id, **params):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(params)}}


def _turn(tool_calls):
    return SimpleNamespace(text="", tool_calls=tool_calls,
                           reasoning_content="", truncated=False)


def _page_state(messages):
    """What the model has actually been handed so far, from its own tool results.

    `novel_bench_read` reports the material path without its `review/` prefix.
    """
    state = {}
    for message in messages:
        if message.get("role") != "tool":
            continue
        try:
            value = json.loads(message.get("content") or "")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict) or not value.get("path"):
            continue
        name = "review/" + str(value["path"])
        if value.get("material_sha256"):
            state[name] = {"complete": bool(value.get("complete")),
                           "next_start": value.get("next_start")}
    return state
    return state


class _Gateway:
    """Stands in for AIGateway: same observer hook, same call site.

    `on_messages_presented` is a property so the test can see whether the engine
    installed a session observer, and `generate_native` invokes it exactly where
    `core/ai_router.py` does — after projection, on a successful call.
    """

    def __init__(self, respond):
        self._respond = respond
        self._observer = None
        self.installed = []
        self.presented = []
        self.litellm_model = "fixture/native"
        self.max_output_tokens = 8192
        self.last_usage = {}
        self.last_outbound = None

    @property
    def on_messages_presented(self):
        return self._observer

    @on_messages_presented.setter
    def on_messages_presented(self, observer):
        self._observer = observer
        self.installed.append(observer)

    def escalate_output_cap(self):
        return 0

    def generate_native(self, messages, *, tools=None, tool_choice="auto"):
        if callable(self._observer):
            self._observer(messages)
        self.presented.append([dict(m) for m in messages])
        return self._respond(messages)


class _Agent:
    def __init__(self, gateway):
        self.gateway = gateway
        self.system_prompt = "independent writing-bench reviewer"

    def turn(self, messages, *, tools=None, tool_choice="auto"):
        return self.gateway.generate_native(messages, tools=tools,
                                            tool_choice=tool_choice)


def _identity(bench_obj, run, phase):
    if phase == "literary":
        key = bench_obj.input(run)[1]["literary_key"]
    else:
        key = decode((bench_obj.work(run) / "ledgers.json").read_bytes())["review_key"]
    return key, bench_obj.review_materials(run, phase)[0]["targets"]


def _responder(bench_obj, run, phase):
    """A deterministic reviewer: read every required material, then one verdict."""

    def respond(messages):
        required = ["review/current_prose.md", "review/review_context.md"]
        if phase == "ledger":
            required.append("review/proposed_ledgers.md")
        state = _page_state(messages)
        for path in required:
            progress = state.get(path)
            if not progress or not progress["complete"]:
                start = 0 if not progress else (progress["next_start"] or 0)
                return _turn([_call("novel_bench_read", "read-" + path,
                                    path=path, start=start, length=8000)])
        key, targets = _identity(bench_obj, run, phase)
        value = verdict(key, reviewed_chapters=targets,
                        feedback=f"independent {phase} review of the frozen material",
                        findings=[])
        return _turn([_call("write_verdict", "verdict", content=encode(value).decode()),
                      _call("finish_step", "finish", summary="review complete")])

    return respond


def _drive(session, run, monkeypatch, engines):
    """Advance/claim/execute through the real runner until the run settles."""
    import api.dependencies as deps
    import core.agents as agents
    import core.dpe_pipeline as dpe

    monkeypatch.setattr(deps, "get_skillflow", lambda: session.sf)
    monkeypatch.setattr(deps, "get_db_manager", lambda: session.db)
    # This graph owns no repository (`repo_mode: none`); the code-path lookup is
    # a host stub, not part of what this test proves.
    # Patch the INSTANCE, not the class. Another test in the suite
    # (`tests/unit/test_public_read_hardening.py`) calls
    # `importlib.reload(core.workspace_manager)`, which rebinds the CLASS object
    # while this workspace keeps the one it was built from; a class-level
    # monkeypatch then silently misses and the real resolver runs.
    monkeypatch.setattr(session.ws, "get_code_path",
                        lambda _pid, run_id=None, *_a, **_k: None)
    recorded = {}
    real_init = dpe.PipelineEngine.__init__

    def build(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        engines.append(self)

    monkeypatch.setattr(dpe.PipelineEngine, "__init__", build)

    def native_agent(_self, name):
        phase = ROLES[name]
        gateway = _Gateway(_responder(session.bench, run, phase))
        recorded[name] = gateway
        return _Agent(gateway)

    monkeypatch.setattr(agents.AgentFactory, "get_native_agent", native_agent)
    monkeypatch.setattr(agents.AgentFactory, "is_native", lambda _self, _name: True)
    monkeypatch.setattr(agents.AgentFactory, "get_max_retries", lambda _self, _name: 1)
    monkeypatch.setattr(agents.AgentFactory, "get_max_tool_turns", lambda _self, _name: 12)
    monkeypatch.setattr(agents.AgentFactory, "get_fallback_to_json",
                        lambda _self, _name: False)

    runner = AgentStepRunner(db_manager=session.db, workspace_manager=session.ws)
    steps = []
    for _ in range(60):
        settled = session.sf.advance_run(run)
        row = session.sf.get_run(run)
        if row["status"] != "running":
            return row["status"], steps, recorded
        if settled is None:
            continue
        claim = session.sf.claim_next_step(run)
        if claim is None:
            continue
        steps.append(claim.step_id)
        result = asyncio.run(runner.execute(claim))
        session.sf.confirm_step(claim.token, result)
    raise AssertionError("run did not settle: " + str(session.sf.get_run(run)))


def test_real_runner_installs_the_observed_session_for_both_review_steps(
        tmp_path, monkeypatch, bench):
    # The engine keeps durable native observations outside the workspace, so
    # point the data root at this tmp tree rather than the operator's home.
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "aitelier-home"))
    session = Session(tmp_path, monkeypatch, bench)
    session.db = DBManager(str(tmp_path / "aitelier.db"))
    session.db.ensure_project("execution", name="E2E", repo_type="none")
    session.ws = WorkspaceManager(base_path=str(tmp_path / "executions"),
                                  projects_base=str(tmp_path / "projects"))
    run = session.start(request(bench))
    engines = []

    status, steps, recorded = _drive(session, run, monkeypatch, engines)

    # Both independent reviewers ran, each through its own freshly built engine.
    assert steps == ["literary_review", "ledger_audit"]
    assert status == "paused"          # the single manual gate, nothing accepted
    assert len(engines) == 2
    sessions = [e._writing_review_session for e in engines]
    assert all(isinstance(s, ReviewSession) for s in sessions)
    assert [s.claim["step_id"] for s in sessions] == ["literary_review", "ledger_audit"]

    # The session was INSTALLED on the gateway the engine really uses, and the
    # input actually presented was recorded by the host.
    for name, phase in ROLES.items():
        gateway = recorded[name]
        assert gateway.installed, f"{name}: no host observation session installed"
        observer = gateway.installed[0]
        assert isinstance(getattr(observer, "__self__", None), ReviewSession)
        assert observer.__self__.claim["step_id"] == (
            "literary_review" if phase == "literary" else "ledger_audit")
        assert gateway.presented, f"{name}: nothing was presented to the model"
        for material in ("review/current_prose.md", "review/review_context.md"):
            assert any(any(material[len("review/"):] in str(m.get("content", ""))
                           for m in presented)
                       for presented in gateway.presented), material

    # The host wrote its own certificate for each phase, bound to the run and to
    # the live reviewer step instance.
    reading = session.bench.work(run) / "reading"
    for phase, step_id in (("literary", "literary_review"), ("ledger", "ledger_audit")):
        cert = load_certificate(reading, phase)
        assert cert["complete"] is True
        assert cert["claim"]["run_id"] == run
        assert cert["claim"]["step_id"] == step_id
        assert cert["report_sha256"]

    # Downstream acceptance: the stage gate consumed the host certificates and
    # the engine still holds the single manual gate.
    stage = decode((session.bench.work(run) / "stage.json").read_bytes())
    assert stage["observed_reading"]["literary"]["complete"] is True
    assert stage["observed_reading"]["ledger"]["complete"] is True
    assert stage["accepted"] is False and not session.backup_calls
