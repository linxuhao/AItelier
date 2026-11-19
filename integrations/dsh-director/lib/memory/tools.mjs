/** The memory tools (design 3.4-3.6). Registered only when the host config has a
 *  `memory` section; which role is shown them is roles.json's business (the steward,
 *  plugin step 12). There is deliberately no purge tool: purging is the owner's alone. */
import { homedir } from 'node:os';
import { join } from 'node:path';
import { MemoryStore, PROFILE_LIMIT, READ_CHARS, READ_IDS_MAX, TOP_K_MAX } from './store.mjs';
import { remoteRanked } from './remote.mjs';
import { memoryTimeZoneProblem } from '../host.mjs';

export const MEMORY_TOOLS = ['memory_search', 'memory_read', 'memory_note', 'memory_profile_update', 'memory_forget'];
export const defaultMemoryRoot = () => join(homedir(), '.local', 'share', 'dsh-director', 'memory');

/** Open the store a host config asks for. Never throws: a memory that cannot start is
 *  {error}, and every memory tool answers that error while the rest of the role runs. */
export async function openMemory(memory, { logger, sessionId } = {}) {
  try {
    const zoneProblem = memoryTimeZoneProblem(memory.timeZone);
    if (zoneProblem) throw new Error(zoneProblem);
    const ranked = memory.remote ? remoteRanked({ ...memory.remote, userId: memory.remote.userId ?? memory.userId,
      sessionId: memory.remote.sessionId ?? sessionId,
      onCheck: r => logger?.('memory/remote-id-check', r) }) : undefined;
    const store = await MemoryStore.open({ root: memory.root ?? defaultMemoryRoot(), userId: memory.userId, ranked, logger,
      ...(memory.timeZone ? { timeZone: memory.timeZone } : {}),
      ...(memory.grepTimeoutMs ? { grepTimeoutMs: Number(memory.grepTimeoutMs) } : {}),
      ...(memory.backupRemote ? { backupRemote: memory.backupRemote } : {}) });
    return { store };
  } catch (error) {
    logger?.('memory/unavailable', { reason: String(error?.message ?? error).slice(0, 300) });
    return { error: String(error?.message ?? error).slice(0, 500) };
  }
}

/** The profile as the startup / post-compaction snapshot shows it. */
export async function profileSection(memory) {
  if (memory.error) return `\n\nOwner profile: unavailable (${memory.error})`;
  const p = await memory.store.profile();
  // f7: what the owner must be told (an interrupted purge) rides along until acknowledged.
  const notices = memory.store.notices?.length
    ? `\n\nMemory notices - tell your owner:\n${memory.store.notices.map(n => '- ' + n).join('\n')}` : '';
  // Design-steward 7.3 layer 3: the latest exchanges verbatim, so a compaction never takes
  // away what was just said (every turn is in the memory log already).
  const recent = memory.store.recentTurns?.() ?? [];
  const conversation = recent.length
    ? `\n\nRecent conversation with your owner (verbatim, oldest first; older turns: memory_search):\n${recent.join('\n\n')}` : '';
  return `\n\nOwner profile (resident; version ${p.version}; edit with memory_profile_update and this version):\n`
    + (p.text.trim() ? p.text : '(empty)') + notices + conversation;
}

/** @param scope the director-scope (or role-scope) agent context
 *  @param ownerMessage async (id, signal) => true when `id` is a message the owner wrote */
