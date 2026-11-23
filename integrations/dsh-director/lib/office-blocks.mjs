/** Step 24.2d (design-steward 7.8.12c "四"): what make_document's arguments become before the
 *  host makes the file - pure functions, no I/O.
 *
 *  - parseBody(body): the small markdown subset of a pdf or docx, parsed HERE (never by the
 *    converter's own markdown or html import, which fetches links and images and draws tables
 *    without borders): `#`, `##`, `###` headings; one paragraph per line; `**bold**` and
 *    `*italic*`; `- ` and `1. ` list items (one level); a GFM table (a header row, a `|---|`
 *    row, then rows; at most office_cols columns and office_table_rows rows); `---` a rule; a
 *    line of its own `\pagebreak` a page break; a line starting `-> ` is right-aligned (a
 *    signature, a date). Everything else - links, images, html tags, code - is printed as the
 *    text it is: nothing is interpreted, nothing fetched.
 *  - sheetCells(rows): an xlsx's cells. A formula is kept only on the whitelist (SUM, AVERAGE,
 *    MIN, MAX, COUNT, ROUND over cells, ranges and numbers); a plain number written as text is a
 *    number; anything else is TEXT - a cell starting with = + - @ that is neither stays text, so
 *    a family's words never become a formula Excel or WPS would run (=HYPERLINK, =WEBSERVICE, DDE).
 *  - dataLine(data): the one line `var DATA = <JSON>;` the host puts before the Document Builder
 *    template - a data literal only: one line (JSON escapes every line break), with U+2028,
 *    U+2029 and < escaped too.
 *  - checkText(...): what a group turn's exit check reads (title, then body or every cell). */
import { MEDIA_LIMITS } from './media.mjs';

