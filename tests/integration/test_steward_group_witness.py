"""Step 23's press witness, backend half (design-steward 7.7.16 end, platform director 418
item 4): `X-AItelier-Read-As: public` must produce an ANONYMOUS answer, measured on the real
FastAPI app with the write gate ON, not argued.

1. The steward's own token WITH the header asks for a private project's overview, and for a
   project that does not exist: both answers are byte for byte what a caller with no
   credential at all gets for the same request (the PUBLIC_READS refusal, 403 "This State DAG
   record is not available." - no existence oracle).
2. The same request for the public project succeeds (200, with content).
3. The very next request of the same token without the header is the writer again (200 for
   the private project): the header is honoured per request and only narrows.
4. A write with the header is refused public_view_read_only; the family memory pair with the
   header is refused unless the recorded turn audience is a bound group.

5. The whole chain in one run (the press witness of 7.7.16 end, director 418 item 4 and 420
   Q3): this real backend served over a loopback socket through a recording layer (each
   request's path, X-AItelier-Read-As, body, status and answer bytes), the real pinned DSH
   0.2.1-rc.5 steward host with dsh-director and the AItelier adapter (runtime.mjs), a
   scripted model that records every request body, and a WeChat-shaped channel plugin
   carrying one family member's @-message of the bound group and the owner's 1:1 messages:
   - the anonymous answer: the group turn's portfolio_project reads of a private project and
     of one that does not exist carry the header and are answered byte for byte what a caller
     with no credential gets; the public project is 200 with content; the owner turn's read of
     the private project, same token, no header, is 200;
   - family memory, both arms of one random marker written with family_memory_note through
     the real backend: no request body of the owner's next turn carries a byte of it (raw or
     JSON-escaped); the compaction in the middle of the group turn (its step after the note,
     threshold crossed) and the idle compaction after it carry neither the marker nor the
     group question, nor does any checkpoint (the scripted checkpoint echoes its input);
   - the snapshot: no request of the group turn after its compaction carries it; the owner's
     next turn's first request does.

The plugin's own tests of the same pieces are integrations/dsh-driver/runtime.group.test.mjs
(real DSH, the plugin with a local host, and the adapter over a recording fake State) and
integrations/dsh-director/test/group-turns.test.mjs."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import authz
from api.dependencies import get_db_manager
from api.main import app
from core import cf_access, drivers
from core.state_service import StateService
from tests.unit.test_steward_channels import OWNER, world  # noqa: F401
from tests.unit.test_steward_groups import MEMBER, OWNER_WX, ROOM, bound_group

HEADER = {"X-AItelier-Read-As": "public"}


@pytest.fixture
def http(world, monkeypatch):
    import api.main as main
    monkeypatch.setattr(cf_access, "_TEAM_DOMAIN", "team.cloudflareaccess.com")
    monkeypatch.setattr(cf_access, "_AUD", "test-aud")
    monkeypatch.setattr(authz, "WRITERS", {OWNER})
    monkeypatch.setattr(authz.cf_access, "email_from_request_headers",
                        lambda headers, cookies=None: headers.get("X-Test-Email"))
    monkeypatch.setattr(authz, "driver_registry", lambda: world.registry)
    monkeypatch.setattr(main, "_ALLOW_EXTERNAL", True)
    monkeypatch.setattr(drivers, "read_pepper", lambda: "steward-channel-pepper-" * 3)
    writer = StateService(world.db, actor="fixture", project_read_trusted=True)
    writer.store.create_project("wuxia-myth", "武虾传奇")
    writer.store.add_nodes("wuxia-myth", [{"key": "a", "goal": "public goal", "acceptance": [
        {"id": "c", "kind": "test", "description": "criterion"}]}])
    writer.open_project("wuxia-myth")
    writer.store.create_project("family-ledger", "家庭账本")
    app.dependency_overrides[get_db_manager] = lambda: world.db
    try:
        with TestClient(app) as client:
            monkeypatch.setattr(app.state, "_test_mode", False, raising=False)
            monkeypatch.setattr(app.state, "dsh_drivers", world.runtime, raising=False)
            assert authz.gate_enabled()
            yield client
    finally:
        app.dependency_overrides.pop(get_db_manager, None)


def _record(name, response):
    print(f"WITNESS {name} status={response.status_code} sha256={hashlib.sha256(response.content).hexdigest()} "
          f"bytes={len(response.content)}")
    return response


@pytest.mark.parametrize("project_id", ["family-ledger", "no-such-project"])
def test_the_header_makes_the_stewards_answer_byte_identical_to_an_unauthenticated_readers(world, http, project_id):
    steward = {"X-AItelier-Driver-Token": world.tokens["steward"], **HEADER}
    body = {"project_id": project_id}
    grouped = _record(f"steward+header {project_id}", http.post("/api/state/query/project_overview", json=body,
                                                                 headers=steward))
    nobody = _record(f"no credential {project_id}", http.post("/api/state/query/project_overview", json=body))
    assert grouped.status_code == nobody.status_code == 403
    assert grouped.content == nobody.content == b'{"detail":"This State DAG record is not available."}'
    # The same over the GET route the dashboard uses.
    get_grouped = http.get(f"/api/state/projects/{project_id}/overview", headers=steward)
    get_nobody = http.get(f"/api/state/projects/{project_id}/overview")
    assert get_grouped.status_code == 403 and get_grouped.content == get_nobody.content


ANONYMOUS_REFUSAL = b'{"detail":"This State DAG record is not available."}'


@pytest.mark.parametrize("mode", ["gate_on", "gate_off", "test_mode"])
def test_the_header_narrows_without_the_gate_and_in_test_mode_too(world, http, monkeypatch, mode):
    """7.7.16: the narrowing never depends on the write gate - with the gate off or in test mode
    (where a caller with no credential reads private projects) the header still yields the
    anonymous refusal, with the steward's token and without any credential."""
    import api.main as main
    if mode == "gate_off":
        monkeypatch.setattr(cf_access, "_TEAM_DOMAIN", "")
        monkeypatch.setattr(cf_access, "_AUD", "")
        assert not authz.gate_enabled()
    elif mode == "test_mode":
        main.app.state._test_mode = True  # the http fixture's monkeypatch undoes it
    body = {"project_id": "family-ledger"}
    steward = {"X-AItelier-Driver-Token": world.tokens["steward"]}
    for headers in ({**steward, **HEADER}, HEADER):
        narrowed = http.post("/api/state/query/project_overview", json=body, headers=headers)
        assert (narrowed.status_code, narrowed.content) == (403, ANONYMOUS_REFUSAL), (mode, headers.keys(), narrowed.text)
    write = http.post("/api/state/commands/send_user_message", headers={**steward, **HEADER},
                      json={"project_id": "aitelier-home", "subject": "s", "body": "b"})
    assert write.status_code == 422 and write.json()["detail"].startswith("public_view_read_only"), (mode, write.text)
    # Without the header the same token is the writer in every mode.
    assert http.post("/api/state/query/project_overview", json=body, headers=steward).status_code == 200


