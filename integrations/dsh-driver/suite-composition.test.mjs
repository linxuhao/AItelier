import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile, readdir } from 'node:fs/promises';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

// This file guards the suite's own composition, not its behaviour.
//
// History, because a guard whose reasons are forgotten gets deleted: on
// 2026-11-05 `npm test` listed ten files while the directory held thirteen, and
// the unlisted one was dsh-entry.test.mjs - the guards that a refusal must never
// become a skip. An hour later a NEW test file carried the hand-rolled install
// expression the project had just finished removing, and main went red on a clean
// checkout the moment it was merged. Both rules had been written down once and
// had decayed.
//
// Two adversarial non-author reviews then broke this file twice, and the repairs
// are the shape you see:
//   - it first matched the literal './node_modules/.bin/dsh', so `new URL(...)`,
//     `join('node_modules', '.bin', 'dsh')`, concatenation and `join('.bin','dsh')`
//     all walked through, while a comment mentioning the path was flagged. It
//     reddened the file that documents the rule and greened the file that breaks it.
//   - it then stripped comments with a regex, which deleted code whenever a string
//     contained `//`, and opened a false block comment on a `'**/*'` literal; and it
//     only looked within 400 characters, so a root kept in a constant at the top of
//     a file and used at the bottom was invisible.
//   - it read one directory with an exact `.mjs` suffix for the resolver rule while
//     discovering files recursively for the other one - so a listed .test.js one
//     level down could resolve the entry and stay green.
//   - it accepted "a glob in the script means discovery", which with one asterisk
//     silenced both observers and left another test file never running.
// Each of those is now a witness below. Do not simplify them back.

const here = dirname(fileURLToPath(import.meta.url));
const ALLOWED = new Set(['dsh-entry.mjs', 'dsh-entry.test.mjs', 'suite-composition.test.mjs']);
const TEST_FILE = /\.(test|spec)\.(mjs|cjs|js)$/i;
const HELPER = /\.(mjs|cjs|js)$/i;

// Walk the source once and return two views: `code` with comments removed and every
// string literal replaced by a hole, and `strings` with the text of those literals.
// A regex cannot do this correctly - a `//` inside a string is not a comment, a
// quote inside a comment is not a string, and `'**/*'` is not the start of a block
// comment. Both views matter: the path is spelled in strings, and the call that uses
// it is spelled in code.
const scan = source => {
  let code = '';
  let strings = '';
  let i = 0;
  while (i < source.length) {
    const ch = source[i];
    const next = source[i + 1];
    if (ch === '/' && next === '/') {
      while (i < source.length && source[i] !== '\n') i += 1;
      continue;
    }
    if (ch === '/' && next === '*') {
      i += 2;
      while (i < source.length && !(source[i] === '*' && source[i + 1] === '/')) i += 1;
      i += 2;
      continue;
    }
    if (ch === '"' || ch === "'" || ch === '`') {
      const quote = ch;
      i += 1;
      let literal = '';
      while (i < source.length && source[i] !== quote) {
        if (source[i] === '\\') {
          literal += source[i + 1] ?? '';
          i += 2;
          continue;
        }
        literal += source[i];
        i += 1;
      }
      i += 1;
      strings += `${literal}\n`;
      code += ' "" ';
      continue;
    }
    code += ch;
    i += 1;
  }
  return { code, strings };
};

// True when the file's own text (code and string literals, comments excluded) names
// all three parts of an install path. Deliberately NOT a window: a root kept in a
// constant at the top of a 6 KB file and used at the bottom is the same resolution
// as one written on a single line, and distance is free to anyone hiding from a
// window. The price is that a file mentioning all three separately is flagged; in
// this directory that is a file worth reading anyway.
const entryParts = text => {
  const { code, strings } = scan(text);
  const whole = `${code}\n${strings}`;
  return whole.includes('node_modules') && whole.includes('.bin') && /(^|[^\w-])dsh([^\w-]|$)/m.test(whole);
};

const walk = async (dir, pattern, out = []) => {
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name !== 'node_modules') await walk(full, pattern, out);
    } else if (entry.isFile() && pattern.test(entry.name)) {
      out.push(full);
    }
  }
  return out;
};

const scriptOf = async () => {
  const pkg = JSON.parse(await readFile(join(here, 'package.json'), 'utf8'));
  return String(pkg.scripts?.test ?? '').trim();
};

// How the script is allowed to name its files. Two shapes and no others: bare
// `node --test`, which discovers everything, or an explicit enumeration of files.
// A glob is REFUSED rather than trusted: one asterisk used to silence both
// observers while a listed test file never ran (review 2, leak 1). Refusing says
// what to write instead; a wrong guess here is worse than a red suite.
const checkScriptShape = script => {
  assert.ok(!/[;&|]/.test(script), `the test script chains commands or swallows failures (${script}). A suite that cannot fail is a suite that cannot report.`);
  assert.ok(/^node\s+--test(\s|$)/.test(script), `the test script must be a \`node --test\` invocation (${script}).`);
  const args = script.replace(/^node\s+--test\s*/, '').trim().split(/\s+/).filter(Boolean);
  const globs = args.filter(arg => /[*?[\]]/.test(arg));
  assert.deepEqual(globs, [], `the test script passes a glob (${globs.join(', ')}). A partial glob runs fewer files than it looks like: name every test file, or use bare \`node --test\` to discover them.`);
  return args;
};

test('every test file the runner would find is run by npm test', async () => {
  const script = await scriptOf();
  const args = checkScriptShape(script);
  const present = (await walk(here, TEST_FILE)).map(file => relative(here, file)).sort();
  assert.ok(present.length > 0, 'found no test files at all - the discovery is broken, not the suite');
  if (args.length === 0) return; // bare discovery runs whatever exists
  const listed = new Set(args.map(arg => relative(here, join(here, arg))));
  const missing = present.filter(file => !listed.has(file));
  assert.deepEqual(missing, [], `npm test does not run: ${missing.join(', ')}. Add them to package.json's "test" script - a file nobody runs is a guard nobody has.`);
  const absent = [...listed].filter(file => !present.includes(file)).sort();
  assert.deepEqual(absent, [], `npm test names files that do not exist: ${absent.join(', ')}`);
});

test('only the resolver and its unit test resolve the DSH entry', async () => {
  // Same discovery as the file-list rule, and no suffix filter: the previous version
  // scanned the top directory only and only .mjs, so the exact remediation its own
  // message prescribes - a new test file, maybe one level down, maybe .js - landed in
  // a blind spot.
  const files = (await walk(here, HELPER)).map(file => relative(here, file));
  const offenders = [];
  for (const rel of files) {
    const base = rel.split('/').pop();
    if (ALLOWED.has(base)) continue;
    if (entryParts(await readFile(join(here, rel), 'utf8'))) offenders.push(rel);
  }
  assert.deepEqual(offenders, [], `${offenders.join(', ')} resolve the DSH entry themselves. Call measuredEntryForTest(t) from dsh-entry.mjs instead - a test that picks its own install measures whatever shell invoked it.`);
});
