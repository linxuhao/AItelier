/** Step 24.4b (design-steward 7.8.24 "十" 1, 4, 5; "十四" 24.4b): the steward's calculate tool -
 *  its parameters and closed refusal set, which refs a turn may give it (read_document's covers
 *  matrix: a group's kept file only in that group's turn), the group budget (3 a turn, 30 an hour
 *  per group; an owner turn not counted; given back when the program never ran), the outside
 *  content mark, and the group guard. The host's sandbox is a fake that keeps host.d.ts's
 *  media.calculate contract; the real one is tested on the adapter side (calc.test.mjs there). */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { GroupTurns, GROUP_SOURCE, GROUP_TOOLS, groupTurnRefusal } from '../lib/group-turns.mjs';
import { StewardCatalog } from '../lib/catalog.mjs';
import { GROUP_LIMITS } from '../lib/channels.mjs';
import { registerCalcTool, CALCULATE_REASONS, CALC_TOOL_LIMITS, CALCULATE_DESCRIPTION } from '../lib/calc-tool.mjs';

const ref = n => 'm_' + String(n).repeat(16).slice(0, 16);
const OWNER_CSV = ref(1), GROUP_CSV = ref(2), KEPT_PNG = ref(3), OTHER_CSV = ref(4), OTHER_KEPT = ref(5), OWNER_PHOTO = ref(6);
const R1 = 'group:wechat:r1', R2 = 'group:wechat:r2';
const row = (r, audience, extra = {}) => ({ ref: r, kind: 'file', mime: 'application/pdf', ext: 'pdf', path: `/media/${r}.pdf`, size: 10, audience, ...extra });
const ROWS = new Map([[OWNER_CSV, row(OWNER_CSV, 'owner')], [GROUP_CSV, row(GROUP_CSV, R1)], [KEPT_PNG, row(KEPT_PNG, R1, { kept: true, kind: 'image' })],
  [OTHER_CSV, row(OTHER_CSV, R2)], [OTHER_KEPT, row(OTHER_KEPT, R2, { kept: true })], [OWNER_PHOTO, row(OWNER_PHOTO, 'owner', { kind: 'image' })]]);

let ids = 0;
const ownerMessage = () => ({ id: 'o' + ++ids, role: 'user', content: [{ type: 'text', text: '算一下' }], source: { kind: 'x' } });
const groupMessage = groupRef => ({ id: 'g' + ++ids, role: 'user', content: [{ type: 'text', text: '@管家' }],
  source: { kind: GROUP_SOURCE, channel: 'wechat', group_ref: groupRef, group_label: '家庭群', question_ids: ['q' + ids], sender_refs: ['w'] } });
const RESULT = { exit_code: 0, timed_out: false, stdout: '1024\n', stdout_truncated: false, stderr_tail: '', files: [] };

/** calculate over a real GroupTurns whose open turn is the owner's or group `r1`'s / `r2`'s. */
function world({ audience = 'owner', answer = async () => RESULT, noHost = false } = {}) {
  const turns = new GroupTurns({ stateDir: mkdtempSync(join(tmpdir(), 'calc-tool-')), limits: GROUP_LIMITS });
  const split = who => turns.split([who === 'owner' ? ownerMessage() : groupMessage(who)], 1, 't' + ++ids);
  split(audience);
  const calls = [];
  const media = { resolve: async r => ROWS.get(r),
    ...(noHost ? {} : { calculate: async (request, label, signal) => { calls.push([request, label]); return answer(request, label, signal); } }) };
  const catalog = new StewardCatalog({ host: {}, stateDir: mkdtempSync(join(tmpdir(), 'calc-tool-catalog-')), rootSession: 'root',
    audience: () => turns.audience(), registryNames: () => [] });
  let clock = 5_000_000;
  const tools = new Map();
  registerCalcTool({ tools: { register: tool => tools.set(tool.name, tool) } }, { defineTool: t => t, jsonOutput: {}, media, catalog, turns,
    now: () => clock });
  const tool = tools.get('calculate');
  const run = (args, session = 'root') => tool.execute(args, { agent: { session: { id: session } }, callId: 'c' + ++ids, token: 'k' + ids,
    signal: AbortSignal.timeout(5000) });
  return { run, tool, turns, calls, catalog, nextTurn: (who = audience) => split(who), tick: ms => { clock += ms; } };
}

