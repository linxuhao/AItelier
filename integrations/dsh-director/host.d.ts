/**
 * directorHost v1: what a work/memory system implements so dsh-director can run on it.
 * Provided in process as the cordis service `directorHost` (ctx.provide). The
 * executable form of this contract is lib/host.mjs `validateHost` (refusals) and
 * conformance/index.mjs (behaviour); this file is their documentation.
 *
 * Built-in implementation: hosts/local.mjs (local files). External: e.g. the
 * the backend adapter in integrations/dsh-driver/runtime.mjs.
 */

/** Work item states, aligned with MCP Tasks / A2A task states. */
export type WorkState = 'working' | 'input_required' | 'completed' | 'failed' | 'cancelled' | 'unknown';

export interface HostConfig {
  /** The director's DSH session id (its native children are workers). */
  sessionId: string;
  provider: string;
  model: string;
  /** Directory for the director's own state (todo plans, finish candidates). */
  stateDir: string;
  /** The DSH entry; DSH libraries are resolved from it. */
  dshEntry: string;
  /** roles.json (director/worker/steward table); role prompts are read next to it. */
  rolesFile: string;
  /** v1.2, plugin step 12: the root session's role, 'director' (default) or 'steward'.
   *  The steward needs a `steward` entry in rolesFile; its native children are workers. */
  rootRole?: 'director' | 'steward';
  /** The todo CLI: `python -m <module>` in `cwd` (default: the process's directory) with
   *  python defaulting to python3; the CLI reads its state directory from `stateDirEnv`. */
  todo: { python?: string; cwd?: string; module: string; stateDirEnv: string };
  /** Environment variable carrying the session id on the driver's processes (default DSH_DIRECTOR_SESSION). */
  sessionEnv?: string;
  /** Included in review notices when set. */
  projectId?: string;
  /** DSH permission preset applied to the director session on start, e.g. 'danger-full-access'. */
  permissionPreset?: string;
  /** The worker capacity this host expects (default: DSH's own maxActiveSubagents). The guard
   *  uses the smaller of this and the limit DSH runs with, so it is never above DSH's. */
  maxActiveSubagents?: number;
  /** Worker slots kept free for resuming workers and follow-ups (default 1, at least 1); the
   *  director's subagent call is refused at capacity - reservedSlots, the refusal names the
   *  dispatch, and one wake naming every refused dispatch comes when a slot frees (kept
   *  across a restart). A dispatch DSH itself refused counts as refused too. */
  reservedSlots?: number;
  idleReviewMs?: number | string;
  dormantMinuteMs?: number | string;
  watchIntervalMs?: number | string;
  dormantCompactMinTokens?: number | string;
  /** v1.2, plugin step 11: the owner's memory (design-steward 3). Absent: no memory and no
   *  memory tools. The repository lives at `<root>/<userId>/` (root default
   *  ~/.local/share/dsh-director/memory; keep it outside anything mounted into containers).
   *  `remote` (off by default) is an Agent Memory Leaderboard endpoint used as the ranked
   *  index instead of the built-in BM25; `amiIds` lets it trace ActiveMemoryIndex hits back to
   *  turns. Every remote call may cost money. `timeZone` (step 21, design-steward 3.8.1) is
   *  the owner's IANA time zone (default UTC): memory_search's line dates, its day groups and
   *  the date words in the BM25 index all use it; an invalid name stops the memory module
   *  (its tools answer the error) and nothing else. Read at start-up only.
   *  `vector` (step 21, design-steward 3.8.2; off when absent) adds a local embedding server
   *  (llama.cpp `llama-server --embedding`) as a second ranked channel beside BM25, fused by
   *  RRF: `url` is loopback only (http://127.0.0.1 | localhost | [::1]), `model` one of the
   *  two the code knows, `timeoutMs` the query embedding's limit (default 5000). Vectors are
   *  kept in `<root>/<userId>.vectors.jsonl` beside the repository (derived, never in git).
   *  It excludes `remote`; a bad value stops the memory module only. */
  memory?: { userId: string; root?: string; grepTimeoutMs?: number; backupRemote?: string; timeZone?: string;
    remote?: { url: string; name: string; tokenFile?: string; userId?: string; sessionId?: string; amiIds?: boolean };
    vector?: { url: string; model: 'bge-m3-q8_0' | 'multilingual-e5-small-q8_0'; timeoutMs?: number } };
}

