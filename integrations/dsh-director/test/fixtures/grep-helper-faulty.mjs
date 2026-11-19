/** A stand-in for the resident grep helper that is broken on purpose (memory-grep-helper.test.mjs
 *  passes it as the helper script): the store must never fail a search over it.
 *
 *  argv[2]: `die`    - answers the first request with a record already streamed and half of a
 *                      second one, then exits mid-request;
 *           `silent` - reads requests and answers none of them, without spinning (a wedged
 *                      helper: an epoll wait on stdin until it is killed).
 *  Both leave on stdin's end, like the real one. */
import { writeSync } from 'node:fs';

const mode = process.argv[2];
const partial = 'log/2026-11.jsonl\u0000 1\u0000' + JSON.stringify({ id: 't-fake-0', at: '2026-11-01T00:00:00Z', role: 'owner', text: 'needle from a broken helper' }) + '\n'
  + 'log/2026-11.jsonl\u0000 2\u0000{"id":"t-fake-1","text":"needle, torn';

process.stdin.on('data', () => {
  if (mode !== 'die') return; // silent: hold the request and answer nothing
  writeSync(1, partial);
  process.exit(1);
});
process.stdin.on('end', () => process.exit(0));
process.stdin.on('close', () => process.exit(0));
