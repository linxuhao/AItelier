/** Turn a raw DSH_DIRECTOR_RECORD_DIR recording into the stored form next to this
 *  file: drop options.signal, replace the run's scratch directory by <SCRATCH> and
 *  every 8-4-4-4-12 UUID by <UUID>, sort keys, gzip. This is the exact substitution
 *  the stored before/ and after/ files went through (normalize.json, recorded_normalization).
 *
 *    node normalize-recording.mjs <raw .json> <out .json.gz>
 *
 *  Re-recording: "after" from any commit at or after the split, with
 *  DSH_DIRECTOR_RECORD_DIR=<dir> node --test runtime.smoke.test.mjs in integrations/dsh-driver;
 *  "before" from commit 9dc85a7886 (the unsplit runtime plus the recorder), whose smoke
 *  test still inserts only the old runtime. */
import { readFileSync, writeFileSync } from 'node:fs';
import { gzipSync } from 'node:zlib';

const UUID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/g;
const sorted = value => Array.isArray(value) ? value.map(sorted)
  : value && typeof value === 'object' ? Object.fromEntries(Object.keys(value).sort().map(k => [k, sorted(value[k])])) : value;

export function normalize(raw) {
  const text = JSON.stringify(raw).split(raw.scratch).join('<SCRATCH>').replace(UUID, '<UUID>');
  const value = JSON.parse(text);
  for (const boot of value.boots ?? []) for (const request of boot) delete request.options.signal;
  return sorted(value);
}

if (process.argv[1] && import.meta.url.endsWith(process.argv[1].split('/').pop())) {
  const [input, output] = process.argv.slice(2);
  if (!input || !output) { console.error('usage: node normalize-recording.mjs <raw .json> <out .json.gz>'); process.exit(2); }
  writeFileSync(output, gzipSync(JSON.stringify(normalize(JSON.parse(readFileSync(input, 'utf8'))))));
}