/** What the director hands the host (host-scoped and director-scoped hooks, events). */
export interface DirectorApi {
  ctx: unknown; defineTool: Function; llm: unknown; createUserMessage: Function;
  config: HostConfig; roles: unknown; texts: Record<string, unknown>; labels: Record<string, string>;
  todo: unknown; children: unknown; jsonOutput: unknown;
  liveSessions(): string[]; planWork(signal?: AbortSignal): Promise<unknown>;
  todoPlans(): Promise<unknown[]>; batchRedispatch(): Promise<unknown[]>;
  /** Steer one wake event into the director (a runtime-event user message). Present from
   *  install() on; it refuses (throws, naming why) until the director session exists.
   *  v1.1: pass `{ id }` - stable across restarts, derived from the event itself, never
   *  from a counter or a random value - and the director delivers each (id, text) once:
   *  the same id with the same text is a redelivery, the same id with other text is a
   *  new event. Checked before delivery, recorded after it: a crash in between
   *  duplicates, never loses. If notify throws, the host must deliver the event again
   *  later under the same id; the conformance suite checks exactly that (a delivery
   *  that failed comes back with the same id), and that every event carries an id. */
  notify(text: string, options?: { id?: string }): Promise<void>;
  eventMessage(text: string): unknown;
  report(phase: string, error?: string, event?: unknown): Promise<void>;
  currentPhase(): string;
  /** A host-side failure the status reports should carry (cleared with clearHostError). */
  setHostError(message: string): Promise<void>;
  clearHostError(): Promise<void>;
  isDisposed(): boolean;
  /** v1.2: the owner's memory store when config.memory is set (lib/memory/store.mjs), else
   *  undefined. A host records each owner/steward turn with memory.recordTurn(turnId,
   *  [{role: 'owner'|'steward', text}]) and runs the owner's own operations (forget,
   *  restore, purge, profile edits) through it - purge exists only here, never as a tool. */
  readonly memory?: unknown;
  /** Plugin step 15: why config.memory is set but the store did not open, else undefined. */
  readonly memoryError?: string;
  /** v1.2, capability `channels`: the host calls this after the root wrote message
   *  `messageId` to its owner (replying to `replyTo` when given), with the text it wrote.
   *  The director sends the same text on the owner's chat channel, shaped for a phone chat
   *  (channelText: cut past CHANNEL_LIMITS.mirrorChars, headings, bold and tables flattened;
   *  an approval phrase line is never cut, a text with a binding code goes as written): a reply goes to the
   *  channel the answered message came from, an unprompted message to the default channel
   *  when one is set and bound, a reply to a dashboard message nowhere. Each message is
   *  sent once and recorded with channels.recordDelivery, a failed send included; a send
   *  is never retried. The delivery row is what keeps a message from being sent again
   *  after a restart (channels.sourceOf), so a record that fails is retried a bounded
   *  number of times (CHANNEL_LIMITS: 6 tries over about 30 s) as the same request, which
   *  the host must take once; a refusal is not retried. A record that fails every try is
   *  logged (channel_record_failed): within the process the message is still not sent
   *  again, but after a restart a replay of the same message id would send it a second
   *  time. Never rejects; without channels it resolves {mirrored: false}. */
  ownerMessageSent(messageId: string, text: string, replyTo?: string): Promise<object>;
  /** v1.2, capability `channels` (step 19, on-demand QR): ask channel `channel` for a new
   *  login (the WeChat QR), for the owner's dashboard request the host runs in code. Answers
   *  {requested: true} or {refused, retry_after_s?} (OwnerChannelsHandle.login), never the
   *  QR itself, which reaches the owner only through channels.reportStatus. Never rejects;
   *  without channels it resolves {refused: 'no_channels'}. */
  ownerChannelLogin(channel: 'slack' | 'wechat'): Promise<ChannelLoginAnswer>;
  /** Step 25 C11 (capability `groups`): send one line into a bound group (a group reminder the
   *  host posted with post_group_notice) through the channel's group send and the bridge's one
   *  queue, like a group reply. true when the channel took it; never rejects; false without
   *  channels. */
  groupSend(channel: string, groupRef: string, text: string): Promise<boolean>;
  /** undefined during install(); the director's agent from setup() on. */
  readonly agent: unknown;
}

export interface DirectorHost {
  /** semver; the director accepts ^1. */
  version: string;
  /** The optional operations this host implements; declared and implemented must match. */
  capabilities: Array<'progress' | 'workState' | 'heldWork' | 'roles' | 'install' | 'setup' | 'texts' | 'labels' | 'attention' | 'memory'
    | 'portfolio' | 'handoff' | 'proposals' | 'channels' | 'groups' | 'catalog' | 'media'>;
  config: HostConfig;

  // ---- required ----
  /** Startup / post-compaction snapshot text for the root role (config.rootRole). */
  context(role: 'director' | 'steward'): Promise<string>;
  /** Deliver what is waiting and everything that arrives later through api.notify, each
   *  event with a stable id (v1.1); deliver before moving your own cursor past it.
   *  `done` rejects on a fatal failure (the director then stops); `stop` ends delivery. */
  events(api: DirectorApi): Promise<{ done?: Promise<unknown>; stop(): void }>;
  // `done` rejects on a fatal failure while running; after stop() it must settle
  // (resolve, or reject with the stop's abort, which the director ignores).

