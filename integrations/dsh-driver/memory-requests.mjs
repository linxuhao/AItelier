/** The owner's memory operations from the AItelier dashboard (plugin step 14, design 3.4):
 *  the memory lives on the host, outside every container, so the backend records each
 *  owner request as a row and this code - not the model - runs it against the memory store
 *  under the store's own write lock, then writes the result back. The steward's model never
 *  sees these requests and has no tool that reaches them.
 *
 *  Kinds: profile_read, profile_update {text, expected_version, reason}, forgotten_list,
 *  forget {ids, reason}, restore {id}, purge {ids}. */

export const MEMORY_REQUEST_KINDS = ['profile_read', 'profile_update', 'forgotten_list', 'forget', 'restore', 'purge'];
/** The State events these requests raise: handled here, never a model turn. */
export const MEMORY_REQUEST_EVENTS = new Set(['steward_memory_request_created', 'steward_memory_request_completed']);

/** One request against the store, as the owner. Never throws: a failure is the result. */
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
 *  not be written stays pending and runs again next time: each kind is safe to repeat (a
 *  read reads again, forget/restore/purge report what was already done, a profile update
 *  repeated with the same version is refused naming the current text). */
export async function processMemoryRequests({ request, memory, signal, log = () => {} }) {
  const listed = await request('/api/state/query/list_steward_memory_requests', { status: 'pending', limit: 50 }, signal);
  const pending = (listed.result ?? listed).requests ?? [];
  let done = 0;
  for (const row of pending) {
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
