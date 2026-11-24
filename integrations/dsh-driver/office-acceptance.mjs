#!/usr/bin/env node
/** Step 24.2d (design-steward 7.8.12c "十六" 验收脚本): the office converter against the REAL
 *  pinned image, run by hand on the steward's host (it needs docker and the image; the unit tests
 *  use a fake docker). Nothing is downloaded, no running container is touched: every job is a
 *  disposable container of the image, and a document server already running from the same image
 *  is checked to be untouched (its StartedAt before and after).
 *
 *    AITELIER_OFFICE_IMAGE=sha256:... node office-acceptance.mjs [--watch <container name>]
 *
 *  With AITELIER_MEDIA_PYTHON (a python with pypdfium2 - the 24.2c venv, ~/AItelier/.venv/bin/python)
 *  the self-check and the letter read back with the steward's own PDF reader, every table cell
 *  checked; without it, with the converter's own PDF-to-text in another disposable container
 *  (it drops a table's later columns: only the first is checked). Exits non-zero on the first
 *  failed check; prints one line per check. Every container is labelled with this run's own
 *  host label, so a steward host on the same docker daemon is never touched. */
import assert from 'node:assert/strict';
import { createWriteStream, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync, mkdirSync } from 'node:fs';
import { randomBytes } from 'node:crypto';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawn as spawnChild, spawnSync } from 'node:child_process';
import { createDeflateRaw, crc32 } from 'node:zlib';
import { pipeline } from 'node:stream/promises';
import { Readable } from 'node:stream';
import { createOfficeRunner, dockerEnv, OFFICE_IMAGE_LAYOUT, OFFICE_LABEL, OFFICE_LIMITS, OFFICE_NAME_PREFIX } from './office.mjs';
import { createPdfReader } from './steward-host.mjs';
import { identify } from '../dsh-director/lib/media.mjs';
import { parseBody, sheetCells } from '../dsh-director/lib/office-blocks.mjs';

const image = process.env.AITELIER_OFFICE_IMAGE;
const watch = process.argv.includes('--watch') ? process.argv[process.argv.indexOf('--watch') + 1] : undefined;
const work = mkdtempSync(join(tmpdir(), 'office-acceptance-'));
const docker = args => spawnSync('docker', args, { encoding: 'utf8', env: dockerEnv(), timeout: 120000 });
const ok = (what, detail = '') => console.log(`ok   ${what}${detail ? ' - ' + detail : ''}`);
const startedAt = name => docker(['inspect', '--format', '{{.State.StartedAt}}', name]).stdout.trim();