  // ---- optional (absent: the default in brackets) ----
  /** Status report body { phase: running|waiting|dormant|failed, subagents, error, event? } [dropped]. */
  progress?(body: object): Promise<unknown> | void;
  /** A work item's state; settled = completed|failed|cancelled; throw or 'unknown' when unsure [unknown]. */
  workState?(workId: string, signal?: AbortSignal): Promise<WorkState>;
  /** Work this director holds; idle_dormant refuses while any [none]. */
  heldWork?(signal?: AbortSignal): Promise<Array<{ node_key: string }>>;
  /** v1.1, plugin step 8: the director's questions to its owner, behind ask_owner /
   *  close_question [none: both tools refuse, naming that the host has no channel].
   *  - ask: one concrete question under the director's own id. The same id with the
   *    same text asks nothing new (a safe retry); the same id with other text is
   *    refused {refused: 'question_id_reused'}.
   *  - close: take an own, unanswered question back, with the reason, as a record (it
   *    stays visible as withdrawn). An answered one is refused {refused:
   *    'question_answered'}; closing twice changes nothing. What `closed` reports on a
   *    retry depends on the host: a retry the host replays (same request) may answer
   *    {closed: true} again, a retry with another reason {closed: false,
   *    already_closed: true}. Either way the question is closed once; `closed` says "a
   *    withdrawal of this question exists", not "this call withdrew it".
   *  - list: the open questions (a host may bound it; it states its bound, and marks a
   *    truncated answer with `truncated: true` on the returned array).
   *  The owner's answer reaches the director through events(). The built-in host's
   *  event names the question id; another host may carry the citation on the
   *  answering message instead. */
  attention?: {
    ask(question: { id: string; text: string; context?: string }, signal?: AbortSignal): Promise<object>;
    close(close: { id: string; reason: string }, signal?: AbortSignal): Promise<object>;
    list(signal?: AbortSignal): Promise<Array<{ id: string; text: string }>>;
  };
  /** v1.2, plugin step 11: whether `id` names a message the owner wrote; memory_forget
   *  needs it (the steward forgets only when the owner asked) [none: memory_forget refuses]. */
  memory?: { ownerMessage(id: string, signal?: AbortSignal): Promise<boolean> };
  /** v1.2, plugin step 12: the steward's read-only view of every project, behind
   *  portfolio_projects / portfolio_project / portfolio_conversation [none: not registered].
   *  Reads only what the host already lets this identity read; it grants nothing.
   *  - projects: each project's id and title, its director's state, its open questions;
   *  - project: one project's overview, open issues and open questions;
   *  - conversation: one page of the owner/director conversation, newest first, with a
   *    cursor for the next older page. */
  portfolio?: {
    projects(signal?: AbortSignal): Promise<object>;
    /** `page` (v1.2+, optional): the cursors a previous answer gave, passed back as the model gave them. */
    project(projectId: string, signal?: AbortSignal, page?: { openAfter?: string; issuesAfter?: number }): Promise<object>;
    conversation(projectId: string, cursor?: string, signal?: AbortSignal): Promise<object>;
  };
  /** v1.2, plugin step 12: pass the owner's words to a project's director, behind the
   *  handoff tool [none: not registered]. The host checks that citesUserMessageId is a
   *  message the owner wrote (refusing otherwise) and quotes it to the director. */
  handoff?(handoff: { projectId: string; text: string; citesUserMessageId: string }, signal?: AbortSignal): Promise<object>;
  /** v1.2, plugin step 13: the steward's proposals, behind propose / withdraw_proposal
   *  [none: not registered]. propose records an OPEN proposal and nothing else - the owner
   *  approves or declines it through the host, the host executes the approved version and
   *  delivers each result to the steward through events(). kind: create_project |
   *  start_driver | stop_driver | archive_project | unarchive_project; supersedes names the steward's own open proposal it replaces.
   *  withdraw takes back the steward's own open proposal, as a record.
   *  kinds (plugin step 15, optional): the kinds this host can execute; propose offers only
   *  those (the built-in local host: create_project). Absent: every kind above. */
  proposals?: {
    propose(proposal: { kind: string; payload: object; reason: string; supersedes?: string }, signal?: AbortSignal): Promise<object>;
    withdraw(withdrawal: { id: string; reason: string }, signal?: AbortSignal): Promise<object>;
    kinds?: string[];
  };
  /** v1.2, design-steward 7.2: the owner's chat channels (Slack, WeChat). Declared, the
   *  director provides the cordis service `ownerChannels` to channel plugins [none: the
   *  service is not provided and channel plugins do not load]. The host owns who the owner
   *  is and the conversation; the director only connects channel plugins to these:
   *  - relay: one channel message from `senderRef` into the owner's conversation, exactly
   *    as a message the owner wrote on the dashboard (it wakes the root). Idempotent by
   *    (channel, senderRef, messageRef): a redelivery answers the first result with
   *    duplicate: true; the same messageRef from another sender is another message.
   *    A redelivery of an approval phrase whose decision was lost (the host stopped
   *    after writing the message, before the decision was recorded) runs the decision
   *    now and answers it with duplicate: true and decided_on_replay: true; the director
   *    then sends the decision's line, which it sends once per channel message. A
   *    redelivery while the first delivery is still executing the approval answers
   *    {duplicate: true, approval: null, approval_pending: true}: no line yet and NOT a
   *    finished answer - the channel must ask again (it keeps the message owed), and a
   *    later redelivery carries the decision. A channel that treats that answer as
   *    terminal loses the owner's line; the shipped plugins retry it.
   *    The host refuses a sender that is not the channel's current binding
   *    (channel_sender_not_bound), a body over its bound (body_too_long) or not valid
   *    Unicode text (invalid_body), and too many messages (rate_limited), as
   *    {refused: code}. `rate_limited` and `unavailable` are TRANSIENT: the message was
   *    not taken, so a channel holds it and asks again with backoff (dropping it loses
   *    the owner's message); every other refusal is final for that message. The bound
   *    owner is told once a minute that a rate_limited burst is held ("no need to send
   *    them again"); that line never uses up the message's own answer.
   *    When the text is exactly an approval phrase the host executes it
   *    in code and says so in `approval`; the model never does. An executed decision
   *    names the decided proposal's `kind` and `target` from the host's own record of it,
   *    and its `result` is that proposal after the run: `status: 'failed'` when the run failed.
   *    `at` is when the channel says the message was written (ISO 8601): a phrase written
   *    too long ago decides nothing (approval refusal approval_too_old), nor one dated
   *    more than two minutes ahead of the host's clock (approval_clock_skew), nor one
   *    without `at` (approval_undated: the message is still written; a host may instead
   *    answer {refused: 'approval_undated'}). A channel passes the time it was given and
   *    never one of its own. The bound owner's message that is a live binding code is
   *    refused code_sent_to_bound_account (the code is retired; the owner issues a new one).
   *  - bind: a six-digit code the owner got on the dashboard, sent from the account to bind;
   *    {bound: true} or {refused: invalid_code | code_expired | code_used | code_retired}
   *    (code_retired: too many refused attempts at the channel's code; the owner issues a new one).
   *    Idempotent by (channel, senderRef, messageRef): the same binding message delivered
   *    again answers {bound, duplicate: true, refusal?} and counts nothing; relayed (its
   *    sender is bound now) it answers {message_id: null, duplicate: true} and writes nothing.
   *  - codeCheck (optional): may this text be kept in the plugin's durable owed store?
    {store_text: boolean}. The host answers with its own rule for a live binding code - a
    bound sender, and no code in the text in any rendering it folds - because the plugin
    writes the owner's own words down before it relays them and must never write a code.
    Absent (an older host), the plugin keeps no text: durability degrades, never a secret.
    Present but not answered (a transport failure, a 5xx, a timeout), the message is not
    relayed either: it is answered `unavailable` and the plugin hands it in again, so no
    relay ever commits while the owed record has no text.
  - sourceOf: where a message came from or went to: null for the dashboard, else
   *    {direction: 'in'|'out', channel, sender_ref, channel_message_ref, at, error}.
   *  - recordDelivery: that the root's message `messageId` was sent on `channel` (with the
   *    channel's own ref) or failed (`error`).
   *  - list: every channel's binding and the default channel for unprompted messages.
 *  - reportStatus (optional): a channel's status changed - connected or not, a detail line
 *    (at most 300 characters), and while a login needs a scan its QR as an SVG document
 *    (`qrSvg`, a login token: shown to the owner, never logged). Called only on a change;
 *    {refused: code} when the host will not take it.
 *  - dueAlerts, claimAlert, recordAlert (optional, all three or none - the plugin checks each
 *    with typeof and runs no alert without all three; not in the required channels set): the
 *    channel-down alert (design-steward 7.9.2); only some hosts have them.
 *  - dueAlerts: the channel-down alerts (`down`) and recovery notices (`up`) due now,
 *    {due: [{channel, kind, down_since, attempt}]} - a read.
 *  - claimAlert: may this one go now? `exclude` names the channels the plugin cannot send
 *    through now (not registered, or their current reading not accepted), never chosen as
 *    `via`. {granted: true, via, to, minutes, detail} - `via` is the channel to send it
 *    through, `to` its bound account - or {granted: false}. A granted claim asked again
 *    answers the first grant, whatever its `exclude`. Called by code, never a model.
 *  - recordAlert: how the granted alert's send went: ok, or the channel's `error`. */
  /** Step 24.1a (design-steward 7.8.7, 7.8.8): the steward's own catalog. Declared, a root
   *  role with `catalog: "host"` finds and calls its further tools only in this turn's
   *  catalog (lib/catalog.mjs); undeclared, it finds none.
   *  - connections: the connection cache - `{connections: [{conn_id, visibility, usable,
   *    tools: [{name, egress, outward, ...}]}], effective_tools: [public names],
   *    effective_hash}`, the same set the host's MCP gateway serves.
   *  - resyncDue: effective tools still missing from the registry a minute later.
   *  - humanInput: whether one input text carries a person's message (synchronous).
   *  - writes (optional): the names of the host's own writing tools (letters, reminders) that,
   *    like the plugin's, need a person's message in a turn that read outside content.
   *  - requesterOf (optional, step 24.1b): the id of the newest person's message one input
   *    text carries (synchronous) - the only requester an outward action can have.
   *  - confirm (optional, step 24.1b, design-steward 7.8.9): request_confirmation and
   *    run_confirmed are registered only with it. create stores the exact call and answers
   *    the code and the text (written by code) the requester reads; deliver sends that text
   *    into the requester's conversation (a reply to their message) and is refused unless it
   *    reached them (`channel` is the requester's: a chat channel's mirror must take it). In a
   *    family-group turn create gets audience 'group' and the turn's question ids
   *    (`questionIds`, newest last; the requester is their one sender) and answers the group
   *    (`group_ref`), which deliver gets as `groupRef` and sends into, through the group send;
   *    cancel voids a pending record whose text did not arrive; get answers the stored
   *    record ({status, name, arguments}) run_confirmed runs. Refusals are {refused}.
   *  - manage (optional, step 24.1c, design-steward 7.8.10a): connections_list,
   *    connection_tools, connection_refresh and propose_connection_change are registered only
   *    with it. tools reads one connection's tools (descriptions are the server's words);
   *    refresh re-reads its server's list (new, changed and gone names) and never changes a
   *    permission; propose records one open proposal of kind connection_change and answers
   *    {proposal_id, version, status, needs_dashboard}. Refusals are {refused}.
   *  The catalog writes no audience of its own: the turn's audience is step 23's (groups.audience,
   *  recorded by the group turns before each turn's first step), which the catalog reads to filter
   *  and the host's MCP gateway reads to judge a private connection's call. */
  catalog?: {
    connections(signal?: AbortSignal): Promise<{ connections: object[]; effective_tools: string[]; effective_hash: string }>;
    resyncDue(effectiveHash: string, signal?: AbortSignal): Promise<unknown>;
    humanInput(text: string): boolean;
    writes?: string[];
    requesterOf?(text: string): string | undefined;
    confirm?: {
      create(request: { name: string; arguments: object; requesterMessageId: string; audience: string; questionIds?: string[] },
        signal?: AbortSignal): Promise<{ code: string; text: string; channel: string; group_ref?: string; reply_to_message_id: string;
        expires_at: string } | { refused: string; reason?: string }>;
      deliver(message: { code: string; text: string; replyTo: string; channel: string; groupRef?: string }, signal?: AbortSignal):
        Promise<{ message_id: string } | { refused: string; reason?: string }>;
      cancel(code: string, signal?: AbortSignal): Promise<unknown>;
      get(code: string, signal?: AbortSignal): Promise<{ status: string; name: string; arguments: object } | { refused: string }>;
    };
    manage?: {
      tools(connId: string, signal?: AbortSignal): Promise<{ conn_id: string; tools: object[] } | { refused: string }>;
      refresh(connId: string, signal?: AbortSignal):
        Promise<{ conn_id: string; added: string[]; changed: string[]; gone: string[] } | { conn_id: string; error: string } | { refused: string }>;
      propose(proposal: { payload: object; reason: string; supersedes?: string }, signal?: AbortSignal):
        Promise<{ proposal_id: string; version: number; status: 'open'; needs_dashboard: boolean } | { refused: string; reason?: string }>;
    };
  };
  /** Step 24.2 (design-steward 7.8.12, 7.8.12a): the host's media library - one writer of
   *  `<media root>/<userId>/` (the root is the host's setting), lib/media.mjs. Declared, the
   *  plugin's ownerChannels.storeMedia stores into it and the steward gets media_call and
   *  send_media; undeclared (the built-in local host), neither tool is registered and
   *  storeMedia answers {refused: 'unsupported'}.
   *  - ingest: the library's one way in (no binding check: storeMedia makes that one first) -
   *    idempotent by (channel, messageRef, index) without calling `open` (a stored one answers
   *    its ref, a refused one the same refusal), then open({signal})'s bytes under deadlines
   *    (first byte, whole file: a stalled source is aborted, `unavailable`), counted against the
   *    limits, typed by media-types.json, an image's pixels and a voice's streams and length
   *    (ffprobe; a video stream is `type`) checked; {ref, kind, mime, size, durationMs?,
   *    duplicate?} or {refused: too_large | type | unsupported | gone | unavailable,
   *    duplicate?}. `audience` is 'owner', 'group:<channel>:<group_ref>' or
   *    'project:<project_id>'; `source` 'in' (a channel's) or 'generated' (held to the
   *    outbound limit). `signal`: the caller giving up stops waiting (`unavailable`).
   *  - resolve: a ref's row with its absolute `path`, `kind` and `ext` (undefined: unknown or gone).
   *  - read: a ref's bytes (media_call fills them in as base64, host-side).
   *  - fetch: a file a connection's tool result named, through the backend gateway's
   *    file route (the plugin passes only a url it saw in this turn's
   *    results, with the conn_id that returned it), stored as `generated` under `audience`.
   *  - postGroup (optional): record one send_media file for the group of the recorded turn -
   *    group_send_budget, caption exit-checked - {posted, channel, group_ref, caption?} or {refused}.
   *    With checkOnly, the same judgement as a dry run before anything is fetched (kind may be
   *    omitted): {may_post: true} or {refused}, no budget used and no row written.
   *  - setProtected (optional): refs the library must not clean (a relay not committed yet). */
  media?: {
    ingest(meta: { channel: string; messageRef?: string; index?: number; audience: string; source: 'in' | 'generated' },
      open: (options: { signal: AbortSignal }) => Promise<AsyncIterable<Uint8Array> | ReadableStream | Uint8Array>,
      options?: { signal?: AbortSignal }): Promise<{ ref: string; kind: 'image' | 'voice' | 'file';
      mime: string; size: number; durationMs?: number; duplicate?: boolean } | { refused: string; duplicate?: boolean }>;
    resolve(ref: string): Promise<undefined | { ref: string; path: string; kind: 'image' | 'voice' | 'file'; ext: string; mime: string;
      size: number; durationMs?: number; audience: string; sha256: string; file: string }>;
    read(ref: string): Promise<undefined | { row: object; bytes: Uint8Array }>;
    fetch(request: { connId: string; url: string; audience: string }, signal?: AbortSignal): Promise<{ ref: string } | { refused: string; reason?: string }>;
    postGroup?(request: { turn: string; kind?: 'image' | 'voice' | 'file'; caption?: string; checkOnly?: boolean }, signal?: AbortSignal):
      Promise<{ posted: true; message_id: string; channel: string; group_ref: string; caption?: string } | { may_post: true }
        | { refused: string; reason?: string }>;
    setProtected?(refs: () => Iterable<string>): void;
  };
  channels?: {
    /** Step 24.2: `attachments` - the files' metadata the plugin checked (each a kind and exactly
     *  one of a library ref or a refusal of the closed set; mime, size, durationMs), kept on the
     *  message's source row; never a byte or a name. */
    relay(inbound: { channel: 'slack' | 'wechat'; senderRef: string; messageRef: string; text: string; at?: string;
      attachments?: Array<{ kind: 'image' | 'voice' | 'file'; ref?: string; refused?: 'too_large' | 'type' | 'unsupported' | 'gone';
        mime?: string; size?: number; durationMs?: number }> }, signal?: AbortSignal):
      Promise<{ message_id: string | null; duplicate: boolean; decided_on_replay?: boolean; approval: null | { proposal_id: string; action: 'approve' | 'decline';
        outcome: 'executed' | 'refused'; refusal?: string; kind?: string; target?: string; result?: object }; approval_pending?: boolean } | { refused: string; reason?: string }>;
    bind(bind: { channel: 'slack' | 'wechat'; senderRef: string; code: string; messageRef: string }, signal?: AbortSignal):
      Promise<{ bound: boolean; channel: string; sender_ref: string; duplicate?: boolean; refusal?: string } | { refused: string; reason?: string }>;
    sourceOf(messageId: string, signal?: AbortSignal): Promise<null | { direction: 'in' | 'out'; channel: 'slack' | 'wechat';
      sender_ref: string | null; channel_message_ref: string | null; at: string; error: string | null }>;
    recordDelivery(delivery: { messageId: string; channel: 'slack' | 'wechat'; channelMessageRef?: string; error?: string },
      signal?: AbortSignal): Promise<object>;
    list(signal?: AbortSignal): Promise<{ channels: Array<{ channel: 'slack' | 'wechat'; bound: boolean; sender_ref: string | null;
      bound_at: string | null; revoked_at: string | null; is_default: boolean }>; default_channel: 'slack' | 'wechat' | null }>;
    codeCheck?(message: { channel: 'slack' | 'wechat'; senderRef: string; messageRef: string; text: string },
      signal?: AbortSignal): Promise<{ store_text: boolean }>;
    /** Step 24.2 (storeMedia step 1): is `senderRef` the channel's current binding? The relay's
     *  own judgement as a read; it throws when the host cannot be reached (storeMedia answers
     *  `unavailable`, transient). Absent: storeMedia answers `unsupported`. */
    senderBound?(who: { channel: 'slack' | 'wechat'; senderRef: string }, signal?: AbortSignal): Promise<{ bound: boolean }>;
    reportStatus?(status: { channel: 'slack' | 'wechat'; connected: boolean; detail?: string; qrSvg?: string }, signal?: AbortSignal):
      Promise<object>;
    dueAlerts?(signal?: AbortSignal): Promise<{ due: { channel: 'slack' | 'wechat'; kind: 'down' | 'up'; down_since: string; attempt: number }[] }>;
    claimAlert?(claim: { channel: 'slack' | 'wechat'; kind: 'down' | 'up'; downSince: string; attempt: number;
      exclude?: ('slack' | 'wechat')[] }, signal?: AbortSignal):
      Promise<object>;
    recordAlert?(outcome: { channel: 'slack' | 'wechat'; kind: 'down' | 'up'; downSince: string; attempt: number; error?: string },
      signal?: AbortSignal): Promise<object>;
  };
  /** Extend the loaded role table (e.g. forbid backend writes to workers) [unchanged].
   *  Synchronous; the result is validated (roles.json rules plus per-role
   *  allowed_actions / notes) and a Promise or an invalid table refuses the start. */
  roles?(table: object): object;
  /** Host-scoped tools/sections, before the director's plan tools [none]. */
  install?(api: DirectorApi): Promise<void>;
  /** Director-scope sections/tools/hooks, before the director's own; may call
   *  api.runtimeSection() and api.contextHooks() to place those [none]. */
  setup?(scope: unknown, subject: unknown, api: DirectorApi & { runtimeSection(): void; contextHooks(): void }): Promise<void>;
  /** Wording overrides: continuationQuestion, reviewInstruction, tools.<name>, ... [neutral defaults]. */
  texts?: Record<string, unknown>;
  /** Message source kinds and section names (persisted in session logs) [dsh-director-*]. */
  labels?: Record<string, string>;
}

