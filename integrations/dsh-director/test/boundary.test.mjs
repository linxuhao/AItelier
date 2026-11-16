/** The generic plugin names no backend (design §4 rule; review criterion
 *  "boundary-independent"): no byte of any shipped file - code, comments, tests,
 *  docs - may carry the backend's name in any case. The recorded request bodies
 *  under test/fixtures are exempt: they are what the backend's driver sent.
 *  Code must also not call an /api/ route, use a state_ tool name or import the
 *  adapter directory. The banned name is assembled at run time so this file does
 *  not contain it either. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readdir, readFile } from 'node:fs/promises';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('..', import.meta.url));
const skip = new Set(['node_modules', 'fixtures', '__pycache__']);
const name = ['ait', 'elier'].join('');
async function files(dir) {
  const out = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    if (skip.has(entry.name)) continue;
    const path = join(dir, entry.name);
    if (entry.isDirectory()) out.push(...await files(path));
    else if (entry.isFile()) out.push(path);
  }
  return out;
}

test('no shipped byte of dsh-director names the backend, and no code calls its routes, its state_ tools or the adapter', async () => {
  const found = [];
  const banned = new RegExp(name, 'i');
  const literal = /(['"`])((?:\\.|(?!\1)[^\\])*)\1/g;
  for (const file of await files(root)) {
    const text = await readFile(file, 'utf8');
    text.split('\n').forEach((line, i) => {
      const where = `${relative(root, file)}:${i + 1}: ${line.trim().slice(0, 160)}`;
      if (banned.test(line)) { found.push(where); return; }
      if (!/\.(mjs|js|ts)$/.test(file)) return;
      if (/(from\s+|import\(\s*)['"][^'"]*dsh-driver/.test(line)) { found.push(where); return; }
      for (const [, , value] of line.matchAll(literal)) if (/\/api\/|\bstate_[a-z]/.test(value)) { found.push(where); break; }
    });
  }
  assert.deepEqual(found, []);
});
