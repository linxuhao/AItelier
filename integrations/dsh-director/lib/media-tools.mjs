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
 *    decided by code; the model names no destination. A url must have appeared verbatim in a
 *    result of one of this turn's connection tools (`UrlLedger`, recorded as (conn_id, url)
 *    when the result came back); the host fetches it through the gateway's /fetch with that
 *    conn_id and stores it (source generated, audience this turn).
 *    - An owner turn (step 24.2b, design-steward 7.8.12a "出站"): the file is one steward
 *      message to the owner (host.media.sendToOwner: a durable message with the ref as its
 *      attachment, the caption as its body), a reply to the turn's owner message when there is
 *      one - the host's requester, never the model's choice. The dashboard shows it whatever
 *      the turn came from; the plugin's mirror sends it on to the channel that message came from
 *      (a dashboard message: nowhere), or with no owner message (a reminder, a director's
 *      letter) to the default channel, like a text reply. A channel that cannot take the file
 *      is the mirror's recorded failure, not a refusal: the message stands.
 *    - A group turn: one of the group's sends (group_send_budget, with the confirmation texts).
 *      Everything that can refuse the send is judged before a url is fetched (review F2): the
 *      channel's kinds, and the backend's budget and caption check as a dry run (postGroup
 *      checkOnly); postGroup without checkOnly stays the authoritative record at send.
 *    What only the fetched bytes decide (a url whose name gave no kind), the authoritative
 *    record or the owner's message refused after the fetch takes back the file this call
 *    stored (media.withdraw).

 *  - A director (step 24.2b, 7.8.12a "导演与管家的不同"): media_call only (registerDirectorMediaCall),
 *    on a fixed few tools of its own registry and on its own project's refs.
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

const MEDIA_CALL_PARAMETERS = Object.freeze({ name: { type: 'string', required: true },
  arguments: { type: 'object', additionalProperties: true }, media: { type: 'object', additionalProperties: true, required: true } });

/** media_call's common part, once the caller has decided `args.name` may be called: every ref
 *  of `args.media` must be in this host's own library (media.resolve reads only its own
 *  subdirectory) and covered by `audience`; the files' base64 fill their arguments here, the
 *  whole call must fit one gateway body, and the call runs as a nested ctx.tools.execute. */
async function callWithMedia({ ctx, HarnessError, media, outcomes }, turn, args, exec) {
  const given = args.arguments ?? {};
  if (typeof given !== 'object' || Array.isArray(given)) return { refused: 'invalid_arguments' };
  const map = args.media;
  if (!map || typeof map !== 'object' || Array.isArray(map) || !Object.keys(map).length || Object.keys(map).length > 5)
    return { refused: 'invalid_media', reason: 'media maps 1 to 5 argument names to refs' };
  const filled = { ...given }, files = [];
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
}

/** A director's media_call (step 24.2b, design-steward 7.8.12a "导演与管家的不同"): a director has
 *  no catalog and no connection gateway, so the tools it may run on its files are a fixed few of
 *  its own registry (`names`, each also registered: ctx.tools.schemas()). Its refs are only its
 *  own library's (its `d-<driver_id>/` subdirectory: media.resolve reads nothing else) whose
 *  audience is its own project ({kind: 'project', project_id}); only its own root session may
 *  call it. No send_media: a director's files reach its owner as its message's attachments.
 *  @param media host.media ({resolve(ref), read(ref)}) @param rootSession the director's session
 *  @param projectId its project @param names the tool names it may run. */
export function registerDirectorMediaCall(scope, { ctx, defineTool, jsonOutput, HarnessError, media, rootSession, projectId, names }) {
  const outcomes = new Map();
  const allowed = Object.freeze([...names]);
  const turn = Object.freeze({ kind: 'project', project_id: projectId });
  scope.tools.register(defineTool({ name: 'media_call',
    description: `Director only. Run ${allowed.join(', ')} on files of your media library: media maps the argument name to an attachment ref (m_...), e.g. {"image_base64": "m_3f9a..."} or {"audio_base64": "m_..."}; your host fills that argument with the file and runs the call. arguments are the other arguments. Use it to read an image (ocr_image, vision_critique) or transcribe a voice message (transcribe_audio) the owner sent you. Only refs of your own project are usable.`,
    parameters: MEDIA_CALL_PARAMETERS,
    output: jsonOutput,
    async execute(args, exec) {
      if (exec.agent?.session?.id !== rootSession) return { refused: 'not_available', reason: 'only in your own conversation' };
      if (!allowed.includes(args.name)) return { refused: 'not_allowed', reason: `media_call runs only ${allowed.join(', ')}` };
      const registered = new Set((ctx.tools.schemas?.() ?? []).map(schema => schema?.name));
      if (!registered.has(args.name)) return { refused: 'not_registered', reason: `${args.name} is not available in this session` };
      return callWithMedia({ ctx, HarnessError, media, outcomes }, turn, args, exec);
    },
    projectContent(exec) { return outcomes.get(exec.token)?.content; },
    finalizeContent(exec) { outcomes.delete(exec.token); },
  }));
}