/** What a channel's login (a new WeChat QR) answers: never the QR itself. */
/** v1.2, capability `groups` (step 23, design-steward 7.7): the family group's host side.
 *  list: the bound account per channel and the bound groups with their @ prefix; bind: a group
 *  code posted in a group; relay: a group @-message ({message_id, sender_role, sender_label} or
 *  {refused, notice?} or {duplicate}); reply: the group turn's last words (exit-checked, to the
 *  question's group: {text, channel, group_ref, withheld}); recent: that group's last exchanges;
 *  audience: record whose turn this is before its first step; ownerSpokeSince: an owner message
 *  newer than `since` is among a turn's input (lifts the pause); familyNote / familyShow: the
 *  family memory tools, accepted by the host only from a group turn. */
export interface GroupsHost {
  list(signal?: AbortSignal): Promise<{ owner_refs: Record<string, string>; groups: Array<{ channel: string; group_ref: string; label: string | null; mention_prefix: string }> }>;
  bind(a: { channel: string; groupRef: string; senderRef: string; messageRef: string; prefix: string; code: string }, signal?: AbortSignal): Promise<unknown>;
  relay(a: { channel: string; groupRef: string; senderRef: string; messageRef: string; text: string; at?: string;
    attachments?: object[] }, signal?: AbortSignal): Promise<unknown>;
  reply(a: { questionId: string; alsoAnswers: string[]; body: string }, signal?: AbortSignal): Promise<unknown>;
  recent(a: { channel: string; groupRef: string }, signal?: AbortSignal): Promise<{ recent: unknown[] }>;
  audience(a: { turn: string; audience: 'owner' | 'group'; group?: { channel: string; group_ref: string }; senderRefs: string[] }, signal?: AbortSignal): Promise<unknown>;
  ownerSpokeSince(since: string, input: string, signal?: AbortSignal): Promise<boolean>;
  /** `turn`: the turn the tool runs in (the plugin's open turn, the one `audience` recorded);
   *  the host refuses a request that does not name the recorded turn. */
  familyNote(text: string, signal?: AbortSignal, options?: { turn?: string }): Promise<unknown>;
  familyShow(signal?: AbortSignal, options?: { turn?: string }): Promise<unknown>;
  /** Step 25 C4: the family lists. `turn` as for the family memory: required by the host in a
   *  group turn (the public-read header), ignored in an owner turn. listRemove is an owner
   *  turn's alone (the guard refuses it in a group turn). */
  listShow(a: { list?: 'shopping' | 'todo' }, signal?: AbortSignal, options?: { turn?: string }): Promise<unknown>;
  listAdd(a: { list: 'shopping' | 'todo'; items: string[] }, signal?: AbortSignal, options?: { turn?: string }): Promise<unknown>;
  listCheck(a: { itemIds: string[]; done?: boolean }, signal?: AbortSignal, options?: { turn?: string }): Promise<unknown>;
  listRemove(a: { itemIds: string[] }, signal?: AbortSignal, options?: { turn?: string }): Promise<unknown>;
}

