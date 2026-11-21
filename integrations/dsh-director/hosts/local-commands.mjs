/** The owner's commands on a standalone DSH (plugin step 15, design-steward 7): registered by
 *  the built-in local host in DSH's command registry (`ctx.commands`, @deepseek-ai/dsh-commands)
 *  and run by the owner in DSH's own client. A command never reaches the model: the registry
 *  logs it and shows its answer to the owner. An external host registers none of them.
 *
 *    /questions                 the open questions to the owner
 *    /answer <id> <text>        answer one; recorded answered_by owner:local, it wakes the root
 *    /proposals                 the open proposals (with the host's projectsDir only)
 *    /approve <id>              run one (create_project) now; the result wakes the steward
 *    /decline <id> [reason]     decline one
 *    /profile                   the resident profile, its version and its file (with memory only)
 *    /purge <id>                purge one memory entry from every version: the first /purge <id>
 *                               only warns; /purge <id> PURGE within 5 minutes of that warning
 *                               purges (with memory only)
 *    /forget <id> [reason]      forget one memory entry: out of search and reads, recoverable;
 *                               it wakes the steward with one line (with memory only)
 *    /restore <id>              bring a forgotten entry back; it wakes the steward (with memory only)
 *
 *  A DSH command carries no user identity (CommandInvocation: commandId, agent, rawInput,
 *  attachments, signal), so every decision taken here is the owner's as owner:local. The
 *  registry is the process's: every agent's UI lists these commands, and each answers only in
 *  the root session's UI (`isRoot(invocation.agent)`), where the owner talks to the director. */

export const PURGE_WORD = 'PURGE';
/** How long a /purge warning stands for its confirmation. */
export const PURGE_CONFIRM_MS = 5 * 60 * 1000;
const OWNER = 'owner:local';
const ok = text => ({ kind: 'success', text });
const fail = text => ({ kind: 'error', text });
/** The first word of a command's input, and the rest (trimmed). */
const split = raw => { const m = /^\s*(\S+)(?:\s+([\s\S]*))?$/.exec(raw); return m ? [m[1], (m[2] ?? '').trim()] : ['', '']; };
const refusal = (what, result) => fail(`${what}: ${result.refused}${result.reason ? ' - ' + result.reason : ''}`);
const isRecord = value => value !== null && typeof value === 'object' && !Array.isArray(value);
/** Oldest first by the given time field, then by id: the order things were asked or proposed in. */
const byTime = field => ([a, x], [b, y]) => String(x[field] ?? '').localeCompare(String(y[field] ?? '')) || a.localeCompare(b);
/** One open proposal as the owner reads it; a record edited by hand into another shape is
 *  shown for what it is, never thrown on (it would hide every other proposal). */
function proposalLine(id, p) {
  const payload = isRecord(p.payload) ? p.payload : {};
  const seeds = Array.isArray(payload.seed_nodes) ? payload.seed_nodes.length : 0;
  const shape = typeof payload.project_id === 'string' && typeof payload.title === 'string' && typeof payload.goal === 'string'
    ? '' : '\n  (this record is not a whole create_project: approving it fails, decline it)';
  return `- ${id} ${p.kind}: ${payload.project_id ?? '?'} "${payload.title ?? ''}" (${seeds} work item(s))\n  goal: ${payload.goal ?? ''}\n  reason: ${p.reason ?? ''}${shape}`;
}

/** @param commands the registry (ctx.commands): register(definition) -> unregister
 *  @param o questions(): the questions.json map; answer({id, text, by}); proposals(): the
 *    proposals.json map and decide({id, approve, reason, by}), both absent without projectsDir;
 *    memory: the owner's memory store (absent without memory); memoryError: why a configured
 *    memory is not open (then /profile, /purge, /forget and /restore say so); wake(line): tell
 *    the steward one line (a wake event; /forget and /restore); isRoot(agent): whether a command
 *    came from the root session's UI (absent: every UI).
 *  @returns the unregister functions. */
