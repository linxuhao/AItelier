/** The remote ranked index (design 3.2): any memory system that speaks the Agent Memory
 *  Leaderboard protocol - POST /add {request_id, user_id, session_id, messages[{role,
 *  content, timestamp}]} and POST /search {query, user_id, top_k} -> {data: [{id, content,
 *  score, created_at?}]}. Off by default. Every call may cost money on the remote side.
 *
 *  batchOf (AMI only, option amiIds): ActiveMemoryIndex derives its ids from ours -
 *  sha1(user_id + "\0" + request_id)[:16] + "-<kind><n>" (AMI app/store.py) - so a remote
 *  hit can be traced to the turn that wrote it, and a forgotten turn filtered out even
 *  though the remote rewrote its text. That rule is AMI's implementation, not the
 *  protocol: the first search that returns hits checks it, and a hit that does not
 *  follow it withdraws batchOf for good (the store then reports remote_forget:
 *  "unsupported"). */
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';

const AMI_ID = /^([0-9a-f]{16})-/;

export function amiPrefix(userId, requestId) {
  return createHash('sha1').update(userId + '\0' + requestId).digest('hex').slice(0, 16);
}

/** @param o url, name (e.g. "ActiveMemoryIndex"), userId, sessionId, tokenFile?, amiIds?,
 *  timeoutMs (30000), fetch (injectable) */
export function leaderboardRanked({ url, name, userId, sessionId = 'steward', tokenFile, amiIds = false,
  timeoutMs = 30000, fetch = globalThis.fetch, onCheck = () => {} }) {
  if (!url || !userId) throw new Error('memory: the remote index needs url and userId');
  const base = url.replace(/\/+$/, '');
  const prefixes = new Map(); // AMI prefix -> our batch (turn) id
  let checked = false;

  async function post(path, body, signal) {
    const headers = { 'content-type': 'application/json' };
    if (tokenFile) headers.authorization = 'Bearer ' + readFileSync(tokenFile, 'utf8').trim();
    const response = await fetch(base + path, { method: 'POST', headers, body: JSON.stringify(body),
      signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(timeoutMs)]) : AbortSignal.timeout(timeoutMs) });
    const text = await response.text();
    if (!response.ok) throw new Error(`${name} ${path} answered ${response.status}: ${text.slice(0, 300)}`);
    try { return JSON.parse(text); } catch { throw new Error(`${name} ${path} answered non-JSON`); }
  }

  const index = {
    name: 'leaderboard · ' + name,
    /** The store tells the adapter every batch id it knows (log and forgotten records). */
    learnBatches(ids) { if (amiIds) for (const id of ids) prefixes.set(amiPrefix(userId, id), id); },
    async add(batch, signal) {
      if (amiIds) prefixes.set(amiPrefix(userId, batch.id), batch.id);
      await post('/add', { request_id: batch.id, user_id: userId, session_id: sessionId,
        messages: batch.entries.map(e => ({ role: e.role === 'owner' ? 'user' : 'assistant', content: e.text, timestamp: e.at })) }, signal);
    },
    async search(query, topK, signal) {
      const answer = await post('/search', { query, user_id: userId, top_k: topK }, signal);
      if (!Array.isArray(answer?.data)) throw new Error(`${name} /search answered without a data list`);
      const hits = answer.data.map(d => ({ text: String(d.content ?? ''), score: Number(d.score ?? 0), at: d.created_at, ref: String(d.id ?? '') }));
      if (index.batchOf && !checked && hits.length) {
        checked = true;
        const off = hits.find(h => { const m = AMI_ID.exec(h.ref); return !m || !prefixes.has(m[1]); });
        if (off) { delete index.batchOf; onCheck({ ok: false, ref: off.ref }); }
        else onCheck({ ok: true });
      }
      return hits;
    },
  };
  if (amiIds) index.batchOf = ref => { const m = AMI_ID.exec(ref); return m ? prefixes.get(m[1]) : undefined; };
  return index;
}