export type ChannelLoginAnswer = { requested: true } | { refused: string; retry_after_s?: number };

/** The cordis service `ownerChannels` (provided only with the host's `channels`), what a
 *  channel plugin injects. `register` returns the function that takes the channel back.
 *  `send` is given the text and where it goes: `to` is the account it is for (the bound
 *  owner, or the sender a binding answer is for), `replyToRef` the channel message it
 *  answers when there is one; it resolves with the channel's own ref for the sent message.
 *  `inbound` is one message that arrived; a text of exactly six digits from an account
 *  that is not bound is a binding attempt. Nothing but the root's own message to its owner
 *  and one-line answers about the sender's own message is ever sent. `status` is read when
 *  the channel registers and every 30 s after; `qrSvg` is a login QR drawn as an SVG while
 *  the channel needs a scan (it goes to the host's optional channels.reportStatus).
 *
 *  What a message still OWES survives the process: an `inbound` is written to a durable
 *  owed store BEFORE the host is asked (`owedFile` in createOwnerChannels, mode 0600:
 *  channel, sender, the message ref and the owner's own text), so a stop while the host call
 *  is still in flight loses nothing - a decision runs longer than the client's own bound, so
 *  a timeout there is ordinary, and the line it owes must still go out; a transient answer
 *  and a decision whose line has not been sent yet leave it there too. It is re-asked at
 *  `ready()` and every 30 s and cleared only once the line is actually sent or the host has
 *  answered for good. A plugin must not answer a message it could not send and forget it -
 *  the store is what a restart inherits.
 *
 *  The store must never hold a live binding code, the one secret the owner sends, and the
 *  text is written down only when the host says it may be: `channels.codeCheck` answers
 *  {store_text} with the same rule the relay applies (a bound sender, and no live code
 *  anywhere in the text in any rendering the backend folds - NFKC, every Nd block, and the
 *  code points unicodedata calls a digit). A digit rule of the client's own cannot do this:
 *  a run of six digits refuses an approval whose proposal id carries one (ids are 20 hex
 *  characters, about 38% of them carry such a run, and their line was lost outright), while
 *  `\p{Nd}{6}` misses every folded rendering (superscripts, circled, dingbat: measured 99
 *  code points) and wrote the live code down. Until the host says store_text the record is
 *  kept WITHOUT the text: a stop in that window costs the sender one "send it again" line,
 *  never a live code on disk. That line is only for a record a restart inherited: one whose
 *  last answer here was transient is still held by its plugin, which hands it in again, so
 *  it is left to that retry. A decision line goes only to the account bound NOW and never
 *  more than an hour after the owner wrote (CHANNEL_LIMITS.lineMaxAgeMs: the earlier of the
 *  channel's time and when it was first owed here); a redelivery past either is answered
 *  with no line and leaves the store. A store over owedMax drops a record without a text
 *  first, so a flood of strangers' messages never pushes out the owner's owed line. The store is 0600 in the director's own directory, never the
 *  backend's world-readable database. An answer of `{duplicate: true, approval: null}` with no
 *  `approval_pending` is read as finished (a backend that predates that marker cannot say
 *  "still deciding", so its line is lost): accepted for the shipped pair, revisit before
 *  running a director plugin against an older backend. */
