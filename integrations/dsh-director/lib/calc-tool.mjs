/** The steward's calculate tool (step 24.4b, design-steward 7.8.24 "十").
 *
 *  calculate({code, files?}) runs a Python program in THIS host's sandbox (host.media.calculate:
 *  a container per call with no network, the host's own bounds) and answers {exit_code,
 *  timed_out, stdout, stdout_truncated, stderr_tail, files: [{ref, mime, bytes}]}. Registered
 *  whatever the host has (a host without the sandbox answers calc_unavailable at call time), on
 *  the root session only (`mine`).
 *
 *  - code: the program, at most CALC_TOOL_LIMITS.codeBytes of UTF-8. files: at most
 *    CALC_TOOL_LIMITS.files media refs, each one this turn covers - read_document's rule
 *    (lib/media.mjs covers: an owner turn the owner's and the groups' mentioned ones, a group turn
 *    its own group's, a kept one included; a kept one never from the owner's turn or another
 *    group). The host copies them in, in order, as f1.<ext>, f2.<ext>, ...: their names never go in.
 *  - The files the program saves come back stored in the library as THIS turn's (source
 *    generated), not sent: send_media({ref}) sends one. Which types are kept is the host's
 *    (an owner turn: png, jpg, pdf, xlsx; a group turn: png); the others are counted in
 *    files_skipped.
 *  - Neither outward nor egress (no network). With input files, the turn is marked as having
 *    read outside content once the run answered (as read_document marks it).
 *  - A group turn: at most groupTurnCalls calls a turn (the turn's record, like make_document's
 *    count) and groupHourCalls an hour per group (counted in this process: a restart starts
 *    again); past either: group_calc_budget. Reserved before the host is called, given back when
 *    the host did not run the program (busy, calc_unavailable, unknown_ref). An owner turn is
 *    not counted, and the host queues an owner's call before the groups' ones. */
import { covers, audienceLabel, REF_PATTERN } from './media.mjs';

export const CALC_TOOL_LIMITS = Object.freeze({ codeBytes: 32 * 1024, files: 4, groupTurnCalls: 3, groupHourCalls: 30, hourMs: 60 * 60 * 1000 });

/** calculate's closed refusal set (7.8.24 "十" 5), each with its sentence. `not_available` (a call
 *  from another session than the root) is every media tool's. */
export const CALCULATE_REASONS = Object.freeze({
  invalid_request: 'code 是 Python 源码（最多 32 KiB）；files 至多 4 个附件 ref（m_...）',
  unknown_ref: '媒体库里没有这个附件（保留 30 天）',
  not_visible_in_this_turn: '这个附件在这个对话里用不了',
  busy: '本机正在跑别的计算：稍后再试',
  timeout: '这次计算超时了（30 秒）：把计算量减小再试',
  calc_unavailable: '本机的计算沙箱没有就绪，请业主看宿主日志',
  output_too_large: '程序输出太多了（文件最多 4 个、每个 10 MiB）：少存一点再试',
  group_calc_budget: '这个群这一回合（3 次）或这一小时（30 次）的计算次数用完了：照实说，不要换个说法再试',
});
/** What the host's calculate may answer as a refusal, passed on; anything else is calc_unavailable. */
const HOST_REFUSALS = Object.freeze(new Set(['unknown_ref', 'busy', 'timeout', 'calc_unavailable', 'output_too_large', 'invalid_request']));
/** Host refusals that mean the program never ran: the group's count is given back. */
const NOT_RUN = Object.freeze(new Set(['unknown_ref', 'busy', 'calc_unavailable', 'invalid_request']));
const CALC_UNAVAILABLE_DETAILS = Object.freeze(new Set(['no_image_configured', 'selfcheck_failed', 'docker_unavailable']));

export const CALCULATE_DESCRIPTION = 'Run a short Python 3.12 program (numpy, sympy, pandas, matplotlib) in a sandbox with no network, and read what it printed. Use it for arithmetic you must get right, tables and charts. code is the whole program (at most 32 KiB); print the answer: stdout comes back (its first 16 KiB) with the last 2 KiB of stderr and the exit code. files (optional) are up to 4 attachment refs (m_...) of this conversation: they are copied in, in that order, as /job/in/f1.<ext>, /job/in/f2.<ext>, ... (the original file names are not kept); /job/in is read-only. The working directory is empty and writable: files the program saves there come back (at most 4, 10 MiB each) - in your owner\'s conversation png, jpg, pdf or xlsx, in a group png only (for csv, txt or json, print the text instead) - stored as this conversation\'s and NOT sent: send one with send_media({ref}). Every call starts a fresh sandbox: nothing is kept between calls. At most 25 seconds and 512 MiB a call.';