export function registerOwnerCommands(commands, { questions, answer, proposals, decide, memory, memoryError, wake, isRoot }) {
  const defs = [
    { name: 'questions', description: 'List the open questions your director or steward asked you.',
      async handler() {
        const open = Object.entries(await questions()).filter(([, q]) => isRecord(q) && q.status === 'open').sort(byTime('asked_at'));
        if (!open.length) return ok('No open questions.');
        return ok([`Open questions (${open.length}):`, ...open.map(([id, q]) => `- ${id}: ${q.text}`
          + (q.context ? `\n  context: ${q.context}` : '') + `\n  asked: ${q.asked_at}`), '', 'Answer with: /answer <id> <text>'].join('\n'));
      } },
    { name: 'answer', description: 'Answer one open question: /answer <id> <text>. It wakes your director or steward.',
      input: { hint: '<id> <text>' },
      async handler({ rawInput }) {
        const [id, text] = split(rawInput);
        if (!id || !text) return fail('Usage: /answer <id> <text>');
        const result = await answer({ id, text, by: OWNER });
        return result.refused ? refusal(`Not answered ${id}`, result) : ok(`Answered ${id} (as ${result.answered_by}, ${result.answered_at}).`);
      } },
  ];
  if (proposals && decide) defs.push(
    { name: 'proposals', description: 'List the open proposals your steward made.',
      async handler() {
        const open = Object.entries(await proposals()).filter(([, p]) => isRecord(p) && p.status === 'open').sort(byTime('created_at'));
        if (!open.length) return ok('No open proposals.');
        return ok([`Open proposals (${open.length}):`, ...open.map(([id, p]) => proposalLine(id, p)),
        '', 'Approve with /approve <id>, decline with /decline <id> [reason].'].join('\n'));
      } },
    { name: 'approve', description: 'Approve one open proposal and run it now: /approve <id>.', input: { hint: '<id>' },
      async handler({ rawInput }) {
        const [id] = split(rawInput);
        if (!id) return fail('Usage: /approve <id>');
        const result = await decide({ id, approve: true, by: OWNER });
        if (result.refused) return refusal(`Not approved ${id}`, result);
        return result.status === 'done'
          ? ok(`Approved ${id}: created project ${result.result?.project_id} at ${result.result?.root} with ${result.result?.work_items} work item(s).`)
          : fail(`Approved ${id}, but it failed: ${result.result?.error} - ${result.result?.reason}`);
      } },
    { name: 'decline', description: 'Decline one open proposal: /decline <id> [reason].', input: { hint: '<id> [reason]' },
      async handler({ rawInput }) {
        const [id, reason] = split(rawInput);
        if (!id) return fail('Usage: /decline <id> [reason]');
        const result = await decide({ id, approve: false, ...(reason ? { reason } : {}), by: OWNER });
        return result.refused ? refusal(`Not declined ${id}`, result) : ok(`Declined ${id}.`);
      } });
  /** One line to the steward, after the memory changed (whitespace typed in a reason is folded,
   *  so it stays one line); a failed wake is said, never thrown: the change is already made. */
  const tell = async line => {
    try { await wake?.(line.replace(/\s+/g, ' ')); return ''; }
    catch (error) { return ` Your steward was not told: ${error?.message ?? error}.`; }
  };
  // id -> until when its confirmation is accepted; filled only by a warning the owner was shown.
  const purgeWarned = new Map();
  if (memory) defs.push(
    { name: 'profile', description: 'Show your resident profile, its version and the file to edit.',
      async handler() {
        const p = await memory.profile();
        return ok([`Profile (version ${p.version}; ${p.text.length} of ${p.limit} characters).`,
          `To change it, edit ${memory.dir}/profile.md: the next read records your edit as yours.`, '',
          p.text.trim() ? p.text : '(empty)'].join('\n'));
      } },
    { name: 'purge', description: `Purge one memory entry from every version, for good: /purge <id>, then /purge <id> ${PURGE_WORD}.`,
      input: { hint: '<id>' },
      async handler({ rawInput }) {
        const [id, word] = split(rawInput);
        if (!id) return fail(`Usage: /purge <id> (then /purge <id> ${PURGE_WORD} to confirm)`);
        if (word && word !== PURGE_WORD) return fail(`Nothing purged: the confirmation word is ${PURGE_WORD} (/purge ${id} ${PURGE_WORD}).`);
        // The word counts only after the warning (design 3.6: type it a second time).
        if (!word || !(purgeWarned.get(id) > Date.now())) {
          purgeWarned.set(id, Date.now() + PURGE_CONFIRM_MS);
          return ok(`Nothing purged yet. Purging ${id} cannot be undone: its text is removed from the whole history of your memory. To go ahead, send within 5 minutes: /purge ${id} ${PURGE_WORD}`);
        }
        purgeWarned.delete(id);
        const result = await memory.purge([id], { by: OWNER });
        return result.purged === true ? ok(`Purged ${id}: ${JSON.stringify(result)}`) : fail(`Not purged ${id}: ${JSON.stringify(result)}`);
      } },
    { name: 'forget', description: 'Forget one memory entry (recoverable with /restore): /forget <id> [reason]. It tells your steward.',
      input: { hint: '<id> [reason]' },
      async handler({ rawInput }) {
        const [id, given] = split(rawInput);
        if (!id) return fail('Usage: /forget <id> [reason]');
        const reason = given || 'no reason given';
        const result = await memory.forget([id], { reason, by: OWNER });
        if (result.refused) return refusal(`Not forgotten ${id}`, result);
        if (result.unknown?.includes(id)) return fail(`Not forgotten ${id}: unknown_id - your memory has no entry ${id}`);
        if (result.already?.includes(id)) return fail(`Not forgotten ${id}: already_forgotten - /restore ${id} brings it back`);
        const told = await tell(`The owner forgot memory entry ${id} with /forget (reason: ${reason}); it is out of search and reads until they /restore it.`);
        return ok(`Forgotten ${id}: it is out of search and reads; /restore ${id} brings it back, /purge ${id} removes it for good (${result.note}; remote: ${result.remote_forget}).${told}`);
      } },
    { name: 'restore', description: 'Bring back one forgotten memory entry: /restore <id>. It tells your steward.', input: { hint: '<id>' },
      async handler({ rawInput }) {
        const [id] = split(rawInput);
        if (!id) return fail('Usage: /restore <id>');
        const result = await memory.restore(id, OWNER);
        if (result.refused) return refusal(`Not restored ${id}`, result);
        const told = await tell(`The owner restored memory entry ${id} with /restore; it is back in search and reads.`);
        return ok(`Restored ${id}: it is back in search and reads.${told}`);
      } });
  else if (memoryError) {
    // Configured but not open: the commands stay, and say why, instead of vanishing.
    const unavailable = async () => fail(`Your memory is not available: ${memoryError}`);
    defs.push({ name: 'profile', description: 'Show your resident profile (your memory is not available now).', handler: unavailable },
      { name: 'purge', description: 'Purge one memory entry (your memory is not available now).', input: { hint: '<id>' }, handler: unavailable },
      { name: 'forget', description: 'Forget one memory entry (your memory is not available now).', input: { hint: '<id> [reason]' }, handler: unavailable },
      { name: 'restore', description: 'Restore one forgotten memory entry (your memory is not available now).', input: { hint: '<id>' }, handler: unavailable });
  }
  const rootOnly = handler => async invocation => isRoot && !isRoot(invocation?.agent)
    ? fail('This command answers only in your director\'s own session, where you talk to it.') : handler(invocation);
  return defs.map(def => commands.register({ ...def, handler: rootOnly(def.handler) }));
}
