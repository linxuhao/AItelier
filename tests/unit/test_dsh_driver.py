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


# ── Auto-compact threshold ──────────────────────────────────────────────────
# DSH fires automatic compaction at min(W * thresholdRatio, W - O - B), shipped
# ratio 0.8. This driver does not need a large window: the State snapshot is
# re-injected once after every successful compaction, so compacting earlier costs
# a summary, not context. The knob is a RATIO of the model's own window, and it is
# clamped so a typo cannot silently disable compaction or make it constant.

def test_compaction_ratio_is_overridable_and_clamped(runtime, monkeypatch):
    row = runtime._provision("alpha", "fixture-provider", "fixture-model", "fixture")
    seen = {}
    for raw, expected in (("0.5", 0.5), ("0.384", 0.384), ("0.99", 0.8), ("0.01", 0.2), ("garbage", 0.5)):
        monkeypatch.setenv("AITELIER_DSH_COMPACT_RATIO", raw)
        runtime._environment(row)
        patch = _patch_document(runtime, row)
        entry = next(p for p in patch if p.get("id") == "compaction-basic")
        seen[raw] = entry["config"]["thresholdRatio"]
        assert seen[raw] == expected, f"{raw!r} should land on {expected}"


def test_the_shipped_ratio_is_lower_than_the_default(runtime, monkeypatch):
    monkeypatch.delenv("AITELIER_DSH_COMPACT_RATIO", raising=False)
    row = runtime._provision("alpha", "fixture-provider", "fixture-model", "fixture")
    runtime._environment(row)
    patch = _patch_document(runtime, row)
    entry = next(p for p in patch if p.get("id") == "compaction-basic")
    assert entry["config"]["thresholdRatio"] == 0.5, (
        "the driver compacts at half the window: its work survives compaction by "
        "design, so it trades window for headroom")
