/** The steward's family-group turns (step 23, design-steward 7.7.3, 7.7.5, 7.7.6, 7.7.6a).
 *
 *  One session answers the owner and the family group (owner's ruling D0 = (a)), so what a
 *  turn may do and see is decided here, in code, from what the turn was given - never from a
 *  message's text:
 *  - the AUDIENCE of a turn: a turn whose first step claims a message tagged by the host
 *    (`source.kind` GROUP_SOURCE) is that group's turn, any other turn the owner's; input of
 *    another audience is put back to open the next turn;
 *  - the GUARD: a group turn may call only GROUP_TOOLS (at most turnToolCalls calls), an owner
 *    turn never the family memory tools, and after a group turn, until the owner writes, an
 *    owner turn not the PAUSED_TOOLS (group-pause.json);
 *  - the FOLD: each finished group turn is replaced on the surface by one fixed line at the
 *    first step of the next turn (DSH's own positional replace, the one compaction uses), so
 *    an owner turn's request carries no byte of a group turn;
 *  - the COMPACTION FILTER: a compaction request never carries a group turn's messages;
 *  - grep_chat_history skips group turns (`withoutGroupTurns`).
 *  Nothing here names a backend; the host's `groups` operations do the backend side. */
import { readFileSync, writeFileSync, rmSync, renameSync } from 'node:fs';
import { join } from 'node:path';

export const GROUP_SOURCE = 'dsh-director-group';
export const FOLD_SOURCE = 'dsh-director-group-fold';
/** What a group turn may call (7.7.5): the public portfolio reads and the family memory. Any
 *  tool added later is refused in a group turn by default. */
export const GROUP_TOOLS = Object.freeze(new Set(['portfolio_projects', 'portfolio_project', 'family_memory_note',
  'family_memory_show']));
/** The family memory is for group turns only (7.7.8a, platform director 420). */
export const FAMILY_TOOLS = Object.freeze(new Set(['family_memory_note', 'family_memory_show']));
/** Refused in an owner turn after a group turn until the owner writes again (7.7.5). */
export const PAUSED_TOOLS = Object.freeze(new Set(['memory_note', 'memory_profile_update', 'propose', 'schedule_set',
  'reply_to_director']));
export const groupTurnRefusal = 'group turn: only portfolio_projects, portfolio_project and the family memory tools; your last words are your reply to the group';
export const familyRefusal = 'family memory is for group turns only';
export const pausedRefusal = 'a group turn ran since your owner last wrote; this waits until your owner writes';
export const answerNow = 'answer now';

const isGroup = message => message?.source?.kind === GROUP_SOURCE;
/** DSH's own per-step context (working directory, sandbox policy): nobody's input, kept in
 *  whichever turn it arrives with and never a reason to put anything back. */
const NEUTRAL = new Set(['runtime-context', FOLD_SOURCE]);
const isNeutral = message => NEUTRAL.has(message?.source?.kind);
const keepOnFold = event => event.type === 'user/message'
  && (event.data?.source?.kind === FOLD_SOURCE || /compact/.test(String(event.data?.source?.kind ?? '')));
const FOLDABLE = new Set(['user/message', 'assistant/message', 'tool/result']);

/** The turns of a session log: [{start, end?, audience}], audience {kind: 'group', channel,
 *  group_ref, label} when any of the turn's user messages carries the group tag, else
 *  {kind: 'owner'}. A turn's tag is persisted with its messages, so this is the same after a
 *  restart. */
