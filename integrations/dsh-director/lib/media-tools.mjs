/** The steward's media tools (step 24.2, design-steward 7.8.12 "模型怎么用" and "发到哪里").
 *
 *  Host capabilities, registered only when the host declares `media` (host.d.ts): the
 *  built-in local host has none, so a standalone steward has neither tool.
 *
 *  - media_call({name, arguments, media: {<argument>: <ref>}}): runs one of this turn's
 *    connection tools with media files filled in BY THE HOST: each named argument gets the
 *    file's base64, read here, never seen or written by the model; the call then goes
 *    through ctx.tools.execute as a nested call (the same guards decide visibility, the
 *    backend's gateway what is allowed and outward). Every ref must be one this turn's
 *    audience covers (lib/media.mjs covers: an owner turn every ref, a group turn its own
 *    group's). The base64 never comes back: the result is the tool's own. An outward tool is
 *    refused here (its confirmation of the exact bytes is not built yet).
 *  - send_media({url | ref, caption?}): sends one file back into THIS turn's conversation,
 *    decided by code - the requester's chat (an owner turn: the channel the turn's newest
 *    owner message came from) or the turn's group; the model names no destination. A url must
 *    have appeared verbatim in a result of one of this turn's connection tools
 *    (`UrlLedger`, recorded as (conn_id, url) when the result came back); the host fetches it
 *    through the gateway's /fetch with that conn_id and stores it (source generated, audience
 *    this turn). A dashboard turn is refused dashboard_no_media (24.2). A group's file is one
 *    of the group's sends (group_send_budget, with the confirmation texts). Everything that
 *    can refuse the send is judged before a url is fetched (review F2): the destination, the
 *    channel's kinds, and for a group the backend's budget and caption check as a dry run
 *    (postGroup checkOnly); postGroup without checkOnly stays the authoritative record at send.
 *    What only the fetched bytes decide (a url whose name gave no kind) or the authoritative
 *    record refuses after the fetch takes back the file this call stored (media.withdraw).
 */
import { covers, audienceLabel, REF_PATTERN, MEDIA_TYPES, MEDIA_LIMITS } from './media.mjs';