export interface OwnerChannels {
  /** `login` (optional; the WeChat channel's): ask for a new login QR now. It answers
   *  {requested: true} or {refused, retry_after_s?} and never the QR, which the channel
   *  shows through `status().qrSvg` only.
   *  `sendFile` and `mediaKinds` (optional, step 24.2, design-steward 7.8.12): one file the
   *  steward's send_media sends - to the bound owner (`to`, `replyToRef?`) or into a group
   *  (`groupRef`) - and the kinds it can send; without sendFile, or for a kind not in
   *  mediaKinds, send_media is refused channel_no_media. */
  register(channel: { name: 'slack' | 'wechat'; send(text: string, options: { replyToRef?: string; to?: string }): Promise<{ ref?: string }>;
    status(): { connected: boolean; detail?: string; qrSvg?: string };
    login?(): Promise<ChannelLoginAnswer>;
    sendFile?(file: { to?: string; groupRef?: string; replyToRef?: string; kind: 'image' | 'voice' | 'file'; mime: string; name: string;
      path: string; caption?: string }): Promise<{ ref?: string }>;
    mediaKinds?: Array<'image' | 'voice' | 'file'> }): () => void;
  /** Step 24.2: `attachments` (optional) - what storeMedia answered for each of the message's
   *  files: each EXACTLY one of `ref` (an m_ library ref storeMedia gave; a channel's own ids
   *  never come here) or `refused` (too_large | type | unsupported | gone - a channel writes
   *  these itself for what it knows it cannot take: a declared size over the bound, a kind it
   *  cannot fetch, a file the source no longer has). A message whose storeMedia answered
   *  channel_sender_not_bound comes WITHOUT attachments. Anything outside that closed set
   *  refuses the message invalid_inbound. The placeholder lines are written by the director
   *  (lib/channels.mjs), never by a channel; the text may be empty when files came. */
  inbound(message: { channel: 'slack' | 'wechat'; senderRef: string; messageRef: string; text: string; at?: string;
    attachments?: Array<{ kind: 'image' | 'voice' | 'file'; ref?: string; refused?: 'too_large' | 'type' | 'unsupported' | 'gone';
      mime?: string; size?: number; name?: string; durationMs?: number }> }):
    Promise<{ accepted: boolean; reason?: string }>;
  /** Step 24.2: store attachment `index` of message `messageRef` BEFORE inbound. In order: (1)
   *  the sender is the channel's current binding (host.channels.senderBound; unreachable:
   *  `unavailable`, transient - hold the message and ask again), else channel_sender_not_bound
   *  and `open` is never called; (2) a stored (channel, messageRef, index) answers its ref
   *  without calling `open`; (3) `open()` streams the bytes, held to the limits and the type
   *  table. {ref, kind, mime, size, durationMs?} or {refused: too_large | type | unsupported |
   *  gone | unavailable | channel_sender_not_bound | invalid_media}. `open` may throw
   *  {refused: 'too_large' | 'gone'} (a bridge's 413 / 410); any other throw is `unavailable`. */
  storeMedia(meta: { channel: 'slack' | 'wechat'; senderRef: string; messageRef: string; index: number; kind: 'image' | 'voice' | 'file';
    mime?: string; name?: string }, open: () => Promise<AsyncIterable<Uint8Array> | ReadableStream | Uint8Array>):
    Promise<{ ref: string; kind: string; mime: string; size: number; durationMs?: number } | { refused: string }>;
}

