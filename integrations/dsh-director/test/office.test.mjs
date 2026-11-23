/** Step 24.2d (design-steward 7.8.12c "四", "六", "十六"): what office documents are to the
 *  plugin without a converter - the zip-directory type judgement (ooxmlType, on the table's
 *  container_samples, which the backend's tests use too), the media library taking a docx,
 *  xlsx or pptx and refusing what is not one, the kept rule, the wake line, and
 *  lib/office-blocks.mjs: the markdown subset, the xlsx cells and the DATA line. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import vm from 'node:vm';
import { attachmentLine, createMediaLibrary, identify, ooxmlType, sniff, MEDIA_LIMITS } from '../lib/media.mjs';
import { checkText, dataLine, inlineRuns, parseBody, sheetCell, sheetCells, shownText, validDocumentName, validTitle, OFFICE_FONT } from '../lib/office-blocks.mjs';
import { SAMPLES, TABLE, ooxml, zipFrom } from './fixtures/zip-samples.mjs';

const DOCX = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document';
const XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';
const PPTX = 'application/vnd.openxmlformats-officedocument.presentationml.presentation';

test('ooxmlType: every container sample of the table answers its mime', () => {
  assert.ok(SAMPLES.length >= 20);
  for (const sample of SAMPLES) assert.equal(ooxmlType(zipFrom(sample))?.mime ?? null, sample.mime, sample.about);
});

test('the first bytes alone never name an office type: sniff skips the container rows, the table\'s samples still hold', () => {
  for (const sample of TABLE.samples) assert.equal(sniff(Buffer.from(sample.hex, 'hex'))?.mime ?? null, sample.mime, sample.hex);
  assert.equal(sniff(ooxml('docx')), undefined);
  assert.equal(identify(ooxml('docx'))?.mime, DOCX);
  assert.equal(identify(ooxml('xlsx'))?.mime, XLSX);
  assert.equal(identify(ooxml('pptx'))?.mime, PPTX);
  // An OLE file (doc, xls, ppt, an encrypted OOXML) is no type at all.
  assert.equal(identify(Buffer.concat([Buffer.from('d0cf11e0a1b11ae1', 'hex'), Buffer.alloc(600)])), undefined);
  assert.equal(identify(Buffer.from('%PDF-1.7\n'))?.mime, 'application/pdf');
});

test('ooxmlType reads the directory only: no zlib in the library, and garbage after a real header is refused', () => {
  const source = readFileSync(new URL('../lib/media.mjs', import.meta.url), 'utf8');
  assert.doesNotMatch(source, /node:zlib|from 'zlib'|inflate/);
  assert.equal(ooxmlType(Buffer.concat([Buffer.from('504b0304', 'hex'), Buffer.alloc(200, 0xff)])), undefined);
  // An EOCD inside a comment that does not reach the end is not the EOCD.
  const good = ooxml('docx');
  assert.equal(ooxmlType(Buffer.concat([good, Buffer.from('trailing')])), undefined);
});

const library = () => createMediaLibrary({ root: mkdtempSync(join(tmpdir(), 'office-media-')), userId: 'owner' });
const once = bytes => async () => bytes;

test('the library takes a docx, xlsx and pptx as files of their own mime, and refuses a docm, a plain zip and an OLE file', async () => {
  const lib = library();
  let n = 0;
  const put = (bytes, extra = {}) => lib.ingest({ channel: 'wechat', messageRef: 'w' + ++n, index: 0, audience: 'owner', source: 'in', ...extra }, once(bytes));
  for (const [ext, mime] of [['docx', DOCX], ['xlsx', XLSX], ['pptx', PPTX]]) {
    const stored = await put(ooxml(ext));
    assert.equal(stored.mime, mime, ext);
    assert.equal(stored.kind, 'file');
    assert.ok(lib.lookup(stored.ref).path.endsWith('.' + ext));
  }
  assert.deepEqual(await put(zipFrom(SAMPLES.find(s => s.about.startsWith('a docm')))), { refused: 'type' });
  assert.deepEqual(await put(zipFrom(SAMPLES.find(s => s.about === 'a plain zip'))), { refused: 'type' });
  assert.deepEqual(await put(Buffer.concat([Buffer.from('d0cf11e0a1b11ae1', 'hex'), Buffer.alloc(600)])), { refused: 'type' });
  // A zip shorter than the first-bytes window is judged too (not taken as a type because it is short).
  assert.deepEqual(await put(Buffer.from('504b03040000', 'hex')), { refused: 'type' });
});

test('a zip is held to the file bound while it streams, before its directory is read', async () => {
  const lib = createMediaLibrary({ root: mkdtempSync(join(tmpdir(), 'office-media-')), userId: 'owner', limits: { file_bytes: 1000 } });
  const big = Buffer.concat([ooxml('docx').subarray(0, 64), Buffer.alloc(2000)]);
  assert.deepEqual(await lib.ingest({ channel: 'wechat', messageRef: 'w', index: 0, audience: 'owner', source: 'in' }, once(big)), { refused: 'too_large' });
});

test('kept: a group\'s un-mentioned docx is refused type, its PDF and image are kept as before', async () => {
  const lib = library();
  const group = 'group:wechat:r1';
  const keep = (bytes, ref) => lib.ingest({ channel: 'wechat', messageRef: ref, index: 0, audience: group, source: 'in', kept: true }, once(bytes));
  assert.deepEqual(await keep(ooxml('docx'), 'k1'), { refused: 'type' });
  assert.equal((await keep(Buffer.concat([Buffer.from('%PDF-1.7\n'), Buffer.alloc(100)]), 'k2')).mime, 'application/pdf');
  // The same docx sent WITH a mention (not kept) is the group's file.
  const addressed = await lib.ingest({ channel: 'wechat', messageRef: 'k3', index: 0, audience: group, source: 'in' }, once(ooxml('docx')));
  assert.equal(addressed.mime, DOCX);
  assert.ok(MEDIA_LIMITS.kept_mimes.includes('image/png') && !MEDIA_LIMITS.kept_mimes.some(mime => mime.includes('officedocument')));
});

test('the wake line names an office file by its extension, never the long mime', () => {
  assert.equal(attachmentLine({ kind: 'file', ref: 'm_0123456789abcdef', mime: DOCX, size: 86016 }), '[文件 m_0123456789abcdef，docx，84 KB]');
  assert.equal(attachmentLine({ kind: 'file', ref: 'm_0123456789abcdef', mime: 'application/pdf', size: 10 }), '[文件 m_0123456789abcdef，application/pdf，10 B]');
});

test('parseBody: each block of the subset', () => {
  const { blocks } = parseBody([
    '# 给物业的信', '## 小标题', '### 小小标题', '物业您好：', '三楼楼道灯**坏了一周**，请*尽快*修理。', '- 第一条', '* 第二条', '1. 甲', '2) 乙',
    '| 项目 | 说明 |', '|---|:---:|', '| 灯 | 坏了 |', '| 只有一格 |', '---', '\\pagebreak', '-> 三楼住户', '->   2026年11月23日', '',
  ].join('\n'));
  assert.deepEqual(blocks, [
    { t: 'h', level: 1, runs: [{ text: '给物业的信' }] },
    { t: 'h', level: 2, runs: [{ text: '小标题' }] },
    { t: 'h', level: 3, runs: [{ text: '小小标题' }] },
    { t: 'p', runs: [{ text: '物业您好：' }] },
    { t: 'p', runs: [{ text: '三楼楼道灯' }, { text: '坏了一周', b: true }, { text: '，请' }, { text: '尽快', i: true }, { text: '修理。' }] },
    { t: 'li', mark: '•', runs: [{ text: '第一条' }] },
    { t: 'li', mark: '•', runs: [{ text: '第二条' }] },
    { t: 'li', mark: '1.', runs: [{ text: '甲' }] },
    { t: 'li', mark: '2.', runs: [{ text: '乙' }] },
    { t: 'table', rows: [[[{ text: '项目' }], [{ text: '说明' }]], [[{ text: '灯' }], [{ text: '坏了' }]], [[{ text: '只有一格' }], [{ text: '' }]]] },
    { t: 'hr' },
    { t: 'pagebreak' },
    { t: 'p', align: 'right', runs: [{ text: '三楼住户' }] },
    { t: 'p', align: 'right', runs: [{ text: '2026年11月23日' }] },
  ]);
});

test('parseBody: links, images, html and code are printed as their text, nothing is interpreted', () => {
  const lines = ['[链接](http://example.com/x)', '![图](http://example.com/a.png)', '<img src="http://example.com/a.png">', '```js', 'alert(1)', '```',
    '#没有空格', '*', '**'];
  const { blocks } = parseBody(lines.join('\n'));
  assert.deepEqual(blocks.map(block => block.t), lines.map(() => 'p'));
  assert.deepEqual(blocks.map(block => block.runs.map(run => run.text).join('')), lines);
  assert.deepEqual(inlineRuns('a * b * c'), [{ text: 'a * b * c' }]);
});

test('parseBody: the bounds, and the same input gives the same blocks', () => {
  assert.equal(parseBody('').refused, 'invalid_request');
  assert.equal(parseBody(42).refused, 'invalid_request');
  assert.equal(parseBody('字'.repeat(MEDIA_LIMITS.office_body_chars + 1)).refused, 'too_large');
  assert.ok(parseBody('字'.repeat(MEDIA_LIMITS.office_body_chars)).blocks);
  assert.equal(parseBody(Array.from({ length: MEDIA_LIMITS.office_blocks + 1 }, () => 'x').join('\n')).refused, 'too_large');
  assert.ok(parseBody(Array.from({ length: MEDIA_LIMITS.office_blocks }, () => 'x').join('\n')).blocks);
  const header = n => '|' + Array.from({ length: n }, (_, i) => `c${i}`).join('|') + '|\n|' + Array.from({ length: n }, () => '---').join('|') + '|';
  assert.equal(parseBody(header(MEDIA_LIMITS.office_cols + 1)).refused, 'too_large');
  assert.ok(parseBody(header(MEDIA_LIMITS.office_cols)).blocks);
  assert.equal(parseBody(header(2) + '\n|a|b|c|').refused, 'invalid_request');
  // A table needs a real separator row (review N6): a bar and a --- per header cell. A bare
  // --- under a |-line is a rule; a separator with another cell count is no separator.
  assert.deepEqual(parseBody('|线\n---').blocks, [{ t: 'p', runs: [{ text: '|线' }] }, { t: 'hr' }]);
  assert.deepEqual(parseBody('| a | b |\n---').blocks.map(block => block.t), ['p', 'hr']);
  assert.deepEqual(parseBody('| a | b |\n|---|---|---|').blocks.map(block => block.t), ['p', 'p']);
  assert.deepEqual(parseBody('| a |\n|---|').blocks, [{ t: 'table', rows: [[[{ text: 'a' }]]] }]);
  assert.deepEqual(parseBody('| a | b |\n---|---').blocks.map(block => block.t), ['table']);
  const rows = n => header(1) + '\n' + Array.from({ length: n }, () => '|x|').join('\n');
  assert.equal(parseBody(rows(MEDIA_LIMITS.office_table_rows)).refused, 'too_large');
  assert.ok(parseBody(rows(MEDIA_LIMITS.office_table_rows - 1)).blocks);
  const body = '# 标题\n正文**粗**\n| a | b |\n|---|---|\n| 1 | 2 |';
  assert.equal(JSON.stringify(parseBody(body)), JSON.stringify(parseBody(body)));
});

test('the DATA line is one data literal whatever the text holds', () => {
  const nasty = '</script>"\\\u2028\u2029*/;Api.builder.SaveFile("docx", "/job/out/x.docx");\nbuilder.CloseFile();\r\n${x}`';
  const data = { format: 'pdf', title: 't', blocks: [{ t: 'p', runs: [{ text: nasty }] }] };
  const line = dataLine(data);
  assert.ok(!/[\n\r\u2028\u2029<]/.test(line), 'one line, no raw separator, no <');
  assert.ok(line.startsWith('var DATA = ') && line.endsWith(';'));
  // Parsed as a script, it is exactly one statement declaring DATA, equal to the input.
  const sandbox = {};
  vm.runInNewContext(line, sandbox);
  assert.deepEqual(Object.keys(sandbox), ['DATA']);
  assert.equal(JSON.stringify(sandbox.DATA), JSON.stringify(data));
  assert.equal(sandbox.DATA.blocks[0].runs[0].text, nasty);
});

test('xlsx cells: whitelisted formulas are formulas, numbers numbers, everything else text', () => {
  for (const f of ['=SUM(B2:B9)', '=AVERAGE(A1,A2,3)', '=ROUND(C3,2)', '=COUNT($A$1:$A$100)', '=MAX(A1:B2,C3)', '=MIN()'])
    assert.deepEqual(sheetCell(f), { f }, f);
  for (const s of ['=HYPERLINK("http://x","y")', "=1+cmd|' /C calc'!A0", '+SUM(A1)', '@SUM(A1)', '-2+3', '=sum(A1)', '=SUM(A1) ', '=WEBSERVICE("http://x")',
    '=SUM(A1:B2)+1', '=INDIRECT(A1)', '=HYPERLINK(A1,B1)', '=DDE(1,2)', '007', '0123', '1e5', '', '3.', 'abc'])
    assert.deepEqual(sheetCell(s), { s }, s);
  assert.deepEqual(sheetCell('-2'), { n: -2 });
  assert.deepEqual(sheetCell('3.5'), { n: 3.5 });
  assert.deepEqual(sheetCell('0'), { n: 0 });
  assert.deepEqual(sheetCell(42), { n: 42 });
  assert.equal(sheetCell(Infinity), undefined);
  assert.equal(sheetCell(null), undefined);
});

test('sheetCells: the bounds and the widths', () => {
  const L = MEDIA_LIMITS;
  assert.equal(sheetCells(undefined).refused, 'invalid_request');
  assert.equal(sheetCells([]).refused, 'invalid_request');
  assert.equal(sheetCells([[]]).refused, 'invalid_request');
  assert.equal(sheetCells([['a', { x: 1 }]]).refused, 'invalid_request');
  assert.equal(sheetCells(Array.from({ length: L.office_rows + 1 }, () => ['x'])).refused, 'too_large');
  assert.ok(sheetCells(Array.from({ length: L.office_rows }, () => ['x'])).rows);
  assert.equal(sheetCells([Array.from({ length: L.office_cols + 1 }, () => 'x')]).refused, 'too_large');
  assert.equal(sheetCells([['x'.repeat(L.office_cell_chars + 1)]]).refused, 'too_large');
  const perRow = L.office_cell_chars, rowsNeeded = Math.ceil((L.office_sheet_chars + 1) / perRow);
  assert.equal(sheetCells(Array.from({ length: rowsNeeded }, () => ['x'.repeat(perRow)])).refused, 'too_large');
  const made = sheetCells([['项目', 'amount'], ['苹果', 3], ['合计', '=SUM(B2:B2)']]);
  assert.deepEqual(made.rows, [[{ s: '项目' }, { s: 'amount' }], [{ s: '苹果' }, { n: 3 }], [{ s: '合计' }, { f: '=SUM(B2:B2)' }]]);
  assert.deepEqual(made.widths, [6, 13]);
});

test('titles and document names', () => {
  for (const ok of ['给物业的信', 'a', 'x'.repeat(60), '2026 年 账目']) assert.equal(validTitle(ok), true, ok);
  for (const bad of ['', ' ', 'x'.repeat(61), 'a/b', 'a\\b', 'a:b', 'a*b', 'a?b', 'a"b', 'a<b', 'a>b', 'a|b', 'a\nb', 'a\u0000b', ' a', 'a ', 42])
    assert.equal(validTitle(bad), false, JSON.stringify(bad));
  assert.equal(validDocumentName('给物业的信.pdf', 'pdf'), true);
  assert.equal(validDocumentName('给物业的信.pdf', 'docx'), false);
  assert.equal(validDocumentName('../x.pdf', 'pdf'), false);
  assert.equal(validDocumentName('x.png', 'png'), false);
  assert.equal(checkText({ title: 't', body: 'b' }), 't\nb');
  assert.equal(checkText({ title: 't', sheet: 's', rows: [['a', 1], ['b']] }), 't\ns\na\t1\nb');
  // Zero-width and bidi characters (review N5): a name that shows other than it is.
  for (const ch of ['\u200b', '\u200c', '\u200d', '\u200e', '\u200f', '\u202a', '\u202b', '\u202c', '\u202d', '\u202e', '\u2066', '\u2067', '\u2068', '\u2069', '\ufeff']) {
    assert.equal(validTitle(`账单${ch}fdp.exe`), false, ch.codePointAt(0).toString(16));
    assert.equal(validDocumentName(`账单${ch}.pdf`, 'pdf'), false, ch.codePointAt(0).toString(16));
  }
  assert.equal(validTitle('账单\u2010二'), true, 'a hyphen next to the range is fine');
});

test('the exit check reads a body as written and as shown: markup that splits a word is gone in the shown text (review S1)', () => {
  const body = '# AI**telier** 报告\n- 第*一*条\n1. 乙\n| 名 | 值 |\n|---|---|\n| A**I** | te*lier* |\n---\n\\pagebreak\n-> 签名';
  const { blocks } = parseBody(body);
  assert.equal(shownText(blocks), 'AItelier 报告\n• 第一条\n1. 乙\n名\t值\nAI\ttelier\n\n\n签名');
  assert.equal(checkText({ title: 't', body, blocks }), `t\n${body}\n${shownText(blocks)}`);
});

test('the Document Builder template: a table of R rows and C columns is CreateTable(R, C) in this build, every cell filled (review M1)', () => {
  // A model of OnlyOffice Docs 10.0.0's builder API as the real image answers it (a 2x5 table made
  // with CreateTable(2, 5) has 2 rows: "Row index 2 is out of bounds [0, 1]"). Everything else is a no-op.
  const source = readFileSync(new URL('../office/document.js', import.meta.url), 'utf8');
  const run = blocks => {
    const tables = [];
    const noop = () => new Proxy(function () {}, { get: (target, key) => (key === 'GetElementsCount' ? () => 1 : () => noop()) });
    const paragraph = cells => new Proxy({}, { get: (target, key) => (key === 'AddElement' ? element => cells.push(element.text) : () => noop()) });
    const Api = new Proxy({}, { get: (target, key) => {
      if (key === 'CreateRun') return () => { const run = { text: '' }; return new Proxy(run, { get: (r, k) => (k === 'text' ? r.text : k === 'AddText' ? t => { r.text += t; } : () => noop()) }); };
      if (key === 'CreateTable') return (rowCount, colCount) => {
        const grid = Array.from({ length: rowCount }, () => Array.from({ length: colCount }, () => []));
        tables.push(grid);
        return new Proxy({}, { get: (t, k) => (k !== 'GetRow' ? () => noop() : r => {
          if (!(r >= 0 && r < rowCount)) throw new Error(`Row index ${r} is out of bounds [0, ${rowCount - 1}]`);
          return { GetCell: c => {
            if (!(c >= 0 && c < colCount)) throw new Error(`Cell index ${c} is out of bounds [0, ${colCount - 1}]`);
            return new Proxy({}, { get: (x, kk) => (kk === 'GetContent' ? () => ({ GetElement: () => paragraph(grid[r][c]) }) : () => noop()) });
          } };
        }) });
      };
      return () => noop();
    } });
    vm.runInNewContext(`${dataLine({ format: 'pdf', title: 't', font: OFFICE_FONT, blocks })}\n${source}`, { Api, builder: { CreateFile: () => {} } });
    return tables.map(grid => grid.map(row => row.map(cell => cell.join(''))));
  };
  const table = (cols, rows) => [...Array.from({ length: rows }, (_, r) => '|' + Array.from({ length: cols }, (_, c) => `r${r}c${c}`).join('|') + '|')];
  for (const [cols, rows] of [[3, 6], [2, 5], [5, 2], [30, 200], [1, 1]]) {
    const lines = table(cols, rows);
    lines.splice(1, 0, '|' + Array.from({ length: cols }, () => '---').join('|') + '|');
    const { blocks, refused } = parseBody(lines.join('\n'), { ...MEDIA_LIMITS, office_body_chars: 1e6 });
    assert.equal(refused, undefined, `${cols}x${rows}`);
    const [grid] = run(blocks);
    assert.deepEqual(grid, Array.from({ length: rows }, (_, r) => Array.from({ length: cols }, (_, c) => `r${r}c${c}`)), `${cols} columns x ${rows} rows`);
  }
});
