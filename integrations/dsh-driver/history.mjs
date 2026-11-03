/** Read the pinned DSH persistence seam without starting an agent or taking a write lease. */
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

export function historyPage(events, { before_seq = Number.MAX_SAFE_INTEGER, limit = 50 } = {}) {
  const visible = new Set(['user/message', 'assistant/message', 'tool/call', 'tool/result', 'compaction/end', 'agent/error']);
  const selected = events.filter(e => e.seq < before_seq && visible.has(e.type));
  const page = selected.slice(-limit);
  return { entries: page.map(e => {
    const text = JSON.stringify(e.data, null, 2);
    return { seq: e.seq, type: e.type, name: e.data?.name ?? null, timestamp: e.time ?? null, text: text.slice(0, 16000), truncated: text.length > 16000 };
  }), has_more: selected.length > page.length, next_before_seq: page[0]?.seq ?? null };
}

export async function readHistory(root, session, options = {}) {
  const require = createRequire(process.env.AITELIER_DSH_ENTRY ?? '/opt/aitelier-dsh/node_modules/.bin/dsh');
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
