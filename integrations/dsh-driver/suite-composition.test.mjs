import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile, readdir } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

// This file guards the suite's own composition, not its behaviour.
//
// Two things went wrong in one evening (2026-11-05) that no other test could see:
//
// 1. `npm test` listed nine files while the directory held thirteen. Among the
//    unlisted ones was dsh-entry.test.mjs - the only place the guards live that a
//    refusal must never become a skip. A regression there would have been green in
//    the command a reader runs.
// 2. The criterion "no test hand-rolls the DSH entry" was verified by a grep taken
//    once. An hour later a NEW test file was merged that carried the old
//    expression, and main went red on a clean checkout. The grep was a snapshot of
//    the files that existed when somebody ran it.
//
// So: the file list must equal the directory, and the hand-rolled expression may
// appear only in the resolver and its own unit test.

const here = dirname(fileURLToPath(import.meta.url));
const ENTRY = './node_modules/.bin/dsh';
const ALLOWED = new Set(['dsh-entry.mjs', 'dsh-entry.test.mjs', 'suite-composition.test.mjs']);

const listedFiles = async () => {
  const scripts = JSON.parse(await readFile(join(here, 'package.json'), 'utf8')).scripts ?? {};
  return new Set((scripts.test ?? '').split(/\s+/).filter(f => f.endsWith('.mjs')));
};

test('every *.test.mjs in the directory is run by npm test', async () => {
  const present = (await readdir(here)).filter(f => f.endsWith('.test.mjs'));
  const listed = await listedFiles();
  const missing = present.filter(f => !listed.has(f)).sort();
  assert.deepEqual(missing, [], `npm test does not run: ${missing.join(', ')}. Add them to package.json's "test" script - a file nobody runs is a guard nobody has.`);
  const absent = [...listed].filter(f => !present.includes(f)).sort();
  assert.deepEqual(absent, [], `npm test names files that do not exist: ${absent.join(', ')}`);
});

test('only the resolver and its unit test name the entry expression', async () => {
  const files = (await readdir(here)).filter(f => f.endsWith('.mjs'));
  const offenders = [];
  for (const file of files) {
    if (ALLOWED.has(file)) continue;
    const text = await readFile(join(here, file), 'utf8');
    if (text.includes(ENTRY)) offenders.push(file);
  }
  assert.deepEqual(offenders, [], `${offenders.join(', ')} resolve the DSH entry themselves. Call measuredEntryForTest(t) from dsh-entry.mjs instead - a test that picks its own install measures whatever shell invoked it.`);
});
