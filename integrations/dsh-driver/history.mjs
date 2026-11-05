/** Read the pinned DSH persistence seam without starting an agent or taking a write lease. */
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { resolveDshEntry } from './dsh-entry.mjs';

export function historyPage(events, { before_seq = Number.MAX_SAFE_INTEGER, limit = 50 } = {}) {
  const visible = new Set(['user/message', 'assistant/message', 'tool/call', 'tool/result', 'compaction/end', 'agent/error']);
  const selected = events.filter(e => e.seq < before_seq && visible.has(e.type));
  const page = selected.slice(-limit);
  return { entries: page.map(e => {
    const text = JSON.stringify(e.data, null, 2);
    return { seq: e.seq, type: e.type, name: e.data?.name ?? null, timestamp: e.time ?? null, text: text.slice(0, 16000), truncated: text.length > 16000 };
  }), has_more: selected.length > page.length, next_before_seq: page[0]?.seq ?? null };
}

/**
 * Which DSH owns the persistence package this reader imports.
 *
 * The backend that serves history spawns this file with AITELIER_DSH_ENTRY set to
 * the driver binary it launched, so that stays the first answer. Everything else
 * — a test, a checkout, a host install — asks the same resolver the test suite
 * asks, instead of trusting a hard-coded /opt path that only exists in one image.
 */
async function entryForReader(explicit) {
  if (explicit) return explicit;
  if (process.env.AITELIER_DSH_ENTRY) return process.env.AITELIER_DSH_ENTRY;
  const resolution = await resolveDshEntry();
  if (resolution.status !== 'measured')
    throw new Error(`cannot read DSH history: ${resolution.reason}`);
  return resolution.entry;
}

export async function readHistory(root, session, { entry, ...options } = {}) {
  const require = createRequire(await entryForReader(entry));
  const { Context } = await import(require.resolve('@deepseek-ai/cordis'));
  const storage = await import(require.resolve('@deepseek-ai/dsh-session-persistence-jsonl'));
  const ctx = new Context();
  const fork = ctx.plugin(storage, { root });
  let handle;
  try {
    await fork.await();
    handle = await ctx.sessionPersistence.open(session, 'read');
    const result = await handle.read();
    return { session_id: session, ...historyPage(result.events, options) };
  } finally {
    await handle?.close();
    await fork.dispose();
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    const [root, session, before, limit] = process.argv.slice(2);
    const result = await readHistory(root, session, { before_seq: Number(before), limit: Number(limit) });
    process.stdout.write(JSON.stringify(result));
  } catch (error) { console.error(String(error)); process.exitCode = 1; }
}
