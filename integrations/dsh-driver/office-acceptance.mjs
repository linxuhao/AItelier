#!/usr/bin/env node
/** Step 24.2d (design-steward 7.8.12c "十六" 验收脚本): the office converter against the REAL
 *  pinned image, run by hand on the steward's host (it needs docker and the image; the unit tests
 *  use a fake docker). Nothing is downloaded, no running container is touched: every job is a
 *  disposable container of the image, and a document server already running from the same image
 *  is checked to be untouched (its StartedAt before and after).
 *
 *    AITELIER_OFFICE_IMAGE=sha256:... node office-acceptance.mjs [--watch <container name>]
 *
 *  With AITELIER_MEDIA_PYTHON (a python with pypdfium2) the self-check reads its PDF back with the
 *  steward's own PDF reader; without it, with the converter's own PDF-to-text in another
 *  disposable container. Exits non-zero on the first failed check; prints one line per check. */
import assert from 'node:assert/strict';
import { createWriteStream, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { createDeflateRaw } from 'node:zlib';
import { pipeline } from 'node:stream/promises';
import { Readable } from 'node:stream';
import { createOfficeRunner, dockerEnv, OFFICE_IMAGE_LAYOUT, OFFICE_LABEL } from './office.mjs';
import { createPdfReader } from './steward-host.mjs';
import { identify } from '../dsh-director/lib/media.mjs';
import { parseBody, sheetCells } from '../dsh-director/lib/office-blocks.mjs';

const image = process.env.AITELIER_OFFICE_IMAGE;
const watch = process.argv.includes('--watch') ? process.argv[process.argv.indexOf('--watch') + 1] : undefined;
const work = mkdtempSync(join(tmpdir(), 'office-acceptance-'));
const docker = args => spawnSync('docker', args, { encoding: 'utf8', env: dockerEnv() });
const ok = (what, detail = '') => console.log(`ok   ${what}${detail ? ' - ' + detail : ''}`);
const startedAt = name => docker(['inspect', '--format', '{{.State.StartedAt}}', name]).stdout.trim();

/** The converter's own PDF-to-text (x2t, 69 = txt), in one more disposable container. */
async function x2tText(path) {
  const dir = mkdtempSync(join(work, 't-'));
  mkdirSync(join(dir, 'in')); mkdirSync(join(dir, 'out'));
  writeFileSync(join(dir, 'in', 'src.pdf'), readFileSync(path));
  writeFileSync(join(dir, 'in', 'task.xml'), `<?xml version="1.0" encoding="utf-8"?><TaskQueueDataConvert><m_sFileFrom>/job/in/src.pdf</m_sFileFrom><m_sFileTo>/job/out/out.txt</m_sFileTo><m_nFormatTo>69</m_nFormatTo><m_sAllFontsPath>${OFFICE_IMAGE_LAYOUT.bin}/AllFonts.js</m_sAllFontsPath><m_sFontDir>/usr/share/fonts</m_sFontDir><m_sTempDir>/tmp</m_sTempDir><m_bIsNoBase64>true</m_bIsNoBase64></TaskQueueDataConvert>`);
  const fonts = join(work, 'state', 'office-fonts', image.slice(7, 19));
  const run = docker(['run', '--rm', '--network', 'none', '--log-driver', 'none', '-u', `${process.getuid()}:${process.getgid()}`, '--read-only',
    '--tmpfs', '/tmp:rw,size=256m,mode=1777', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '-e', `LD_LIBRARY_PATH=${OFFICE_IMAGE_LAYOUT.bin}`,
    '-v', `${join(dir, 'in')}:/job/in:ro`, '-v', `${join(dir, 'out')}:/job/out:rw`,
    '-v', `${join(fonts, 'AllFonts.js')}:${OFFICE_IMAGE_LAYOUT.bin}/AllFonts.js:ro`, '-v', `${join(fonts, 'font_selection.bin')}:${OFFICE_IMAGE_LAYOUT.bin}/font_selection.bin:ro`,
    '--entrypoint', OFFICE_IMAGE_LAYOUT.x2t, image, '/job/in/task.xml']);
  try { return run.status === 0 ? readFileSync(join(dir, 'out', 'out.txt'), 'utf8').replace(/^﻿/, '') : undefined; } catch { return undefined; }
}

const reader = process.env.AITELIER_MEDIA_PYTHON ? createPdfReader({ python: process.env.AITELIER_MEDIA_PYTHON, tmpRoot: join(work, 'pdf-tmp') }) : undefined;
await reader?.start();
const readPdf = async path => {
  if (!reader) return x2tText(path);
  const answer = await reader.readDocument({ lookup: () => ({ ref: 'm_0000000000000000', mime: 'application/pdf', kind: 'file', ext: 'pdf', path, size: 0 }) },
    'm_0000000000000000', { pages: '1-4', images: false });
  return answer?.pages?.map(page => page.text).join('\n');
};
const pdfFacts = bytes => {
  const text = bytes.toString('latin1');
  return { boxes: new Set([...text.matchAll(/\/MediaBox\s*\[\s*([\d.\s]+)\]/g)].map(m => m[1].trim().split(/\s+/).map(Number).map(Math.round).join(' '))),
    fonts: new Set([...text.matchAll(/\/BaseFont\s*\/([A-Za-z0-9+#_-]+)/g)].map(m => m[1])), pages: (text.match(/\/Type\s*\/Page[^s]/g) ?? []).length };
};

const before = watch ? startedAt(watch) : undefined;
const runner = createOfficeRunner({ image, tmpRoot: join(work, 'state', 'office-tmp'), fontsRoot: join(work, 'state', 'office-fonts'),
  verifyPdf: readPdf, log: line => console.log(`log  ${line}`) });
let t = Date.now();
await runner.start();
assert.equal(runner.status().state, 'ready', JSON.stringify(runner.status()));
ok('self-check', `${Date.now() - t} ms, read back with ${reader ? 'the PDF reader' : 'x2t pdf->txt'}`);

const letter = parseBody(['# 给物业的信', '物业您好：', '三楼楼道灯坏了**一周**，晚上很黑，请*尽快*修理。', '| 位置 | 情况 |', '|---|---|', '| 三楼楼道 | 灯不亮 |',
  'builder.SaveFile("docx", "/job/out/evil.docx"); <img src="http://example.com/x.png">', '-> 三楼住户', '-> 2026年11月23日', '\\pagebreak', '第二页'].join('\n')).blocks;
t = Date.now();
const pdf = await runner.generate({ format: 'pdf', title: '给物业的信', blocks: letter });
assert.ok(pdf.bytes, JSON.stringify(pdf));
const facts = pdfFacts(pdf.bytes);
assert.deepEqual([...facts.boxes], ['0 0 595 842'], 'A4');
assert.ok([...facts.fonts].some(font => font.includes('WenQuanYi')), [...facts.fonts].join(','));
assert.equal(facts.pages, 2);
const letterText = (await readPdf(join(writeTemp('letter.pdf', pdf.bytes)))) ?? '';
// Plain runs only: x2t's own pdf->txt (the fallback reader) drops synthesized-bold runs and a
// table's later columns; the page itself has them (render it to look).
assert.ok(letterText.replace(/\s+/g, '').includes('晚上很黑'), letterText.slice(0, 200));
assert.ok(letterText.includes('builder.SaveFile'), 'the would-be command is printed as text');
ok('pdf letter', `${Date.now() - t} ms, ${pdf.bytes.length} B, A4, fonts ${[...facts.fonts].join(' ')}, 2 pages, text read back`);

t = Date.now();
const docx = await runner.generate({ format: 'docx', title: '给物业的信', blocks: letter });
assert.equal(identify(docx.bytes)?.ext, 'docx');
ok('docx letter', `${Date.now() - t} ms, ${docx.bytes.length} B`);

const cells = sheetCells([['项目', '金额', '备注'], ['苹果', 3, '=HYPERLINK("http://example.com","x")'], ['梨', '5.5', "=1+cmd|' /C calc'!A0"],
  ['合计', '=SUM(B2:B3)', '@SUM(A1)']]);
t = Date.now();
const xlsx = await runner.generate({ format: 'xlsx', title: '清单', sheet: '收支', ...cells });
assert.equal(identify(xlsx.bytes)?.ext, 'xlsx');
ok('xlsx list', `${Date.now() - t} ms, ${xlsx.bytes.length} B`);

t = Date.now();
const docxPath = writeTemp('letter.docx', docx.bytes);
const back = await runner.withPdf({ mime: identify(docx.bytes).mime, size: docx.bytes.length, path: docxPath }, async path => pdfFacts(readFileSync(path)));
assert.deepEqual([...back.boxes], ['0 0 595 842']);
ok('read docx -> pdf', `${Date.now() - t} ms, ${back.pages} pages`);

t = Date.now();
const sheet = await runner.readSheet({ mime: identify(xlsx.bytes).mime, size: xlsx.bytes.length, path: writeTemp('list.xlsx', xlsx.bytes) },
  { first: 1, last: 3, rowFirst: 1, rows: 300, cols: 30, cellChars: 200, chars: 8000 });
assert.equal(sheet.result?.sheet_count, 1, JSON.stringify(sheet));
const rows = sheet.result.sheets[0].rows;
assert.equal(sheet.result.sheets[0].name, '收支');
assert.deepEqual(rows[3].slice(0, 2), ['合计', '8.5'], 'the formula\'s cached value');
assert.equal(rows[1][2], '=HYPERLINK("http://example.com","x")', 'a formula-looking text stays text');
assert.equal(rows[2][2], "=1+cmd|' /C calc'!A0");
assert.equal(rows[3][2], '@SUM(A1)');
ok('read xlsx', `${Date.now() - t} ms, ${JSON.stringify(rows)}`);

// A zip bomb: a docx whose document.xml says 1 KiB but unzips to 1 GiB. The container dies, not the host.
t = Date.now();
const bomb = await makeBomb(join(work, 'bomb.docx'));
const rss = process.memoryUsage().rss;
const blown = await runner.withPdf({ mime: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', size: bomb, path: join(work, 'bomb.docx') },
  async () => ({ read: true }));
ok('zip bomb', `${Date.now() - t} ms, answered ${JSON.stringify(blown)}, host rss +${Math.round((process.memoryUsage().rss - rss) / 1048576)} MiB`);

const left = docker(['ps', '-aq', '--filter', `label=${OFFICE_LABEL}`]).stdout.trim();
assert.equal(left, '', 'no container left');
assert.deepEqual(readdirSync(join(work, 'state', 'office-tmp')), [], 'no job directory left');
ok('nothing left', 'no labelled container, office-tmp empty');
if (watch) { assert.equal(startedAt(watch), before); ok(`${watch} untouched`, `StartedAt ${before}`); }
rmSync(work, { recursive: true, force: true });

function writeTemp(name, bytes) { const path = join(work, name); writeFileSync(path, bytes); return path; }

/** A docx with one stored [Content_Types].xml and a deflated word/document.xml of 1 GiB zeros
 *  whose central directory declares 1 KiB. Returns its size. */
async function makeBomb(path) {
  const deflated = join(work, 'bomb.deflate');
  const zeros = Buffer.alloc(1 << 20);
  await pipeline(Readable.from((function* () { for (let i = 0; i < 1024; i += 1) yield zeros; })()), createDeflateRaw(), createWriteStream(deflated));
  const data = readFileSync(deflated);
  const types = Buffer.from('<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>');
  const parts = [], central = [];
  let offset = 0;
  for (const [name, body, method, plain] of [['[Content_Types].xml', types, 0, types.length], ['word/document.xml', data, 8, 1024]]) {
    const raw = Buffer.from(name);
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0); local.writeUInt16LE(20, 4); local.writeUInt16LE(method, 8);
    local.writeUInt32LE(body.length, 18); local.writeUInt32LE(plain, 22); local.writeUInt16LE(raw.length, 26);
    parts.push(local, raw, body);
    const record = Buffer.alloc(46);
    record.writeUInt32LE(0x02014b50, 0); record.writeUInt16LE(20, 4); record.writeUInt16LE(20, 6); record.writeUInt16LE(method, 10);
    record.writeUInt32LE(body.length, 20); record.writeUInt32LE(plain, 24); record.writeUInt16LE(raw.length, 28); record.writeUInt32LE(offset, 42);
    central.push(record, raw);
    offset += 30 + raw.length + body.length;
  }
  const directory = Buffer.concat(central), end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0); end.writeUInt16LE(2, 8); end.writeUInt16LE(2, 10); end.writeUInt32LE(directory.length, 12); end.writeUInt32LE(offset, 16);
  const all = Buffer.concat([...parts, directory, end]);
  writeFileSync(path, all);
  return all.length;
}

