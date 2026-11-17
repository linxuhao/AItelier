/** Purge step 2 (design 3.6): rewrite a `git fast-export` stream so that no blob holds a
 *  purged entry. In every blob, a JSON line naming a purged id is
 *   - dropped when it is a log entry (it carries `text`), and
 *   - reduced to {id, at, purged: true, batch} when it is a forgotten.jsonl record
 *     (its reason and the hash of the text go too).
 *  Every other byte is kept; commit messages are untouched (they only ever name ids). */

export function filterLine(line, purged) {
  if (!line.startsWith('{')) return line;
  let row;
  try { row = JSON.parse(line); } catch { return line; }
  if (!row || typeof row !== 'object' || !purged.has(row.id)) return line;
  if (typeof row.text === 'string') return null;
  return JSON.stringify({ id: row.id, at: row.at, purged: true, ...(row.batch ? { batch: row.batch } : {}) });
}

export function filterBlob(content, purged, lineFilter = filterLine) {
  const text = content.toString('utf8');
  if (![...purged].some(id => text.includes(id))) return content;
  const lines = text.split('\n');
  const kept = [];
  for (const line of lines) {
    const out = lineFilter(line, purged);
    if (out !== null) kept.push(out);
  }
  return Buffer.from(kept.join('\n'), 'utf8');
}

/** Filter a whole fast-export stream (Buffer in, Buffer out). `lineFilter` exists for the
 *  tests that prove the verification catches a wrong filter. */
export function filterExportStream(stream, purged, lineFilter = filterLine) {
  const parts = [];
  let pos = 0, lastCommand = '';
  while (pos < stream.length) {
    const nl = stream.indexOf(0x0a, pos);
    const end = nl === -1 ? stream.length : nl;
    const line = stream.subarray(pos, end).toString('utf8');
    if (line.startsWith('data ')) {
      const n = Number(line.slice(5));
      if (!Number.isInteger(n) || n < 0) throw new Error('fast-export stream: unsupported data header ' + JSON.stringify(line.slice(0, 40)));
      const body = stream.subarray(end + 1, end + 1 + n);
      if (body.length !== n) throw new Error('fast-export stream: truncated data');
      const out = lastCommand === 'blob' ? filterBlob(body, purged, lineFilter) : body;
      parts.push(Buffer.from(`data ${out.length}\n`), out);
      pos = end + 1 + n;
      continue;
    }
    if (line && !line.startsWith('mark ') && !line.startsWith('original-oid ')) lastCommand = line.split(' ')[0];
    parts.push(stream.subarray(pos, nl === -1 ? end : end + 1));
    pos = nl === -1 ? end : end + 1;
  }
  return Buffer.concat(parts);
}
