#!/usr/bin/env node
/** `node media-gc.mjs <subdirectory>`: the daily media cleanup of one writer's subdirectory of the
 *  media root, for a writer whose own host is not running (step 24.2b, design-steward 7.8.12a
 *  "一个写者一个子目录" 清理): the DSH host's supervise loop runs it on a stopped director's
 *  `<root>/d-<driver_id>` before it starts that project's process, so there is still one writer
 *  at a time. It is the library's own cleanup() with that writer's limits (lib/media.mjs
 *  limitsFor: 30 days, then 1 GiB in all for a director, oldest first); nothing outside the
 *  subdirectory is read or touched. A subdirectory that does not exist has nothing to clean.
 *
 *  Prints one JSON line {removed, evicted}; logs (a mime and a size at most) go to stderr.
 *  Exit 0 when cleaned, 2 on a usage error, 1 when the cleanup failed. */
import { basename, dirname, resolve } from 'node:path';
import { createMediaLibrary } from './lib/media.mjs';

const args = process.argv.slice(2);
if (args.length !== 1 || !args[0]) {
  process.stderr.write('usage: node media-gc.mjs <media-root>/<subdirectory>\n');
  process.exit(2);
}
const home = resolve(args[0]);
const userId = basename(home);
let library;
try {
  // No limits given: the library takes this writer's own (limitsFor(userId)).
  library = createMediaLibrary({ root: dirname(home), userId,
    log: (event, fields) => process.stderr.write(JSON.stringify({ event, ...fields }) + '\n') });
} catch (error) {
  process.stderr.write(`media-gc: ${error?.message ?? error}\n`);
  process.exit(2);
}
try {
  const { removed, evicted } = library.cleanup();
  process.stdout.write(JSON.stringify({ removed, evicted }) + '\n');
} catch (error) {
  process.stderr.write(`media-gc: cleanup failed: ${String(error?.code ?? 'error').slice(0, 40)}\n`);
  process.exit(1);
}
