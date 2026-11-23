/** The steward's family-group turns (step 23, design-steward 7.7.3, 7.7.5, 7.7.6, 7.7.6a).
 *
 *  One session answers the owner and the family group (owner's ruling D0 = (a)), so what a
 *  turn may do and see is decided here, in code, from what the turn was given - never from a
 *  message's text:
 *  - the AUDIENCE of a turn: a turn whose first step claims a message tagged by the host
 *    (`source.kind` GROUP_SOURCE) is that group's turn, any other turn the owner's; input of
 *    another audience is put back to open the next turn;
 *  - the GUARD: a group turn may call only GROUP_TOOLS and the turn's connection catalog (public
 *    connections only, `connectionTools`) - at most turnToolCalls top-level calls, every one
 *    counted, refused or not, per turn id from 0; a nested call (call_tool, run_confirmed) is
 *    judged by name and not counted again - an owner turn never the family memory tools, and after a group turn,
 *    until the owner writes, an owner turn not the PAUSED_TOOLS (group-pause.json);
 *  - the FOLD: each finished group turn is replaced on the surface by one fixed line at the
 *    first step of the next turn (DSH's own positional replace, the one compaction uses), so
 *    an owner turn's request carries no byte of a group turn - and, with several groups (step
 *    23.1), the next group's turn neither: the line names no group, time or count (FOLD_LINE);
 *  - the COMPACTION FILTER: a compaction request never carries a group turn's messages;
 *  - grep_chat_history skips group turns (`withoutGroupTurns`).
 *  Nothing here names a backend; the host's `groups` operations do the backend side. */
