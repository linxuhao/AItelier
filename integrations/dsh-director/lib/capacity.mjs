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

/** DSH's own capacity refusal, as the director's `subagent` tool result carries it. */
export function isDshLimitRefusal(result) {
  if (!result?.isError) return false;
  const text = JSON.stringify([result.error ?? null, result.content ?? null]);
  return /ACTIVATION_LIMIT_REACHED|subagent limit reached/.test(text) && !text.includes('at_capacity');
}

/** Refused dispatches waiting for a free slot, persisted (`refused-dispatches.json`). */
export class RefusedDispatches {
  constructor(dir) { this.dir = dir; this.file = join(dir, 'refused-dispatches.json'); this.list = []; this.seq = 0; this.writing = Promise.resolve(); }
  async load() {
    await mkdir(this.dir, { recursive: true });
    try { const parsed = JSON.parse(await readFile(this.file, 'utf8')); if (Array.isArray(parsed)) this.list = parsed.filter(e => typeof e?.key === 'string'); }
    catch (error) { if (error.code !== 'ENOENT' && !(error instanceof SyntaxError)) throw error; }
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
    this.list.push({ ...dispatch, by, at: new Date().toISOString(), key: `${Date.now()}-${process.pid}-${++this.seq}` });
    return this.#save();
  }
  /** Drop the refusals a delivered wake named; refusals recorded since stay. */
  remove(taken) {
    const keys = new Set(taken.map(entry => entry.key));
    this.list = this.list.filter(entry => !keys.has(entry.key));
    return this.#save();
  }
}
