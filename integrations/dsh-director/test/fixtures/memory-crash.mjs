/** A purge that dies by SIGKILL at a chosen point (memory-purge.test.mjs).
 *  argv: root userId killAt flags(0|1|recheck-fails) mode(purge|recover)
 *  Before dying it optionally writes one turn, which a purge sends to the spool.
 *  mode=purge (the default) opens a fresh store, writes two turns and runs the purge.
 *  mode=recover opens a store whose boot must recover an unfinished purge, so the kill
 *  lands inside the recovery (the hook has to be handed to open(), before boot runs). */
import { writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { MemoryStore } from '../../lib/memory/store.mjs';

const [root, userId, killAt, flags = '0', mode = 'purge'] = process.argv.slice(2);
const verifyFault = flags === 'recheck-fails'
  ? { verify: (() => { let calls = 0; return async () => (++calls === 2 ? ['injected at the re-check'] : []); })() }
  : {};
const die = async store => {
  if (flags === '1') await store.recordTurn('during', [{ role: 'owner', text: 'written during the purge' }]);
  process.kill(process.pid, 'SIGKILL');
  await new Promise(() => {});
};

if (mode === 'recover') {
  const faults = { ...verifyFault, [killAt]: async () => { process.kill(process.pid, 'SIGKILL'); await new Promise(() => {}); } };
  await MemoryStore.open({ root, userId, faults });
  process.exit(9); // the kill point was never reached
}

const store = await MemoryStore.open({ root, userId });
await store.recordTurn('keep', [{ role: 'owner', text: 'keep this entry' }]);
await store.recordTurn('gone', [{ role: 'owner', text: 'PURGE-ME secret account 4242' }]);
await store.flush();
// Step 21 (design 3.8.4 item 10): a vector file beside the repository (vectors are off here;
// the purge filters it all the same), so every kill point is also a vector-file case.
const v = Buffer.alloc(1024, 7).toString('base64');
writeFileSync(join(root, userId + '.vectors.jsonl'), '{"model":"bge-m3-q8_0","dims":1024,"quant":"int8","input_chars":1000}\n'
  + ['t-keep-0', 't-gone-0'].map(id => JSON.stringify({ id, v }) + '\n').join(''), { mode: 0o600 });
store.faults = { ...verifyFault, [killAt]: () => die(store) };
await store.purge(['t-gone-0']);
process.exit(9); // the kill point was never reached