/** The formats make_document makes, with their table rows' mime and extension. */
export const OFFICE_FORMATS = Object.freeze({
  pdf: Object.freeze({ mime: 'application/pdf', ext: 'pdf' }),
  docx: Object.freeze({ mime: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', ext: 'docx' }),
  xlsx: Object.freeze({ mime: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', ext: 'xlsx' }),
});
/** Every generated document's one font: the CJK font the converter's image has (7.8.12c "三"). */
export const OFFICE_FONT = 'WenQuanYi Zen Hei';
export const TITLE_CHARS = 60;
export const SHEET_CHARS = 31;
const CONTROL = /[\u0000-\u001f\u007f-\u009f\u2028\u2029]/;
const NOT_IN_NAME = /[/\\:*?"<>|]/;

const refuse = (refused, reason) => ({ refused, reason });

/** A title: 1-60 characters, not only spaces, no control character and none of / \ : * ? " < > |
 *  (it names the file: `<title>.<ext>`). */
export function validTitle(title) {
  return typeof title === 'string' && title.trim() !== '' && [...title].length <= TITLE_CHARS
    && !CONTROL.test(title) && !NOT_IN_NAME.test(title) && title === title.trim();
}
/** A file name make_document gave a library row: a valid title and one of the formats' extensions. */
export function validDocumentName(name, ext) {
  return typeof name === 'string' && name.endsWith('.' + ext) && Object.values(OFFICE_FORMATS).some(f => f.ext === ext)
    && validTitle(name.slice(0, -(ext.length + 1)));
}

/** `**bold**` and `*italic*` in one line: runs [{text, b?, i?}]; an unmatched star is text. */
export function inlineRuns(line) {
  const runs = [];
  const pattern = /\*\*([^*]+?)\*\*|\*([^*\s][^*]*?)\*/g;
  let at = 0;
  for (const match of line.matchAll(pattern)) {
    if (match.index > at) runs.push({ text: line.slice(at, match.index) });
    runs.push(match[1] !== undefined ? { text: match[1], b: true } : { text: match[2], i: true });
    at = match.index + match[0].length;
  }
  if (at < line.length) runs.push({ text: line.slice(at) });
  return runs.length ? runs : [{ text: '' }];
}

const TABLE_RULE = /^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?$/;
/** One table row's cells: `| a | b |` -> ['a', 'b'] (the outer bars optional). */
const cellsOf = line => {
  let inner = line.trim();
  if (inner.startsWith('|')) inner = inner.slice(1);
  if (inner.endsWith('|')) inner = inner.slice(0, -1);
  return inner.split('|').map(cell => cell.trim());
};

/** The blocks of a pdf's or docx's body (the subset above), or {refused, reason}:
 *  invalid_request (not text, a table row with more cells than its header) or too_large (over
 *  office_body_chars characters, office_blocks blocks, a table over its rows or columns). */
export function parseBody(body, limits = MEDIA_LIMITS) {
  if (typeof body !== 'string' || body.trim() === '') return refuse('invalid_request', 'body is the document\'s text (markdown), not empty');
  if ([...body].length > limits.office_body_chars) return refuse('too_large', `body is at most ${limits.office_body_chars} characters`);
  const lines = body.replace(/\r\n?/g, '\n').split('\n');
  const blocks = [];
  for (let i = 0; i < lines.length; i += 1) {
    const raw = lines[i], line = raw.trim();
    if (line === '') continue;
    let match;
    if (line === '\\pagebreak') blocks.push({ t: 'pagebreak' });
    else if (/^-{3,}$/.test(line)) blocks.push({ t: 'hr' });
    else if ((match = /^(#{1,3})\s+(.*)$/.exec(line))) blocks.push({ t: 'h', level: match[1].length, runs: inlineRuns(match[2]) });
    else if ((match = /^->\s+(.*)$/.exec(line))) blocks.push({ t: 'p', align: 'right', runs: inlineRuns(match[1]) });
    else if ((match = /^[-*]\s+(.*)$/.exec(line))) blocks.push({ t: 'li', mark: '•', runs: inlineRuns(match[1]) });
    else if ((match = /^(\d{1,4})[.)]\s+(.*)$/.exec(line))) blocks.push({ t: 'li', mark: `${Number(match[1])}.`, runs: inlineRuns(match[2]) });
    else if (line.startsWith('|') && i + 1 < lines.length && TABLE_RULE.test(lines[i + 1].trim())) {
      const header = cellsOf(line);
      if (header.length > limits.office_cols) return refuse('too_large', `a table has at most ${limits.office_cols} columns`);
      const rows = [header];
      i += 2;
      for (; i < lines.length && lines[i].trim().startsWith('|'); i += 1) {
        const cells = cellsOf(lines[i]);
        if (cells.length > header.length) return refuse('invalid_request', `a table row has more cells than its header (${header.length})`);
        while (cells.length < header.length) cells.push('');
        rows.push(cells);
        if (rows.length > limits.office_table_rows) return refuse('too_large', `a table has at most ${limits.office_table_rows} rows`);
      }
      i -= 1;
      blocks.push({ t: 'table', rows: rows.map(row => row.map(inlineRuns)) });
    } else blocks.push({ t: 'p', runs: inlineRuns(line) });
    if (blocks.length > limits.office_blocks) return refuse('too_large', `body makes at most ${limits.office_blocks} blocks (lines, list items, tables)`);
  }
  return { blocks };
}

const CELL = String.raw`\$?[A-Z]{1,3}\$?[1-9]\d{0,6}`;
const ARG = String.raw`(?:${CELL}(?::${CELL})?|-?\d+(?:\.\d+)?)`;
const FORMULA = new RegExp(String.raw`^=(SUM|AVERAGE|MIN|MAX|COUNT|ROUND)\((?:${ARG}(?:,${ARG})*)?\)$`);
// No leading zero: 007 or a phone number starting 0 stays text.
const NUMBER = /^-?(?:0|[1-9]\d{0,14})(?:\.\d{1,15})?$/;
/** One cell: {f} a whitelisted formula, {n} a number (a finite number, or a string that is only
 *  one), {s} text - everything else. */
export function sheetCell(value) {
  if (typeof value === 'number') return Number.isFinite(value) ? { n: value } : undefined;
  if (typeof value !== 'string') return undefined;
  if (FORMULA.test(value)) return { f: value };
  if (NUMBER.test(value)) return { n: Number(value) };
  return { s: value };
}

const width = text => [...text].reduce((sum, ch) => sum + (/[ᄀ-￿]/.test(ch) ? 2 : 1), 0);
/** An xlsx's rows as cells and its column widths, or {refused, reason}: invalid_request (not
 *  rows of strings and numbers, no cell at all, a cell that is neither) or too_large (over
 *  office_rows rows, office_cols columns, office_cell_chars per cell, office_sheet_chars in all). */
export function sheetCells(rows, limits = MEDIA_LIMITS) {
  if (!Array.isArray(rows) || !rows.length || !rows.every(Array.isArray) || !rows.some(row => row.length))
    return refuse('invalid_request', 'rows is a list of rows, each a list of cells (text or numbers); the first row is the header');
  if (rows.length > limits.office_rows) return refuse('too_large', `at most ${limits.office_rows} rows`);
  const cells = [], widths = [];
  let total = 0;
  for (const row of rows) {
    if (row.length > limits.office_cols) return refuse('too_large', `at most ${limits.office_cols} columns`);
    const out = [];
    for (const [c, value] of row.entries()) {
      const cell = sheetCell(value);
      if (!cell) return refuse('invalid_request', 'each cell is text or a finite number');
      const text = String(value);
      const length = [...text].length;
      if (length > limits.office_cell_chars) return refuse('too_large', `a cell is at most ${limits.office_cell_chars} characters`);
      total += length;
      if (total > limits.office_sheet_chars) return refuse('too_large', `the cells are at most ${limits.office_sheet_chars} characters in all`);
      widths[c] = Math.max(widths[c] ?? 0, width(text));
      out.push(cell);
    }
    cells.push(out);
  }
  return { rows: cells, widths: Array.from(widths, w => Math.min(60, Math.max(6, (w ?? 0) + 2))) };
}

/** `var DATA = <JSON>;` - one line, a data literal whatever the strings hold. */
export function dataLine(data) {
  return 'var DATA = ' + JSON.stringify(data).replace(/[\u2028\u2029<]/g, ch => '\\u' + ch.charCodeAt(0).toString(16).padStart(4, '0')) + ';';
}

/** The text a group turn's exit check reads: the title, then the body or every row's cells
 *  (tab-separated, one row a line) - what the made file will show. */
export function checkText({ title, body, sheet, rows }) {
  if (Array.isArray(rows)) return [title, sheet ?? '', ...rows.map(row => row.map(String).join('\t'))].join('\n');
  return `${title}\n${body}`;
}