export function turnSpans(events) {
  const spans = [];
  let open;
  for (const event of events) {
    if (event.type === 'turn/start') { open = { start: event.seq, audience: { kind: 'owner' }, questions: 0 }; spans.push(open); }
    else if (event.type === 'turn/end' && open) { open.end = event.seq; open = undefined; }
    else if (event.type === 'user/message' && open && isGroup(event.data)) {
      const source = event.data.source;
      if (open.audience.kind !== 'group') open.audience = { kind: 'group', channel: source.channel, group_ref: source.group_ref, label: source.group_label };
      open.questions += (source.question_ids ?? []).length;
    }
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

const REMOVED = "Your host removed it from this conversation: what is said in a group is not your owner's context. Its record is on your owner's dashboard.]";
export const FOLD_LINE = '[A family-group turn ran here. ' + REMOVED;
export const foldText = span => `[A family-group turn ran here (group ${span.audience.label ?? span.audience.group_ref ?? '?'}, ${span.time ?? 'earlier'}, ${span.questions} question(s)). ${REMOVED}`;

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
  for (const { span, seqs } of runs) {
    span.time = bySeq.get(span.start)?.time ? new Date(bySeq.get(span.start).time).toISOString() : undefined;
    const message = createUserMessage({ content: [{ type: 'text', text: foldText(span) }], source: { kind: FOLD_SOURCE } });
    session.append('user/message', message, { surfaceOp: { op: 'replace', startSeq: seqs[0], endSeq: seqs.at(-1) }, sourceEventSeqs: seqs });
  }
  return runs.length;
}

/** A compaction request without any message of a group turn (each removed run becomes one
 *  fold line), or undefined when it carries none. Messages are matched to the log by id. */
export function filterCompactionRequest(options, events, createUserMessage) {
  const inGroup = groupSeqs(events);
  const seqOf = new Map();
  for (const event of events) {
    const id = event.type === 'user/message' ? event.data?.id : event.data?.message?.id;
    if (id !== undefined) seqOf.set(id, event);
  }
  const messages = [];
  let changed = false, folding = false;
  for (const message of options.messages ?? []) {
    const event = seqOf.get(message?.id);
    const drop = message?.role !== 'system' && event !== undefined && inGroup.has(event.seq) && !keepOnFold(event);
    if (!drop) { messages.push(message); folding = false; continue; }
    changed = true;
    if (!folding) messages.push(createUserMessage({ content: [{ type: 'text', text: FOLD_LINE }], source: { kind: FOLD_SOURCE } }));
    folding = true;
  }
  return changed ? { ...options, messages } : undefined;
}

/** Turn bookkeeping for the root session: audience, merge bounds, the guard and the pause. */
export class GroupTurns {
  constructor({ stateDir, limits, log = () => {} }) {
    Object.assign(this, { limits, log });
    this.pauseFile = stateDir ? join(stateDir, 'group-pause.json') : undefined;
    this.current = undefined;
  }

  /** Whose turn it is now; unknown reads as a group (fail closed: the narrower rules). */
  audience() { return this.current?.audience ?? { kind: 'group', unknown: true }; }

  /** Split one step's claimed messages: what this turn keeps and what goes back (in order). */
  split(messages, step) {
    if (step === 1 || !this.current) {
      const first = messages.find(isGroup);
      const audience = first ? { kind: 'group', channel: first.source.channel, group_ref: first.source.group_ref,
        label: first.source.group_label } : { kind: 'owner' };
      this.current = { audience, calls: 0, merged: 0, ownerWaiting: false, questions: [] };
    }
    const turn = this.current, keep = [], back = [];
    for (const message of messages) {
      if (isNeutral(message)) { keep.push(message); continue; }
      if (turn.audience.kind === 'owner') { (isGroup(message) ? back : keep).push(message); continue; }
      const same = isGroup(message) && message.source.group_ref === turn.audience.group_ref
        && message.source.channel === turn.audience.channel;
      if (!same) { if (!isGroup(message)) turn.ownerWaiting = true; back.push(message); continue; }
      // Owner input already waiting stops LATER merges (a first step takes its questions).
      if ((step !== 1 && turn.ownerWaiting) || turn.merged >= this.limits.mergeMax) { back.push(message); continue; }
      turn.merged += 1;
      turn.questions.push(...(message.source.question_ids ?? []));
      keep.push(message);
    }
    return { keep, back };
  }

  /** The guard's verdict for one root tool call (a refusal string, or undefined). */
  guard(name) {
    const audience = this.audience();
    if (audience.kind === 'group') {
      if (!GROUP_TOOLS.has(name)) return groupTurnRefusal;
      if (this.current) this.current.calls += 1;
      if ((this.current?.calls ?? Infinity) > this.limits.turnToolCalls) return answerNow;
      return undefined;
    }
    if (FAMILY_TOOLS.has(name)) return familyRefusal;
    if (this.paused() && PAUSED_TOOLS.has(name)) return pausedRefusal;
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
