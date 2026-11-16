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
 *  What "already delivered" means: the same id WITH the same text. The key recorded is
 *  `id#sha256(text)`, so a structural id that recurs with new content (one child
 *  stopping twice on one plan, a batch still due at a later stop) is a new wake, and
 *  only an identical redelivery is held back.
 *
 *  The trade-off, stated: a key is CHECKED before delivery and RECORDED only after the
 *  delivery returned. A crash between the two delivers that wake a second time; it
 *  never loses one. Recording first would turn the same window into a lost wake,
 *  which is not acceptable for an owner's answer or a slot that freed.
 *
 *  A notice whose delivery fails at boot is kept and the others are still delivered:
 *  one bad entry never stops the director from starting. After `maxReplays` failed
 *  boots it is set aside (outbox-set-aside.jsonl) and named in the log.
 *
 *  Files (under the director's state directory): outbox.jsonl ({id, text} per line, id
 *  being the key), delivered.jsonl (one key per line), outbox-failures.json (failed
 *  boot replays per key). Compacted to the newest `keep` delivered keys and the
 *  still-undelivered outbox entries. */
import { appendFile, readFile, writeFile, rename, mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import { createHash } from 'node:crypto';

const lines = async file => {
  try { return (await readFile(file, 'utf8')).split('\n').filter(Boolean); }
  catch (error) { if (error.code === 'ENOENT') return []; throw error; }
};
/** The key a wake is recognised by: its id and a digest of its text. */
export const wakeKey = (id, text) => `${id}#${createHash('sha256').update(text).digest('hex').slice(0, 40)}`;

export class Outbox {
  /** @param dir - the state directory; @param deliver - async text => void (the raw steer). */
  constructor({ dir, deliver, keep = 1000, maxReplays = 3, log = () => {} }) {
    Object.assign(this, { dir, deliver, keep, maxReplays, log });
    this.outboxFile = join(dir, 'outbox.jsonl');
    this.deliveredFile = join(dir, 'delivered.jsonl');
    this.failuresFile = join(dir, 'outbox-failures.json');
    this.setAsideFile = join(dir, 'outbox-set-aside.jsonl');
    this.delivered = new Set();
    this.order = [];
    this.pending = new Map();
    this.failures = {};
    this.writes = 0;
  }
  /** Read the files: the delivered keys, and the outbox entries not yet delivered. */
  async load() {
    await mkdir(this.dir, { recursive: true });
    for (const key of await lines(this.deliveredFile)) this.#remember(key);
    for (const line of await lines(this.outboxFile)) {
      let entry;
      try { entry = JSON.parse(line); } catch { this.log(`outbox: unreadable line skipped`); continue; }
      if (typeof entry?.id === 'string' && typeof entry.text === 'string' && !this.delivered.has(entry.id)) this.pending.set(entry.id, entry.text);
    }
    try { this.failures = JSON.parse(await readFile(this.failuresFile, 'utf8')) ?? {}; }
    catch (error) { if (error.code !== 'ENOENT' && !(error instanceof SyntaxError)) throw error; }
    return this;
  }
  #remember(key) {
    if (this.delivered.has(key)) return;
    this.delivered.add(key); this.order.push(key);
    while (this.order.length > this.keep) this.delivered.delete(this.order.shift());
  }
  /** Whether this wake (id and text) was already delivered. */
  seen(id, text) { return this.delivered.has(wakeKey(id, text)); }
  async #mark(key) {
    this.#remember(key);
    this.pending.delete(key);
    await appendFile(this.deliveredFile, key + '\n', { mode: 0o600 });
    if (++this.writes % this.keep === 0) await this.compact();
  }
  /** A host event: delivered unless the same id with the same text was delivered before. */
  async deliverOnce(id, text) {
    if (!id) { await this.deliver(text); return { delivered: true }; }
    const key = wakeKey(id, text);
    if (this.delivered.has(key)) { this.log(`wake ${key} already delivered; not delivered again`); return { duplicate: true }; }
    await this.deliver(text);
    await this.#mark(key);
    return { delivered: true };
  }
  /** A runtime notice: recorded first, then delivered, then marked. */
  async send(id, text) {
    const key = wakeKey(id, text);
    if (this.delivered.has(key)) { this.log(`notice ${key} already delivered; not delivered again`); return { duplicate: true }; }
    if (!this.pending.has(key)) {
      this.pending.set(key, text);
      await appendFile(this.outboxFile, JSON.stringify({ id: key, text }) + '\n', { mode: 0o600 });
    }
    await this.deliver(text);
    await this.#mark(key);
    return { delivered: true };
  }
  /** At boot: deliver every recorded notice a previous process did not finish. A failed
   *  one is kept for the next boot (counted), the rest still go; after `maxReplays`
   *  failed boots it is set aside. Never throws for one entry. */
  async replay() {
    const replayed = [], failed = [], setAside = [];
    for (const [key, text] of [...this.pending]) {
      try {
        await this.deliver(text);
        await this.#mark(key);
        delete this.failures[key];
        replayed.push(key);
      } catch (error) {
        const count = (this.failures[key] ?? 0) + 1;
        if (count >= this.maxReplays) {
          await appendFile(this.setAsideFile, JSON.stringify({ id: key, text, error: String(error), at: new Date().toISOString() }) + '\n', { mode: 0o600 });
          this.pending.delete(key); delete this.failures[key];
          setAside.push(key);
          this.log(`outbox: notice ${key} failed delivery on ${count} boots and is set aside (outbox-set-aside.jsonl): ${error}`);
        } else {
          this.failures[key] = count;
          failed.push(key);
          this.log(`outbox: notice ${key} not delivered (boot ${count} of ${this.maxReplays}), kept: ${error}`);
        }
      }
    }
    await writeFile(this.failuresFile, JSON.stringify(this.failures), { mode: 0o600 });
    if (replayed.length || setAside.length) await this.compact();
    return { replayed, failed, setAside };
  }
  /** Rewrite both files to what is still needed. */
  async compact() {
    const write = async (file, text) => { await writeFile(file + '.tmp', text, { mode: 0o600 }); await rename(file + '.tmp', file); };
    await write(this.deliveredFile, this.order.map(key => key + '\n').join(''));
    await write(this.outboxFile, [...this.pending].map(([id, text]) => JSON.stringify({ id, text }) + '\n').join(''));
  }
}
