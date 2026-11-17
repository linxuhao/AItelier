/** A purge that dies by SIGKILL at a chosen point (memory-purge.test.mjs).
 *  argv: root userId killAt writeDuring(0|1)
 *  Before dying it optionally writes one turn, which a purge sends to the spool. */
import { MemoryStore } from '../../lib/memory/store.mjs';

const [root, userId, killAt, writeDuring] = process.argv.slice(2);
const store = await MemoryStore.open({ root, userId });
await store.recordTurn('keep', [{ role: 'owner', text: 'keep this entry' }]);
await store.recordTurn('gone', [{ role: 'owner', text: 'PURGE-ME secret account 4242' }]);
await store.flush();
store.faults = {
  [killAt]: async () => {
    if (writeDuring === '1') await store.recordTurn('during', [{ role: 'owner', text: 'written during the purge' }]);
    process.kill(process.pid, 'SIGKILL');
    await new Promise(() => {});
  },
};
await store.purge(['t-gone-0']);
process.exit(9); // the kill point was never reached
