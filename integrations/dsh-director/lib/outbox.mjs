/** dsh-director's outbox and delivered-id log (plugin step 7).
 *
 *  Two kinds of wake reach the director:
 *  - the host's events (work changed, a message arrived, an owner's answer). The host
 *    owns their source and replays from its own cursor, so the director only needs to
 *    recognise one it already delivered: `deliverOnce(id, text)`.
 *  - the runtime's own notices (a watched job ended, a child handed a plan back, a
 *    batch is ready, a worker slot freed, workers were resumed). Nobody else holds
 *    them, so they are written to the outbox BEFORE delivery and replayed after a
 *    restart until delivered: `send(id, text)` and, at boot, `replay()`.
 *
 *  The trade-off, stated: an id is CHECKED before delivery and RECORDED only after the
 *  delivery returned. A crash between the two delivers that wake a second time; it
 *  never loses one. Recording first would turn the same window into a lost wake,
 *  which is not acceptable for an owner's answer or a slot that freed.
 *
 *  Files (under the director's state directory): outbox.jsonl ({id, text} per line)
 *  and delivered.jsonl (one id per line). Both are compacted to the newest `keep`
 *  delivered ids and the still-undelivered outbox entries. */
import { appendFile, readFile, writeFile, rename, mkdir } from 'node:fs/promises';
import { join } from 'node:path';

const lines = async file => {
  try { return (await readFile(file, 'utf8')).split('\n').filter(Boolean); }
  catch (error) { if (error.code === 'ENOENT') return []; throw error; }
};

export class Outbox {
  /** @param dir - the state directory; @param deliver - async text => void (the raw steer). */
  constructor({ dir, deliver, keep = 1000, log = () => {} }) {
    Object.assign(this, { dir, deliver, keep, log });
    this.outboxFile = join(dir, 'outbox.jsonl');
    this.deliveredFile = join(dir, 'delivered.jsonl');
    this.delivered = new Set();
    this.order = [];
    this.pending = new Map();
    this.writes = 0;
  }
  /** Read both files: the delivered ids, and the outbox entries not yet delivered. */
  async load() {
    await mkdir(this.dir, { recursive: true });
    for (const id of await lines(this.deliveredFile)) this.#remember(id);
    for (const line of await lines(this.outboxFile)) {
      let entry;
      try { entry = JSON.parse(line); } catch { this.log(`outbox: unreadable line skipped`); continue; }
      if (typeof entry?.id === 'string' && typeof entry.text === 'string' && !this.delivered.has(entry.id)) this.pending.set(entry.id, entry.text);
    }
    return this;
  }
  #remember(id) {
    if (this.delivered.has(id)) return;
    this.delivered.add(id); this.order.push(id);
    while (this.order.length > this.keep) this.delivered.delete(this.order.shift());
  }
  /** Whether a wake with this id was already delivered. */
  seen(id) { return this.delivered.has(id); }
  async #mark(id) {
    this.#remember(id);
    this.pending.delete(id);
    await appendFile(this.deliveredFile, id + '\n', { mode: 0o600 });
    if (++this.writes % this.keep === 0) await this.compact();
  }
  /** A host event: delivered unless its id was delivered before. */
  async deliverOnce(id, text) {
    if (!id) { await this.deliver(text); return { delivered: true }; }
    if (this.delivered.has(id)) { this.log(`wake ${id} already delivered; not delivered again`); return { duplicate: true }; }
    await this.deliver(text);
    await this.#mark(id);
    return { delivered: true };
  }
  /** A runtime notice: recorded first, then delivered, then marked. */
  async send(id, text) {
    if (this.delivered.has(id)) { this.log(`notice ${id} already delivered; not delivered again`); return { duplicate: true }; }
    if (!this.pending.has(id)) {
      this.pending.set(id, text);
      await appendFile(this.outboxFile, JSON.stringify({ id, text }) + '\n', { mode: 0o600 });
    }
    await this.deliver(text);
    await this.#mark(id);
    return { delivered: true };
  }
  /** At boot: deliver every recorded notice a previous process did not finish. */
  async replay() {
    const replayed = [];
    for (const [id, text] of [...this.pending]) {
      await this.deliver(text);
      await this.#mark(id);
      replayed.push(id);
    }
    if (replayed.length) await this.compact();
    return replayed;
  }
  /** Rewrite both files to what is still needed. */
  async compact() {
    const write = async (file, text) => { await writeFile(file + '.tmp', text, { mode: 0o600 }); await rename(file + '.tmp', file); };
    await write(this.deliveredFile, this.order.map(id => id + '\n').join(''));
    await write(this.outboxFile, [...this.pending].map(([id, text]) => JSON.stringify({ id, text }) + '\n').join(''));
  }
}