test('calculate: its parameters, its description and its closed refusal set; it may run in a group turn', () => {
  const w = world();
  assert.deepEqual(Object.keys(w.tool.parameters), ['code', 'files']);
  assert.equal(w.tool.parameters.code.required, true);
  assert.deepEqual(w.tool.parameters.files, { type: 'array', items: { type: 'string' }, description: w.tool.parameters.files.description });
  assert.deepEqual(Object.keys(CALCULATE_REASONS).sort(), ['busy', 'calc_unavailable', 'group_calc_budget', 'invalid_request', 'not_visible_in_this_turn',
    'output_too_large', 'timeout', 'unknown_ref']);
  assert.deepEqual([CALC_TOOL_LIMITS.codeBytes, CALC_TOOL_LIMITS.files, CALC_TOOL_LIMITS.groupTurnCalls, CALC_TOOL_LIMITS.groupHourCalls],
    [32768, 4, 3, 30]);
  assert.equal(w.tool.description, CALCULATE_DESCRIPTION);
  for (const words of [/no network/, /send_media/, /\/job\/in\/f1\.<ext>/, /original file names are not kept/, /png only/, /16 KiB/])
    assert.match(CALCULATE_DESCRIPTION, words);
  assert.ok(GROUP_TOOLS.has('calculate'));
  assert.match(groupTurnRefusal, /make_document, calculate and your public connection tools/);
  const g = new GroupTurns({ stateDir: mkdtempSync(join(tmpdir(), 'calc-guard-')), limits: GROUP_LIMITS });
  g.split([groupMessage('r1')], 1);
  assert.equal(g.guard('calculate'), undefined, 'the group guard lets calculate through');
});

test('invalid_request: no code, empty code, code over 32 KiB of UTF-8, files not a list of at most 4 refs - nothing reaches the host', async () => {
  const w = world();
  for (const args of [{}, { code: '' }, { code: '  \n' }, { code: 42 }, { code: '数'.repeat(10923) }, { code: 'x', files: 'm_1111111111111111' },
    { code: 'x', files: [OWNER_CSV, OWNER_CSV, OWNER_CSV, OWNER_CSV, OWNER_CSV] }, { code: 'x', files: ['../etc/passwd'] }, { code: 'x', files: [1] }]) {
    const answer = await w.run(args);
    assert.equal(answer.refused, 'invalid_request', JSON.stringify(args).slice(0, 80));
    assert.equal(answer.reason, CALCULATE_REASONS.invalid_request);
  }
  assert.equal(w.calls.length, 0);
  // Exactly 32 KiB is taken; 4 refs are taken.
  assert.equal((await w.run({ code: 'x'.repeat(32768) })).exit_code, 0);
  assert.equal((await w.run({ code: 'x', files: [OWNER_CSV, OWNER_CSV, OWNER_PHOTO, GROUP_CSV] })).exit_code, 0);
  // Another session than the root: not_available, like every media tool.
  assert.equal((await w.run({ code: 'x' }, 'child')).refused, 'not_available');
});

test('which refs a turn may give: read_document\'s covers matrix (a kept file only in its own group\'s turn)', async () => {
  const cases = [
    ['owner', OWNER_CSV, true], ['owner', GROUP_CSV, true], ['owner', KEPT_PNG, false], ['owner', OTHER_KEPT, false],
    ['r1', GROUP_CSV, true], ['r1', KEPT_PNG, true], ['r1', OWNER_CSV, false], ['r1', OTHER_CSV, false], ['r1', OTHER_KEPT, false],
    ['r2', OTHER_KEPT, true], ['r2', KEPT_PNG, false],
  ];
  for (const [audience, file, allowed] of cases) {
    const w = world({ audience });
    const answer = await w.run({ code: 'print(1)', files: [file] });
    if (allowed) {
      assert.equal(answer.exit_code, 0, `${audience} ${file}`);
      assert.deepEqual(w.calls[0], [{ code: 'print(1)', refs: [file] }, audience === 'owner' ? 'owner' : `group:wechat:${audience}`]);
    } else {
      assert.equal(answer.refused, 'not_visible_in_this_turn', `${audience} ${file}`);
      assert.equal(w.calls.length, 0);
    }
  }
  const w = world();
  assert.equal((await w.run({ code: 'x', files: ['m_ffffffffffffffff'] })).refused, 'unknown_ref');
});