/** The converter's own PDF-to-text (x2t, 69 = txt), in one more disposable container. */
async function x2tText(path) {
  const dir = mkdtempSync(join(work, 't-'));
  mkdirSync(join(dir, 'in')); mkdirSync(join(dir, 'out'));
  writeFileSync(join(dir, 'in', 'src.pdf'), readFileSync(path));
  writeFileSync(join(dir, 'in', 'task.xml'), `<?xml version="1.0" encoding="utf-8"?><TaskQueueDataConvert><m_sFileFrom>/job/in/src.pdf</m_sFileFrom><m_sFileTo>/job/out/out.txt</m_sFileTo><m_nFormatTo>69</m_nFormatTo><m_sAllFontsPath>${OFFICE_IMAGE_LAYOUT.bin}/AllFonts.js</m_sAllFontsPath><m_sFontDir>/usr/share/fonts</m_sFontDir><m_sTempDir>/tmp</m_sTempDir><m_bIsNoBase64>true</m_bIsNoBase64></TaskQueueDataConvert>`);
  const fonts = join(work, 'state', 'office-fonts', image.slice(7, 19));
  const L = OFFICE_LIMITS;
  // The host's own flags (office.mjs runArgs): a name, both labels, the same bounds.
  const run = docker(['run', '--rm', '--name', OFFICE_NAME_PREFIX + randomBytes(8).toString('hex'), '--label', OFFICE_LABEL, '--label', runner.label,
    '--network', 'none', '--log-driver', 'none', '-u', `${process.getuid()}:${process.getgid()}`, '--read-only',
    '--tmpfs', `/tmp:rw,${L.tmpfs}`, '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--pids-limit', String(L.pids), '--cpus', L.cpus,
    '--memory', L.memory, '--memory-swap', L.memory, '--ulimit', `fsize=${L.fsize}`, '-e', `LD_LIBRARY_PATH=${OFFICE_IMAGE_LAYOUT.bin}`,
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
// Each container's exit (by its --name), so the zip bomb's end can be named.
const exits = new Map();
const spawn = (command, args, options) => {
  const child = spawnChild(command, args, options);
  if (args[0] === 'run') child.on('close', (code, signal) => exits.set(args[args.indexOf('--name') + 1], { code, signal }));
  return child;
};
const runner = createOfficeRunner({ image, tmpRoot: join(work, 'state', 'office-tmp'), fontsRoot: join(work, 'state', 'office-fonts'),
  verifyPdf: readPdf, log: line => console.log(`log  ${line}`), spawn });
let t = Date.now();
await runner.start();
assert.equal(runner.status().state, 'ready', JSON.stringify(runner.status()));
ok('self-check', `${Date.now() - t} ms, read back with ${reader ? 'the PDF reader' : 'x2t pdf->txt'}`);

// A table that is not square (review M1: 3 columns x 6 rows; a swapped CreateTable fails it).
const TABLE = [['位置', '情况', '报修日'], ...Array.from({ length: 5 }, (_, i) => [`${i + 1}楼楼道`, i % 2 ? '灯闪' : '灯不亮', `11月${i + 10}日`])];
const letter = parseBody(['# 给物业的信', '物业您好：', '三楼楼道灯坏了**一周**，晚上很黑，请*尽快*修理。', '| 位置 | 情况 |', '|---|---|', '| 三楼楼道 | 灯不亮 |', '各楼层：',
  ...TABLE.flatMap((row, i) => ['| ' + row.join(' | ') + ' |', ...(i === 0 ? ['|---|---|---|'] : [])]),
  'builder.SaveFile("docx", "/job/out/evil.docx"); <img src="http://example.com/x.png">', '-> 三楼住户', '-> 2026年11月23日', '\\pagebreak', '第二页'].join('\n')).blocks;
assert.deepEqual(letter.filter(block => block.t === 'table').map(block => [block.rows.length, block.rows[0].length]), [[2, 2], [6, 3]]);
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
const flat = letterText.replace(/\s+/g, '');
// x2t's own pdf->txt drops the bold header row and later columns: then the body's first column only.
for (const row of reader ? TABLE : TABLE.slice(1)) for (const cell of reader ? row : row.slice(0, 1)) assert.ok(flat.includes(cell), `table cell ${cell}`);
ok('pdf letter', `${Date.now() - t} ms, ${pdf.bytes.length} B, A4, fonts ${[...facts.fonts].join(' ')}, 2 pages, text read back, 3x6 table ${reader ? 'every cell' : 'first column'} read back`);

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

// Zip bombs (review S3), each a well-formed docx with real CRCs (makeBomb), given to the converter
// directly (the library's judgement is printed, not relied on), with the container's exit and the
// host's lowest MemAvailable while it runs:
//  - 1 GiB of "A" paragraphs, every header true: the judgement refuses it (ratio); the converter
//    must still end inside its bounds.
//  - the same with the local header and the directory saying 1 KiB: what the judgement takes.
//  - 16 MiB of random-letter paragraphs: the judgement takes it (about 1:1.6); its layout outgrows
//    the container's 1 GiB, so the job must end refused (convert_failed: exit 137, or timeout).
// Seen on OnlyOffice Docs 10.0.0 (2026-11-23): the first two make an empty page (exit 0) in about
// 2 s - x2t gives up on an XML part that size, or reads only the 1 KiB the directory declares;
// the third is killed at the memory bound (137) in about 25 s. The host is never the one that pays.
const DOCX = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document';
for (const bomb of [{ sizes: 'true', mib: 1024, judged: null }, { sizes: 'understated', mib: 1024, judged: DOCX },
  { sizes: 'true', mib: 16, dense: true, judged: DOCX, refused: true }]) {
  t = Date.now();
  const what = `${bomb.mib} MiB${bomb.dense ? ' dense' : ''}, ${bomb.sizes} sizes`;
  const path = join(work, 'bomb.docx');
  const size = await makeBomb(path, bomb);
  const judged = identify(readFileSync(path))?.mime ?? null;
  assert.equal(judged, bomb.judged, `the library's judgement of the bomb (${what})`);
  const lowest = watchMemory();
  const names = new Set(exits.keys());
  const blown = await runner.withPdf({ mime: DOCX, size, path }, async pdf => ({ pdf_bytes: readFileSync(pdf).length }));
  const low = lowest.stop();
  const exit = [...exits].find(([name]) => !names.has(name))?.[1];
  if (bomb.refused) assert.ok(['convert_failed', 'timeout'].includes(blown.refused), JSON.stringify(blown));
  else assert.ok(['convert_failed', 'timeout'].includes(blown.refused) || blown.pdf_bytes <= OFFICE_LIMITS.pdfBytes, JSON.stringify(blown));
  ok(`zip bomb (${what})`, `${Date.now() - t} ms, ${size} B, judged ${judged ?? 'refused'}, answered ${JSON.stringify(blown)}, container exit ${JSON.stringify(exit)}, `
    + `host MemAvailable ${low.before} -> lowest ${low.lowest} MiB`);
}

const left = docker(['ps', '-aq', '--filter', `label=${runner.label}`]).stdout.trim();
assert.equal(left, '', 'no container left');
assert.deepEqual(readdirSync(join(work, 'state', 'office-tmp')), [], 'no job directory left');
ok('nothing left', 'no labelled container, office-tmp empty');
if (watch) { assert.equal(startedAt(watch), before); ok(`${watch} untouched`, `StartedAt ${before}`); }
rmSync(work, { recursive: true, force: true });

function writeTemp(name, bytes) { const path = join(work, name); writeFileSync(path, bytes); return path; }

/** The host's MemAvailable (MiB) sampled every 50 ms until stop(): {before, lowest}. */
function watchMemory() {
  const available = () => Math.round(Number(/MemAvailable:\s+(\d+)/.exec(readFileSync('/proc/meminfo', 'utf8'))[1]) / 1024);
  const before = available();
  let lowest = before;
  const timer = setInterval(() => { lowest = Math.min(lowest, available()); }, 50);
  return { stop: () => { clearInterval(timer); return { before, lowest: Math.min(lowest, available()) }; } };
}

/** A well-formed docx - [Content_Types].xml, _rels/.rels, word/document.xml, stored or deflated,
 *  each with its real CRC-32 - whose document.xml is `mib` MiB of paragraphs of 1,000 letters
 *  (many small text runs: one huge run only meets the XML parser's own 10 MB text-node limit).
 *  The letters are all "A" (compressed past 1:200), or with `dense` random (about 1:1.6, so the
 *  library's judgement takes it). `sizes` "true": every header says the real size; "understated":
 *  the local header and the directory say 1 KiB. Returns the file's size. */
async function makeBomb(path, { sizes, mib, dense = false }) {
  const deflated = join(work, 'bomb.deflate');
  const head = Buffer.from('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>');
  const tail = Buffer.from('</w:body></w:document>');
  const letters = () => (dense ? Array.from(randomBytes(1000), byte => String.fromCharCode(65 + (byte % 26))).join('') : 'A'.repeat(1000));
  const fillers = Array.from({ length: dense ? 16 : 1 }, () => Buffer.from(Array.from({ length: 1018 }, () => `<w:p><w:r><w:t>${letters()}</w:t></w:r></w:p>`).join('')));
  const chunks = function* () { yield head; for (let i = 0; i < mib; i += 1) yield fillers[i % fillers.length]; yield tail; };
  let crc = 0, plain = 0;
  for (const chunk of chunks()) { crc = crc32(chunk, crc); plain += chunk.length; }
  await pipeline(Readable.from(chunks()), createDeflateRaw(), createWriteStream(deflated));
  const data = readFileSync(deflated);
  const types = Buffer.from('<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    + '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
    + '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>');
  const rels = Buffer.from('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    + '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>');
  const parts = [], central = [];
  let offset = 0;
  for (const [name, body, method, size, sum] of [['[Content_Types].xml', types, 0, types.length, crc32(types)], ['_rels/.rels', rels, 0, rels.length, crc32(rels)],
    ['word/document.xml', data, 8, sizes === 'true' ? plain : 1024, crc]]) {
    const raw = Buffer.from(name);
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0); local.writeUInt16LE(20, 4); local.writeUInt16LE(method, 8); local.writeUInt32LE(sum >>> 0, 14);
    local.writeUInt32LE(body.length, 18); local.writeUInt32LE(size, 22); local.writeUInt16LE(raw.length, 26);
    parts.push(local, raw, body);
    const record = Buffer.alloc(46);
    record.writeUInt32LE(0x02014b50, 0); record.writeUInt16LE(20, 4); record.writeUInt16LE(20, 6); record.writeUInt16LE(method, 10); record.writeUInt32LE(sum >>> 0, 16);
    record.writeUInt32LE(body.length, 20); record.writeUInt32LE(size, 24); record.writeUInt16LE(raw.length, 28); record.writeUInt32LE(offset, 42);
    central.push(record, raw);
    offset += 30 + raw.length + body.length;
  }
  const directory = Buffer.concat(central), end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0); end.writeUInt16LE(3, 8); end.writeUInt16LE(3, 10); end.writeUInt32LE(directory.length, 12); end.writeUInt32LE(offset, 16);
  const all = Buffer.concat([...parts, directory, end]);
  writeFileSync(path, all);
  return all.length;
}