const URL_IN_TEXT = /https?:\/\/[^\s"'<>\\`)\]}]+/g;
const CAPTION_CHARS = 1000;
// What a nested tools/call carries besides its arguments (the JSON-RPC envelope, the gateway's
// prefixed tool name, _meta): a conservative allowance on top of the arguments' own JSON.
const CALL_ENVELOPE_BYTES = 64 * 1024;
const KIND_BY_EXT = new Map(MEDIA_TYPES.map(type => [type.ext, type.kind]));

/** The kind a url's file name says (its extension in the shared media-types table), or
 *  undefined. Only a guess, used to refuse before fetching; the fetched file's first bytes decide. */
export function kindOfUrl(url) {
  try {
    const name = new URL(url).pathname.split('/').pop() ?? '', dot = name.lastIndexOf('.');
    return dot > 0 ? KIND_BY_EXT.get(name.slice(dot + 1).toLowerCase()) : undefined;
  } catch { return undefined; }
}

/** The urls this turn's connection tools returned, each with its connection: what send_media
 *  may fetch. Cleared at every turn's start; a restart forgets them (fail closed). */
export class UrlLedger {
  constructor({ server = 'sw', max = 500 } = {}) { Object.assign(this, { server, max }); this.urls = new Map(); }
  connectionOf(name) {
    const prefix = `mcp__${this.server}__`;
    if (typeof name !== 'string' || !name.startsWith(prefix)) return undefined;
    const rest = name.slice(prefix.length), cut = rest.indexOf('__');
    return cut > 0 ? rest.slice(0, cut) : undefined;
  }
  /** A connection tool's result came back: every url in it, verbatim. */
  record(name, result) {
    const connId = this.connectionOf(name);
    if (!connId || result?.isError) return 0;
    let text;
    try { text = JSON.stringify(result); }
    catch {
      try { text = JSON.stringify([result?.value ?? null, result?.content ?? null]); } catch { return 0; }
    }
    let n = 0;
    for (const match of text.matchAll(URL_IN_TEXT)) {
      if (!this.urls.has(match[0])) { this.urls.set(match[0], connId); n += 1; }
      while (this.urls.size > this.max) this.urls.delete(this.urls.keys().next().value);
    }
    return n;
  }
  lookup(url) { return typeof url === 'string' ? this.urls.get(url) : undefined; }
  reset() { this.urls.clear(); }
}

/** @param media host.media: {resolve(ref), read(ref), fetch({connId, url, audience}), postGroup?({turn, kind, caption})}
 *  @param catalog the StewardCatalog (this turn's names, the requester, outward marks).
 *  @param turns the GroupTurns (audience() and open()).
 *  @param channels the owner channels handle (ownerMediaTarget, sendFile), or undefined. */
export function registerMediaTools(scope, { ctx, defineTool, jsonOutput, HarnessError, media, catalog, turns, channels, ledger }) {
  const outcomes = new Map();
  const audience = () => { try { return turns?.audience() ?? { kind: 'group', unknown: true }; } catch { return { kind: 'group', unknown: true }; } };
  const mine = exec => exec.agent?.session?.id === catalog.rootSession;

  scope.tools.register(defineTool({ name: 'media_call',
    description: 'Run one of your connection tools (a name search_tools gave you) on media files: media maps an argument name to an attachment ref (m_...), e.g. {"image_base64": "m_3f9a..."}; your host fills that argument with the file and runs the call. arguments are the other arguments. Use it to read a photo (ocr_image), transcribe a voice message (transcribe_audio) and the like.',
    parameters: { name: { type: 'string', required: true }, arguments: { type: 'object', additionalProperties: true },
      media: { type: 'object', additionalProperties: { type: 'string' }, required: true } },
    output: jsonOutput,
    async execute(args, exec) {
      if (!mine(exec)) return { refused: 'not_available', reason: 'only in your own conversation' };
      const names = catalog.catalog(exec.agent.session.id);
      if (!names?.has(args.name)) return { refused: 'not_visible_in_this_turn', reason: `${args.name} is not one of your connection tools in this conversation` };
      if (catalog.outward(args.name) === true) return { refused: 'outward_media_not_supported', reason: 'an outward tool cannot take media files yet' };
      const given = args.arguments ?? {};
      if (typeof given !== 'object' || Array.isArray(given)) return { refused: 'invalid_arguments' };
      const map = args.media;
      if (!map || typeof map !== 'object' || Array.isArray(map) || !Object.keys(map).length || Object.keys(map).length > 5)
        return { refused: 'invalid_media', reason: 'media maps 1 to 5 argument names to refs' };
      const turn = audience(), filled = { ...given }, files = [];
      for (const [param, ref] of Object.entries(map)) {
        if (!/^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(param) || typeof ref !== 'string' || !REF_PATTERN.test(ref))
          return { refused: 'invalid_media', reason: `media.${param}: an argument name and an m_ ref` };
        const row = await media.resolve(ref);
        if (!row) return { refused: 'unknown_ref', reason: `${ref} is not in the media library (kept 30 days)` };
        if (!covers(turn, row)) return { refused: 'not_visible_in_this_turn', reason: `${ref} is not usable in this conversation` };
        const file = await media.read(ref);
        if (!file) return { refused: 'unknown_ref', reason: `${ref} is not in the media library (kept 30 days)` };
        files.push([param, file.bytes]);
        filled[param] = '';
      }
      // The whole call must fit the gateway's body bound (one shared limit): the arguments' JSON
      // with each file's base64 (4 characters per 3 bytes) and the envelope, counted before
      // anything is encoded or sent - over it the gateway would answer a bare 413.
      const bodyBytes = Buffer.byteLength(JSON.stringify(filled)) + CALL_ENVELOPE_BYTES
        + files.reduce((sum, [, bytes]) => sum + 4 * Math.ceil(bytes.length / 3), 0);
      if (bodyBytes > MEDIA_LIMITS.call_body_bytes) {
        const mib = n => (n / 1048576).toFixed(1);
        return { refused: 'media_too_large_for_one_call', reason: `these files make a call of about ${mib(bodyBytes)} MiB once encoded, `
          + `over the ${mib(MEDIA_LIMITS.call_body_bytes)} MiB one call can carry: send fewer refs per call` };
      }
      for (const [param, bytes] of files) filled[param] = Buffer.from(bytes).toString('base64');
      const result = await ctx.tools.execute({ name: args.name, arguments: filled, agent: exec.agent,
        callId: exec.callId + ':media_call', rootCallId: exec.rootCallId, parent: exec.token, signal: exec.signal });
      outcomes.set(exec.token, result);
      if (result.isError) {
        const error = new HarnessError(result.error.message, result.error.info?.code ?? 'TOOL_ERROR');
        error.name = result.error.info?.name ?? 'ToolError';
        throw error;
      }
      return result.value;
    },
    projectContent(exec) { return outcomes.get(exec.token)?.content; },
    finalizeContent(exec) { outcomes.delete(exec.token); },
  }));

  scope.tools.register(defineTool({ name: 'send_media',
    description: 'Send one file into THIS conversation (the chat of the person you are answering, or this family group) - never anywhere else: ref is an attachment ref (m_...), or url is a file address that one of your connection tools returned in this turn (e.g. a generated image). caption is an optional short text. Not on the dashboard yet.',
    parameters: { ref: { type: 'string' }, url: { type: 'string' }, caption: { type: 'string' } },
    output: jsonOutput,
    async execute(args, exec) {
      if (!mine(exec)) return { refused: 'not_available', reason: 'only in your own conversation' };
      const hasRef = typeof args.ref === 'string' && args.ref !== '', hasUrl = typeof args.url === 'string' && args.url !== '';
      if (hasRef === hasUrl) return { refused: 'invalid_request', reason: 'give exactly one of ref and url' };
      if (args.caption !== undefined && (typeof args.caption !== 'string' || args.caption.length > CAPTION_CHARS))
        return { refused: 'invalid_request', reason: `caption is text of at most ${CAPTION_CHARS} characters` };
      const turn = audience(), label = audienceLabel(turn);
      if (!label) return { refused: 'no_conversation', reason: 'this turn\'s conversation cannot be named' };
      // Whether this turn can be answered with this file is decided first - where it goes, that
      // its channel can send the kind (a ref's is known; a url's is guessed from its name, or
      // any kind will do), and for a group the backend's budget and caption check (a dry run:
      // no budget used, no row written). Only then is a url fetched and stored.
      if (turn.kind === 'owner') {
        if (!catalog.requester) return { refused: 'no_requester', reason: 'a file goes back only to a person\'s message in this turn' };
        if (!channels) return { refused: 'dashboard_no_media' };
      } else if (typeof media.postGroup !== 'function' || !channels) return { refused: 'channel_no_media' };
      let row;
      if (hasRef) {
        if (!REF_PATTERN.test(args.ref)) return { refused: 'unknown_ref' };
        row = await media.resolve(args.ref);
        if (!row) return { refused: 'unknown_ref', reason: 'not in the media library (kept 30 days)' };
        if (!covers(turn, row)) return { refused: 'not_visible_in_this_turn', reason: `${args.ref} is not usable in this conversation` };
      }
      const connId = hasUrl ? ledger.lookup(args.url) : undefined;
      if (hasUrl && !connId) return { refused: 'url_not_seen', reason: 'that address did not come back from one of your connection tools in this turn' };
      const kind = row?.kind ?? kindOfUrl(args.url);
      let target;
      if (turn.kind === 'owner') {
        target = await channels.ownerMediaTarget(catalog.requester, kind);
        if (target?.refused) return target;
      } else {
        if (!channels.canSendFile(turn.channel, kind)) return { refused: 'channel_no_media' };
        const check = await media.postGroup({ turn: turns.open()?.turn, ...(kind ? { kind } : {}), caption: args.caption, checkOnly: true }, exec.signal);
        if (!check?.may_post) return { refused: check?.refused ?? 'not_posted', ...(check?.reason ? { reason: check.reason } : {}) };
      }
      // A file this call fetched and stored, taken back if the send is refused after all (review
      // MF3): a library row that existed before (a duplicate answer) is never this call's.
      let created;
      const unsent = async refusal => {
        if (created) { try { await media.withdraw?.(created); } catch { /* kept: cleanup removes it in 30 days */ } }
        return refusal;
      };
      if (hasUrl) {
        const fetched = await media.fetch({ connId, url: args.url, audience: label }, exec.signal);
        if (!fetched?.ref) return { refused: fetched?.refused ?? 'fetch_failed', ...(fetched?.reason ? { reason: fetched.reason } : {}) };
        if (fetched.duplicate !== true) created = fetched.ref;
        row = await media.resolve(fetched.ref);
        if (!row) return unsent({ refused: 'unknown_ref', reason: 'not in the media library (kept 30 days)' });
      }
      const file = { kind: row.kind, mime: row.mime, name: `${row.kind}-${row.ref.slice(2, 10)}.${row.ext}`, path: row.path,
        ...(args.caption ? { caption: args.caption } : {}) };
      if (turn.kind === 'owner') {
        // A url whose name said nothing: its real kind is known only now - its bytes are fetched
        // and stored by now, so a kind the channel cannot send is not sent and the file is taken back.
        if (!channels.canSendFile(target.channel, row.kind)) return unsent({ refused: 'channel_no_media' });
        const sent = await channels.sendFile(target.channel, { ...file, to: target.to, ...(target.replyToRef ? { replyToRef: target.replyToRef } : {}) });
        return sent.sent ? { sent: true, ref: row.ref, kind: row.kind, channel: target.channel } : sent;
      }
      // A group's file: the backend counts it among the group's sends and checks the caption -
      // only once the group's channel can send this kind at all (a url's real kind: when its name
      // said nothing, its bytes are fetched and stored by now; refused, the file is taken back).
      if (!channels.canSendFile(turn.channel, row.kind)) return unsent({ refused: 'channel_no_media' });
      const open = turns.open();
      const posted = await media.postGroup({ turn: open?.turn, kind: row.kind, caption: args.caption }, exec.signal);
      if (!posted || posted.refused || !posted.group_ref)
        return unsent({ refused: posted?.refused ?? 'not_posted', ...(posted?.reason ? { reason: posted.reason } : {}) });
      const { caption: _asked, ...bare } = file;
      const sent = await channels.sendFile(posted.channel, { ...bare, groupRef: posted.group_ref,
        ...(posted.caption ? { caption: posted.caption } : {}) });
      return sent.sent ? { sent: true, ref: row.ref, kind: row.kind, channel: posted.channel } : sent;
    },
  }));
}