import { readFileSync, writeFileSync, rmSync, renameSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { withoutLeadingClockLines } from './core.mjs';

export const GROUP_SOURCE = 'dsh-director-group';
export const FOLD_SOURCE = 'dsh-director-group-fold';
/** What a group turn may call (7.7.5): the public portfolio reads, the family memory, (step
 *  25 C4, 7.9.5, owner 2026-11-21) the family lists' show, add and check - never list_remove -
 *  and (step 24, 7.8.8) the way to the turn's connection tools: search_tools and call_tool find
 *  and call only the turn's catalog, which in a group turn holds public connections alone, and an
 *  outward one runs only after the asker confirms it in the group (request_confirmation,
 *  run_confirmed; 7.8.9). The connection tools themselves are the catalog's names
 *  (`connectionTools`), never listed here; the four connection-configuration tools are not here.
 *  Step 23.1 (7.9.4, owner 2026-11-22): group_reminder_set, a one-time reminder to this same
 *  group. Any tool added later is refused in a group turn by default. */
export const GROUP_TOOLS = Object.freeze(new Set(['portfolio_projects', 'portfolio_project', 'family_memory_note',
  'family_memory_show', 'list_show', 'list_add', 'list_check', 'group_reminder_set', 'search_tools', 'call_tool',
  'request_confirmation', 'run_confirmed',
  // Step 24.2 (7.8.8, 7.8.12): media_call (its own nested call is the catalog's, judged by name and
  // not counted again) and send_media (only into this group, one of its group_send_budget sends).
  'media_call', 'send_media',
  // Step 24.2c (7.8.12b "五"): read_document - only this group's own refs (covers), never a kept one.
  'read_document']));
/** Refused in an owner turn after a group turn until the owner writes again (7.7.5): the
 *  plugin's own writes here; a host adds its own (GroupTurns `pausedTools`, host.groups.pausedTools). */
export const PAUSED_TOOLS = Object.freeze(new Set(['memory_note', 'memory_profile_update', 'propose',
  // 7.8.8: a connection change waits for the owner too.
  'propose_connection_change']));
export const groupTurnRefusal = 'group turn: only portfolio_projects, portfolio_project, the family memory tools, list_show, list_add, list_check, group_reminder_set, read_document and your public connection tools (search_tools, call_tool, request_confirmation, run_confirmed, media_call, send_media); your last words are your reply to the group';
/** The tools only a group turn may call (7.7.5): the family memory (7.7.8a) and, step 23.1, a
 *  family member's reminder to their own group (7.9.4). */
export const FAMILY_TOOLS = Object.freeze(new Set(['family_memory_note', 'family_memory_show', 'group_reminder_set']));
export const familyRefusal = 'family memory is for group turns only';
export const groupReminderRefusal = 'group_reminder_set is for group turns only: in your owner\'s turns use your host\'s reminder tool';
export const pausedRefusal = 'a group turn ran since your owner last wrote; this waits until your owner writes';
export const answerNow = 'answer now';

const isGroup = message => message?.source?.kind === GROUP_SOURCE;
/** Whether a logged user message's audience can be read: a source object with a string kind,
 *  and for a group tag its channel and group_ref. One that cannot makes its turn a group turn
 *  of unknown group (7.7.3 item 1, 7.7.6a two: fail closed, for the whole turn). */
const readable = message => {
  const source = message?.source;
  if (!source || typeof source !== 'object' || typeof source.kind !== 'string') return false;
  return source.kind !== GROUP_SOURCE || (typeof source.channel === 'string' && typeof source.group_ref === 'string');
};
const turnKey = turn => turn === undefined || turn === null ? undefined : String(turn);
/** DSH's own per-step context (working directory, sandbox policy): nobody's input, kept in
 *  whichever turn it arrives with and never a reason to put anything back. The host adds its
 *  own such kinds (`neutralKinds`: the system clock line, which joins a step's input inside
 *  this hook). */
const NEUTRAL = new Set(['runtime-context', FOLD_SOURCE]);
const keepOnFold = event => event.type === 'user/message'
  && (event.data?.source?.kind === FOLD_SOURCE || /compact/.test(String(event.data?.source?.kind ?? '')));
const FOLDABLE = new Set(['user/message', 'assistant/message', 'tool/result']);

/** The turns of a session log, segmented by turn/start .. turn/end first: [{start, end?, turn,
 *  audience}], audience {kind: 'group', channel, group_ref, label} when any of the turn's user
 *  messages carries the group tag, {kind: 'group', unknown: true} when any of them has an
 *  audience that cannot be read, else {kind: 'owner'}. A turn's tag is persisted with its
 *  messages, so this is the same after a restart. */
export function turnSpans(events) {
  const spans = [];
  let open;
  for (const event of events) {
    if (event.type === 'turn/start') { open = { start: event.seq, turn: turnKey(event.data?.turn), audience: { kind: 'owner' }, questions: 0, questionIds: [], calls: 0 }; spans.push(open); }
    else if (event.type === 'turn/end' && open) { open.end = event.seq; open = undefined; }
    else if (event.type === 'user/message' && open && !readable(event.data)) open.audience = { kind: 'group', unknown: true };
    else if (event.type === 'user/message' && open && isGroup(event.data)) {
      const source = event.data.source;
      if (open.audience.kind !== 'group') open.audience = { kind: 'group', channel: source.channel, group_ref: source.group_ref, label: source.group_label };
      open.questions += (source.question_ids ?? []).length;
      open.questionIds.push(...(source.question_ids ?? []));
    } else if (event.type === 'assistant/message' && open)
      open.calls += (event.data?.message?.content ?? []).filter(block => block?.type === 'tool-call').length;
  }
  return spans;
}

/** The seqs inside group turns (finished or not). */
export function groupSeqs(events) {
  const inGroup = new Set();
  for (const span of turnSpans(events)) if (span.audience.kind === 'group')
    for (const event of events) if (event.seq >= span.start && (span.end === undefined || event.seq <= span.end)) inGroup.add(event.seq);
  return inGroup;
}

/** The raw log without anything a group turn said (grep_chat_history). */
export const withoutGroupTurns = events => { const inGroup = groupSeqs(events); return events.filter(e => !inGroup.has(e.seq)); };

/** The one line a folded group turn leaves (7.7.6a one): fixed, without the group's name, the
 *  time or how many questions (step 23.1, 7.7.24 item 3: another group's turn reads this surface).
 *  The named lines written before step 23.1 stay as they are (accepted, review P4). */
export const FOLD_LINE = "[A family-group turn ran here. Your host removed it from this conversation: what is said in a group is not your owner's context. Its record is on your owner's dashboard.]";

/** Replace every finished group turn's surface nodes (each contiguous run) by one fold line.
 *  Returns how many runs were folded. Throws whatever the session's append refuses. */
export function foldGroupTurns(session, createUserMessage, { before } = {}) {
  const events = session.snapshotEvents();
  const bySeq = new Map(events.map(event => [event.seq, event]));
  const spans = turnSpans(events).filter(span => span.audience.kind === 'group' && span.end !== undefined
    && (before === undefined || span.end < before));
  if (!spans.length) return 0;
  const spanOf = seq => spans.find(span => seq >= span.start && seq <= span.end);
  const runs = [];
  let run;
  for (const seq of session.surface.nodes) {
    const event = bySeq.get(seq), span = event && spanOf(seq);
    if (span && FOLDABLE.has(event.type) && !keepOnFold(event)) {
      if (run && run.span === span) run.seqs.push(seq);
      else { run = { span, seqs: [seq] }; runs.push(run); }
    } else run = undefined;
  }
  for (const { seqs } of runs) {
    const message = createUserMessage({ content: [{ type: 'text', text: FOLD_LINE }], source: { kind: FOLD_SOURCE } });
    session.append('user/message', message, { surfaceOp: { op: 'replace', startSeq: seqs[0], endSeq: seqs.at(-1) }, sourceEventSeqs: seqs });
  }
  return runs.length;
}

/** A compaction request without any message of a group turn (each removed run becomes one
 *  fold line), or undefined when it carries none (7.7.6a two). Turns are segmented first
 *  (turn/start .. turn/end), then judged: messages are matched to the log by id; one that
 *  cannot be matched is the turn's of the message before it (or, with none before it in a turn,
 *  of the one after it) and makes that whole turn count as a group's. So what is removed is
 *  always whole turns: never a tool call without its result, or a result without its call. A
 *  message outside every turn stays: the system prompt, a checkpoint, a fold line, and the
 *  compaction instruction compaction-basic appends last (no id). */
export function filterCompactionRequest(options, events, createUserMessage) {
  const spans = turnSpans(events);
  const spanOf = seq => spans.findIndex(span => seq >= span.start && (span.end === undefined || seq <= span.end));
  const seqOf = new Map();
  for (const event of events) {
    const id = event.type === 'user/message' ? event.data?.id : event.data?.message?.id;
    if (id !== undefined) seqOf.set(id, event);
  }
  const list = options.messages ?? [];
  // The turn of each message: a span index, -1 for none, undefined while unmatched.
  const segment = list.map((message, at) => {
    if (message?.role === 'system') return -1;
    if (at === list.length - 1 && message?.role === 'user' && message.id === undefined) return -1;
    const event = message?.id === undefined ? undefined : seqOf.get(message.id);
    if (event === undefined) return undefined;
    return keepOnFold(event) ? -1 : spanOf(event.seq);
  });
  const unreadable = new Set();
  const matched = segment.map(value => value);
  segment.forEach((value, at) => {
    if (value !== undefined) return;
    let before = at - 1, after = at + 1;
    while (before >= 0 && matched[before] === undefined) before -= 1;
    while (after < list.length && matched[after] === undefined) after += 1;
    const turn = before >= 0 && matched[before] >= 0 ? matched[before] : after < list.length && matched[after] >= 0 ? matched[after] : -1;
    segment[at] = turn;
    if (turn >= 0) unreadable.add(turn);
  });
  const messages = [];
  let changed = false, folding = false;
  list.forEach((message, at) => {
    const turn = segment[at];
    const drop = turn >= 0 && (spans[turn].audience.kind === 'group' || unreadable.has(turn));
    if (!drop) { messages.push(message); folding = false; return; }
    changed = true;
    if (!folding) messages.push(createUserMessage({ content: [{ type: 'text', text: FOLD_LINE }], source: { kind: FOLD_SOURCE } }));
    folding = true;
  });
  return changed ? { ...options, messages } : undefined;
}

/** Turn bookkeeping for the root session: audience, merge bounds, the guard and the pause.
 *  The merge count and the tool call count belong to one turn id (`current.turn`): each turn
 *  starts them at 0, and only a group turn counts (7.7.3 item 3, 7.7.5). */
export class GroupTurns {
  /** @param connectionTools () => the Set of connection tool names this turn may use (the
   *  steward's catalog: in a group turn its public connections), or undefined for none. */
  constructor({ stateDir, limits, log = () => {}, pausedTools = [], neutralKinds = [], connectionTools = () => undefined }) {
    Object.assign(this, { limits, log, connectionTools });
    this.neutral = new Set([...NEUTRAL, ...neutralKinds]);
    this.pausedTools = new Set([...PAUSED_TOOLS, ...pausedTools]);
    this.pauseFile = stateDir ? join(stateDir, 'group-pause.json') : undefined;
    // The owner input a group turn put back (message ids): it opens the very next turn, so the
    // owner waits at most one group turn (7.7.3 item 3, risk (e)). Kept on disk for a restart.
    this.waitingFile = stateDir ? join(stateDir, 'group-owner-waiting.json') : undefined;
    this.waiting = new Set();
    try { if (this.waitingFile && existsSync(this.waitingFile)) this.waiting = new Set(JSON.parse(readFileSync(this.waitingFile, 'utf8'))); }
    catch { this.waiting = new Set(); }
    this.current = undefined;
    // The turn DSH has open (its turn/start), when the host reports it (turnOpened).
    this.liveTurn = undefined;
  }

  /** Nobody's input: kept in whichever turn it arrives with, never put back or waited on. */
  isNeutral(message) { return this.neutral.has(message?.source?.kind); }
  /** DSH opened turn `turn` (the root session's turn/start). */
  turnOpened(turn) { this.liveTurn = turnKey(turn); }
  /** The bookkeeping of the turn that is open now, or undefined when it is another turn's. */
  open() {
    const turn = this.current;
    return turn && (this.liveTurn === undefined || turn.turn === undefined || turn.turn === this.liveTurn) ? turn : undefined;
  }

  /** Owner input `id` was put back by a group turn (or redelivered after a failed setup). */
  wait(id) {
    if (id === undefined || this.waiting.has(id)) return;
    this.waiting.add(id);
    while (this.waiting.size > 50) this.waiting.delete(this.waiting.values().next().value);
    this.saveWaiting();
  }
  saveWaiting() {
    if (!this.waitingFile) return;
    const temporary = this.waitingFile + '.tmp';
    writeFileSync(temporary, JSON.stringify([...this.waiting]), { mode: 0o600 });
    renameSync(temporary, this.waitingFile);
  }

  /** A restart in the middle of a turn: its audience, questions and calls so far, from the
   *  session log (the group tag is persisted with the turn's messages), never from memory.
   *  Only this turn's open span: a log whose open turn is 8 says nothing about turn 7, so it is
   *  no resume of turn 7 (an audience it cannot place reads as a group, 7.7.6a two). */
  resume(events, turn) {
    const open = turnSpans(events).findLast(span => span.end === undefined);
    if (!open || open.turn !== turnKey(turn)) return false;
    // The group messages the turn took so far (their askers and questions), for a later merge
    // to record the whole turn again (createGroupSetup).
    const kept = open.audience.unknown ? [] : events.filter(e => e.seq >= open.start && e.type === 'user/message' && isGroup(e.data)
      && e.data.source.channel === open.audience.channel && e.data.source.group_ref === open.audience.group_ref).map(e => e.data);
    this.current = { turn: turnKey(turn) ?? open.turn, audience: open.audience, calls: open.calls, merged: open.questionIds.length,
      ownerWaiting: false, questions: open.audience.unknown ? [] : [...open.questionIds], kept };
    return true;
  }

  /** Whose turn it is now; unknown (or another turn's) reads as a group (fail closed: the
   *  narrower rules). */
  audience() { return this.open()?.audience ?? { kind: 'group', unknown: true }; }

  /** Split one step's claimed messages: what this turn keeps and what goes back (in order). */
  split(messages, step, turnId) {
    if (step === 1 || !this.current || this.current.turn !== turnKey(turnId)) {
      // Owner input a group turn put back opens this turn, whatever else came with it.
      const waited = messages.some(m => !isGroup(m) && !this.isNeutral(m) && this.waiting.has(m.id));
      const first = waited ? undefined : messages.find(isGroup);
      // A later step whose turn this object has no bookkeeping for (a log it could not read it
      // back from) is nobody's it can name: the same shape as any unknown audience, never the
      // owner's (a first step's input with no group tag is the owner's, 7.7.3 item 1).
      const audience = first ? { kind: 'group', channel: first.source.channel, group_ref: first.source.group_ref,
        label: first.source.group_label } : step === 1 ? { kind: 'owner' } : { kind: 'group', unknown: true };
      this.current = { turn: turnKey(turnId), audience, calls: 0, merged: 0, ownerWaiting: false, questions: [] };
    }
    const turn = this.current, keep = [], back = [];
    let served = false;
    for (const message of messages) {
      if (this.isNeutral(message)) { keep.push(message); continue; }
      if (turn.audience.kind === 'owner') {
        if (isGroup(message)) back.push(message);
        else { keep.push(message); served = this.waiting.delete(message.id) || served; }
        continue;
      }
      const same = isGroup(message) && message.source.group_ref === turn.audience.group_ref
        && message.source.channel === turn.audience.channel;
      if (!same) { if (!isGroup(message)) turn.ownerWaiting = true; back.push(message); continue; }
      // Owner input already waiting stops LATER merges (a first step takes its questions).
      if ((step !== 1 && turn.ownerWaiting) || turn.merged >= this.limits.mergeMax) { back.push(message); continue; }
      turn.merged += 1;
      turn.questions.push(...(message.source.question_ids ?? []));
      keep.push(message);
    }
    if (served) this.saveWaiting();
    return { keep, back };
  }

  /** The guard's verdict for one root tool call (a refusal string, or undefined). The audience
   *  first, then the count: only a group turn counts its calls, from 0 in each turn, and it
   *  counts EVERY top-level call the guard sees (a refused one too, as resume() counts them from
   *  the log), so a group turn is bounded at turnToolCalls calls: from the next one on every call
   *  is answerNow, whatever its name; before that a name off the whitelist (GROUP_TOOLS and the
   *  turn's connection catalog) is groupTurnRefusal. A `nested` call - the one call_tool or
   *  run_confirmed makes inside a counted call - is judged by its name alone and not counted. */
  guard(name, { nested = false } = {}) {
    const turn = this.open();
    if ((turn?.audience.kind ?? 'group') === 'group') {
      if (turn && !nested) turn.calls += 1;
      if (turn && !nested && turn.calls > this.limits.turnToolCalls) return answerNow;
      let catalog;
      try { catalog = this.connectionTools(); } catch { catalog = undefined; }
      if (!GROUP_TOOLS.has(name) && !catalog?.has(name)) return groupTurnRefusal;
      return turn ? undefined : answerNow;
    }
    if (FAMILY_TOOLS.has(name)) return name === 'group_reminder_set' ? groupReminderRefusal : familyRefusal;
    if (this.paused() && this.pausedTools.has(name)) return pausedRefusal;
    return undefined;
  }

  /** The pause record: {at} after a group turn, until an owner message clears it. Unreadable
   *  or broken reads as in effect. */
  pauseRecord() {
    if (!this.pauseFile) return undefined;
    try { return JSON.parse(readFileSync(this.pauseFile, 'utf8')); }
    catch (error) { return error?.code === 'ENOENT' ? undefined : { at: '1970-01-01T00:00:00.000Z', broken: true }; }
  }
  paused() { return this.pauseRecord() !== undefined; }
  pause() {
    if (!this.pauseFile) return;
    const temporary = this.pauseFile + '.tmp';
    writeFileSync(temporary, JSON.stringify({ at: new Date().toISOString() }), { mode: 0o600 });
    renameSync(temporary, this.pauseFile);
  }
  unpause() { if (this.pauseFile) rmSync(this.pauseFile, { force: true }); }
}

/** The name a group wake carries (step 23.1, 7.7.4): the bound group's own label, required and
 *  unlike every other bound group's, so the model tells two groups apart by it - never a shared
 *  fallback name and never null. A group the host's list does not name (the list could not be
 *  read, or the group is not in it) throws `group_wake_unnamed` (director 602 defect 3: fail
 *  closed, steward.md promises every wake names its group); the caller logs it and wakes nothing. */
export function groupWakeName(group) {
  if (typeof group?.label !== 'string' || !group.label.trim()) throw new Error('group_wake_unnamed: the host named no bound group for this question; no wake');
  return group.label;
}

/** Puts input of another audience back for a later turn (7.7.3 item 2). DSH opens one turn per
 *  queued `next-turn` message, so owner input goes right after the owner input already waiting
 *  (it opens the very next turn, in its own order) and a group question to the end (in arrival
 *  order): the owner waits at most one group turn, however many questions keep coming. */
export function putBack(inbox, messages, turns) {
  for (const message of messages) {
    if (isGroup(message)) { inbox.append('next-turn', message); continue; }
    const queued = inbox.nextTurn ?? [];
    let at = 0;
    while (at < queued.length && turns.waiting.has(queued[at]?.id)) at += 1;
    inbox.splice('next-turn', at, 0, [message]);
    turns.wait(message.id);
  }
}

const short = error => String(error?.message ?? error).slice(0, 300);

/** The first step of every steward turn (7.7.3 items 2, 5, 6): decide its audience, put input of
 *  another audience back, fold the finished group turns, record the audience on the host and
 *  set or clear the pause - all before the step runs. Any failure (the fold, the audience
 *  record, writing group-pause.json) rejects the step; its input is
 *  kept in `pendingFile` and delivered again after `retryMs`; a second failure in a row is
 *  reported as the host error group_turn_setup_failed (cleared, and what was kept delivered,
 *  by the next first step that succeeds). The audience record itself is tried `recordAttempts`
 *  times, `recordRetryMs` apart, before it counts as failed (director 602 defect 1: one lost
 *  request must not cost a group its answer; recording a turn again is idempotent). */
export function createGroupSetup({ turns, groups, createUserMessage, pendingFile, canSteer = () => true, steer,
  setHostError = async () => {}, clearHostError = async () => {}, log = () => {}, retryMs = 30000, fold = foldGroupTurns,
  recordAttempts = 3, recordRetryMs = 500 }) {
  let failures = 0, reported = false;
  const pending = () => {
    try { return pendingFile ? JSON.parse(readFileSync(pendingFile, 'utf8')) : []; } catch { return []; }
  };
  const redeliver = () => {
    const kept = pending();
    if (!kept.length || !canSteer()) return 0;
    rmSync(pendingFile, { force: true });
    const messages = kept.map(m => createUserMessage({ content: m.content, source: m.source }));
    for (const message of messages) if (!isGroup(message) && !turns.isNeutral(message)) turns.wait(message.id);
    steer(messages);
    return messages.length;
  };
  // The host records the turn's audience, its askers and (step 23.1, 7.7.7) the questions it took,
  // which the backend stamps with the turn: a reply answers only a question of its own turn.
  const recordOnce = (payload, audience, keep) => groups.audience({ turn: String(payload.turn), audience: audience.kind,
    ...(audience.kind === 'group' ? { group: { channel: audience.channel, group_ref: audience.group_ref } } : {}),
    senderRefs: [...new Set(keep.flatMap(m => m.source?.kind === GROUP_SOURCE ? m.source.sender_refs ?? [] : []))],
    ...(audience.kind === 'group' ? { questionIds: keep.flatMap(m => m.source?.kind === GROUP_SOURCE ? m.source.question_ids ?? [] : []) } : {}) },
  payload.signal);
  const record = async (payload, audience, keep) => {
    if (!groups?.audience) return undefined;
    for (let attempt = 1; ; attempt += 1) {
      try { return await recordOnce(payload, audience, keep); }
      catch (error) {
        if (attempt >= recordAttempts || payload.signal?.aborted) throw error;
        log(`group audience not recorded (attempt ${attempt} of ${recordAttempts}): ` + short(error));
        await new Promise(resolve => setTimeout(resolve, recordRetryMs));
      }
    }
  };
  async function step(payload, decision) {
    if (payload.step !== 1 && turns.current?.turn !== turnKey(payload.turn)) turns.resume(payload.agent.session.snapshotEvents(), payload.turn);
    const { keep, back } = turns.split(decision.messages, payload.step, payload.turn);
    putBack(payload.agent.inbox, back, turns);
    if (payload.step !== 1) {
      // A later step that merged more questions of this group records them too (the turn's
      // askers and questions so far); one that cannot be recorded goes back for a later turn,
      // which records it - never a question kept in a turn that did not take it on record.
      const merged = keep.filter(isGroup);
      const turn = turns.open();
      if (merged.length && turn?.audience.kind === 'group') {
        turn.kept = [...(turn.kept ?? []), ...merged];
        try { await record(payload, turn.audience, turn.kept); }
        catch (error) {
          log('group merge not recorded: ' + short(error));
          turn.kept = turn.kept.filter(m => !merged.includes(m));
          const ids = new Set(merged.flatMap(m => m.source.question_ids ?? []));
          turn.questions = turn.questions.filter(id => !ids.has(id));
          turn.merged -= merged.length;
          putBack(payload.agent.inbox, merged, turns);
          return { ...decision, messages: keep.filter(m => !merged.includes(m)) };
        }
      }
      return { ...decision, messages: keep };
    }
    const audience = turns.audience();
    try {
      fold(payload.agent.session, createUserMessage);
      if (turns.current) turns.current.kept = keep.filter(isGroup);
      await record(payload, audience, keep);
      if (audience.kind === 'group') turns.pause();
      else {
        const pause = turns.pauseRecord();
        const given = keep.map(m => (m.content ?? []).map(b => b.text ?? '').join('')).join('\n');
        // A failed read leaves the pause in effect: it fails closed for the pause, never for
        // the owner's turn.
        if (pause && groups?.ownerSpokeSince && await groups.ownerSpokeSince(pause.at, given, payload.signal)
          .catch(error => { log('group pause check failed: ' + short(error)); return false; })) turns.unpause();
      }
      const recovered = failures > 0;
      failures = 0;
      if (recovered) {
        redeliver();
        if (reported) { reported = false; await clearHostError(); }
      }
      return { ...decision, messages: keep };
    } catch (error) {
      failures += 1;
      log('group turn setup failed: ' + short(error));
      if (pendingFile) writeFileSync(pendingFile, JSON.stringify([...pending(), ...keep.map(m => ({ content: m.content, source: m.source }))]), { mode: 0o600 });
      turns.current = undefined;
      if (failures >= 2) { reported = true; await setHostError('group_turn_setup_failed: ' + short(error)); }
      else setTimeout(redeliver, retryMs).unref?.();
      return { kind: 'reject' };
    }
  }
  return { step, redeliver, get failures() { return failures; } };
}

/** A group turn that ended normally is answered in its group by code: the steward's last
 *  words go to the host (`reply`: exit check, one message, the latest question's group), then
 *  out through the channel (`sendGroup`). An owner turn's words never go to a group. The system
 *  clock lines the steward copied to the start of its words are not sent; words that were only
 *  such lines send nothing (only_clock_text, as the owner's automatic reply). A withheld
 *  reply's line for the owner (`owner_notice`, r44w 7.7.7) goes to `mirror`, the owner's
 *  channel mirror, never into the group. Returns the handler of the root session's events. */
export function groupReplies({ turns, reply, sendGroup, warn, mirror }) {
  let last = '';
  return event => {
    if (event.type === 'turn/start') last = '';
    else if (event.type === 'assistant/message') {
      const said = (event.data?.message?.content ?? []).filter(b => b.type === 'text').map(b => b.text ?? '').join('').trim();
      if (said) last = said;
    } else if (event.type === 'turn/end') {
      const turn = turns.open(), said = last;
      last = '';
      if (turn?.audience.kind !== 'group' || !said || event.data?.reason?.kind !== 'completed' || !turn.questions.length
        || (turn.turn !== undefined && turn.turn !== String(event.data?.turn))) return;
      const questions = [...turn.questions], body = withoutLeadingClockLines(said);
      if (!body) return void warn('group reply not sent ' + JSON.stringify({ reason: 'only_clock_text', question_id: questions.at(-1) }));
      // The turn is named: the backend answers only a question this turn took (step 23.1, 7.7.7).
      return reply({ questionId: questions.at(-1), alsoAnswers: questions.slice(0, -1), body, turn: String(event.data?.turn ?? turn.turn) })
        .then(sent => {
          if (sent?.owner_notice?.message_id && mirror) void mirror(sent.owner_notice.message_id, sent.owner_notice.body);
          return sent?.text ? sendGroup(sent.channel, sent.group_ref, sent.text) : warn('group reply refused: ' + String(sent?.refused));
        })
        .catch(error => warn('group reply: ' + short(error)));
    }
  };
}