/** @param media host.media: {resolve(ref), read(ref), fetch({connId, url, audience}), postGroup?({turn, kind, caption}),
 *    sendToOwner?({ref, caption?, replyTo?})}
 *  @param catalog the StewardCatalog (this turn's names, the requester, outward marks).
 *  @param turns the GroupTurns (audience() and open()).
 *  @param channels the owner channels handle (canSendFile, sendFile: a group's files), or undefined. */
export function registerMediaTools(scope, { ctx, defineTool, jsonOutput, HarnessError, media, catalog, turns, channels, ledger }) {
  const outcomes = new Map();
  const audience = () => { try { return turns?.audience() ?? { kind: 'group', unknown: true }; } catch { return { kind: 'group', unknown: true }; } };
  const mine = exec => exec.agent?.session?.id === catalog.rootSession;

  scope.tools.register(defineTool({ name: 'media_call',
    description: 'Run one of your connection tools (a name search_tools gave you) on media files: media maps an argument name to an attachment ref (m_...), e.g. {"image_base64": "m_3f9a..."}; your host fills that argument with the file and runs the call. arguments are the other arguments. Use it to read a photo (ocr_image), transcribe a voice message (transcribe_audio) and the like.',
    parameters: MEDIA_CALL_PARAMETERS,
    output: jsonOutput,
    async execute(args, exec) {
      if (!mine(exec)) return { refused: 'not_available', reason: 'only in your own conversation' };
      const names = catalog.catalog(exec.agent.session.id);
      if (!names?.has(args.name)) return { refused: 'not_visible_in_this_turn', reason: `${args.name} is not one of your connection tools in this conversation` };
      if (catalog.outward(args.name) === true) return { refused: 'outward_media_not_supported', reason: 'an outward tool cannot take media files yet' };
      return callWithMedia({ ctx, HarnessError, media, outcomes }, audience(), args, exec);
    },
    projectContent(exec) { return outcomes.get(exec.token)?.content; },
    finalizeContent(exec) { outcomes.delete(exec.token); },
  }));

  scope.tools.register(defineTool({ name: 'send_media',
    description: 'Send one file into THIS conversation (the chat of the person you are answering, or this family group) - never anywhere else: ref is an attachment ref (m_...), or url is a file address that one of your connection tools returned in this turn (e.g. a generated image). caption is an optional short text. To your owner it is one message from you with the file attached: the dashboard shows it, and their chat gets it too when they wrote from one.',
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
      // Whether this turn can be answered with this file is decided first - for a group that
      // its channel can send the kind (a ref's is known; a url's is guessed from its name, or
      // any kind will do) and the backend's budget and caption check (a dry run: no budget
      // used, no row written). Only then is a url fetched and stored. An owner turn needs no
      // channel (24.2b): the message is the owner's on the dashboard, a reminder's included.
      if (turn.kind === 'owner') {
        if (typeof media.sendToOwner !== 'function') return { refused: 'unsupported', reason: 'this host cannot send your owner a file' };
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
      if (turn.kind !== 'owner') {
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
      if (turn.kind === 'owner') {
        // One message to the owner, replying to the turn's owner message when there is one
        // (the requester; none for a reminder's turn). The host writes it and has it mirrored.
        const sent = await media.sendToOwner({ ref: row.ref, ...(args.caption ? { caption: args.caption } : {}),
          ...(catalog.requester ? { replyTo: catalog.requester } : {}) }, exec.signal);
        if (!sent?.message_id) return unsent({ refused: sent?.refused ?? 'not_sent', ...(sent?.reason ? { reason: sent.reason } : {}) });
        return { sent: true, ref: row.ref, kind: row.kind, message_id: sent.message_id, ...(sent.mirror ? { mirror: sent.mirror } : {}) };
      }
      const file = { kind: row.kind, mime: row.mime, name: `${row.kind}-${row.ref.slice(2, 10)}.${row.ext}`, path: row.path,
        ...(args.caption ? { caption: args.caption } : {}) };
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