test('the answer: the host\'s result as it is; files come back as refs, not sent; a host without the sandbox is calc_unavailable', async () => {
  const files = [{ ref: ref('a'), mime: 'image/png', bytes: 2048 }];
  const w = world({ audience: 'r1', answer: async () => ({ ...RESULT, exit_code: 1, stderr_tail: 'ZeroDivisionError', files, files_skipped: 2 }) });
  const answer = await w.run({ code: '1/0' });
  assert.deepEqual(answer, { exit_code: 1, timed_out: false, stdout: '1024\n', stdout_truncated: false, stderr_tail: 'ZeroDivisionError', files,
    files_skipped: 2, next: '文件已存为本对话的，没有发出：要发就用 send_media({ref})' });
  assert.equal(w.calls[0][1], R1, 'the files are stored as this group\'s');
  const none = world({ noHost: true });
  assert.deepEqual(await none.run({ code: 'x' }), { refused: 'calc_unavailable', reason: CALCULATE_REASONS.calc_unavailable, detail: 'no_image_configured' });
  // The host's refusals pass on; anything else is calc_unavailable; a detail only of the closed set.
  for (const [given, code, detail] of [[{ refused: 'busy' }, 'busy'], [{ refused: 'timeout' }, 'timeout'], [{ refused: 'output_too_large' }, 'output_too_large'],
    [{ refused: 'calc_unavailable', detail: 'selfcheck_failed' }, 'calc_unavailable', 'selfcheck_failed'],
    [{ refused: 'calc_unavailable', detail: '/secret/path' }, 'calc_unavailable'], [{ refused: 'weird' }, 'calc_unavailable'], [undefined, 'calc_unavailable'],
    [{ exit_code: 'x' }, 'calc_unavailable']]) {
    const v = world({ answer: async () => given });
    const out = await v.run({ code: 'x' });
    assert.deepEqual(out, { refused: code, reason: CALCULATE_REASONS[code], ...(detail ? { detail } : {}) }, JSON.stringify(given));
  }
  const thrown = world({ answer: async () => { throw new Error('boom'); } });
  assert.equal((await thrown.run({ code: 'x' })).refused, 'calc_unavailable');
});

test('a run with input files marks the turn as having read outside content; one without, or a refusal, marks nothing', async () => {
  const w = world();
  await w.run({ code: 'x' });
  assert.equal(w.catalog.externalSeen, false);
  const refused = world({ answer: async () => ({ refused: 'busy' }) });
  await refused.run({ code: 'x', files: [OWNER_CSV] });
  assert.equal(refused.catalog.externalSeen, false);
  await w.run({ code: 'x', files: [OWNER_CSV] });
  assert.equal(w.catalog.externalSeen, true);
});

test('a group\'s budget: at most 3 a turn and 30 an hour per group, group_calc_budget past either; an owner turn is never counted', async () => {
  const w = world({ audience: 'r1' });
  for (let i = 0; i < 3; i += 1) assert.equal((await w.run({ code: 'x' })).exit_code, 0);
  const fourth = await w.run({ code: 'x' });
  assert.deepEqual(fourth, { refused: 'group_calc_budget', reason: CALCULATE_REASONS.group_calc_budget });
  assert.equal(w.calls.length, 3, 'the fourth never reached the host');
  // Next turns of the same group: 30 in the hour, then refused; another group has its own 30.
  for (let turn = 1; turn < 10; turn += 1) {
    w.nextTurn('r1');
    for (let i = 0; i < 3; i += 1) assert.equal((await w.run({ code: 'x' })).exit_code, 0, `turn ${turn} call ${i}`);
  }
  assert.equal(w.calls.length, 30);
  w.nextTurn('r1');
  assert.equal((await w.run({ code: 'x' })).refused, 'group_calc_budget', 'the 31st in the hour');
  w.nextTurn('r2');
  assert.equal((await w.run({ code: 'x' })).exit_code, 0, 'another group counts apart');
  // An owner turn in between: never counted, never refused.
  w.nextTurn('owner');
  for (let i = 0; i < 10; i += 1) assert.equal((await w.run({ code: 'x' })).exit_code, 0);
  // An hour after the first: the group may again.
  w.tick(CALC_TOOL_LIMITS.hourMs);
  w.nextTurn('r1');
  assert.equal((await w.run({ code: 'x' })).exit_code, 0);
});

test('a group\'s count is given back when the program never ran (busy, calc_unavailable), kept when it ran or timed out', async () => {
  let next = { refused: 'busy' };
  const w = world({ audience: 'r1', answer: async () => next });
  for (let i = 0; i < 5; i += 1) assert.equal((await w.run({ code: 'x' })).refused, 'busy');
  next = { refused: 'calc_unavailable', detail: 'docker_unavailable' };
  assert.equal((await w.run({ code: 'x' })).refused, 'calc_unavailable');
  assert.equal(w.turns.open().calcCalls, 0, 'nothing counted');
  next = { refused: 'timeout' };
  for (let i = 0; i < 3; i += 1) assert.equal((await w.run({ code: 'x' })).refused, 'timeout');
  assert.equal((await w.run({ code: 'x' })).refused, 'group_calc_budget', 'a timeout ran: counted');
});
