/** Plugin step 4: driver.md / worker.md were split into dsh-director's generic
 *  director.md / worker.md and this adapter's state.md / worker-state.md. Content
 *  moved, it was not rewritten: every sentence of the originals (test-fixtures/
 *  *.step2.md) is in exactly one of the halves, verbatim, except the few reworded
 *  ones prompt-split.json lists (each with its original), the one sentence the
 *  spec keeps in both, and the additions it lists (added_to_generic) - nothing else. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8');
const spec = JSON.parse(read('./prompt-split.json'));
// The splitter the spec was written with: paragraphs, whitespace flattened, then
// sentence ends followed by a capital, a quote/paren/backtick or a tool name.
const sentences = text => text.trim().split(/\n\s*\n/).flatMap(paragraph =>
  paragraph.split(/\s+/).join(' ').split(/(?<=[.!?])\s+(?=[A-Z"(`]|run_pipeline|todo_|watch_|idle_)/));
const unreword = Object.fromEntries(Object.entries(spec.reworded).map(([original, generic]) => [generic, original]));

for (const [name, original, generic, local] of [
  ['driver', './test-fixtures/driver.step2.md', '../dsh-director/director.md', './state.md'],
  ['worker', './test-fixtures/worker.step2.md', '../dsh-director/worker.md', './worker-state.md']]) {
  test(`${name} prompt: the two halves hold exactly the original sentences`, () => {
    const before = sentences(read(original));
    const both = (spec.kept_in_both?.[name] ?? []).map(key => {
      const [p, s] = key.split('.').map(Number);
      return read(original).trim().split(/\n\s*\n/)[p - 1].split(/\s+/).join(' ')
        .split(/(?<=[.!?])\s+(?=[A-Z"(`]|run_pipeline|todo_|watch_|idle_)/)[s - 1];
    });
    const added = spec.added_to_generic?.[name] ?? [];
    const genericSentences = sentences(read(generic));
    for (const sentence of added) assert.ok(genericSentences.includes(sentence), 'listed addition missing: ' + sentence);
    const after = [...genericSentences.filter(s => !added.includes(s)).map(s => unreword[s] ?? s), ...sentences(read(local))];
    assert.deepEqual([...after].sort(), [...before, ...both].sort());
  });
}

test('every reworded sentence is in the generic half, and only those are reworded', () => {
  const generic = sentences(read('../dsh-director/director.md'));
  for (const reworded of Object.values(spec.reworded)) assert.ok(generic.includes(reworded), reworded);
  assert.equal(Object.keys(spec.reworded).length, 5);
});