def test_the_public_project_succeeds_and_the_next_request_without_the_header_is_the_writer(world, http):
    token = {"X-AItelier-Driver-Token": world.tokens["steward"]}
    public = _record("steward+header wuxia-myth", http.post("/api/state/query/project_overview",
                                                            json={"project_id": "wuxia-myth"}, headers={**token, **HEADER}))
    assert public.status_code == 200 and public.json()["project"]["project_id"] == "wuxia-myth"
    catalog = http.post("/api/state/query/project_catalog", json={}, headers={**token, **HEADER})
    assert catalog.status_code == 200 and "family-ledger" not in catalog.text and "wuxia-myth" in catalog.text
    private = _record("steward family-ledger", http.post("/api/state/query/project_overview",
                                                         json={"project_id": "family-ledger"}, headers=token))
    assert private.status_code == 200, private.text


def test_with_the_header_writes_are_refused_and_the_family_memory_needs_a_group_turn(world, http):
    token = {"X-AItelier-Driver-Token": world.tokens["steward"]}
    write = http.post("/api/state/commands/send_user_message", headers={**token, **HEADER},
                      json={"project_id": "aitelier-home", "subject": "s", "body": "b"})
    assert write.status_code == 422 and write.json()["detail"].startswith("public_view_read_only"), write.text
    family = http.post("/api/state/commands/note_family_memory", headers={**token, **HEADER},
                       json={"request_key": "k", "text": "x"})
    assert family.status_code == 422 and "family_memory_group_turn_only" in family.text, family.text
    plain = http.post("/api/state/commands/note_family_memory", headers=token, json={"request_key": "k", "text": "x"})
    assert plain.status_code == 422 and "family_memory_group_turn_only" in plain.text, plain.text
    questions = http.post("/api/state/query/list_user_questions", headers={**token, **HEADER},
                          json={"project_id": "aitelier-home"})
    assert questions.status_code == 403, questions.text


