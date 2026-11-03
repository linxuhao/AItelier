"""Managed DSH lifecycle against isolated real State/identity storage."""
import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from core.drivers import DriverRegistry
from core.dsh_driver import DshDrivers
from core.state_database import StateDatabase
from core.state_service import StateService


@pytest.fixture
def runtime(tmp_path):
    db = StateDatabase(str(tmp_path / "state.sqlite"))
    service = StateService(db, actor="fixture", project_read_trusted=True)
    service.store.create_project("alpha", "Alpha")
    service.store.create_project("beta", "Beta")
    registry = DriverRegistry(db, "dsh-fixture-pepper-" * 3)
    registry.seed("fixture-owner-token")
    return DshDrivers(db, registry, tmp_path / "drivers", command=[sys.executable,
        "-c", "import time; time.sleep(90)"])


async def test_independent_tokens_one_process_per_dag_and_clean_stop(runtime):
    try:
        a = await runtime.start("alpha", actor="fixture")
        again = await runtime.start("alpha", actor="fixture")
        b = await runtime.start("beta", actor="fixture")
        assert a["driver_id"] == again["driver_id"] != b["driver_id"]
        assert len(runtime.processes) == 2
        tokens = [(runtime.root / d["driver_id"] / "token") for d in (a, b)]
        assert tokens[0].read_text() != tokens[1].read_text()
        assert all(p.stat().st_mode & 0o777 == 0o600 for p in tokens)
        assert runtime.registry.lookup_token(tokens[0].read_text().strip())["driver_id"] == a["driver_id"]
        runtime.update("alpha", phase="running", subagents_json=json.dumps(["child-1", "child-2"]))
        status = runtime.status()
        assert status["projects"]["alpha"] | {} == {
            "total": 1, "online": 1, "running_subagents": 2, "running_pipelines": 0,
            "managed": runtime.public(runtime.get("alpha")),
        }
        assert "token" not in json.dumps(status)
        proc = runtime.processes["alpha"]
        await runtime.stop("alpha")
        assert proc.returncode is not None
        assert runtime.status()["projects"]["alpha"]["running_subagents"] == 0
        assert runtime.status()["projects"]["alpha"]["online"] == 0
        assert runtime.processes["beta"].returncode is None
    finally:
        await runtime.close()


async def test_resume_preserves_identity_and_session_but_does_not_restart_stopped(runtime):
    a = await runtime.start("alpha", actor="fixture")
    await runtime.start("beta", actor="fixture")
    await runtime.stop("beta")
    await runtime.close()
    resumed = DshDrivers(runtime.db, runtime.registry, runtime.root, command=runtime.command)
    try:
        await resumed.resume()
        assert set(resumed.processes) == {"alpha"}
        assert resumed.get("alpha")["session_id"] == a["session_id"]
        assert resumed.get("alpha")["driver_id"] == a["driver_id"]
    finally:
        await resumed.close()


async def test_missing_binary_and_unexpected_exit_are_visible(runtime):
    runtime.command = ["/missing-dsh-fixture"]
    with pytest.raises(ValueError):
        await runtime.start("alpha", actor="fixture")
    assert runtime.get("alpha")["phase"] == "failed"
    assert not runtime.get("alpha")["enabled"]
    runtime.command = [sys.executable, "-c", "raise SystemExit(7)"]
    await runtime.start("alpha", actor="fixture")
    await asyncio.gather(*runtime.monitors)
    assert runtime.get("alpha")["phase"] == "failed"
    assert "7" in runtime.get("alpha")["last_error"]
    assert runtime.status()["projects"]["alpha"]["online"] == 0