export function registerMemoryTools(scope, { defineTool, jsonOutput, memory, ownerMessage }) {
  const unavailable = () => ({ refused: 'memory_unavailable', reason: memory.error });
  scope.tools.register(defineTool({ name: 'memory_search',
    description: `Search your owner's memory. Two answers come back, each naming its source and each giving the whole entries it found as text - grouped by day in your owner's time zone, every line dated - with hits beside it (ids for memory_read): grep (literal, case-insensitive match of the whole query in the memory log and the profile - quote these as the owner's own words) and ranked (BM25, or the configured remote index: records close in meaning - say they are similar records, not quotes). A ranked line marked （日期命中） is a record from that day: its text has none of your words, so it is neither a quote nor close in meaning. An entry both answers found is whole in one of them; the other says → 见 grep / → 见 ranked. ranked with an error has no ranked answer this time; when ranked.vector is there and is not ok, the ranked answer is only part of the memory - say it may be incomplete. Both answers share one budget: when budget says omitted above 0, the entries listed in omitted did not fit - read them with memory_read. Use exact words, names, dates or ids for grep; describe the idea for ranked. top_k 1-${TOP_K_MAX} (default 10).`,
    parameters: { query: { type: 'string', required: true }, top_k: { type: 'integer' } }, output: jsonOutput,
    execute: args => memory.error ? unavailable() : memory.store.search(args.query, args.top_k ?? 10),
  }));
  scope.tools.register(defineTool({ name: 'memory_read',
    description: `Read whole memory entries by id: take the ids from memory_search's hits[].id and omitted[].id (a remote hit has an id only when its text is a local entry's); "profile" is the resident profile. Read what search listed in omitted, or with_turn: true for the rest of that conversation turn (marked context), e.g. your own reply to an owner message. Forgotten entries are not returned. Up to ${READ_IDS_MAX} ids and ${READ_CHARS} characters in all: entries that did not fit are listed in omitted (read them next); one entry longer than that comes in pieces - read it alone with offset = cut.next_offset.`,
    parameters: { ids: { type: 'array', items: { type: 'string' }, required: true }, with_turn: { type: 'boolean' }, offset: { type: 'integer' } },
    output: jsonOutput,
    execute: args => memory.error ? unavailable() : memory.store.read(args.ids, { withTurn: args.with_turn === true, offset: args.offset ?? 0 }),
  }));
  scope.tools.register(defineTool({ name: 'memory_note',
    description: 'Write one thing worth remembering into your owner\'s memory (an idea, a decision, a preference in their words). Each conversation turn is recorded already; use this for what you concluded or were asked to keep.',
    parameters: { text: { type: 'string', required: true } }, output: jsonOutput,
    execute: args => memory.error ? unavailable() : memory.store.note(args.text, 'steward'),
  }));
  scope.tools.register(defineTool({ name: 'memory_profile_update',
    description: `Replace your owner's resident profile (who they are, preferences, standing decisions; at most ${PROFILE_LIMIT} characters, never truncated). expected_version is the version your snapshot or the last update showed; if the owner changed it since, you are refused and given the current text to start from. reason says why.`,
    parameters: { text: { type: 'string', required: true }, expected_version: { type: 'string', required: true }, reason: { type: 'string', required: true } },
    output: jsonOutput,
    execute: args => memory.error ? unavailable() : memory.store.updateProfile(args.text, args.expected_version, args.reason, 'steward'),
  }));
  scope.tools.register(defineTool({ name: 'memory_forget',
    description: 'Forget memory entries, only because your owner asked you to: owner_message_id is the id of the owner\'s own message that asked for it, and anything else is refused. Forgotten entries leave search; the owner can restore them, and only the owner can purge them for good.',
    parameters: { ids: { type: 'array', items: { type: 'string' }, required: true }, reason: { type: 'string', required: true },
      owner_message_id: { type: 'string', required: true } }, output: jsonOutput,
    async execute(args, exec) {
      if (memory.error) return unavailable();
      if (!ownerMessage) return { refused: 'no_owner_channel', reason: 'this host cannot confirm an owner message, so the steward cannot forget; the owner can' };
      const id = typeof args.owner_message_id === 'string' ? args.owner_message_id.trim() : '';
      let ok = false;
      try { ok = id ? await ownerMessage(id, exec.signal) : false; }
      catch (error) { return { refused: 'owner_message_unverifiable', reason: String(error?.message ?? error).slice(0, 300) }; }
      if (!ok) return { refused: 'not_an_owner_message', reason: 'owner_message_id must name a message your owner wrote' };
      return memory.store.forget(args.ids, { reason: args.reason, by: `steward (owner message ${id})` });
    },
  }));
}
