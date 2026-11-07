"""Managed DSH lifecycle against isolated real State/identity storage."""
import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from core.drivers import DriverRegistry
from core.dsh_driver import DshDrivers, SERVED_BY_SEEN_LIMIT
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
            # The card also carries the unsettled-attempt figures, kept separate
            # from the running counts so a registered-but-idle attempt can never
            # read as a live worker.
            "unsettled_attempts": 0, "unsettled_expired_attempts": 0,
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


async def test_running_workers_are_live_children_not_registered_attempts(runtime):
    """`running_subagents` must mean LIVE workers, not registered attempts.

    An external attempt proves a worker was registered. Only the native child
    list tracks liveness, because the runtime empties it when a child exits.
    Measured 2026-11-03: two of three "running" external attempts had leases
    expired 5 and 21 days earlier, and a worker visible through both a native
    child and an external attempt was counted twice (native ids are UUIDs,
    external_ids are slugs, so they can never be matched by identity).
    """
    from core.state_commands import execute
    import datetime as _dt
    service = StateService(runtime.db, actor='fixture', project_read_trusted=True)
    node = lambda key: {'key': key, 'goal': key, 'acceptance': [{'id': 'check', 'kind': 'test', 'description': 'fixture'}]}
    service.store.add_nodes('alpha', [node('live-worker'), node('dead-worker')])
    run_id = 'c1708e1a-d013-4dfe-9c9b-6a4a346a7d35'

    def register(project, node_key, request_key, external_id):
        return execute(service, 'start_external_attempt', {'project_id': project, 'node_key': node_key,
            'expected_revision': 1, 'harness': 'dsh', 'external_id': external_id,
            'request_key': request_key}, allow_write=True)

    def set_lease(attempt_id, iso):
        with runtime.db.get_connection() as conn:
            conn.execute('UPDATE state_attempts SET lease_expires_at=? WHERE attempt_id=?', (iso, attempt_id))
            conn.commit()

    card = lambda project: runtime.status()['projects'][project]
    future = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=2)).isoformat()
    past = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=21)).isoformat()
    try:
        await runtime.start('alpha', actor='fixture')
        # One live native child, and an external attempt registered for that same
        # piece of work under a slug id. That is one worker, not two.
        runtime.update('alpha', phase='running', subagents_json=json.dumps([run_id]))
        live = register('alpha', 'live-worker', 'rk-live', 'compact-pane-20261103')
        set_lease(live['attempt_id'], future)
        assert card('alpha')['running_subagents'] == 1, 'the registered attempt must not inflate a live child'
        assert card('alpha')['unsettled_attempts'] == 1
        assert card('alpha')['unsettled_expired_attempts'] == 0

        # A dead row is never a running worker, and it is visibly expired.
        dead = register('alpha', 'dead-worker', 'rk-dead', 'agent-depth-pollution-r5-20261013')
        set_lease(dead['attempt_id'], past)
        assert card('alpha')['running_subagents'] == 1, 'a dead attempt is not a worker'
        assert card('alpha')['unsettled_attempts'] == 2
        assert card('alpha')['unsettled_expired_attempts'] == 1, 'the expired one must be distinguishable'

        # Settling removes it from the unsettled figure entirely.
        with runtime.db.get_connection() as conn:
            conn.execute("UPDATE state_attempts SET status='failed' WHERE attempt_id=?", (dead['attempt_id'],))
            conn.commit()
        assert card('alpha')['unsettled_attempts'] == 1
        assert card('alpha')['unsettled_expired_attempts'] == 0

        # A stopped driver has no live children, whatever is still registered.
        # (phase='waiting' is NOT enough - the process is still alive, which is
        # exactly the distinction this contract is about.)
        await runtime.stop('alpha')
        assert card('alpha')['running_subagents'] == 0, 'a stopped driver holds no live workers'
        assert card('alpha')['unsettled_attempts'] == 1, 'the unsettled attempt is still visible'
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
        payload = {'session_id': driver['session_id'], 'messages': [{'role': 'user', 'content': 'work'}],
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
            # The plan recovers: once the probe interval passes, the session
            # leaves the paid fallback instead of sticking to it for its life.
            fail_go = False
            assert client.post('/api/dsh-drivers/alpha/completion', json=payload).json()['served_by'] == endpoints[2]
            import time as _time
            later = _time.time() + ai._RETURN_PROBE_EVERY_S + 1
            monkeypatch.setattr(_time, 'time', lambda: later)
            result = client.post('/api/dsh-drivers/alpha/completion', json=payload)
            assert result.json()['served_by'] == endpoints[1]
            assert called[-1][1]['extra_headers']['x-opencode-session'] == session_header
            identity.driver_id = 'other'
            assert client.post('/api/dsh-drivers/alpha/completion', json=payload).status_code == 403
        await runtime.stop('alpha')
        assert 'alpha' not in runtime.gateways
    finally:
        await runtime.close()


async def test_native_workers_rotate_once_per_session_and_fail_over_without_changing_director(runtime, tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api import authz
    from api.dsh_driver_routers import router, manager
    import core.ai_router as ai
    import litellm

    providers = tmp_path / 'providers.json'
    routes = tmp_path / 'routes.json'
    providers.write_text(json.dumps({p: {'base_url': 'https://example.invalid/v1', 'api_key_env': 'FIXTURE'}
                                    for p in ('opencodego', 'ollamacloud', 'deepseek')}))
    pool = ['opencodego/deepseek-v4.1-flash', 'ollamacloud/deepseek-v4.1-flash']
    routes.write_text(json.dumps({'driver': ['deepseek/deepseek-flash'], 'worker': {'rotate': pool}}))
    monkeypatch.setattr(ai, '_read_secret', lambda _: 'fixture-key')
    monkeypatch.setattr(ai, '_ENDPOINT_COOLDOWN', {})
    gateway_class = ai.AIGateway
    monkeypatch.setattr(ai, 'AIGateway', lambda model, **kw: gateway_class(model, config_path=str(providers), routes_path=str(routes), **kw))
    calls = []
    fail_go = False
    def call(gateway, kwargs, **unused):
        calls.append(gateway.active_model)
        if fail_go and gateway.provider == 'opencodego':
            raise litellm.AuthenticationError('fixture unavailable', llm_provider='openai', model=gateway.active_model)
        return litellm.ModelResponse(choices=[{'message': {'role': 'assistant', 'content': 'worker ok'},
                                             'finish_reason': 'stop'}],
                                    usage={'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12})
    monkeypatch.setattr(gateway_class, '_call_llm', call)
    try:
        driver = await runtime.start('alpha', actor='fixture')
        monkeypatch.setattr(authz, 'request_identity', lambda _: SimpleNamespace(driver_id=driver['driver_id']))
        app = FastAPI(); app.include_router(router)
        app.dependency_overrides[manager] = lambda: runtime
        app.dependency_overrides[authz.require_writer] = lambda: None
        with TestClient(app) as client:
            def complete(session):
                response = client.post('/api/dsh-drivers/alpha/completion', json={
                    'session_id': session, 'messages': [{'role': 'user', 'content': 'work'}]})
                assert response.status_code == 200, response.text
                return response.json()['served_by']
            assert complete(driver['session_id']) == 'deepseek/deepseek-flash'
            assert complete('native-child-a') == pool[0]
            assert complete('native-child-a') == pool[0], 'tool turns must not rotate'
            assert complete('native-child-b') == pool[1]
            assert complete('native-child-c') == pool[0]
            assert complete('native-child-b') == pool[1]
            fail_go = True
            assert complete('native-child-a') == pool[1], 'failure must try the other pool member'
            assert calls[-2:] == pool
            assert complete('native-child-a') == pool[1], 'successful failover remains sticky'
            assert complete(driver['session_id']) == 'deepseek/deepseek-flash'
            assert len(runtime.gateways['alpha']) == 4
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


# ── Two unguarded failures in the host runtime ───────────────────────────────
# Both were found while reading this file for the owner's "what is broken in
# your own runtime" question, and both were left unfixed because each looked
# like it needed a contract decision. Neither does: one is a missing None check
# inside a background task, the other is undoing our own side effects when we
# lose a race.

async def test_observe_tolerates_a_project_deleted_while_the_process_died(runtime):
    """A row deleted between process exit and the read must not raise.

    `_observe` is a background task whose only consumer discards it, so an
    exception here was never retrieved: it surfaced as "Task exception was never
    retrieved" while the project kept a phase nobody updated.
    """
    row = runtime._provision("alpha", "fixture-provider", "fixture-model", "fixture")
    deleted = []

    class Exited:
        async def wait(self):
            # The row disappears while the process is exiting.
            # The managed driver row is what _observe reads; deleting it is the
            # vanishing project as far as this code path is concerned (the State
            # project row itself is referenced by other tables and is a different
            # condition).
            with runtime.db.get_connection() as conn:
                conn.execute("DELETE FROM dsh_drivers WHERE project_id='alpha'")
                conn.commit()
            deleted.append(True)
            return 3

    proc = Exited()
    # _observe only acts when the mapped process IS the one that exited.
    runtime.processes["alpha"] = proc
    await runtime._observe("alpha", proc)   # must not raise
    assert deleted == [True]
    assert runtime.processes.get("alpha") is None


def test_losing_the_provision_race_removes_the_orphan_identity(runtime, tmp_path):
    """The loser of a cross-process insert race must undo its own effects.

    Before the fix the loser kept a directory, a token file and a registered
    membership, so the project had a second identity holding a token it would
    never use.
    """
    winner = runtime._provision("alpha", "fixture-provider", "fixture-model", "fixture")
    loser_id = "dsh-loser000000000001"
    directory = runtime.root / loser_id
    directory.mkdir(parents=True, mode=0o700)
    (directory / "token").write_text("orphan-token\n")
    runtime.registry.register(loser_id, "DSH · alpha", capabilities={"runtime": "dsh"}, actor="fixture")
    runtime.registry.set_membership("alpha", loser_id, "member", 0, "provisioned", actor="fixture",
                                    project_exists=lambda _: True)

    runtime._discard_provision("alpha", loser_id, directory, "fixture")

    assert not directory.exists(), "the orphan's directory and token must be gone"
    members = {m["driver_id"]: m["status"] for m in runtime.registry.project_members("alpha")}
    assert members.get(winner["driver_id"]) == "member", "the winner must be untouched"
    assert members.get(loser_id) == "removed", "the loser must no longer be a member"


# ── Local MCP services are configuration, not code ──────────────────────────
# The host runs MCP servers the driver can use: agentmcp (asr/ocr/pdf/web,
# media generation, video), devmcp (files, bash, subagent jobs), and zvec. They are
# registered through the same cordis patch the State MCP already uses, and the
# URL is the switch - no URL, no entry, so an operator can withdraw one without
# touching code and a host that lacks them behaves exactly as before.


def _patch_document(runtime, row):
    """The cordis patch as data.

    It carries `!!js` tags (the driver token expression is code, not text), which
    a safe loader refuses; the tag is irrelevant to what these tests assert, so
    every unknown tag is read as the scalar it decorates.
    """
    import yaml
    class _Any(yaml.SafeLoader):
        pass
    _Any.add_multi_constructor("tag:yaml.org,2002:",
                               lambda loader, suffix, node: loader.construct_scalar(node))
    path = (runtime.root / row["driver_id"] / "home" / "profiles" / "driver"
            / "cordis.patch.yml")
    return yaml.load(path.read_text(), Loader=_Any)


def test_native_spawn_and_fork_use_worker_without_changing_the_director(runtime):
    row = runtime._provision("alpha", "aitelier", "driver", "fixture")
    env = runtime._environment(row)
    patch = {p.get("id"): p for p in _patch_document(runtime, row)}
    for tool in ("tool-subagent", "tool-subagent-fork"):
        assert patch[tool]["config"]["agentOptions"] == {"provider": "aitelier", "model": "worker"}
    assert patch["tool-subagent"]["config"]["provider"] == "spawn"
    assert patch["tool-subagent-fork"]["config"]["provider"] == "fork"
    assert env["AITELIER_DSH_MODEL"] == "driver"
    assert runtime.get("alpha")["model"] == "driver"


def test_local_mcp_servers_are_registered_by_default(runtime):
    import yaml
    row = runtime._provision("alpha", "fixture-provider", "fixture-model", "fixture")
    runtime._environment(row)
    patch = _patch_document(runtime, row)
    insert = next(p["insert"] for p in patch if "insert" in p)
    by_id = {e["id"]: e for e in insert}

    # The State MCP must keep failing hard: it is a prerequisite, not a capability.
    assert by_id["mcp-aitelier"]["config"]["failOnStartupError"] is True
    for server, url in (("devmcp", "http://127.0.0.1:7676/mcp"),
                        ("zvec", "http://127.0.0.1:7999/mcp"),
                        ("agentmcp", "http://127.0.0.1:9003/mcp")):
        entry = by_id["mcp-" + server]
        assert entry["config"]["serverName"] == server
        assert entry["config"]["url"] == url
        assert entry["config"]["transport"] == "streamable-http"
        # A capability that is down must not stop the driver from starting.
        assert entry["config"]["failOnStartupError"] is False


def test_agentmcp_can_be_redirected_or_disabled(runtime, monkeypatch):
    row = runtime._provision("alpha", "fixture-provider", "fixture-model", "fixture")
    monkeypatch.setenv("AITELIER_DSH_MCP_AGENTMCP", "http://127.0.0.1:19003/mcp")
    runtime._environment(row)
    patch = _patch_document(runtime, row)
    by_id = {e["id"]: e for e in next(p["insert"] for p in patch if "insert" in p)}
    assert by_id["mcp-agentmcp"]["config"]["url"] == "http://127.0.0.1:19003/mcp"
    assert by_id["mcp-agentmcp"]["config"]["failOnStartupError"] is False, (
        "a media service being down still "
        "must not stop the driver")
    monkeypatch.setenv("AITELIER_DSH_MCP_AGENTMCP", "")
    runtime._environment(row)
    patch = _patch_document(runtime, row)
    ids = {e["id"] for e in next(p["insert"] for p in patch if "insert" in p)}
    assert "mcp-agentmcp" not in ids
    assert {"mcp-aitelier", "mcp-devmcp", "mcp-zvec"} <= ids


def test_blanking_one_local_mcp_url_removes_only_that_entry(runtime, monkeypatch):
    monkeypatch.setenv("AITELIER_DSH_MCP_DEVMCP", "")
    row = runtime._provision("alpha", "fixture-provider", "fixture-model", "fixture")
    runtime._environment(row)
    patch = _patch_document(runtime, row)
    ids = {e["id"] for e in next(p["insert"] for p in patch if "insert" in p)}
    assert "mcp-devmcp" not in ids, "a blank URL must withdraw the entry"
    assert "mcp-zvec" in ids, "the other services are independent"
    assert "mcp-aitelier" in ids


# ── Auto-compact policy ──────────────────────────────────────────────────────
# DSH fires automatic compaction at min(W * thresholdRatio, W - O - B), shipped
# ratio 0.8. The director's own route compacts near-clear: the State snapshot is
# re-injected once after every successful compaction, so its verbatim tail is
# cost, not memory. Workers (every other route) keep the previous policy. The director
# knob is a RATIO of the model's own window, clamped so a typo cannot silently
# disable compaction or make it constant.

def _compaction(runtime, row):
    runtime._environment(row)
    patch = _patch_document(runtime, row)
    return next(p for p in patch if p.get("id") == "compaction-basic")["config"]


def test_compaction_ratio_is_overridable_and_clamped(runtime, monkeypatch):
    row = runtime._provision("alpha", "fixture-provider", "fixture-model", "fixture")
    seen = {}
    for raw, expected in (("0.5", 0.5), ("0.384", 0.384), ("0.99", 0.8), ("0.01", 0.2), ("garbage", 0.25)):
        monkeypatch.setenv("AITELIER_DSH_COMPACT_RATIO", raw)
        config = _compaction(runtime, row)
        seen[raw] = config["modelPolicies"][0]["thresholdRatio"]
        assert seen[raw] == expected, f"{raw!r} should land on {expected}"
        assert config["thresholdRatio"] == 0.5, "the knob is the director's; workers keep theirs"


def test_pressure_goes_straight_to_the_summary_without_the_tool_result_pruner(runtime, monkeypatch):
    row = runtime._provision("alpha", "fixture-provider", "fixture-model", "fixture")
    runtime._environment(row)
    patch = _patch_document(runtime, row)
    assert {"id": "tool-result-pruner", "disabled": True} in patch, (
        "the pruner trims tool outputs first and can drop the context just under the "
        "trigger, skipping the summary and breaking the prefix cache")
    assert any(p.get("id") == "compaction-basic" for p in patch)


def test_director_compacts_near_clear_and_workers_keep_the_previous_policy(runtime, monkeypatch):
    monkeypatch.delenv("AITELIER_DSH_COMPACT_RATIO", raising=False)
    row = runtime._provision("alpha", "fixture-provider", "fixture-model", "fixture")
    config = _compaction(runtime, row)
    assert config["modelPolicies"] == [{
        "provider": "fixture-provider", "model": "fixture-model",
        "thresholdRatio": 0.25, "retainTokens": 0}], (
        "exactly one policy, for the director's own route: compact at a quarter of "
        "the window and keep only the last balanced step")
    assert config["thresholdRatio"] == 0.5, (
        "every other route (the aitelier/worker subagents) compacts at half the window")
    assert "retainTokens" not in config and "retainRatio" not in config, (
        "workers keep DSH's default verbatim tail (16%)")


# ── The model identity the endpoint resolved ────────────────────────────────
# The completion endpoint already picked a concrete endpoint per session
# (`gateway.active_model`, returned as `served_by`) and then kept nothing: the
# child's own session file only recorded the route alias `worker`, so a review
# criterion keyed on the writer's model family had no measured value to cite.
# These tests drive the REAL handler and read the stored row back, then measure
# the read surface's refusals through the app's own authz verdicts.

DRIVER_ENDPOINTS = ['deepseek/deepseek-flash']
WORKER_ENDPOINTS = ['opencodego/deepseek-v4.1-flash']


def _driver_token(runtime, project_id):
    """The managed driver's own credential, as it sits on disk."""
    row = runtime.get(project_id)
    return (runtime.root / row['driver_id'] / 'token').read_text().strip()


def _driver_app(runtime, tmp_path, monkeypatch, unnamed_flag=None, *,
                alias_flag=None, worker_endpoints=None, worker_rotate=True, transport=None):
    """The real router, the real authz verdicts, a stubbed transport.

    Nothing here overrides `require_reader`, `require_writer` or
    `request_identity`: the gate is switched ON and the fixture's own registered
    tokens are presented, so a 403 measured through this app is the app's
    decision rather than something the test declared.

    `worker_endpoints` / `worker_rotate` shape the worker route (a rotation pool
    by default, a plain ordered list when a test needs a deterministic first
    endpoint), and `transport` replaces the one canned answer, so a test can make
    an ENDPOINT fail the way the real transport fails one. `unnamed_flag` /
    `alias_flag` make the gateway hand back, respectively, nothing and the ROUTE
    ARGUMENT it was constructed with - the two ways a turn fails to name an
    endpoint that the store has to survive.
    """
    from fastapi import FastAPI
    from api import authz
    from api.dsh_driver_routers import manager, router
    from core import cf_access
    import core.ai_router as ai
    import litellm

    worker_endpoints = list(worker_endpoints or WORKER_ENDPOINTS)
    providers = tmp_path / 'providers.json'
    routes = tmp_path / 'routes.json'
    # Every provider the routes name must be registered, or litellm cannot place
    # the endpoint: derive them from the endpoints so a test's own pool works.
    named = {endpoint.split('/', 1)[0] for endpoint in DRIVER_ENDPOINTS + worker_endpoints}
    providers.write_text(json.dumps({p: {'base_url': 'https://example.invalid/v1',
                                        'api_key_env': 'FIXTURE'}
                                     for p in sorted(named)}))
    routes.write_text(json.dumps({
        'driver': DRIVER_ENDPOINTS,
        'worker': {'rotate': worker_endpoints} if worker_rotate else worker_endpoints}))
    monkeypatch.setattr(ai, '_read_secret', lambda _: 'fixture-key')
    monkeypatch.setattr(ai, '_ENDPOINT_COOLDOWN', {})

    def answer(gateway, kwargs, **unused):
        if transport is not None:
            return transport(gateway, kwargs)
        return litellm.ModelResponse(choices=[{'message': {'role': 'assistant', 'content': 'ok'},
                                               'finish_reason': 'stop'}],
                                     usage={'prompt_tokens': 10, 'completion_tokens': 2,
                                            'total_tokens': 12})

    gateway_class = ai.AIGateway
    monkeypatch.setattr(gateway_class, '_call_llm', answer)

    class Unnameable(gateway_class):
        """A turn whose endpoint the gateway cannot attribute to one model.

        The unnamed/alias report is a POST-HOC edit of what this gateway says it
        was bound to, so the real binding is put back before the next turn:
        otherwise the fixture would leave the gateway believing it is bound to ''
        and a later turn could not fail over or answer at all - an artifact of
        the fixture, not anything the deployment does.
        """
        _true_binding = None

        def generate_native(self, messages, **kwargs):
            if self._true_binding is not None:
                self.active_model = self._true_binding
                self._true_binding = None
            turn = super().generate_native(messages, **kwargs)
            self._true_binding = self.active_model
            if unnamed_flag is not None and unnamed_flag[0]:
                self.active_model = ''
            if alias_flag is not None and alias_flag[0]:
                # A gateway that hands back the model argument it was GIVEN
                # (`worker`, `driver`) rather than the endpoint it bound. It is
                # the route alias, so the store must refuse it - `served_by or
                # None` did not, and stored `worker` as if the alias answered.
                self.active_model = self.internal_model
            return turn

    monkeypatch.setattr(ai, 'AIGateway', lambda model, **kw: Unnameable(
        model, config_path=str(providers), routes_path=str(routes), **kw))
    # The gate is ON because the DEPLOYMENT is configured, not because a verdict
    # function was replaced: `cf_access.is_configured()` is the real switch, so
    # `require_reader`/`require_writer`/`request_identity` run their own logic.
    # Only the registry lookup is redirected, since it would open the production DB.
    monkeypatch.setattr(cf_access, '_TEAM_DOMAIN', 'fixture.cloudflareaccess.com')
    monkeypatch.setattr(cf_access, '_AUD', 'fixture-aud')
    monkeypatch.setattr(authz, 'driver_registry', lambda: runtime.registry)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[manager] = lambda: runtime
    return app


async def test_completion_records_the_resolved_model_for_each_session(runtime, tmp_path, monkeypatch):
    """The row is written where the endpoint was chosen, with that endpoint's name."""
    from fastapi.testclient import TestClient
    unnamed = [False]
    app = _driver_app(runtime, tmp_path, monkeypatch, unnamed_flag=unnamed)
    try:
        driver = await runtime.start('alpha', actor='fixture')
        headers = {'X-AItelier-Driver-Token': _driver_token(runtime, 'alpha')}
        payload = lambda session: {'session_id': session,
                                   'messages': [{'role': 'user', 'content': 'work'}]}
        with TestClient(app) as client:
            root = client.post('/api/dsh-drivers/alpha/completion',
                               json=payload(driver['session_id']), headers=headers)
            assert root.status_code == 200, root.text
            assert root.json()['served_by'] == DRIVER_ENDPOINTS[0]
            child = client.post('/api/dsh-drivers/alpha/completion',
                                json=payload('native-child-a'), headers=headers)
            assert child.status_code == 200, child.text
            assert child.json()['served_by'] == WORKER_ENDPOINTS[0]

            # The row as FIRST written: one call, first and last sighting equal.
            first = {r['session_id']: r for r in runtime.session_models('alpha')}['native-child-a']
            assert first['calls'] == 1
            assert first['first_seen'] == first['last_seen']

            # Age BOTH stamps, then call again: the second call must MOVE
            # last_seen. `last_seen > first_seen` alone would not measure that -
            # the first insert already satisfies it, so dropping
            # `last_seen=excluded.last_seen` from the UPSERT still passed.
            with runtime.db.get_connection() as conn:
                conn.execute("UPDATE dsh_driver_session_models SET first_seen=?, last_seen=? "
                             "WHERE project_id='alpha' AND session_id='native-child-a'",
                             ('2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'))
                conn.commit()
            again = client.post('/api/dsh-drivers/alpha/completion',
                                json=payload('native-child-a'), headers=headers)
            assert again.status_code == 200, again.text

            # A gateway that cannot name a concrete model records UNKNOWN.
            unnamed[0] = True
            unknown = client.post('/api/dsh-drivers/alpha/completion',
                                  json=payload('native-child-z'), headers=headers)
            unnamed[0] = False
            assert unknown.status_code == 200, unknown.text
            assert unknown.json()['served_by'] in ('', None)

            stored = {r['session_id']: r for r in runtime.session_models('alpha')}
            assert set(stored) == {driver['session_id'], 'native-child-a', 'native-child-z'}
            assert stored[driver['session_id']]['route'] == 'driver', (
                'the root session is recorded under its own route, not as a worker')
            assert stored[driver['session_id']]['served_by'] == DRIVER_ENDPOINTS[0]
            assert stored[driver['session_id']]['calls'] == 1
            child_row = stored['native-child-a']
            assert child_row['route'] == 'worker'
            assert child_row['served_by'] == WORKER_ENDPOINTS[0], (
                'the concrete endpoint that answered, not the alias it asked for')
            assert child_row['calls'] == 2, 'a later call is counted on the same row'
            assert child_row['first_seen'] == '2026-01-01T00:00:00+00:00', (
                'first_seen is the first sighting, not the latest')
            assert child_row['last_seen'] != first['last_seen'], (
                'the second call stores a NEW last_seen instead of keeping the first')
            assert child_row['last_seen'] != '2026-01-01T00:00:00+00:00', (
                'the aged stamp is overwritten; deleting last_seen=excluded.last_seen '
                'from the UPSERT leaves it and would still satisfy last_seen > first_seen')
            assert child_row['last_seen'] > child_row['first_seen']
            unknown_row = stored['native-child-z']
            # The alias assertion comes FIRST so it is the one that fails when
            # the store stops refusing it: behind an `is None` check it could
            # never fire, because a stored 'worker' is not None either and the
            # earlier assertion aborted the test with the wrong complaint.
            assert unknown_row['served_by'] != 'worker', (
                'storing the route alias in served_by would claim the alias answered')
            assert unknown_row['served_by'] is None, (
                'an unattributable turn stays unknown instead of repeating the route name')
            assert unknown_row['route'] == 'worker'
            assert unknown_row['served_by_seen'] == [], (
                'a turn that named nothing adds nothing to the endpoints the session named')

            served = client.get('/api/dsh-drivers/alpha/model-sessions', headers=headers)
            assert served.status_code == 200, served.text
            sessions = {s['session_id']: s for s in served.json()['sessions']}
            assert set(sessions) == set(stored)
            assert set(sessions['native-child-a']) == {'session_id', 'route', 'served_by',
                                                       'served_by_seen', 'first_seen',
                                                       'last_seen', 'calls'}
            assert sessions['native-child-a']['served_by'] == WORKER_ENDPOINTS[0]
            assert sessions['native-child-a']['served_by_seen'] == list(WORKER_ENDPOINTS)
            assert sessions['native-child-a']['calls'] == 2
            assert sessions['native-child-z']['served_by'] != 'worker', (
                'the read surface must not hand back the alias in place of a model')
            assert sessions['native-child-z']['served_by'] is None
            assert sessions['native-child-z']['served_by_seen'] == []

        # The record is durable, not a cache that the gateway teardown clears.
        await runtime.stop('alpha')
        assert 'alpha' not in runtime.gateways
        with TestClient(app) as client:
            after = client.get('/api/dsh-drivers/alpha/model-sessions', headers=headers)
            assert after.status_code == 200, after.text
            assert len(after.json()['sessions']) == 3
    finally:
        await runtime.close()


async def test_a_failed_record_write_still_returns_the_turn(runtime, tmp_path, monkeypatch, caplog):
    """A bookkeeping write that fails must not fail a turn the model answered.

    The endpoint writes the identity AFTER the model answered, and that call is
    already paid for. A write that cannot land (a locked SQLite file past the
    busy timeout, a missing table, a read-only or full disk) is a reason to lose
    the RECORD, not a reason to answer the driver with a 500 for a turn it
    already has. The loss is reported - once, naming the project, the session and
    the exception class - so an unrecorded turn is observable rather than silent.

    Measured on REAL SQLite failures, not by stubbing `record_model_call` out:
    the store method runs for real and SQLite is what refuses it, so the 200
    below is this handler's `except` branch surviving an actual failed write.
    Two shapes, because they break different parts of it: another connection
    holding the database's write lock (the real contention case - the
    transaction cannot even begin) and the table absent from a database that
    still answers reads (the statement never prepares).
    """
    import contextlib
    import logging
    import sqlite3
    from fastapi.testclient import TestClient
    app = _driver_app(runtime, tmp_path, monkeypatch)
    try:
        await runtime.start('alpha', actor='fixture')
        headers = {'X-AItelier-Driver-Token': _driver_token(runtime, 'alpha')}
        payload = lambda session: {'session_id': session,
                                   'messages': [{'role': 'user', 'content': 'work'}]}
        database = runtime.db
        with TestClient(app) as client:
            recorded = client.post('/api/dsh-drivers/alpha/completion',
                                   json=payload('native-child-ok'), headers=headers)
            assert recorded.status_code == 200, recorded.text

            def warnings_for(session):
                return [r for r in caplog.records
                        if 'could not record the answering model' in r.getMessage()
                        and session in r.getMessage()]

            @contextlib.contextmanager
            def impatient():
                """The REAL database; only the busy timeout is short.

                The lock, the refusal and the OperationalError are SQLite's own -
                waiting the production 10s would change the duration of this test
                and nothing it measures.
                """
                conn = sqlite3.connect(database.db_path, timeout=0.05)
                conn.row_factory = sqlite3.Row
                try:
                    conn.execute('PRAGMA foreign_keys=ON')
                    yield conn
                finally:
                    conn.close()

            # ── a second connection holds the write lock ────────────────────
            locker = sqlite3.connect(database.db_path, timeout=0.05)
            try:
                locker.execute('BEGIN IMMEDIATE')
                database.get_connection = impatient
                caplog.set_level(logging.WARNING)
                try:
                    broken = client.post('/api/dsh-drivers/alpha/completion',
                                         json=payload('native-child-locked'), headers=headers)
                finally:
                    del database.get_connection
            finally:
                locker.rollback()
                locker.close()
            assert broken.status_code == 200, (
                'the answer is already paid for: a failed record must not 500 the turn')
            assert set(broken.json()) == set(recorded.json()), (
                'a failed write leaves the response shape unchanged')
            assert broken.json()['served_by'] == recorded.json()['served_by'] == WORKER_ENDPOINTS[0]
            assert broken.json()['text'] == 'ok'

            lost = warnings_for('native-child-locked')
            assert len(lost) == 1, 'reported once: not swallowed, and not flooded'
            assert lost[0].levelname == 'WARNING'
            message = lost[0].getMessage()
            assert 'alpha' in message and 'native-child-locked' in message, (
                'the warning names the project and the session that went unrecorded')
            assert 'OperationalError' in message and 'locked' in message, (
                'the exception class and SQLite\'s own reason tell an operator a '
                'locked database from a bug')

            # The write really failed: the 200 is the answer surviving, not a
            # record that quietly was not attempted, and the earlier row is untouched.
            stored = {r['session_id']: r for r in runtime.session_models('alpha')}
            assert 'native-child-locked' not in stored
            assert stored['native-child-ok']['calls'] == 1

            # ── the table is gone; reads still answer, the write cannot ─────
            with database.get_connection() as conn:
                conn.execute('DROP TABLE dsh_driver_session_models')
                conn.commit()
            caplog.clear()
            absent = client.post('/api/dsh-drivers/alpha/completion',
                                 json=payload('native-child-no-table'), headers=headers)
            assert absent.status_code == 200, (
                'a record that cannot be written is still not a reason to lose the turn')
            assert set(absent.json()) == set(recorded.json())
            assert absent.json()['text'] == 'ok'
            lost = warnings_for('native-child-no-table')
            assert len(lost) == 1
            assert lost[0].levelname == 'WARNING'
            message = lost[0].getMessage()
            assert 'alpha' in message and 'native-child-no-table' in message
            assert 'OperationalError' in message and 'no such table' in message, (
                'the statement really reached SQLite and was refused there')
            with database.get_connection() as conn:
                exists = conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                      "AND name='dsh_driver_session_models'").fetchone()
            assert exists is None, 'no row can exist for that session: the table does not'
    finally:
        await runtime.close()


async def test_recorded_served_by_follows_failover_not_the_first_endpoint(runtime, tmp_path, monkeypatch):
    """A failed first endpoint must never be what the record names.

    `served_by` is read off the gateway after the call, so a turn that failed
    over records the endpoint that actually answered. A record taken from the
    route's first configured candidate would name a model that produced nothing.
    """
    import litellm
    from fastapi.testclient import TestClient
    pool = ['opencodego/deepseek-v4.1-flash', 'deepseek/deepseek-pro']

    def first_endpoint_is_down(gateway, kwargs):
        if gateway.active_model == pool[0]:
            raise litellm.AuthenticationError('fixture unavailable', llm_provider='openai',
                                              model=gateway.active_model)
        return litellm.ModelResponse(choices=[{'message': {'role': 'assistant',
                                                          'content': 'second answered'},
                                               'finish_reason': 'stop'}],
                                     usage={'prompt_tokens': 10, 'completion_tokens': 2,
                                            'total_tokens': 12})

    app = _driver_app(runtime, tmp_path, monkeypatch, worker_endpoints=pool, worker_rotate=False,
                      transport=first_endpoint_is_down)
    try:
        driver = await runtime.start('alpha', actor='fixture')
        headers = {'X-AItelier-Driver-Token': _driver_token(runtime, 'alpha')}
        with TestClient(app) as client:
            answer = client.post('/api/dsh-drivers/alpha/completion', headers=headers, json={
                'session_id': 'native-child-failover',
                'messages': [{'role': 'user', 'content': 'work'}]})
            assert answer.status_code == 200, answer.text
            assert answer.json()['served_by'] == pool[1], 'the second endpoint is the one that answered'
            assert answer.json()['text'] == 'second answered'
        row = {r['session_id']: r for r in runtime.session_models('alpha')}['native-child-failover']
        assert row['served_by'] == pool[1], (
            'the record names the endpoint that ANSWERED, not the first configured one')
        assert row['served_by'] != pool[0], 'the endpoint that failed produced no answer'
        assert row['route'] == 'worker', 'a native child is recorded under its own route'
        assert row['calls'] == 1
    finally:
        await runtime.close()


async def test_an_unnamed_turn_keeps_the_endpoint_the_session_already_named(runtime, tmp_path, monkeypatch):
    """A turn that cannot name an endpoint must not erase one the session named.

    Measured through the real completion handler: turn 1 names endpoint A, turn
    2 is a turn the gateway cannot attribute (`active_model` is ''), turn 3
    names B after a failover. The stored row keeps A across the unattributable
    turn - `served_by = A`, `calls = 2` - because the column records the last
    endpoint this session could NAME, not the endpoint of the last turn; a turn
    that does name B moves it. The read surface reports the same value.
    """
    import litellm
    from fastapi.testclient import TestClient
    pool = ['opencodego/deepseek-v4.1-flash', 'deepseek/deepseek-pro']
    unnamed = [False]
    state = {'first_endpoint_down': False}

    def transport(gateway, kwargs):
        if state['first_endpoint_down'] and gateway.active_model == pool[0]:
            raise litellm.AuthenticationError('fixture unavailable', llm_provider='openai',
                                              model=gateway.active_model)
        return litellm.ModelResponse(choices=[{'message': {'role': 'assistant', 'content': 'ok'},
                                               'finish_reason': 'stop'}],
                                     usage={'prompt_tokens': 10, 'completion_tokens': 2,
                                            'total_tokens': 12})

    app = _driver_app(runtime, tmp_path, monkeypatch, unnamed_flag=unnamed,
                      worker_endpoints=pool, worker_rotate=False, transport=transport)
    try:
        await runtime.start('alpha', actor='fixture')
        headers = {'X-AItelier-Driver-Token': _driver_token(runtime, 'alpha')}
        payload = lambda session: {'session_id': session,
                                   'messages': [{'role': 'user', 'content': 'work'}]}
        with TestClient(app) as client:
            named = client.post('/api/dsh-drivers/alpha/completion',
                                json=payload('native-child-keep'), headers=headers)
            assert named.status_code == 200, named.text
            assert named.json()['served_by'] == pool[0]
            first = {r['session_id']: r for r in runtime.session_models('alpha')}['native-child-keep']
            assert first['served_by'] == pool[0] and first['calls'] == 1
            assert first['served_by_seen'] == [pool[0]]

            unnamed[0] = True
            silent = client.post('/api/dsh-drivers/alpha/completion',
                                 json=payload('native-child-keep'), headers=headers)
            unnamed[0] = False
            assert silent.status_code == 200, silent.text
            assert silent.json()['served_by'] in ('', None), (
                'the fixture must hand back NO endpoint for this turn, or the test '
                'measures a turn that named one')
            kept = {r['session_id']: r for r in runtime.session_models('alpha')}['native-child-keep']
            assert kept['calls'] == 2, 'the unattributable turn still counts as a turn'
            assert kept['served_by'] == pool[0], (
                'a turn that could not name an endpoint ERASED the one the session had '
                'named: the row now claims this session was never attributed, and the '
                'only measured identity it had is gone')
            assert kept['served_by_seen'] == [pool[0]], (
                'and the list of named endpoints neither gains nor loses anything')

            served = client.get('/api/dsh-drivers/alpha/model-sessions', headers=headers)
            assert served.status_code == 200, served.text
            row = {s['session_id']: s for s in served.json()['sessions']}['native-child-keep']
            assert row['served_by'] == pool[0], (
                'the read surface must report the endpoint the session kept')
            assert row['served_by_seen'] == [pool[0]]

            state['first_endpoint_down'] = True
            moved = client.post('/api/dsh-drivers/alpha/completion',
                                json=payload('native-child-keep'), headers=headers)
            assert moved.status_code == 200, moved.text
            assert moved.json()['served_by'] == pool[1], 'the second endpoint answered this turn'
        final = {r['session_id']: r for r in runtime.session_models('alpha')}['native-child-keep']
        assert final['served_by'] == pool[1], (
            'a turn that DOES name an endpoint moves the record to it')
        assert final['served_by_seen'] == pool, 'both endpoints the session named, first-seen order'
        assert final['calls'] == 3
    finally:
        await runtime.close()


async def test_a_session_that_fails_over_records_every_endpoint_it_named(runtime, tmp_path, monkeypatch):
    """A, B, A: the session's model families are BOTH, not "whatever spoke last".

    One session survives more than one gateway: a failover to B (a real endpoint
    error, measured above) and a driver restart that binds a fresh gateway back
    to A while the child's session keeps its id. The record must name A and B in
    that order, with no duplicate when A answers again, and `served_by` stays the
    last one NAMED - so "which model family wrote this session" is answerable for
    a session that failed over mid-life instead of reading as the last family.
    """
    import litellm
    from fastapi.testclient import TestClient
    pool = ['opencodego/deepseek-v4.1-flash', 'deepseek/deepseek-pro']
    state = {'first_endpoint_down': False}

    def transport(gateway, kwargs):
        if state['first_endpoint_down'] and gateway.active_model == pool[0]:
            raise litellm.AuthenticationError('fixture unavailable', llm_provider='openai',
                                              model=gateway.active_model)
        return litellm.ModelResponse(choices=[{'message': {'role': 'assistant', 'content': 'ok'},
                                               'finish_reason': 'stop'}],
                                     usage={'prompt_tokens': 10, 'completion_tokens': 2,
                                            'total_tokens': 12})

    app = _driver_app(runtime, tmp_path, monkeypatch, worker_endpoints=pool,
                      worker_rotate=False, transport=transport)
    try:
        await runtime.start('alpha', actor='fixture')
        headers = {'X-AItelier-Driver-Token': _driver_token(runtime, 'alpha')}
        payload = lambda session: {'session_id': session,
                                   'messages': [{'role': 'user', 'content': 'work'}]}
        turn = lambda client, session: client.post('/api/dsh-drivers/alpha/completion',
                                                   json=payload(session), headers=headers)
        with TestClient(app) as client:
            first = turn(client, 'native-child-life')
            assert first.status_code == 200, first.text
            assert first.json()['served_by'] == pool[0]

            state['first_endpoint_down'] = True
            failed_over = turn(client, 'native-child-life')
            state['first_endpoint_down'] = False
            assert failed_over.status_code == 200, failed_over.text
            assert failed_over.json()['served_by'] == pool[1], 'the second endpoint answered'

            # A restarted driver rebuilds its gateway per session; the SESSION id
            # is what survives, and the record has to span both lifetimes.
            runtime.gateways.clear()
            back = turn(client, 'native-child-life')
            assert back.status_code == 200, back.text
            assert back.json()['served_by'] == pool[0], 'a fresh gateway binds the first endpoint'

            served = client.get('/api/dsh-drivers/alpha/model-sessions', headers=headers)
            assert served.status_code == 200, served.text
            row = {s['session_id']: s for s in served.json()['sessions']}['native-child-life']
            assert row['served_by'] == pool[0], 'served_by is the last endpoint this session named'
            assert row['served_by_seen'] == pool, (
                'both endpoints, in first-seen order, without repeating the one that '
                'answered twice - a single last-value column records only A here and '
                'so reports the wrong model family for the middle of the session')
            assert row['calls'] == 3

        # The list is bounded, and the bound is on the LIST - the last named
        # endpoint is still recorded past it. Measured on the store directly: a
        # session that names more endpoints than the cap is a routing story, not
        # a longer list to read.
        endpoints = [f'pool/m{i}' for i in range(SERVED_BY_SEEN_LIMIT + 2)]
        for endpoint in endpoints:
            runtime.record_model_call('alpha', 'native-child-cap', route='worker', served_by=endpoint)
        capped = {r['session_id']: r for r in runtime.session_models('alpha')}['native-child-cap']
        assert capped['served_by_seen'] == endpoints[:SERVED_BY_SEEN_LIMIT], (
            'the cap keeps the FIRST endpoints the session named (the order the column '
            'promises) rather than dropping the oldest to make room')
        assert capped['served_by'] == endpoints[-1], (
            'the cap bounds the list, not the last named endpoint')
        assert capped['calls'] == len(endpoints)
    finally:
        await runtime.close()


async def test_a_route_alias_is_stored_as_unknown_never_as_an_endpoint(runtime, tmp_path, monkeypatch):
    """`served_by` equal to the route is an ALIAS, and the store refuses it.

    An endpoint that is merely the route name says nothing about where a turn was
    served from; storing it claims the alias answered. Measured twice: through
    the real handler with a gateway that hands back the model argument it was
    given (`worker`), and against the store directly for both routes - plus the
    control that a real endpoint is NOT nulled, and that an alias turn does not
    overwrite an endpoint the session had already named.
    """
    from fastapi.testclient import TestClient
    alias = [False]
    app = _driver_app(runtime, tmp_path, monkeypatch, alias_flag=alias)
    try:
        await runtime.start('alpha', actor='fixture')
        headers = {'X-AItelier-Driver-Token': _driver_token(runtime, 'alpha')}
        with TestClient(app) as client:
            alias[0] = True
            echoed = client.post('/api/dsh-drivers/alpha/completion', headers=headers, json={
                'session_id': 'native-child-alias',
                'messages': [{'role': 'user', 'content': 'work'}]})
            alias[0] = False
            assert echoed.status_code == 200, echoed.text
            assert echoed.json()['served_by'] == 'worker', (
                'the fixture must actually hand the ALIAS back, or this test measures nothing')

            row = {r['session_id']: r for r in runtime.session_models('alpha')}['native-child-alias']
            assert row['served_by'] != 'worker', (
                'the store kept the route alias as if `worker` had answered')
            assert row['served_by'] is None, (
                'an endpoint that is only the route name is UNKNOWN, not an attribution')
            assert row['served_by_seen'] == [], (
                'and the alias must not enter the list of endpoints the session named')

            served = client.get('/api/dsh-drivers/alpha/model-sessions', headers=headers)
            assert served.status_code == 200, served.text
            read = {s['session_id']: s for s in served.json()['sessions']}['native-child-alias']
            assert read['served_by'] != 'worker'
            assert read['served_by'] is None
            assert read['served_by_seen'] == []

        # The store's own refusal, for both routes, and the control that shows it
        # is a comparison against the route rather than "drop everything".
        runtime.record_model_call('alpha', 'alias-only', route='worker', served_by='worker')
        runtime.record_model_call('alpha', 'driver-alias', route='driver', served_by='driver')
        runtime.record_model_call('alpha', 'real-endpoint', route='worker',
                                  served_by='deepseek/deepseek-flash')
        runtime.record_model_call('alpha', 'named-then-alias', route='worker',
                                  served_by='deepseek/deepseek-flash')
        runtime.record_model_call('alpha', 'named-then-alias', route='worker', served_by='worker')
        rows = {r['session_id']: r for r in runtime.session_models('alpha')}
        assert rows['alias-only']['served_by'] != 'worker'
        assert rows['alias-only']['served_by'] is None
        assert rows['alias-only']['served_by_seen'] == []
        assert rows['driver-alias']['served_by'] != 'driver'
        assert rows['driver-alias']['served_by'] is None
        assert rows['driver-alias']['served_by_seen'] == []
        assert rows['real-endpoint']['served_by'] == 'deepseek/deepseek-flash', (
            'a concrete endpoint is a real attribution, not something to null')
        assert rows['real-endpoint']['served_by_seen'] == ['deepseek/deepseek-flash']
        assert rows['named-then-alias']['served_by'] == 'deepseek/deepseek-flash', (
            'an alias turn is unnamed: it must not overwrite the endpoint the session named')
        assert rows['named-then-alias']['served_by_seen'] == ['deepseek/deepseek-flash']
        assert rows['named-then-alias']['calls'] == 2
    finally:
        await runtime.close()


async def test_an_alias_that_reaches_an_already_migrated_database_is_cleaned_on_the_next_start(
        runtime, tmp_path):
    """The migration's alias cleanup must not be a one-off inside the column-add branch.

    A build older than this one can still write an alias row into a database this
    build already migrated - a rollback to an older image, or a second deployment
    sharing the file - and the measured consequence of cleaning only while the
    column is being added is that `model-sessions` then reports `worker` as the
    endpoint that answered (repro/probe_alias_cleanup_boundary.py, in the
    delivery's evidence). So every start re-establishes the invariant, and this
    test writes the shape an older build writes into a migrated database, opens
    the store again, and requires the alias to be gone.
    """
    db = runtime.db
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO dsh_driver_session_models "
            "(project_id,session_id,route,served_by,served_by_seen,first_seen,last_seen,calls) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ('alpha', 'old-build-alias', 'worker', 'worker', '[]',
             '2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z', 3))
        conn.commit()
    with db.get_connection() as conn:
        raw = {r['session_id']: dict(r) for r in conn.execute(
            "SELECT session_id,served_by FROM dsh_driver_session_models").fetchall()}
    assert raw['old-build-alias']['served_by'] == 'worker', (
        'the fixture must actually write the alias, or this test measures nothing')

    # A second construction is what a restart is: the column already exists, so the
    # migration branch above does not run and only the per-start cleanup can act.
    reopened = DshDrivers(db, runtime.registry, runtime.root, command=runtime.command)
    rows = {r['session_id']: r for r in reopened.session_models('alpha')}
    assert rows['old-build-alias']['served_by'] is None, (
        'an alias row that reached a migrated database is still read back as an endpoint')
    assert rows['old-build-alias']['calls'] == 3, 'the cleanup must not invent or drop calls'


async def test_recorded_session_identity_is_driver_scoped_and_never_anonymous(runtime, tmp_path, monkeypatch):
    """A valid credential that is not THIS project's driver reads nothing."""
    from api import authz
    from fastapi.testclient import TestClient
    app = _driver_app(runtime, tmp_path, monkeypatch)
    try:
        alpha = await runtime.start('alpha', actor='fixture')
        beta = await runtime.start('beta', actor='fixture')
        alpha_token, beta_token, owner_token = (_driver_token(runtime, 'alpha'),
                                                _driver_token(runtime, 'beta'),
                                                'fixture-owner-token')
        payload = lambda session: {'session_id': session,
                                   'messages': [{'role': 'user', 'content': 'work'}]}
        with TestClient(app) as client:
            for project, driver, token in (('alpha', alpha, alpha_token), ('beta', beta, beta_token)):
                wrote = client.post(f'/api/dsh-drivers/{project}/completion', json=payload(driver['session_id']),
                                    headers={'X-AItelier-Driver-Token': token})
                assert wrote.status_code == 200, wrote.text

            anonymous = client.get('/api/dsh-drivers/alpha/model-sessions')
            assert anonymous.status_code == 403, 'the recorded identity is not an anonymous read'
            assert anonymous.headers['X-AItelier-Denial'] == 'read_denied_not_authenticated'
            assert alpha['session_id'] not in anonymous.text

            # A token this app WOULD accept elsewhere must not become a read of
            # this project. Each is measured against the layer that refuses it, in
            # its own iteration: a loop that stops on its first token would leave
            # the other two unmeasured (review finding). Another driver's token and
            # the owner's seeded credential PASS the reader gate - they are refused
            # by this runtime's OWNERSHIP check; an unregistered token never gets
            # that far, the gate answers it with its own code.
            for token, refusal in ((beta_token, 'owned-by-another-driver'),
                                   (owner_token, 'owned-by-another-driver'),
                                   ('not-a-registered-token', authz.READ_DENIED_BAD_ADMIN_TOKEN)):
                refused = client.get('/api/dsh-drivers/alpha/model-sessions',
                                     headers={'X-AItelier-Driver-Token': token})
                assert refused.status_code == 403, f'{token!r} must not read alpha'
                assert alpha['session_id'] not in refused.text
                if refusal == 'owned-by-another-driver':
                    assert 'X-AItelier-Denial' not in refused.headers, (
                        f'{token!r} passed the reader gate and was refused by ownership')
                    assert 'another driver' in refused.json()['detail']
                else:
                    assert refused.headers.get('X-AItelier-Denial') == refusal, (
                        'an unregistered token is refused before any project is read')

            own = client.get('/api/dsh-drivers/alpha/model-sessions',
                             headers={'X-AItelier-Driver-Token': alpha_token})
            assert own.status_code == 200, own.text
            assert [s['session_id'] for s in own.json()['sessions']] == [alpha['session_id']]
            assert beta['session_id'] not in own.text, "another project's session stays out"

            theirs = client.get('/api/dsh-drivers/beta/model-sessions',
                                headers={'X-AItelier-Driver-Token': beta_token})
            assert theirs.status_code == 200, theirs.text
            assert [s['session_id'] for s in theirs.json()['sessions']] == [beta['session_id']]
            assert alpha['session_id'] not in theirs.text
    finally:
        await runtime.close()
