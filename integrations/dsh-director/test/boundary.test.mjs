/** The generic plugin names no backend (design §4 rule): no file of dsh-director
 *  outside the recorded fixtures and this test may say aitelier, call an /api/
 *  route or use a state_ tool name. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readdir, readFile } from 'node:fs/promises';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('..', import.meta.url));
const skip = new Set(['node_modules', 'fixtures', 'todo']);
async function files(dir) {
  const out = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    if (skip.has(entry.name)) continue;
    const path = join(dir, entry.name);
    if (entry.isDirectory()) out.push(...await files(path));
    else if (/\.(mjs|js|json|md|ts)$/.test(entry.name) && entry.name !== 'boundary.test.mjs') out.push(path);
  }
  return out;
}

test('dsh-director never names AItelier in code: no such string, no AITELIER_ variable, no /api/ route or state_ tool, no adapter import', async () => {
  // Comments may say where code was moved from (provenance); code may not couple:
  // every quoted string, every environment variable name and every import is checked.
  const found = [];
  const literal = /(['"`])((?:\\.|(?!\1)[^\\])*)\1/g;
  for (const file of await files(root)) {
    const lines = (await readFile(file, 'utf8')).split('\n');
    lines.forEach((line, i) => {
      const where = `${relative(root, file)}:${i + 1}: ${line.trim().slice(0, 160)}`;
      if (/\bAITELIER_[A-Z]/.test(line) || /(from\s+|import\(\s*)['"][^'"]*dsh-driver/.test(line)) found.push(where);
      for (const [, , text] of line.matchAll(literal)) if (/aitelier|\/api\/|\bstate_[a-z]/i.test(text)) { found.push(where); break; }
    });
  }
  assert.deepEqual(found, []);
});
