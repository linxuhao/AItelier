/** The root session's conversation in the owner's memory, recorded by the plugin itself
 *  (overfit review 2026-11-24, 重-10). A host with the `memory` capability knows which
 *  messages its owner wrote and records each turn with memory.recordTurn (the backend adapter:
 *  steward-host.mjs syncConversation); a host without it has no conversation source of its own,
 *  so without this nothing the owner said would ever reach memory_search.
 *
 *  A turn is one DSH turn opened by an owner message: a user message whose source is DSH's own
 *  `user`, which is what DSH's client sends. Wakes, clock lines and snapshots are the plugin's
 *  (other source kinds) and are never recorded. The owner's message is written as it arrives
 *  (part `o`, as the backend adapter names it), each reply of the root in that turn as it is
 *  written (part: its message id); the store writes an entry once, so the same events seen
 *  again write nothing. Writes run one after another; a failure is logged and the next write
 *  still runs. */
import { createHash } from 'node:crypto';

const KEY = /^[A-Za-z0-9._:-]{1,64}$/;
/** A DSH message id as a memory id part: as it is when it fits, else its digest. */
const keyOf = id => KEY.test(String(id)) ? String(id) : createHash('sha256').update(String(id)).digest('hex').slice(0, 32);
const textOf = content => (Array.isArray(content) ? content : []).filter(block => block?.type === 'text')
  .map(block => block.text ?? '').join('\n');
const timeOf = event => {
  const at = new Date(event?.time ?? NaN);
  return Number.isNaN(at.getTime()) ? undefined : at.toISOString();
};

export class TurnRecorder {
  /** @param store the owner's MemoryStore; @param log (line) => void */
  constructor({ store, log = () => {} }) {
    this.store = store;
    this.log = log;
    this.turn = undefined;
    this.writes = Promise.resolve();
  }

  /** One session event of the root session. */
  observe(event) {
    if (event?.type === 'turn/start') this.turn = undefined;
    else if (event?.type === 'user/message') {
      const message = event.data;
      if (message?.source?.kind !== 'user' || message.id === undefined) return;
      this.turn = keyOf(message.id);
      this.#write(this.turn, { role: 'owner', part: 'o', at: timeOf(event), text: textOf(message.content) });
    } else if (event?.type === 'assistant/message' && this.turn) {
      const message = event.data?.message;
      if (message?.id === undefined) return;
      this.#write(this.turn, { role: 'steward', part: keyOf(message.id), at: timeOf(event), text: textOf(message.content) });
    }
  }

  #write(turn, part) {
    if (!part.text.trim()) return;
    this.writes = this.writes.then(() => this.store.recordTurn(turn, [part]))
      .catch(error => this.log('memory turn: ' + String(error?.message ?? error).slice(0, 300)));
  }

  /** Resolves once every write observed so far has settled. */
  settled() { return this.writes; }
}

/** The recorder this plugin runs for a host with these capabilities and this open store: none
 *  when the host has `memory` (it records the turns itself) or no store is open. */
export function turnRecorderFor({ capabilities, store, log }) {
  return store && !(capabilities ?? []).includes('memory') ? new TurnRecorder({ store, log }) : undefined;
}
