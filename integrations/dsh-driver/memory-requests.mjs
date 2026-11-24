/** The owner's memory operations from the AItelier dashboard (plugin step 14, design 3.4):
 *  the memory lives on the host, outside every container, so the backend records each
 *  owner request as a row and this code - not the model - runs it against the memory store
 *  under the store's own write lock, then writes the result back. The steward's model never
 *  sees these requests and has no tool that reaches them.
 *
 *  Kinds: profile_read, profile_update {text, expected_version, reason}, forgotten_list,
 *  forget {ids, reason}, restore {id}, purge {ids}; and media_clear (no payload: the dashboard's
 *  清空媒体, design-steward 7.8.12 "保留 30 天"), run against the steward's media library - its
 *  own subdirectory, never a director's d-<driver_id>/ - and answered with counts only. */
import { randomUUID } from 'node:crypto';
import { basename } from 'node:path';

export const MEMORY_REQUEST_KINDS = ['profile_read', 'profile_update', 'forgotten_list', 'forget', 'restore', 'purge', 'media_clear'];
/** The State events these requests raise: handled here, never a model turn. */
export const MEMORY_REQUEST_EVENTS = new Set(['steward_memory_request_created', 'steward_memory_request_completed']);

/** The owner's 清空媒体: the steward's whole media subdirectory, files and index rows. A
 *  failure answers its code alone (an fs error's message names a path, and so a file). */
export async function clearMedia(media) {
  if (!media || /^d-/.test(basename(media.home))) return { refused: 'media_unavailable' };
  try { return { cleared: true, removed: (await media.clear()).removed }; }
  catch (error) { return { error: String(error?.code ?? 'clear_failed').slice(0, 40) }; }
}

/** The take's key names this host process: a take this process sends again after a lost answer
 *  is State's replay of its own take; a host that restarted after taking is media_clear_taken. */
const PROCESS = randomUUID();
/** The media_clear requests this process holds: {job} while one runs, {result} while its
 *  write-back has not landed (the next pass sends that result again, never a second clear). */
const clears = new Map();
const refusalCode = error => /HTTP 4\d\d "([a-z][a-z0-9_]*)/.exec(String(error?.message ?? error))?.[1];

/** Take the request (the backend's check that the owner's click is under two minutes old and
 *  not taken before), clear, write the counts back. Resolves true once written. */
async function runMediaClear(row, entry, { request, media, signal, log }) {
  const id = row.request_id;
  if (!entry.result && (!media || /^d-/.test(basename(media.home)))) entry.result = { refused: 'media_unavailable' };
  if (!entry.result) {
    try {
      await request('/api/state/commands/take_steward_media_clear', { request_key: `media-clear-take:${id}:${PROCESS}`, request_id: id }, signal);
      entry.result = await clearMedia(media);
    } catch (error) {
      const code = refusalCode(error);
      // Taken by this host before it restarted: whether that clear ran is unknown, and a second
      // one would delete what came after the owner's click.
      if (code !== 'media_clear_taken') {
        // Lapsed or answered (nothing to do), or the take did not get through (the next pass takes it).
        log(`media clear ${id}: not taken (${code ?? String(error?.message ?? error).slice(0, 200)})`);
        return false;
      }
      entry.result = { error: 'interrupted' };
    }
  }
  try {
    await request('/api/state/commands/complete_steward_memory_request',
      { request_key: `memory-request:${id}`, request_id: id, result: entry.result }, signal);
    return true;
  } catch (error) {
    log(`memory request ${id}: result not written (${String(error?.message ?? error).slice(0, 200)})`);
    return false;
  }
}

/** Start one media_clear beside the pass (the clear waits on the ingest lane, which may be held
 *  by a long ingest; the host's event pump must not wait with it). One job per request. */
function startMediaClear(row, context) {
  const entry = clears.get(row.request_id) ?? {};
  if (entry.job) return;
  clears.set(row.request_id, entry);
  entry.job = runMediaClear(row, entry, context)
    .catch(error => { context.log(`media clear ${row.request_id}: ${String(error?.message ?? error).slice(0, 200)}`); return false; })
    .then(written => {
      delete entry.job;
      if (written || !entry.result) clears.delete(row.request_id);
      return written;
    });
}

/** The media clears this process is running now, settled (tests; a host never waits on them). */
export function mediaClearsSettled() {
  return Promise.all([...clears.values()].map(entry => entry.job).filter(Boolean));
}

/** One request against the store, as the owner (a media_clear runs through
 *  processMemoryRequests alone: it is taken first). Never throws: a failure is the result. */
export async function runOwnerMemoryRequest(memory, { kind, payload = {} }) {
  if (!memory) return { refused: 'memory_unavailable' };
  try {
    switch (kind) {
      case 'profile_read': return await memory.profile();
      case 'profile_update': return await memory.updateProfile(payload.text, payload.expected_version, payload.reason || 'edited by the owner', 'owner');
      case 'forgotten_list': return { forgotten: memory.forgottenList() };
      case 'forget': return await memory.forget(payload.ids, { reason: payload.reason || 'forgotten by the owner', by: 'owner' });
      case 'restore': return await memory.restore(payload.id, 'owner');
      case 'purge': return await memory.purge(payload.ids, { by: 'owner' });
      default: return { refused: 'unknown_kind', kinds: MEMORY_REQUEST_KINDS };
    }
  } catch (error) {
    return { error: String(error?.message ?? error).slice(0, 500) };
  }
}

/** Run every pending request, oldest first, and write each result back. A result that could
 *  not be written stays pending and runs again next time: each memory kind is safe to repeat
 *  (a read reads again, forget/restore/purge report what was already done, a profile update
 *  repeated with the same version is refused naming the current text). A media_clear is not:
 *  it is started beside the pass (not counted in `done`), taken first, run once, and a result
 *  that was not written is sent again as it was. */
export async function processMemoryRequests({ request, memory, media, signal, log = () => {} }) {
  const listed = await request('/api/state/query/list_steward_memory_requests', { status: 'pending', limit: 50 }, signal);
  const pending = (listed.result ?? listed).requests ?? [];
  let done = 0;
  for (const row of pending) {
    if (row.kind === 'media_clear') { startMediaClear(row, { request, media, signal, log }); continue; }
    const result = await runOwnerMemoryRequest(memory, row);
    try {
      await request('/api/state/commands/complete_steward_memory_request',
        { request_key: `memory-request:${row.request_id}`, request_id: row.request_id, result }, signal);
      done++;
    } catch (error) {
      log(`memory request ${row.request_id}: result not written (${String(error?.message ?? error).slice(0, 200)})`);
    }
  }
  return { pending: pending.length, done };
}