def test_empty_install_and_running_pipeline_counts_are_truthful(runtime, tmp_path):
    empty = DshDrivers(StateDatabase(str(tmp_path / 'empty.sqlite')), None, tmp_path / 'empty-drivers')
    assert empty.status() == {"total": 0, "online": 0, "projects": {}}
    service = StateService(runtime.db, actor='fixture', project_read_trusted=True)
    for name, status in [('executing', 'running'), ('paused', 'paused'), ('queued', 'reserved')]:
        service.store.add_nodes('alpha', [{'key':name, 'goal':name,
            'acceptance':[{'id':'check', 'kind':'test', 'description':'fixture'}]}])
        attempt = service.attempts.reserve('alpha', name, 1, 'fixture_pipeline', 'fixture-' + name)
        # Persist the lifecycle observations that this read-only dashboard consumes.
        with runtime.db.get_connection() as conn:
            conn.execute('UPDATE state_attempts SET status=? WHERE attempt_id=?', (status, attempt['attempt_id']))
            conn.commit()
    assert runtime.status()['projects']['alpha']['running_pipelines'] == 1


async def test_every_running_worker_is_counted_once_across_native_and_external_attempts(runtime):
    """`running_subagents` is the union of the native and the external workers.

    It used to read only `subagents_json`, so an attempt registered through
    `start_external_attempt` (`execution_kind='external'`) was counted by
    neither figure: three running external attempts showed as
    `running_subagents: 0` on 2026-11-03.

    The dedupe identity is the per-worker id string, because both sides
    enumerate workers, not attempts:

    * native children are the DSH subagent *run ids* the runtime adds on
      `subagent/start` (integrations/dsh-driver/runtime.mjs, `info.runId`), and
    * an external attempt's worker is its `external_id`.

    It is deliberately NOT `claim_node`'s `subagent` field: that one is a
    claimed label `'<driver_id>/<label>'`, a different namespace from the run
    ids in `subagents_json`, so it can never identify the same worker. A
    driver that starts an external attempt for a native child therefore uses
    that child's run id as `external_id` and counts once. `running_pipelines`
    keeps counting running skillflow attempts only.
    """
    from core.state_commands import execute
    service = StateService(runtime.db, actor='fixture', project_read_trusted=True)
    node = lambda key: {'key': key, 'goal': key, 'acceptance': [{'id': 'check', 'kind': 'test', 'description': 'fixture'}]}
    service.store.add_nodes('alpha', [node('external-one'), node('external-two'), node('pipeline')])
    service.store.add_nodes('beta', [node('shared'), node('external-only')])
    run_id = 'run-2f7c1f9a4f0e4d6c'

    def start_external(project, node_key, request_key, external_id):
        attempt = execute(service, 'start_external_attempt', {'project_id': project, 'node_key': node_key,
            'expected_revision': 1, 'harness': 'dsh', 'external_id': external_id,
            'request_key': request_key}, allow_write=True)
        return execute(service, 'report_external_attempt', {'attempt_id': attempt['attempt_id'],
            'observation_id': request_key + '-running', 'expected_version': attempt['observation_version'],
            'context_hash': attempt['context_hash'], 'status': 'running',
            'report_ref': '/tmp/not-retained-while-running', 'report_sha256': '0' * 64},
            allow_write=True)

    def settle(*attempt_ids, status='failed'):
        # Persist the lifecycle observation this read-only dashboard consumes.
        with runtime.db.get_connection() as conn:
            conn.executemany('UPDATE state_attempts SET status=? WHERE attempt_id=?',
                             [(status, attempt_id) for attempt_id in attempt_ids])
            conn.commit()

    card = lambda project: runtime.status()['projects'][project]
    try:
        # 1. One running external attempt and no native child: 1, not 0. This
        #    project has no DSH driver row at all, so its card must still exist
        #    with zeros elsewhere instead of disappearing.
        first = start_external('alpha', 'external-one', 'rk-external-one', 'run-alpha-one')
        assert card('alpha') == {'total': 0, 'online': 0, 'running_subagents': 1, 'running_pipelines': 0}
        # The same worker is a worker, not two: one external_id twice on two
        # nodes is still one running worker.
        second = start_external('alpha', 'external-two', 'rk-external-two', 'run-alpha-one')
        assert card('alpha')['running_subagents'] == 1
        # skillflow attempts stay out of this figure and keep their own.
        pipeline = service.attempts.reserve('alpha', 'pipeline', 1, 'fixture_pipeline', 'fixture-pipeline')
        settle(pipeline['attempt_id'], status='running')
        assert card('alpha')['running_pipelines'] == 1 and card('alpha')['running_subagents'] == 1
        # 2. Settled attempts are not running, in every terminal status.
        settle(first['attempt_id'])
        assert card('alpha')['running_subagents'] == 1
        settle(second['attempt_id'], status='abandoned')
        assert card('alpha')['running_subagents'] == 0
        # ... and the pipeline is untouched by that.
        assert card('alpha')['running_pipelines'] == 1

        # 3. No double count when the worker is both a registered native
        #    subagent (its run id is in subagents_json) and a running external
        #    attempt using that same run id as external_id.
        await runtime.start('beta', actor='fixture')
        runtime.update('beta', phase='running', subagents_json=json.dumps([run_id, 'run-native-only']))
        start_external('beta', 'shared', 'rk-shared', run_id)
        assert card('beta')['running_subagents'] == 2, 'the shared worker must be counted once'
        start_external('beta', 'external-only', 'rk-external-only', 'run-external-only')
        assert card('beta')['running_subagents'] == 3
        assert card('beta')['running_pipelines'] == 0
    finally:
        await runtime.close()


