/** Plugin step 11 (design-steward 3.2/3.5/3.7): a remote ranked index over the Agent Memory
 *  Leaderboard protocol, against a fixture server. The response shapes follow the
 *  protocol (AMI app/main.py); a real AMI exchange is a separate, owner-approved check. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtempSync, readFileSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { MemoryStore } from '../lib/memory/store.mjs';
import { amiPrefix, remoteRanked } from '../lib/memory/remote.mjs';

const USER = 'owner@example.com';
const newRoot = () => mkdtempSync(join(tmpdir(), 'dsh-remote-'));

async function fixture() {
  const state = { adds: [], searches: [], failAdd: false, failSearch: false, data: () => [] };
  const server = createServer((req, res) => {
    let body = '';
    req.on('data', d => { body += d; });
    req.on('end', () => {
      const json = JSON.parse(body);
      if (req.url === '/add') {
        if (state.failAdd) { res.writeHead(503); return res.end('add down'); }
        state.adds.push(json); res.writeHead(200); return res.end('{"ok":true}');
      }
      if (req.url === '/search') {
        state.searches.push(json);
        if (state.failSearch) { res.writeHead(500); return res.end('search exploded'); }
        res.writeHead(200); return res.end(JSON.stringify({ data: state.data(json) }));
      }
      res.writeHead(404); res.end();
    });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  state.url = `http://127.0.0.1:${server.address().port}`;
  state.close = () => new Promise(resolve => { server.close(resolve); server.closeAllConnections(); });
  return state;
}

const openRemote = (root, srv, extra = {}) => MemoryStore.open({ root, userId: USER,
  ranked: remoteRanked({ url: srv.url, name: 'Fixture', userId: 'u-1', sessionId: 's-1', ...extra }) });
const settle = () => new Promise(resolve => setTimeout(resolve, 50));

test('protocol mapping: one /add per turn, /search mapped, the source names the remote', async (t) => {
  const srv = await fixture(); const root = newRoot();
  t.after(() => srv.close());
  const store = await openRemote(root, srv);
  await store.recordTurn('turn-9', [{ role: 'owner', text: '想做记账工具' }, { role: 'steward', text: '记下了' }]);
  await settle();
  assert.equal(srv.adds.length, 1);
  assert.deepEqual({ ...srv.adds[0], messages: srv.adds[0].messages.map(m => [m.role, m.content]) },
    { request_id: 'turn-9', user_id: 'u-1', session_id: 's-1', messages: [['user', '想做记账工具'], ['assistant', '记下了']] });
  srv.data = () => [{ id: 'r1', content: '想做记账工具', score: 0.9, created_at: '2026-11-16T22:31:00Z' }];
  const r = await store.search('记账', 5);
  assert.deepEqual(srv.searches.at(-1), { query: '记账', user_id: 'u-1', top_k: 5 });
  assert.equal(r.ranked.source, 'remote · Fixture');
  assert.deepEqual(r.ranked.hits.map(h => [h.ref, h.score, h.at]), [['r1', 0.9, '2026-11-16T22:31:00Z']]);
  assert.equal(r.grep.hits[0].shown, 'full', 'grep is placed first');
  assert.equal(r.ranked.hits[0].shown, 'see_grep', 'matched to the local entry by its text, which grep shows whole');
  assert.equal(r.ranked.hits[0].role, 'owner');
  assert.match(r.ranked.text, /^\[[^\]]+\] 业主 \{t-turn-9-0\}: → 见 grep$/);
  assert.equal(r.ranked.remote_forget, 'unsupported', 'no remove, no batchOf: said on every search');
  assert.equal(r.ranked.hits[0].id, 't-turn-9-0', 'the matched local id, which memory_read takes');
  assert.equal((await store.read([r.ranked.hits[0].id])).entries[0].text, '想做记账工具');
  srv.data = () => [{ id: 'r2', content: 'User wants a ledger', score: 0.8 }];
  const loose = await store.search('ledger', 5);
  assert.equal(loose.ranked.hits[0].id, undefined, 'an extracted fact with no local twin has no id to read');
  assert.deepEqual([loose.ranked.hits[0].ref, loose.ranked.hits[0].role, loose.ranked.hits[0].shown], ['r2', 'remote', 'full']);
  assert.equal(loose.ranked.text, '[时间不明] 远端: User wants a ledger', 'the remote\'s own line, no id; it gave no time');
  // A hit close in meaning whose text is a local entry WITHOUT the query's words: grep cannot
  // find it, and it still gets the local id (design 3.5: text equal to a local entry).
  await store.recordTurn('t9', [{ role: 'owner', text: '先说别的' }, { role: 'owner', text: '预算表要能导出' }]);
  srv.data = () => [{ id: 'r9', content: '预算表要能导出', score: 0.7 }];
  const meaning = await store.search('记账', 5);
  assert.deepEqual(meaning.grep.hits.map(h => h.id), ['t-turn-9-0'], 'grep finds only the literal match');
  assert.equal(meaning.ranked.hits[0].id, 't-t9-1');
  assert.equal(meaning.ranked.hits[0].shown, 'full', 'not in grep, so ranked has it whole');
  assert.match(meaning.ranked.text, /\{t-t9-1\}: 预算表要能导出$/);
  assert.equal((await store.read([meaning.ranked.hits[0].id])).entries[0].text, '预算表要能导出');
  // Identical texts: the newest entry by time, whatever order the entries were loaded in.
  await store.recordTurn('t5', [{ role: 'owner', text: '先说别的', at: '2026-11-17T05:00:00Z' }, { role: 'owner', text: '同意', at: '2026-11-17T05:00:01Z' }]);
  await store.recordTurn('t4', [{ role: 'owner', text: '同意', at: '2026-11-17T04:00:00Z' }]);
  srv.data = () => [{ id: 'r5', content: '同意', score: 0.6 }];
  assert.equal((await store.search('记账', 5)).ranked.hits[0].id, 't-t5-1', 'the newest of two identical texts, though written last is the older');
  srv.data = () => [{ id: 'r9', content: '预算表要能导出', score: 0.7 }];
  // A forgotten local twin gives no id: its text is not readable.
  await store.forget(['t-t9-1'], { reason: 'asked', by: 'owner' });
  assert.equal((await store.search('记账', 5)).ranked.hits[0].id, undefined);
  await store.close();
});

test('an unreachable remote: ranked carries the error verbatim, grep is complete, no BM25 stands in', async () => {
  const srv = await fixture(); const root = newRoot();
  const store = await openRemote(root, srv);
  await store.recordTurn('t1', [{ role: 'owner', text: 'ledger idea' }]);
  srv.failSearch = true;
  const r = await store.search('ledger');
  assert.match(r.ranked.error, /search exploded/);
  assert.equal(r.ranked.hits, undefined);
  assert.deepEqual(Object.keys(r.ranked).sort(), ['error', 'source'], 'no hits, text or omitted: there is no ranked answer');
  assert.equal(r.grep.hits[0].shown, 'full');
  assert.match(r.budget, /^\d+\/6000 字 · omitted 0$/);
  assert.equal(r.ranked.source, 'remote · Fixture');
  assert.deepEqual([r.grep.complete, r.grep.hits.length], [true, 1]);
  await store.close(); await srv.close();
});

test('a failed add waits in ranked-pending and is replayed once the remote is back, never twice', async () => {
  const srv = await fixture(); const root = newRoot();
  const store = await openRemote(root, srv);
  srv.failAdd = true;
  await store.recordTurn('t1', [{ role: 'owner', text: 'kept locally' }]);
  await settle();
  assert.ok(JSON.parse(readFileSync(join(root, USER + '.ranked-pending.json'), 'utf8')).t1);
  assert.equal((await store.search('kept')).grep.hits.length, 1, 'the entry is local already');
  srv.failAdd = false;
  await store.search('kept');
  await store.search('kept');
  assert.deepEqual(srv.adds.map(a => a.request_id), ['t1']);
  assert.deepEqual(JSON.parse(readFileSync(join(root, USER + '.ranked-pending.json'), 'utf8')), {});
  await store.close(); await srv.close();
});

test('enabling the remote does not backfill the existing log', async () => {
  const srv = await fixture(); const root = newRoot();
  const local = await MemoryStore.open({ root, userId: USER });
  await local.recordTurn('old', [{ role: 'owner', text: 'before the remote' }]);
  await local.flush(); await local.close();
  const store = await openRemote(root, srv);
  await settle();
  assert.deepEqual(srv.adds, []);
  assert.ok(!existsSync(join(root, USER + '.ranked-pending.json')));
  await store.close(); await srv.close();
});

test('AMI ids: a forgotten turn is filtered by turn even though the remote rewrote its text, and still after a purge', async () => {
  const srv = await fixture(); const root = newRoot();
  const checks = [];
  const store = await openRemote(root, srv, { amiIds: true, onCheck: c => checks.push(c) });
  await store.recordTurn('bank', [{ role: 'owner', text: '我的银行卡号是 6222 0000' }]);
  await store.recordTurn('idea', [{ role: 'owner', text: '想做记账工具' }]);
  await settle();
  // The remote answers with extracted, rewritten facts - not our text.
  srv.data = () => [
    { id: amiPrefix('u-1', 'bank') + '-f0', content: 'User has a bank card ending 0000', score: 0.8 },
    { id: amiPrefix('u-1', 'idea') + '-f0', content: 'User wants an accounting tool', score: 0.7 },
  ];
  const before = await store.search('bank card');
  assert.equal(before.ranked.filtered_forgotten, 0);
  assert.equal(before.ranked.hits.length, 2);
  assert.deepEqual(checks, [{ ok: true }], 'the id rule held on the first real answer');
  await store.forget(['t-bank-0'], { reason: 'owner asked', by: 'owner' });
  const after = await store.search('bank card');
  assert.deepEqual(after.ranked.hits.map(h => [h.ref, h.shown]), [[amiPrefix('u-1', 'idea') + '-f0', 'full']]);
  assert.equal(after.ranked.text, '[时间不明] 远端: User wants an accounting tool');
  assert.ok(!JSON.stringify(after).includes('bank card ending'), 'the forgotten turn\'s fact is nowhere in the answer');
  assert.equal(after.ranked.filtered_forgotten, 1);
  assert.equal(after.ranked.remote_forget, undefined, 'filtering is supported');
  assert.equal((await store.purge(['t-bank-0'])).purged, true);
  assert.equal((await store.search('bank card')).ranked.filtered_forgotten, 1, 'the reduced forgotten record keeps the turn');
  await store.close();
  const reopened = await openRemote(root, srv, { amiIds: true });
  assert.equal((await reopened.search('bank card')).ranked.filtered_forgotten, 1, 'and after a restart');
  await reopened.close(); await srv.close();
});

test('AMI ids that do not follow the rule withdraw batchOf: forgetting is then reported unsupported', async () => {
  const srv = await fixture(); const root = newRoot();
  const checks = [];
  const store = await openRemote(root, srv, { amiIds: true, onCheck: c => checks.push(c) });
  await store.recordTurn('t1', [{ role: 'owner', text: 'x' }]);
  srv.data = () => [{ id: 'mem_123', content: 'x', score: 1 }];
  const r = await store.search('x');
  assert.deepEqual(checks, [{ ok: false, ref: 'mem_123' }]);
  assert.equal(r.ranked.remote_forget, 'unsupported');
  assert.equal(r.ranked.hits.length, 1, 'results still come back');
  await store.close(); await srv.close();
});

test('f4: a write spooled during a purge reaches the remote index once the spool is replayed', async (t) => {
  const srv = await fixture(); const root = newRoot();
  t.after(() => srv.close());
  const store = await openRemote(root, srv);
  await store.recordTurn('keep', [{ role: 'owner', text: 'keep' }]);
  await store.recordTurn('gone', [{ role: 'owner', text: 'gone' }]);
  await store.flush();
  store.faults = { afterVerify: async () => { assert.equal((await store.recordTurn('during', [{ role: 'owner', text: 'said during the purge' }])).spooled, true); } };
  assert.equal((await store.purge(['t-gone-0'])).purged, true);
  await settle();
  assert.deepEqual(srv.adds.map(a => a.request_id), ['keep', 'gone', 'during']);
  await store.close();
});