/** @param media host.media: {resolve(ref), calculate?({code, refs}, audience, signal)}
 *  @param catalog the StewardCatalog (rootSession, markExternal) @param turns the GroupTurns. */
export function registerCalcTool(scope, { defineTool, jsonOutput, media, catalog, turns, limits = CALC_TOOL_LIMITS, now = Date.now }) {
  const mine = exec => exec.agent?.session?.id === catalog.rootSession;
  const audience = () => { try { return turns?.audience() ?? { kind: 'group', unknown: true }; } catch { return { kind: 'group', unknown: true }; } };
  // Each group's calls in the last hour (this process's count).
  const hours = new Map();
  const refuse = (code, extra = {}) => ({ refused: code, reason: CALCULATE_REASONS[code], ...extra });

  scope.tools.register(defineTool({ name: 'calculate',
    description: CALCULATE_DESCRIPTION,
    parameters: {
      code: { type: 'string', required: true, description: 'The Python program; print what you want back.' },
      files: { type: 'array', items: { type: 'string' }, description: 'Up to 4 attachment refs (m_...), copied in as /job/in/f1.<ext>, f2.<ext>, ...' },
    },
    output: jsonOutput,
    async execute(args, exec) {
      if (!mine(exec)) return { refused: 'not_available', reason: 'only in your own conversation' };
      const code = args?.code;
      if (typeof code !== 'string' || !code.trim() || Buffer.byteLength(code, 'utf8') > limits.codeBytes) return refuse('invalid_request');
      const refs = args.files ?? [];
      if (!Array.isArray(refs) || refs.length > limits.files || refs.some(ref => typeof ref !== 'string' || !REF_PATTERN.test(ref)))
        return refuse('invalid_request');
      const turn = audience(), label = audienceLabel(turn);
      if (!label) return refuse('not_visible_in_this_turn');
      for (const ref of refs) {
        const row = typeof media.resolve === 'function' ? await media.resolve(ref) : undefined;
        if (!row) return refuse('unknown_ref');
        if (!covers(turn, row)) return refuse('not_visible_in_this_turn');
      }
      if (typeof media.calculate !== 'function') return refuse('calc_unavailable', { detail: 'no_image_configured' });
      // A group's budget: reserved synchronously (no await between the counts and their update).
      let giveBack = () => {};
      if (turn.kind !== 'owner') {
        let record;
        try { record = turns?.open?.(); } catch { record = undefined; }
        const at = now(), recent = (hours.get(label) ?? []).filter(t => at - t < limits.hourMs);
        hours.set(label, recent);
        if (!record || (record.calcCalls ?? 0) >= limits.groupTurnCalls || recent.length >= limits.groupHourCalls) return refuse('group_calc_budget');
        record.calcCalls = (record.calcCalls ?? 0) + 1;
        recent.push(at);
        giveBack = () => {
          record.calcCalls = Math.max(0, (record.calcCalls ?? 1) - 1);
          const list = hours.get(label), i = list?.lastIndexOf(at) ?? -1;
          if (i >= 0) list.splice(i, 1);
        };
      }
      let answer;
      try { answer = await media.calculate({ code, refs: [...refs] }, label, exec.signal); }
      catch { answer = { refused: 'calc_unavailable' }; }
      if (!answer || answer.refused !== undefined || !Number.isSafeInteger(answer.exit_code) || !Array.isArray(answer.files)) {
        const refused = HOST_REFUSALS.has(answer?.refused) ? answer.refused : 'calc_unavailable';
        if (NOT_RUN.has(refused)) giveBack();
        return refuse(refused, refused === 'calc_unavailable' && CALC_UNAVAILABLE_DETAILS.has(answer?.detail) ? { detail: answer.detail } : {});
      }
      // What the program printed may carry the input files' content: outside content.
      if (refs.length) catalog.markExternal();
      const files = answer.files.filter(file => file && REF_PATTERN.test(file.ref ?? ''))
        .map(file => ({ ref: file.ref, mime: String(file.mime), bytes: Number(file.bytes) }));
      return { exit_code: answer.exit_code, timed_out: answer.timed_out === true, stdout: String(answer.stdout ?? ''),
        stdout_truncated: answer.stdout_truncated === true, stderr_tail: String(answer.stderr_tail ?? ''), files,
        ...(Number.isSafeInteger(answer.files_skipped) && answer.files_skipped > 0 ? { files_skipped: answer.files_skipped } : {}),
        ...(files.length ? { next: '文件已存为本对话的，没有发出：要发就用 send_media({ref})' } : {}) };
    },
  }));
}