# --- 5. the whole chain in one run --------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[2]
DSH_ENTRY = Path("/opt/aitelier-dsh/node_modules/@deepseek-ai/dsh/lib/bin.js")

# The scripted model: what it calls is decided by the run's phase (set by the driver plugin
# below), never by guessing at text; every request body is appended to W23_RECORD.
PROVIDER = r"""
import { createRequire } from 'node:module';
import { appendFileSync } from 'node:fs';
const require = createRequire(process.env.AITELIER_DSH_ENTRY);
const { LlmAdapter } = await import(require.resolve('@deepseek-ai/dsh-llm'));
export const name = 'w23-provider';
export const inject = ['llm'];
const tool = (id, name, args) => ({ type: 'tool-call', id, name, arguments: JSON.stringify(args) });
export function apply(ctx) {
  const done = new Set();
  const once = (id, name, args) => done.has(id) ? undefined : (done.add(id), tool(id, name, args));
  class FixtureAdapter extends LlmAdapter {
    async resolveModel(provider, model) { return { provider, id: model, name: model, context: { contextWindow: 64000 }, defaultMaxTokens: 1024 }; }
    async *stream(options) {
      const phase = globalThis.__w23Phase ?? 0, body = JSON.stringify(options.messages);
      const last = options.messages.at(-1), after = last?.role === 'tool' ? last.toolCallId : undefined;
      let block;
      if (options.purpose === 'compaction') block = { type: 'text', text: 'Checkpoint: ' + [...new Set(options.messages.filter(m => m.role !== 'system')
        .map(m => JSON.stringify(m)).join(' ').match(/FAMILY-[0-9a-f-]{36}|[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+/g) ?? [])].join(' ') };
      else if (!options.tools?.length || options.sessionId !== process.env.AITELIER_DSH_SESSION) block = { type: 'text', text: 'Fixture.' };
      else if (phase === 1) block = after === 'g1' ? once('g2', 'portfolio_project', { project_id: 'no-such-project' })
        : after === 'g2' ? once('g3', 'portfolio_project', { project_id: 'wuxia-myth' })
        : after === 'g3' ? once('g4', 'family_memory_note', { text: process.env.W23_MARKER })
        : after === 'g4' ? { type: 'text', text: 'GROUP_W23_ANSWER' }
        : once('g1', 'portfolio_project', { project_id: 'family-ledger' });
      else if (phase === 3) block = after === 'o1' ? { type: 'text', text: 'OWNER_W23_ANSWER' } : once('o1', 'portfolio_project', { project_id: 'family-ledger' });
      block ??= { type: 'text', text: 'OWNER_W23_HELLO_ANSWER' };
      appendFileSync(process.env.W23_RECORD, JSON.stringify({ session: options.sessionId, purpose: options.purpose ?? null, phase, body,
        ...(options.purpose === 'compaction' ? { checkpoint: block.text } : {}) }) + '\n');
      yield { type: 'block-start', index: 0, blockType: block.type };
      if (block.type === 'text') yield { type: 'text-delta', index: 0, text: block.text };
      else yield { type: 'tool-call-delta', index: 0, id: block.id, name: block.name, argumentsDelta: block.arguments };
      yield { type: 'block-end', index: 0, block };
      // The family note's step reports a prompt at the threshold (64000 x 0.5): the group turn's
      // next step starts with a pressure compaction.
      yield { type: 'usage', usage: { inputTokens: block.id === 'g4' ? 32000 : 100, outputTokens: 20 } };
      yield { type: 'finish', reason: { kind: block.type === 'text' ? 'stop' : 'tool-calls' } };
    }
  }
  ctx.llm.registerAdapter(['fixture'], new FixtureAdapter());
}
"""

