/** A purge that dies by SIGKILL at a chosen point (memory-purge.test.mjs).
 *  argv: root userId killAt flags(0|1|recheck-fails) mode(purge|recover)
 *  Before dying it optionally writes one turn, which a purge sends to the spool.
 *  mode=purge (the default) opens a fresh store, writes two turns and runs the purge.
 *  mode=recover opens a store whose boot must recover an unfinished purge, so the kill
 *  lands inside the recovery (the hook has to be handed to open(), before boot runs). */
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
store.faults = { ...verifyFault, [killAt]: () => die(store) };
await store.purge(['t-gone-0']);
process.exit(9); // the kill point was never reached
