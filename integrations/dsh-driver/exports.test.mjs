/** The export surface of runtime.mjs across the dsh-director split, pinned: every
 *  name it exported before (test-fixtures/runtime-exports.before-split.json) is still
 *  exported, except `boot`, which moved to dsh-director/index.mjs with a different
 *  signature; and the only new names are the ones the file's header lists. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import * as runtime from './runtime.mjs';

const before = JSON.parse(readFileSync(new URL('./test-fixtures/runtime-exports.before-split.json', import.meta.url), 'utf8')).names;

test('runtime.mjs exports everything it exported before the split except boot, and adds only the named three', () => {
  const now = Object.keys(runtime);
  assert.deepEqual(before.filter(name => !now.includes(name)), ['boot']);
  assert.deepEqual(now.filter(name => !before.includes(name)).sort(), ['SESSION_ENV', 'aitelierHost', 'attemptWorkState']);
});
