/** Plugin design step 2, D5: the split changes no request a model receives.
 *
 *  Reads the two recordings in fixtures/request-bodies/step2 (before = the unsplit
 *  runtime, after = dsh-director + the backend adapter) and asserts they are equal
 *  under the rules written next to them (normalize.json): equal outside the windows
 *  in which two runs of the same code already differ. ../tools/normalize-recording.mjs is the
 *  substitution the stored files went through; it says how to re-record each side. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { gunzipSync } from 'node:zlib';

const dir = new URL('./fixtures/request-bodies/step2/', import.meta.url);
const rules = JSON.parse(readFileSync(new URL('normalize.json', dir), 'utf8'));
const load = (side, name) => JSON.parse(gunzipSync(readFileSync(new URL(`${side}/${name}.json.gz`, dir))).toString());
const ignored = new Set(rules.compare_normalization.ignored_keys);
const strip = value => Array.isArray(value) ? value.map(strip)
  : value && typeof value === 'object' ? Object.fromEntries(Object.entries(value).filter(([k]) => !ignored.has(k)).map(([k, v]) => [k, strip(v)]))
    : value;
const kind = r => r.summary ? 'S' : r.child ? 'C' : 'R';
const streams = boot => boot.reduce((by, r) => ((by[kind(r)] ??= []).push(strip(r.options)), by), {});

test('D5: the first two smoke boots send the same requests before and after the split, stream by stream', () => {
  const before = load('before', 'smoke-requests'), after = load('after', 'smoke-requests');
  assert.equal(before.boots.length, 3); assert.equal(after.boots.length, 3);
  const allowed = rules.compare_normalization.allowed_length_differences;
  let compared = 0;
  for (const boot of [0, 1]) {
    const b = streams(before.boots[boot]), a = streams(after.boots[boot]);
    assert.deepEqual(Object.keys(a).sort(), Object.keys(b).sort(), `boot ${boot + 1}: request kinds`);
    for (const k of Object.keys(b)) {
      const exception = allowed.find(x => x.boot === boot + 1 && x.kind === k);
      // Equal lengths everywhere, except a listed stream with exactly its listed lengths.
      if (exception) assert.deepEqual([b[k].length, a[k].length], [exception.before, exception.after], `boot ${boot + 1} ${k}: listed lengths`);
      else assert.equal(a[k].length, b[k].length, `boot ${boot + 1} ${k}: ${b[k].length} vs ${a[k].length} requests`);
      const n = Math.min(b[k].length, a[k].length);
      assert.ok(n > 0, `boot ${boot + 1} ${k}: nothing to compare`);
      for (let i = 0; i < n; i++) { assert.deepEqual(a[k][i], b[k][i], `boot ${boot + 1} ${k}[${i}] differs`); compared++; }
    }
  }
  assert.ok(compared >= 42, 'too few requests compared: ' + compared);
});

test('D5: the third boot (second restart) presents the same system prompt, tools and model', () => {
  const pick = rec => { const r = rec.boots[2].find(x => kind(x) === 'R').options;
    return { provider: r.provider, model: r.model, tools: r.tools, system: r.messages[0] }; };
  assert.deepEqual(pick(load('after', 'smoke-requests')), pick(load('before', 'smoke-requests')));
});

test('D5: the completion bridge receives identical bodies (route, reasoning effort, tools, messages)', () => {
  const before = load('before', 'bridge-completions'), after = load('after', 'bridge-completions');
  assert.ok(before.completions.length > 0);
  assert.deepEqual(after.completions, before.completions);
  assert.ok(before.completions.filter(c => c.session_id === 'host-root').every(c => c.reasoning_effort === 'max'));
});
