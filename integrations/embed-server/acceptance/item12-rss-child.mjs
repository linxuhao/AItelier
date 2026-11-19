/** One reading for item12-scale.mjs: open the store at <root> with vectors <on|off>, wait
 *  for the vector load, collect garbage, print process.memoryUsage() as JSON.
 *  Usage: node --expose-gc item12-rss-child.mjs <root> on|off */
import { MODEL, STORE, URL_, USER, sleep } from './common.mjs';

const { MemoryStore } = await import(STORE);
const [root, mode] = process.argv.slice(2);
const store = await MemoryStore.open({ root, userId: USER, ...(mode === 'on' ? { vector: { url: URL_, model: MODEL } } : {}) });
if (store.vector) await store.vector.whenLoaded();
await sleep(3000); // the worker's probe, the background BM25 months
for (let i = 0; i < 3; i++) { globalThis.gc(); await sleep(300); }
const m = process.memoryUsage();
console.log(JSON.stringify({ mode, ...m, vectors: store.vector?.mem.size ?? 0, blocks: store.vector?.mem.blocks.length ?? 0, entries: store.entries.size, vector: store.statusLine().vector }));
await store.close();
