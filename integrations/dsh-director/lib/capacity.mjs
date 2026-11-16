/** Worker capacity (plugin step 9): the limits the director's dispatch guard uses, what
 *  a refused dispatch was, and the refusals still waiting for a free slot.
 *
 *  DSH refuses a new child at its maxActiveSubagents with ACTIVATION_LIMIT_REACHED and
 *  never queues. The guard refuses one step earlier, at capacity - reserve, and never
 *  above DSH's own limit: capacity is the smaller of the host's number and the one DSH
 *  runs with. A dispatch DSH refused anyway (a race the guard could not see) is treated
 *  as a refusal too. Refusals are kept on disk until the slot-free wake that names them
 *  went out, so a restart in between still wakes the director. */
import { readFile, writeFile, rename, mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';

/** The guard's numbers. `reserve` is at least 1: the slot step 6 resumes a worker in. */
export function capacityLimits({ hostLimit, dshLimit, reservedSlots }) {
  const host = Number.isSafeInteger(hostLimit) && hostLimit > 0 ? hostLimit : undefined;
  const dsh = Number.isSafeInteger(dshLimit) && dshLimit > 0 ? dshLimit : undefined;
  const capacity = Math.min(host ?? dsh ?? 8, dsh ?? Infinity);
  const reserved = Math.max(1, Number.isSafeInteger(reservedSlots) ? reservedSlots : 1);
  return { capacity, plugin_limit: host ?? null, dsh_limit: dsh ?? null, reserved, threshold: Math.max(0, capacity - reserved) };
}

const field = (text, name) => new RegExp(`\\b${name}["']?\\s*[:=]\\s*["']?([A-Za-z0-9][\\w.:/-]{0,119})`, 'i').exec(text)?.[1];
/** What a refused `subagent` call was: its description, and the attempt / node it names. */
export function describeDispatch(args = {}) {
  const prompt = typeof args.prompt === 'string' ? args.prompt : '';
  const out = { description: String(args.description ?? '').slice(0, 200) };
  const attempt = field(prompt, 'attempt_id'), node = field(prompt, 'node_key') ?? field(prompt, 'node_id');
  if (attempt) out.attempt_id = attempt;
  if (node) out.node = node;
  return out;
}

/** DSH's own capacity refusal, as the director's `subagent` tool result carries it.
 *
 *  What this rests on, read from the pinned DSH (dsh-subagent ActivationPool.reserve):
 *  the refusal is a thrown SubagentError, code ACTIVATION_LIMIT_REACHED, message
 *  "subagent limit reached (active child limit: N) ...", which the tool pipeline turns
 *  into a FAILED result (isError true, the message in `error` / `content`). A result
 *  that is not an error is never a refusal: a worker that started and printed the
 *  token is not one.
 *
 *  NOT witnessed end to end. The guard counts the dispatches it let through, so in the
 *  real-DSH shapes measured (sequential, and four parallel calls in one message) DSH's
 *  own refusal is unreachable from the director's `subagent` calls, and no real run
 *  has produced a by:'dsh' entry. The witnesses are the unit test of this function
 *  and the real-DSH proof that the director's tools/result events reach the listener
 *  that calls it (the in-flight count it also clears: the main run refuses at live 2,
 *  not 4). If DSH changes the result shape, this returns false and the refusal is not
 *  recorded - the dispatch itself still answered the director with DSH's error. */
export function isDshLimitRefusal(result) {
  if (!result?.isError) return false;
  const text = JSON.stringify([result.error ?? null, result.content ?? null]);
  return /ACTIVATION_LIMIT_REACHED|subagent limit reached/.test(text) && !text.includes('at_capacity');
}

/** Refused dispatches waiting for a free slot, persisted (`refused-dispatches.json`). */
export class RefusedDispatches {
  constructor(dir, log = () => {}) { this.dir = dir; this.log = log; this.file = join(dir, 'refused-dispatches.json'); this.list = []; this.writing = Promise.resolve(); }
  /** Read the refusals. An unreadable file is not "no refusals": it is moved aside
   *  (refused-dispatches.corrupt-<time>.json) and named, and the list starts empty. */
  async load() {
    await mkdir(this.dir, { recursive: true });
    let text;
    try { text = await readFile(this.file, 'utf8'); }
    catch (error) { if (error.code === 'ENOENT') return this; throw error; }
    let parsed;
    try { parsed = JSON.parse(text); } catch { parsed = undefined; }
    if (Array.isArray(parsed)) { this.list = parsed.filter(e => typeof e?.key === 'string'); return this; }
    const aside = join(this.dir, `refused-dispatches.corrupt-${new Date().toISOString().replace(/[:.]/g, '-')}-${randomUUID().slice(0, 8)}.json`);
    await rename(this.file, aside);
    this.log(`refused dispatches unreadable: moved to ${aside}; refusals recorded there get no slot-free wake`);
    return this;
  }
  get size() { return this.list.length; }
  entries() { return this.list.map(entry => ({ ...entry })); }
  #save() {
    const text = JSON.stringify(this.list);
    this.writing = this.writing.catch(() => {}).then(async () => {
      await writeFile(this.file + '.tmp', text, { mode: 0o600 }); await rename(this.file + '.tmp', this.file);
    });
    return this.writing;
  }
  /** Record one refusal (`by`: 'plugin' for the guard, 'dsh' for DSH's own refusal). */
  add(dispatch, by) {
    // A random key: a counter restarts with each instance and a clock repeats within
    // a millisecond, and a repeated key would drop a refusal no wake named.
    this.list.push({ ...dispatch, by, at: new Date().toISOString(), key: randomUUID() });
    return this.#save();
  }
  /** Drop the refusals a delivered wake named; refusals recorded since stay. */
  remove(taken) {
    const keys = new Set(taken.map(entry => entry.key));
    this.list = this.list.filter(entry => !keys.has(entry.key));
    return this.#save();
  }
}