async def test_profile_keeps_owner_credentials_out_and_uses_pinned_runtime(runtime, monkeypatch):
    monkeypatch.setenv("AITELIER_ADMIN_TOKEN", "fixture-never-inherit-owner")
    monkeypatch.setenv("AITELIER_DRIVER_TOKEN", "fixture-wrong-driver")
    try:
        await runtime.start("alpha", actor="fixture")
        row = runtime.get("alpha")
        env = runtime._environment(row)
        assert "AITELIER_ADMIN_TOKEN" not in env
        assert env["AITELIER_DRIVER_TOKEN"] != "fixture-wrong-driver"
        assert env["DSH_HOME"].startswith(str(runtime.root))
        patch = (Path(env["DSH_HOME"]) / "profiles/driver/cordis.patch.yml").read_text()
        assert env["AITELIER_DRIVER_TOKEN"] not in patch
        assert "process.env.AITELIER_DRIVER_TOKEN" in patch
    finally:
        await runtime.close()


async def test_real_http_context_is_own_private_scope_and_compact_payload_is_minimal(runtime, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api import authz
    from api.dsh_driver_routers import router, manager
    from api.state_graph_routers import get_service
    from core.state_commands import execute
    try:
        driver = await runtime.start("alpha", actor="fixture")
        driver_id = driver["driver_id"]
        service = StateService(runtime.db, actor="driver:" + driver_id, project_read_trusted=True, driver_id=driver_id)
        service.store.add_nodes("alpha", [{"key": "n", "goal": "A full node description that must not be injected",
            "acceptance": [{"id": "a", "kind": "test", "description": "fixture criterion"}]}])
        execute(service, "write_driver_note_entry", {"project_id": "alpha", "assertion": "Project standing rule",
            "body": "Project body stays fetchable by address", "director_identity": driver_id}, allow_write=True)
        execute(service, "write_driver_note_entry", {"driver_id": driver_id, "assertion": "Private standing rule",
            "body": "Private body stays fetchable by address", "director_identity": driver_id}, allow_write=True)
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[manager] = lambda: runtime
        app.dependency_overrides[get_service] = lambda: service
        app.dependency_overrides[authz.require_reader] = lambda: None
        app.dependency_overrides[authz.require_writer] = lambda: None
        identity = SimpleNamespace(driver_id=driver_id)
        monkeypatch.setattr(authz, "request_identity", lambda _: identity)
        with TestClient(app) as client:
            result = client.get("/api/dsh-drivers/alpha/context")
            assert result.status_code == 200, result.text
            body = result.json()
            assert body["nodes"] == [{"name": "n", "status": "OPEN"}]
            assert "Project standing rule" in json.dumps(body["project_note"])
            assert "Private standing rule" in json.dumps(body["private_note"])
            assert "fetchable by address" not in json.dumps(body)
            assert "grep_chat_history" in body["runtime_skill"]
            assert client.post("/api/dsh-drivers/alpha/heartbeat").status_code == 200
            update = client.post("/api/dsh-drivers/alpha/activity", json={"phase": "waiting", "subagents": ["native-child"]})
            assert update.status_code == 200, update.text
            assert client.get("/api/dsh-drivers").json()["projects"]["alpha"]["running_subagents"] == 1
            identity.driver_id = "other-driver"
            assert client.get("/api/dsh-drivers/alpha/context").status_code == 403
            assert client.post("/api/dsh-drivers/alpha/activity", json={"phase": "running"}).status_code == 403
            assert client.post("/api/dsh-drivers/alpha/heartbeat").status_code == 403
    finally:
        await runtime.close()


async def test_host_completion_reuses_native_session_gateway_and_real_failover(runtime, tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api import authz
    from api.dsh_driver_routers import router, manager
    import core.ai_router as ai
    import litellm
    providers = tmp_path / 'providers.json'
    routes = tmp_path / 'routes.json'
    providers.write_text(json.dumps({p: {'base_url': 'https://example.invalid/v1', 'api_key_env': 'FIXTURE'}
                                    for p in ('opencode', 'opencodego', 'deepseek')}))
    endpoints = ['opencode/deepseek-v4.1-flash-free', 'opencodego/deepseek-v4.1-flash', 'deepseek/deepseek-flash']
    routes.write_text(json.dumps({'driver': endpoints}))
    monkeypatch.setattr(ai, '_read_secret', lambda _: 'fixture-key')
    monkeypatch.setattr(ai, '_ENDPOINT_COOLDOWN', {})
    gateway_class = ai.AIGateway
    monkeypatch.setattr(ai, 'AIGateway', lambda model, **kw: gateway_class(model, config_path=str(providers), routes_path=str(routes), **kw))
    called = []
    fail_go = False
    def call(gateway, kwargs, **unused):
        called.append((gateway.active_model, kwargs))
        if gateway.provider == 'opencode' or (gateway.provider == 'opencodego' and fail_go):
            raise litellm.AuthenticationError('fixture unavailable', llm_provider='openai', model=gateway.active_model)
        return litellm.ModelResponse(choices=[{'message': {'role': 'assistant', 'content': None,
            'reasoning_content': 'checked', 'tool_calls': [{'id': 'call-1', 'type': 'function',
             'function': {'name': 'wait', 'arguments': '{}'}}]}, 'finish_reason': 'tool_calls'}],
             usage={'prompt_tokens': 100, 'completion_tokens': 10, 'total_tokens': 110})
    monkeypatch.setattr(gateway_class, '_call_llm', call)
    try:
        driver = await runtime.start('alpha', actor='fixture')
        assert driver['provider'] == 'aitelier' and driver['model'] == 'driver'
        identity = SimpleNamespace(driver_id=driver['driver_id'])
        monkeypatch.setattr(authz, 'request_identity', lambda _: identity)
        app = FastAPI(); app.include_router(router)
        app.dependency_overrides[manager] = lambda: runtime
        app.dependency_overrides[authz.require_writer] = lambda: None
        payload = {'session_id': 'native-root', 'messages': [{'role': 'user', 'content': 'work'}],
                   'tools': [{'type': 'function', 'function': {'name': 'wait', 'parameters': {'type': 'object'}}}]}
        with TestClient(app) as client:
            result = client.post('/api/dsh-drivers/alpha/completion', json=payload)
            assert result.status_code == 200, result.text
            assert result.json()['served_by'] == endpoints[1]
            assert result.json()['tool_calls'][0]['function']['name'] == 'wait'
            assert result.json()['reasoning_content'] == 'checked'
            assert result.json()['usage']['prompt_tokens'] == 100
            assert [c[0] for c in called] == endpoints[:2]
            session_header = called[-1][1]['extra_headers']['x-opencode-session']
            assert called[-1][1]['extra_headers']['User-Agent'].startswith('AItelier/')
            assert client.post('/api/dsh-drivers/alpha/completion', json=payload).status_code == 200
            assert called[-1][0] == endpoints[1]
            assert called[-1][1]['extra_headers']['x-opencode-session'] == session_header
            assert len(runtime.gateways['alpha']) == 1
            fail_go = True
            result = client.post('/api/dsh-drivers/alpha/completion', json=payload)
            assert result.json()['served_by'] == endpoints[2]
            assert 'x-opencode-session' not in called[-1][1].get('extra_headers', {})
            identity.driver_id = 'other'
            assert client.post('/api/dsh-drivers/alpha/completion', json=payload).status_code == 403
        await runtime.stop('alpha')
        assert 'alpha' not in runtime.gateways
    finally:
        await runtime.close()


async def test_history_is_bounded_private_and_live_events_use_existing_private_channel(runtime, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api import authz
    from api.dsh_driver_routers import router, manager
    from api.sse_manager import stream_manager
    from unittest.mock import AsyncMock
    try:
        driver = await runtime.start('alpha', actor='fixture')
        row = runtime.get('alpha')
        assert runtime.history('alpha')['entries'] == []
        root = runtime.root / row['driver_id'] / 'home/sessions'
        root.mkdir()
        seen = []
        def read(command, **kwargs):
            seen.append((command, kwargs))
            return SimpleNamespace(returncode=0, stdout=json.dumps({'session_id': row['session_id'], 'entries': [], 'has_more': False, 'next_before_seq': None}))
        monkeypatch.setattr('subprocess.run', read)
        app = FastAPI(); app.include_router(router)
        app.dependency_overrides[manager] = lambda: runtime
        monkeypatch.setattr(authz, 'write_denial_reason', lambda _: authz.WRITE_DENIED_NOT_AUTHENTICATED)
        with TestClient(app) as client:
            assert client.get('/api/dsh-drivers/alpha/history').status_code == 403
            assert seen == []
            app.dependency_overrides[authz.require_reader] = lambda: None
            result = client.get('/api/dsh-drivers/alpha/history?before_seq=19&limit=3')
            assert result.status_code == 200, result.text
            assert result.json()['driver_id'] == row['driver_id']
            assert seen[-1][0][-4:] == [str(root), row['session_id'], '19', '3']
            assert 'token' not in json.dumps(seen[-1][0])
            assert client.get('/api/dsh-drivers/alpha/history?limit=101').status_code == 422
            assert client.get('/api/dsh-drivers/alpha/history?before_seq=-1').status_code == 422
            app.dependency_overrides[authz.require_writer] = lambda: None
            monkeypatch.setattr(authz, 'request_identity', lambda _: SimpleNamespace(driver_id=row['driver_id']))
            push = AsyncMock(); monkeypatch.setattr(stream_manager, 'push_log', push)
            entry = {'seq': 12, 'type': 'tool/result', 'text': 'private tool result', 'timestamp': 1}
            result = client.post('/api/dsh-drivers/alpha/activity', json={'phase': 'running', 'subagents': [], 'event': entry})
            assert result.status_code == 200, result.text
            channel, text = push.call_args.args
            assert channel == 'dsh:' + row['driver_id']
            assert json.loads(text)['entry']['text'] == 'private tool result'
            monkeypatch.setattr(authz, 'request_identity', lambda _: SimpleNamespace(driver_id='other'))
            assert client.post('/api/dsh-drivers/alpha/activity', json={'phase': 'running', 'event': entry}).status_code == 403
    finally:
        await runtime.close()