/** What `createOwnerChannels` returns: `service` is the whole plugin-facing surface (it is
 *  what the host provides as `ownerChannels`, so `register`/`inbound`/`storeMedia` and nothing else), and
 *  the rest is the director's own control of it. `ready()` hands the host's handle in and
 *  starts the status and owed polls; `pollStatus()`/`pollOwed()` are those polls' own steps;
 *  `ownerMessageSent` mirrors the root's message to the owner; `stop()` ends both timers. */
export interface OwnerChannelsHandle {
  service: OwnerChannels;
  ownerMessageSent(messageId: string, body: string, replyTo?: string): Promise<{ mirrored: boolean; reason?: string }>;
  /** A new login on a registered channel (its optional `login`): {requested: true} - then
   *  the status is reported every 5 s for 3 minutes so the QR shows up quickly - or
   *  {refused: not_registered | not_supported | unavailable | rate_limited (past
   *  CHANNEL_LIMITS.loginPerHour/loginPerDay accepted logins: the channel is not asked) |
   *  the channel's own code, retry_after_s?}. Only those fields are passed on; never throws. Behind the steward's
   *  request_wechat_qr tool and DirectorApi.ownerChannelLogin. */
  login(channel: 'slack' | 'wechat'): Promise<ChannelLoginAnswer>;
  /** Step 24.2: where a file answering owner message `replyTo` goes ({channel, to, replyToRef?})
   *  or {refused: dashboard_no_media | not_bound | channel_not_connected | channel_no_media}. */
  ownerMediaTarget(replyTo: string, kind?: string): Promise<{ channel: string; to: string; replyToRef?: string } | { refused: string }>;
  /** Step 24.2: one file through `channel`'s sendFile: {sent, channel, ref?} or {refused}. */
  sendFile(channel: string, file: object): Promise<{ sent: true; channel: string; ref?: string } | { refused: string; reason?: string }>;
  /** Whether `channel` can send a file of `kind` - or, kind omitted, of any kind. */
  canSendFile(channel: string, kind?: string): boolean;
  ready(): void;
  pollStatus(): Promise<void>;
  /** Every message the director still owes an answer asks again now (the 30 s poll's own step). */
  pollOwed(): Promise<void>;
  stop(): void;
}