# A WeChat-shaped channel plugin that also drives the run: the owner's 1:1 hello (phase 0);
# after that turn, a family member's @-message in the bound group (1); after the group turn an
# idle compaction (2) and the owner's next 1:1 message (3); after that turn, done (4).
CHANNEL = r"""
import { appendFileSync } from 'node:fs';
export const name = 'w23-channel';
export const inject = ['ownerChannels', 'compaction'];
const note = (kind, value) => appendFileSync(process.env.W23_CHANNEL, JSON.stringify({ kind, ...value }) + '\n');
export function apply(ctx) {
  globalThis.__w23Phase = 0;
  ctx.on('dispose', ctx.ownerChannels.register({ name: 'wechat', status: () => ({ connected: true }),
    async send(text, options) { note('send', { text, options }); return { ref: 'out-' + Date.now() }; },
    async sendGroup(room, text) { note('group', { room, text }); return { ref: 'gout-' + Date.now() }; },
    async setRooms(rooms) { note('rooms', rooms); } }));
  const inbound = async message => note('inbound', { ref: message.messageRef,
    answer: await ctx.ownerChannels.inbound({ channel: 'wechat', at: new Date().toISOString(), ...message }) });
  void inbound({ senderRef: process.env.W23_OWNER, messageRef: 'o-1', text: 'OWNER_W23_HELLO' });
  let ended = false, busy = false;
  ctx.on('session/event', (session, event) => { if (session.id === process.env.AITELIER_DSH_SESSION && event.type === 'turn/end') ended = true; });
  ctx.on('agent/status', ({ agent, status }) => {
    if (agent.session.id !== process.env.AITELIER_DSH_SESSION || status !== 'idle' || !ended || busy) return;
    ended = false; busy = true;
    setTimeout(async () => {
      try {
        const phase = globalThis.__w23Phase;
        if (phase === 0) { globalThis.__w23Phase = 1;
          await inbound({ senderRef: process.env.W23_MEMBER, messageRef: 'g-1', text: 'GROUP_W23_QUESTION 武侠那个项目怎样', conversationRef: process.env.W23_ROOM }); }
        else if (phase === 1) { globalThis.__w23Phase = 2;
          note('compacted', { compacted: Boolean(await ctx.compaction.compactNow(agent, AbortSignal.timeout(60000))) });
          globalThis.__w23Phase = 3;
          await inbound({ senderRef: process.env.W23_OWNER, messageRef: 'o-2', text: 'OWNER_W23_SECOND' }); }
        else if (phase === 3) { globalThis.__w23Phase = 4; note('done', {}); }
      } catch (error) { note('error', { error: String(error?.stack ?? error) }); }
      finally { busy = false; }
    }, 300);
  });
}
"""


class Recorder:
    """The recording layer between the adapter and the backend: path, X-AItelier-Read-As,
    request body, status and the answer's bytes of every HTTP request. It never changes one."""

    def __init__(self, app):
        self.app, self.calls = app, []

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        body, answer = bytearray(), {"status": None, "bytes": bytearray()}

        async def recv():
            message = await receive()
            if message["type"] == "http.request":
                body.extend(message.get("body", b""))
            return message

        async def snd(message):
            if message["type"] == "http.response.start":
                answer["status"] = message["status"]
            elif message["type"] == "http.response.body":
                answer["bytes"].extend(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, recv, snd)
        finally:
            self.calls.append({"path": scope["path"], "read_as": headers.get("x-aitelier-read-as"),
                               "token": "x-aitelier-driver-token" in headers, "body": bytes(body),
                               "status": answer["status"], "answer": bytes(answer["bytes"])})


@pytest.fixture
def served(http):
    import uvicorn
    recorder = Recorder(app)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(recorder, log_level="error", lifespan="off", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started, "the loopback server never started"
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}", recorder
    finally:
        server.should_exit = True
        thread.join(10)


def _lines(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()] \
        if Path(path).exists() else []


def test_the_press_witness_one_run_real_backend_real_dsh_adapter_and_recording(world, http, served, monkeypatch):
    node = shutil.which("node")
    assert node and DSH_ENTRY.exists(), "the witness needs the shipped image (node and the pinned DSH)"
    url, recorder = served
    bound_group(world)
    # The steward's driver is "running" for the backend: this test starts its process itself
    # (process supervision is not what is measured here).
    world.runtime.update("aitelier-home", enabled=1, phase="running")
    monkeypatch.setattr(world.runtime, "running", lambda project_id: project_id == "aitelier-home")
    marker = "FAMILY-" + str(uuid.uuid4())
    escaped = json.dumps(marker)[1:-1]
    driver_id = world.steward_row["driver_id"]
    directory = Path(tempfile.mkdtemp(prefix="w23-witness-", dir=Path.home()))
    proc = None
    try:
        profile = directory / "home/profiles/driver"
        profile.mkdir(parents=True)
        (directory / "provider.mjs").write_text(PROVIDER, encoding="utf-8")
        (directory / "channel.mjs").write_text(CHANNEL, encoding="utf-8")
        (profile / "package.json").write_text(json.dumps(
            {"private": True, "dsh": {"profile": {"bundles": ["@deepseek-ai/dsh-base"]}}}), encoding="utf-8")
        (profile / "cordis.patch.yml").write_text(json.dumps([
            {"id": "hmr", "disabled": True}, {"id": "tool-result-pruner", "disabled": True},
            # The steward's policy shape, retaining nothing; a headroom that fits the 64000 window.
            {"id": "compaction-basic", "config": {"thresholdRatio": 0.5, "retainTokens": 0, "headroomTokens": 1024}},
            *[{"id": plugin, "disabled": True} for plugin in ("tool-subagent-fork", "tool-workflow", "plan-mode", "tool-todo")],
            {"insert": [{"id": "fixture", "name": str(directory / "provider.mjs")},
                        {"id": "dsh-director", "name": str(ROOT / "integrations/dsh-director/index.mjs")},
                        {"id": "aitelier-driver", "name": str(ROOT / "integrations/dsh-driver/runtime.mjs")},
                        {"id": "dsh-channel-wechat", "name": str(directory / "channel.mjs")}]}]), encoding="utf-8")
        record, channel = directory / "model.jsonl", directory / "channel.jsonl"
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("AITELIER_") and k not in ("DSH_PERMISSION_MODE", "DSH_HOME")}
        env.update({
            "DSH_HOME": str(directory / "home"), "AITELIER_DSH_ENTRY": str(DSH_ENTRY),
            "AITELIER_DRIVER_TOKEN_FILE": str(world.runtime.root / driver_id / "token"), "AITELIER_DRIVER_ID": driver_id,
            "AITELIER_DSH_PROJECT": "aitelier-home", "AITELIER_DSH_SESSION": "w23-root", "AITELIER_DSH_PROVIDER": "fixture",
            "AITELIER_DSH_MODEL": "fixture-model", "AITELIER_URL": url, "AITELIER_DSH_STATE_DIR": str(directory),
            "AITELIER_SECRETS_DIR": str(directory / "no-secrets"), "AITELIER_DSH_ROLE": "steward",
            "AITELIER_DSH_MEMORY_USER": "owner", "AITELIER_DSH_MEMORY_ROOT": str(directory / "memory"),
            "W23_RECORD": str(record), "W23_CHANNEL": str(channel), "W23_MARKER": marker,
            "W23_OWNER": OWNER_WX, "W23_MEMBER": MEMBER, "W23_ROOM": ROOM})
        log = (directory / "dsh.log").open("wb")
        proc = subprocess.Popen([node, str(DSH_ENTRY), "--profile", "driver"], cwd=directory, env=env,
                                stdout=log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 150
        notes = []
        while time.monotonic() < deadline and proc.poll() is None:
            notes = _lines(channel)
            if any(n["kind"] in ("done", "error") for n in notes):
                break
            time.sleep(0.2)
        notes = _lines(channel)
        assert any(n["kind"] == "done" for n in notes), json.dumps(notes, ensure_ascii=False)[:3000] + "\n" + \
            json.dumps([(c["path"], c["status"], c["answer"][:200].decode("utf-8", "replace")) for c in recorder.calls
                        if c["status"] and c["status"] >= 400])[:3000] + "\n" + \
            (directory / "dsh.log").read_text(encoding="utf-8", errors="replace")[-4000:]
        requests = [r for r in _lines(record) if r["session"] == "w23-root"]
        turn = lambda phase: [r for r in requests if r["phase"] == phase and r["purpose"] is None]  # noqa: E731

        # 1. The anonymous answer, measured at the recording layer.
        reads = [c for c in recorder.calls if c["path"] == "/api/state/query/project_overview" and c["token"]]
        by = {(json.loads(c["body"])["project_id"], c["read_as"]): c for c in reads}
        for project_id in ("family-ledger", "no-such-project"):
            grouped = by[(project_id, "public")]
            nobody = http.post("/api/state/query/project_overview", content=grouped["body"],
                               headers={"Content-Type": "application/json"})
            print(f"WITNESS e2e {project_id} status={grouped['status']} sha256={hashlib.sha256(grouped['answer']).hexdigest()} "
                  f"nobody sha256={hashlib.sha256(nobody.content).hexdigest()}")
            assert (grouped["status"], grouped["answer"]) == (nobody.status_code, nobody.content) == (403, ANONYMOUS_REFUSAL)
        public = by[("wuxia-myth", "public")]
        assert public["status"] == 200 and json.loads(public["answer"])["project"]["project_id"] == "wuxia-myth"
        owner_read = by[("family-ledger", None)]
        assert owner_read["status"] == 200, owner_read["answer"][:300]
        assert [c["read_as"] for c in reads if json.loads(c["body"])["project_id"] == "family-ledger"] == ["public", None]
        # The host's own requests in the group turn never carried the header.
        for path in ("relay_group_message", "set_steward_turn_audience", "post_group_reply"):
            hits = [c for c in recorder.calls if c["path"] == "/api/state/commands/" + path]
            assert hits and all(c["read_as"] is None and c["status"] == 200 for c in hits), (path, hits[:1])
        family = [c for c in recorder.calls if c["path"] == "/api/state/commands/note_family_memory"]
        assert [(c["read_as"], c["status"]) for c in family] == [("public", 200)], family
        with world.db.get_connection() as conn:
            assert [tuple(r) for r in conn.execute("SELECT text FROM state_family_memory")] == [(marker,)]
        assert [n for n in notes if n["kind"] == "group"] and all(n["room"] == ROOM for n in notes if n["kind"] == "group")

        # 2. Family memory, arm one: the owner's next turn never sees a byte of the marker.
        assert turn(1) and any("GROUP_W23_QUESTION" in r["body"] for r in turn(1))
        assert turn(3), "the owner's next turn made no request"
        for r in turn(3):
            assert marker not in r["body"] and escaped not in r["body"], "the owner's next turn carries the family marker"
            assert "GROUP_W23_QUESTION" not in r["body"] and "GROUP_W23_ANSWER" not in r["body"]
        # Arm two: the compaction inside the group turn and the idle one after it.
        mid = [r for r in requests if r["purpose"] == "compaction" and r["phase"] == 1]
        idle = [r for r in requests if r["purpose"] == "compaction" and r["phase"] == 2]
        assert mid, "no compaction ran inside the group turn"
        assert idle and {"kind": "compacted", "compacted": True} in notes
        for r in mid + idle:
            assert marker not in r["body"] and escaped not in r["body"], "a compaction request carries the family marker"
            assert "GROUP_W23_QUESTION" not in r["body"], "a compaction request carries the group question"
        assert "A family-group turn ran here" in mid[0]["body"]
        assert "OWNER_W23_HELLO" in mid[0]["checkpoint"], "the scripted checkpoint echoes what it is given"
        # The checkpoints those compactions wrote carry neither (the scripted one echoes its input).
        for r in mid + idle:
            assert r["checkpoint"].startswith("Checkpoint: ")
            assert marker not in r["checkpoint"] and "GROUP_W23_QUESTION" not in r["checkpoint"], r["checkpoint"]

        # 3. The snapshot: none in the rest of the group turn after its compaction; in the owner's next turn.
        snapshot = '"kind":"aitelier-driver-state"'  # the AItelier host's snapshot (aitelier-texts.mjs)
        rest = [r for r in requests[requests.index(mid[0]) + 1:] if r["phase"] == 1 and r["purpose"] is None]
        assert rest, "the group turn made no request after its compaction"
        assert all(snapshot not in r["body"] for r in rest), "the group turn was given the snapshot after its compaction"
        assert snapshot in turn(3)[0]["body"], "the owner's next turn was not given the snapshot"
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(15)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(directory, ignore_errors=True)
