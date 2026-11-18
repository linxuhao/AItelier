import { createHash } from 'node:crypto';

/** The AItelier side of the steward (plugin step 12, design-steward 4): its owner's
 *  conversation recorded into memory, the owner-message check, the read-only portfolio
 *  (which includes the steward's own inbox page, read whole) and the handoff, all over
 *  State reads and the State writes the steward is allowed (send_user_message is
 *  runtime.mjs's tool; send_director_message is the handoff; acknowledge_director_message
 *  settles one delivery in its own PROJECT inbox, acknowledge_driver_message marks one
 *  read in its own PRIVATE inbox, and nothing else).
 *
 *  `request(path, body, signal)` is the runtime's authenticated State caller. */

/** How many conversation pages a sync or an owner-message check reads at most. */
const PAGE = 100, MAX_PAGES = 10, OWNER_CHECK_PAGES = 5;

/** The bounds of the steward's inbox reader (`portfolio.inbox`), stated in its tool
 *  description by the plugin (dsh-director/lib/portfolio.mjs) and echoed in every answer
 *  as `limits`. They exist because a wake injects only the first 240 characters of each
 *  body (runtime.mjs): one call must be able to read a long message whole without letting
 *  a page of long messages flood the steward's context. A body past `body_chars` comes
 *  back clipped and flagged with its true size; a page past `items_per_page`, or one whose
 *  bodies would pass `answer_chars`, is cut and counted. */
export const INBOX_LIMITS = Object.freeze({ items_per_page: 5, body_chars: 8000, answer_chars: 24000 });

/** One delivery of the steward's inbox: its delivery identity and the whole stored body,
 *  the way `list_director_messages` carries it. The body is clipped only past the per-body
 *  bound, and then it says its true `body_chars` and `body_truncated`. */
function inboxItem(row) {
  const message = row?.message ?? {}, delivery = row?.delivery ?? {};
  const body = typeof message.body === 'string' ? message.body : message.body == null ? '' : String(message.body);
  const seq = Number(delivery.delivery_seq);
  return {
    delivery_id: delivery.delivery_id ?? null,
    delivery_seq: Number.isFinite(seq) ? seq : null,
    message_id: message.message_id ?? null,
    subject: message.subject ?? null,
    sender_project_id: message.sender_project_id ?? null,
    created_at: message.created_at ?? null,
    status: delivery.status ?? null,
    version: delivery.version ?? null,
    body_chars: body.length,
    ...(body.length > INBOX_LIMITS.body_chars ? { body_truncated: true } : {}),
    body: body.length > INBOX_LIMITS.body_chars ? body.slice(0, INBOX_LIMITS.body_chars) : body,
  };
}

const textOf = message => [message.body, ...(message.segments ?? []).map(s => s.text)]
  .filter(t => typeof t === 'string' && t.trim()).join('\n\n');
const older = (a, b) => (a.created_at < b.created_at ? -1 : a.created_at > b.created_at ? 1 : a.message_id < b.message_id ? -1 : 1);

/** The conversation as memory turns, oldest first. An owner message opens a turn (turn id =
 *  its message id, part `o`); a message this steward sent joins the turn of the owner
 *  message it replies to, else of the latest owner message before it, else opens its own;
 *  its part is its own message id, so a reply recorded later never renumbers anything.
 *  Messages from anyone else are not the owner's conversation with this steward. */
export function conversationTurns(messages, me) {
  const turns = new Map();
  let current;
  for (const m of [...messages].sort(older)) {
    if (m.sender_kind === 'user') {
      current = m.message_id;
      turns.set(current, [{ role: 'owner', part: 'o', at: m.created_at, text: textOf(m) }]);
    } else if (m.sender_kind === 'driver' && m.sender_driver_id === me) {
      // State lets a driver's message reply only to a user's message, so reply_to names an
      // owner message even when that message is older than the pages read.
      const turn = m.reply_to_message_id ?? current ?? m.message_id;
      if (!turns.has(turn)) turns.set(turn, []);
      turns.get(turn).push({ role: 'steward', part: m.message_id, at: m.created_at, text: textOf(m) });
    }
  }
  return turns;
}

/** Record the conversation into memory: newest pages first, until a page reaches an owner
 *  message memory already knows (or MAX_PAGES), then every turn on them - recordTurn writes only
 *  what is new, and never brings back what was forgotten. */
export async function syncConversation({ request, project, me, memory, signal }) {
  if (!memory) return { written: 0 };
  const messages = [];
  let cursor;
  for (let page = 0; page < MAX_PAGES; page++) {
    const listed = await request('/api/state/query/list_user_conversation',
      { project_id: project, limit: PAGE, ...(cursor ? { cursor } : {}) }, signal);
    const result = listed.result ?? listed;
    messages.push(...(result.messages ?? []));
    // An owner message memory already has means everything older was synced before.
    const known = (result.messages ?? []).some(m => m.sender_kind === 'user' && memory.known(`t-${m.message_id}-o`));
    if (known || !result.next_cursor) break;
    cursor = result.next_cursor;
  }
  let written = 0;
  for (const [turn, parts] of conversationTurns(messages, me)) written += (await memory.recordTurn(turn, parts)).written ?? 0;
  return { written };
}

/** The owner's message with this id in the steward's own conversation, or undefined. */
async function ownerMessageById({ request, project, id, signal }) {
  let cursor;
  for (let page = 0; page < OWNER_CHECK_PAGES; page++) {
    const listed = await request('/api/state/query/list_user_conversation',
      { project_id: project, limit: PAGE, ...(cursor ? { cursor } : {}) }, signal);
    const result = listed.result ?? listed;
    const hit = (result.messages ?? []).find(m => m.message_id === id);
    if (hit) return hit.sender_kind === 'user' ? hit : undefined;
    if (!result.next_cursor) return undefined;
    cursor = result.next_cursor;
  }
  return undefined;
}

/** A State refusal as a tool answer: the error text, bounded. */
const refusedBy = error => ({ refused: 'state_refused', reason: String(error?.message ?? error).slice(0, 500) });

/** The steward's open proposals, for its snapshot. */
export async function openProposals(request, signal) {
  const listed = await request('/api/state/query/list_steward_proposals', { status: 'open', limit: 50 }, signal);
  return (listed.result ?? listed).proposals ?? [];
}

// ---- letters: replying to a director and marking a letter read (owner 2026-11-17) ----

const digest = text => createHash('sha256').update(String(text)).digest('hex').slice(0, 16);

/** A delivery addressed to the steward, with what a reply needs: in its project inbox
 *  (`project`, State director messages) or its private driver inbox (`private`). */
export async function findLetter({ request, project, me, inbox, deliveryId, signal }) {
  let after = 0;
  for (let page = 0; page < 10; page++) {
    const listed = inbox === 'private'
      ? await request('/api/state/query/list_driver_messages', { driver_id: me, after, limit: 100 }, signal)
      : await request('/api/state/query/list_director_messages', { project_id: project, protocol_version: 'v3',
        statuses: ['unread', 'acknowledged', 'resolved'], after, limit: 100 }, signal);
    const result = listed.result ?? listed;
    if (inbox === 'private') {
      const row = (result.messages ?? []).find(m => m.delivery_id === deliveryId);
      if (row) return { deliveryId, version: row.version, status: row.status, subject: row.subject, sender: row.sender_driver_id };
      if (!result.truncated) return undefined;
      after = result.next_after;
    } else {
      const item = (result.items ?? []).find(i => i.delivery?.delivery_id === deliveryId);
      if (item) return { deliveryId, version: item.delivery.version, status: item.delivery.status,
        subject: item.message?.subject, sender: item.message?.sender_project_id };
      if (!result.has_more || !(result.items ?? []).length) return undefined;
      after = result.next_after;
    }
  }
  return undefined;
}

/** Reply to one letter addressed to the steward: back to its sender (the sending project,
 *  or the sending director privately), threaded on that delivery. The server allows the
 *  steward a private message only as such a reply. */
export async function replyToLetter({ request, project, me, inbox, deliveryId, text, signal }) {
  if (!['project', 'private'].includes(inbox)) return { refused: 'unknown_inbox', inboxes: ['project', 'private'] };
  if (typeof text !== 'string' || !text.trim()) return { refused: 'empty_text' };
  const letter = await findLetter({ request, project, me, inbox, deliveryId, signal });
  if (!letter || !letter.sender) return { refused: 'unknown_delivery', reason: `no ${inbox} letter ${deliveryId} addressed to you` };
  const subject = ('Re: ' + (letter.subject ?? '')).slice(0, 200);
  const key = `reply:${deliveryId}:${digest(text)}`;
  const answer = inbox === 'private'
    ? request('/api/state/commands/send_driver_message', { request_key: key, subject, body: text.trim(),
      target_driver_id: letter.sender, reply_to_delivery_id: deliveryId }, signal)
    // A reply names exactly ONE destination: the cited delivery. Sending target_project_id
    // as well is refused ("choose exactly one destination; replies omit target_project_id"),
    // and the server already derives the recipient from the letter the delivery belongs to.
    : request('/api/state/commands/send_director_message', { protocol_version: 'v3', sender_project_id: project,
      director_identity: me, request_key: key, subject, body: text.trim(),
      reply_to_delivery_id: deliveryId }, signal);
  // A refusal can arrive as a 200 with isError=false and a code inside the result, so the
  // answer decides the outcome: reported as a success, the steward believes a letter it
  // never answered went out (measured 2026-11-17, issue iss-8ba3f8ce4b254704).
  return answer.then(r => {
    const result = r?.result ?? r ?? {};
    const code = result.code ?? result.refused ?? result.error?.code;
    if (code) {
      const detail = result.detail?.message ?? result.detail ?? result.message ?? result.error?.message ?? null;
      return { refused: String(code), inbox, to: letter.sender, ...(detail ? { reason: String(detail).slice(0, 500) } : {}) };
    }
    return { replied: true, inbox, to: letter.sender, ...result };
  }, refusedBy);
}

/** Mark ONE letter addressed to the steward's PRIVATE inbox read (owner 2026-11-17:
 *  the steward settles both boxes, acknowledgement only). A wake re-injects every unread
 *  private delivery, so this - not a reply - is what stops the same letter coming back.
 *
 *  Bounded by construction: the letter is looked up in the steward's own inbox only, so a
 *  delivery addressed to another driver is `unknown_delivery` here and no write is made;
 *  `version` is optional (the looked-up version is used when it is omitted) and a supplied
 *  version that the delivery has moved past is refused rather than silently overridden.
 *  The server allows the steward `acknowledge_driver_message` and nothing else in this
 *  inbox: resolve is not offered, and break-glass is never sent. The request key is the
 *  delivery identity and version, so a retry is State's replay, not a second
 *  acknowledgement. */
export async function acknowledgePrivateLetter({ request, project, me, deliveryId, version, signal }) {
  if (typeof deliveryId !== 'string' || !deliveryId.trim()) {
    return { refused: 'invalid_delivery', reason: 'delivery_id must name a delivery in your private inbox' };
  }
  if (version !== undefined && (!Number.isInteger(version) || version < 1)) {
    return { refused: 'invalid_version', reason: 'version must be the delivery version you were shown (1 or more)' };
  }
  const letter = await findLetter({ request, project, me, inbox: 'private', deliveryId, signal });
  if (!letter) return { refused: 'unknown_delivery', reason: `no private letter ${deliveryId} addressed to you` };
  const expected = letter.version;
  if (version !== undefined && version !== expected) {
    return { refused: 'version_conflict', delivery_id: deliveryId, current_version: expected,
      reason: `that letter is at version ${expected}, not ${version}: read it again before acknowledging` };
  }
  // A refusal can arrive as a 200 with a code inside the result (the same shape replyToLetter
  // guards against), so the answer decides the outcome: reported as success, the steward
  // believes a letter it never settled stopped being re-injected.
  return request('/api/state/commands/acknowledge_driver_message', { delivery_id: deliveryId,
    expected_version: expected, request_key: `steward-ack-private:${deliveryId}:${expected}` }, signal).then(r => {
    const result = r?.result ?? r ?? {};
    const code = result.code ?? result.refused ?? result.error?.code;
    if (code) {
      const detail = result.detail?.message ?? result.detail ?? result.message ?? result.error?.message ?? null;
      return { refused: 'state_refused', code: String(code), delivery_id: deliveryId,
        ...(detail ? { reason: String(detail).slice(0, 500) } : {}) };
    }
    return { acknowledged: true, inbox: 'private', ...result };
  }, refusedBy);
}

// ---- every owner message gets an answer (owner 2026-11-17: 管家需要事事有回应) ----

/** The owner's messages after the steward's last message in its own conversation: the ones
 *  not answered yet, oldest first (the newest page; a reply answers what came before it). */
export async function unansweredOwnerMessages({ request, project, me, signal }) {
  const listed = await request('/api/state/query/list_user_conversation', { project_id: project, limit: 30 }, signal);
  const newest = [...((listed.result ?? listed).messages ?? [])].sort(older).reverse();
  const unanswered = [];
  for (const m of newest) {
    if (m.sender_kind === 'driver' && m.sender_driver_id === me) break;
    if (m.sender_kind === 'user') unanswered.push(m);
  }
  return unanswered.reverse();
}

const blockText = message => (message?.content ?? []).filter(b => b?.type === 'text').map(b => b.text ?? '').join('');

/** Watches the steward's session events and calls `onFinalText(text, input)` when a turn
 *  ends normally: the last thing the steward said in it, and everything the turn was given
 *  (its user/message inputs: the wakes, the snapshots). */
export function turnWatcher(onFinalText) {
  let last = '', input = '';
  return event => {
    if (event?.type === 'turn/start') { last = ''; input = ''; }
    else if (event?.type === 'user/message') input += blockText(event.data?.message ?? event.data) + '\n';
    else if (event?.type === 'assistant/message') {
      const text = blockText(event.data?.message).trim();
      if (text) last = text;
    } else if (event?.type === 'turn/end') {
      const text = last, given = input; last = ''; input = '';
      if (text && event.data?.reason?.kind === 'completed') onFinalText(text, given);
    }
  };
}

/** A turn that was given an owner message ended with that message still unanswered: what
 *  the steward said last goes to the owner as the answer to the newest such message (the
 *  steward's words reach the owner only as a sent message). Not sent when the turn was not
 *  given an unanswered owner message (a turn woken by a director's letter answers nobody).
 *  One automatic reply per owner message: the request key is the message id alone, so a
 *  retry after a lost answer is State's replay and a second, different text is refused
 *  (idempotency_conflict) instead of becoming a second reply. */
export async function forwardFinalText({ request, project, me, text, input = '', signal }) {
  const waiting = (await unansweredOwnerMessages({ request, project, me, signal }))
    .filter(m => input.includes(m.message_id));
  if (!waiting.length) return { forwarded: false };
  const newest = waiting.at(-1);
  const body = text.length > 8000 ? text.slice(0, 7990) + '\n[…]' : text;
  try {
    const result = await request('/api/state/commands/send_user_message', { request_key: `auto-reply:${newest.message_id}`,
      project_id: project, subject: ('Re: ' + (newest.subject ?? '')).slice(0, 200), body, reply_to_message_id: newest.message_id }, signal);
    return { forwarded: true, reply_to: newest.message_id, message_id: (result.result ?? result).message_id };
  } catch (error) {
    if (/idempotency_conflict/.test(String(error?.message ?? error))) return { forwarded: false, already_replied: newest.message_id };
    throw error;
  }
}

/** The steward's wording where the director's would mislead it (the director's names State
 *  claims, frontier nodes and workers the steward does not have). */
const stewardAsk = 'Anything you wait on from your owner (a decision, an approval) must be a question they can see: ask it with ask_owner before you go dormant.';
export const stewardTexts = Object.freeze({
  heldWork: 'your owner has messages you have not answered: answer each with send_user_message first',
  idleDormant: 'Call when nothing is waiting for you: your owner\'s messages are answered and no director letter needs a reply. Nothing is expected of you until your owner or a director writes; any owner message, director letter or proposal result wakes you. wake_after_minutes is how long you sleep before you look again. Compacts a large context once. Refused while your owner has a message you have not answered.',
  askOwner: 'Ask your owner ONE concrete question only they can decide (a choice, an approval). question_id is yours (1-64 of letters, digits, . _ : -): the same id with the same question is a safe retry; another question under that id is refused. context is optional background shown with it. The answer comes back to you as a message from your owner. Use close_question when it no longer needs an answer.',
  closeQuestion: 'Take back one of your own questions to your owner that no longer needs an answer, with the reason; it stays visible as withdrawn. An answered question cannot be closed; closing twice changes nothing.',
  continuationQuestion: 'Steward check: you ended a turn. Is every message from your owner answered with send_user_message? Is every director letter that needs it answered with reply_to_director, and read ones marked with portfolio_inbox_acknowledge (a project letter) or private_inbox_acknowledge (a letter in your private inbox)? Anything to pass on with handoff, propose, or keep with memory_note? Do it now; if nothing is left, answer with one line that starts with NOTHING_LEFT:. ' + stewardAsk,
  reviewInstruction: 'Look again at your owner\'s conversation, your inbox (portfolio_inbox) and your open proposals: answer what waits on you, pass on what your owner asked you to pass on, and tell your owner what changed for them. ' + stewardAsk + ' If nothing waits, reply with one line that starts with NOTHING_LEFT: and call idle_dormant.',
});

/** The host texts the steward runs with: the director's, with every line that names a
 *  director-only capability replaced. */
export const stewardHostTexts = texts => ({ ...texts, askTheUser: stewardAsk, heldWork: stewardTexts.heldWork,
  continuationQuestion: stewardTexts.continuationQuestion, reviewInstruction: stewardTexts.reviewInstruction,
  reviewDue: 'AItelier steward review due:\n',
  tools: { ...texts.tools, idle_dormant: stewardTexts.idleDormant, ask_owner: stewardTexts.askOwner, close_question: stewardTexts.closeQuestion } });

/** The directorHost v1.2 operations the steward runs with on AItelier. */
/** Handoffs the steward sent, kept by the runtime for its open-threads view (7.3 layer 2). */
export async function recordHandoff(stateDir, row) {
  if (!stateDir) return;
  const { appendFile } = await import('node:fs/promises');
  const { join } = await import('node:path');
  await appendFile(join(stateDir, 'handoffs.jsonl'), JSON.stringify(row) + '\n', { mode: 0o600 });
}

/** Design-steward 7.3 layer 2: what is open, computed from data each snapshot - the owner's
 *  unanswered messages and the last 7 days of handoffs, each with whether its project wrote
 *  back (a reply in the steward's inbox citing that handoff's delivery). Never throws. */
export async function openThreads({ request, project, me, stateDir, now = Date.now(), signal }) {
  const threads = {};
  try {
    threads.unanswered_owner_messages = (await unansweredOwnerMessages({ request, project, me, signal }))
      .map(m => ({ message_id: m.message_id, at: m.created_at, text: textOf(m).slice(0, 300) }));
  } catch (error) { threads.unanswered_owner_messages = { error: String(error?.message ?? error).slice(0, 200) }; }
  try {
    const { readFile } = await import('node:fs/promises');
    const { join } = await import('node:path');
    const since = new Date(now - 7 * 24 * 3600e3).toISOString();
    const sent = (await readFile(join(stateDir, 'handoffs.jsonl'), 'utf8').catch(error => { if (error.code === 'ENOENT') return ''; throw error; }))
      .split('\n').filter(Boolean).map(line => { try { return JSON.parse(line); } catch { return undefined; } })
      .filter(h => h && h.at >= since);
    if (sent.length) {
      const listed = await request('/api/state/query/list_director_messages', { project_id: project, protocol_version: 'v3',
        statuses: ['unread', 'acknowledged', 'resolved'], after: 0, limit: 100 }, signal);
      const replied = new Set(((listed.result ?? listed).items ?? []).map(i => i.message?.reply_to_delivery_id).filter(Boolean));
      threads.handoffs_last_7_days = sent.map(h => ({ at: h.at, project_id: h.project_id, cites_user_message_id: h.cites_user_message_id,
        answered: (h.delivery_ids ?? []).some(id => replied.has(id)) }));
    } else threads.handoffs_last_7_days = [];
  } catch (error) { threads.handoffs_last_7_days = { error: String(error?.message ?? error).slice(0, 200) }; }
  return threads;
}

export function stewardOps({ request, project, me, stateDir }) {
  const list = (path, body, signal) => request(path, body, signal).then(r => r.result ?? r);
  return {
    memory: { ownerMessage: async (id, signal) => Boolean(await ownerMessageById({ request, project, id, signal })) },
    portfolio: {
      async projects(signal) {
        const [projects, drivers] = await Promise.all([list('/api/state/query/list_projects', {}, signal),
          request('/api/dsh-drivers', undefined, signal)]);
        const rows = (Array.isArray(projects) ? projects : projects.projects ?? []).filter(p => p.project_id !== project);
        return { projects: await Promise.all(rows.map(async p => {
          const driver = drivers.projects?.[p.project_id];
          const questions = await list('/api/state/query/list_user_questions', { project_id: p.project_id, status: 'open', limit: 200 }, signal)
            .catch(error => ({ error: String(error?.message ?? error).slice(0, 200) }));
          return { project_id: p.project_id, title: p.title,
            director: driver ? { phase: driver.managed?.phase ?? 'unmanaged', online: driver.online ?? 0 } : null,
            // More than the newest 200 open: said as "200+", not hidden.
            open_questions: questions.error ? { error: questions.error }
              : questions.has_more ? `${(questions.questions ?? []).length}+` : (questions.questions ?? []).length };
        })) };
      },
      async project(projectId, signal) {
        const [overview, issues, questions] = await Promise.all([
          list('/api/state/query/project_overview', { project_id: projectId }, signal),
          list('/api/state/query/list_issues', { project_id: projectId, statuses: ['open'], limit: 50 }, signal),
          list('/api/state/query/list_user_questions', { project_id: projectId, status: 'open', limit: 50 }, signal)]);
        return { project_id: projectId, overview, open_issues: issues, open_questions: questions };
      },
      conversation: (projectId, cursor, signal) => list('/api/state/query/list_user_conversation',
        { project_id: projectId, limit: 30, ...(cursor ? { cursor } : {}) }, signal),
      /** This steward's OWN inbox, one bounded page, bodies whole: the wake injection of a
       *  delivery carries only its first 240 characters, so a longer message is unread until
       *  it is fetched here. Read-only: the same query the director reads, no acknowledgement
       *  and no write. `after` is the last delivery_seq read; the answer says where the next
       *  page starts (`next_after`) and what it did not show (`items_omitted`,
       *  `body_truncated`, `has_more`) instead of clipping silently. */
      async inbox({ after, limit } = {}, signal) {
        const start = Number.isFinite(Number(after)) ? Math.max(0, Math.trunc(Number(after))) : 0;
        const want = Number.isFinite(Number(limit))
          ? Math.min(Math.max(Math.trunc(Number(limit)), 1), INBOX_LIMITS.items_per_page) : INBOX_LIMITS.items_per_page;
        const page = await list('/api/state/query/list_director_messages', { project_id: project,
          protocol_version: 'v3', statuses: ['unread', 'acknowledged'], after: start, limit: want }, signal);
        const rows = (Array.isArray(page.items) ? page.items : []).slice(0, want);
        // What this cursor has left: the query counts the deliveries after it, so its
        // matched_total is a remainder, not the size of the whole inbox.
        const matched = Number.isInteger(page.matched_total)
          ? Math.max(page.matched_total, rows.length) : rows.length + (page.has_more === true ? 1 : 0);
        const items = rows.map(inboxItem);
        const answer = (kept, omitted = 0) => ({ project_id: project, items: kept, matched_total: matched,
          has_more: matched > kept.length,
          next_after: matched > kept.length ? kept.at(-1)?.delivery_seq ?? null : null,
          items_omitted: omitted, limits: { ...INBOX_LIMITS } });
        let kept = items, built = answer(kept);
        // One call must not flood the steward's context: keep the oldest deliveries of the
        // page that fit the answer bound, and let the cursor say where the next call resumes.
        while (JSON.stringify(built).length > INBOX_LIMITS.answer_chars && kept.length > 1) {
          kept = kept.slice(0, -1);
          built = answer(kept, items.length - kept.length);
        }
        return built;
      },
      /** Acknowledge ONE delivery of this steward's OWN inbox: the wake states each
       *  delivery's id and version (projectInboxDigest), and State records the ack under
       *  this steward's own driver id, so an acknowledged delivery's body is withheld
       *  from later wakes. Bounded by construction: the only project named is this
       *  steward's own (the server refuses any other), and every field the caller could
       *  widen is checked here first. One write; the request key is derived from the
       *  delivery identity, so a retry is State's replay, not a second acknowledgement.
       *  resolve is not offered: dismissing standing guidance is not the steward's call. */
      async acknowledge({ deliveryId, version } = {}, signal) {
        if (typeof deliveryId !== 'string' || !deliveryId.trim()) {
          return { refused: 'invalid_delivery', reason: 'delivery_id must name a delivery in your own inbox' };
        }
        if (!Number.isInteger(version) || version < 1) {
          return { refused: 'invalid_version', reason: 'version must be the delivery version you were shown (1 or more)' };
        }
        let response;
        try {
          response = await request('/api/state/commands/acknowledge_director_message', {
            project_id: project, protocol_version: 'v3', delivery_id: deliveryId,
            expected_version: version, request_key: `steward-ack:${deliveryId}:${version}` }, signal);
        } catch (error) {
          return refusedBy(error);
        }
        // The director-messaging contract answers a refusal as an envelope with `code`
        // (e.g. unknown_delivery), not an HTTP error: say so, never return it as success.
        if (response?.code) {
          return { refused: 'state_refused', code: String(response.code),
            reason: String(response.detail?.message ?? response.code).slice(0, 500) };
        }
        return response?.result ?? response;
      },
      /** Acknowledge ONE delivery of this steward's PRIVATE inbox, so the wake stops
       *  re-injecting it. The same narrowing as `acknowledge`, one box over: the letter
       *  must be one addressed to this steward (findLetter reads only its own inbox), the
       *  server allows acknowledgement only, and no break-glass is ever sent. */
      acknowledgePrivate({ deliveryId, version } = {}, signal) {
        return acknowledgePrivateLetter({ request, project, me, deliveryId, version, signal });
      },
    },
    /** Plugin step 13: one open proposal row and nothing more; the owner decides, the backend
     *  executes the version approved and wakes the steward with the result. The request key
     *  is the proposal's digest, so a retried call is State's replay, not a second card. */
    proposals: {
      async propose({ kind, payload, reason, supersedes }, signal) {
        const body = { kind, payload, reason, ...(supersedes ? { supersedes } : {}) };
        const key = 'proposal:' + createHash('sha256').update(JSON.stringify(body)).digest('hex').slice(0, 40);
        return request('/api/state/commands/create_steward_proposal', { request_key: key, ...body }, signal)
          .then(r => r.result ?? r, refusedBy);
      },
      withdraw: ({ id, reason }, signal) => request('/api/state/commands/withdraw_steward_proposal',
        { request_key: `withdraw-proposal:${id}`, proposal_id: id, reason }, signal).then(r => r.result ?? r, refusedBy),
    },
    async handoff({ projectId, text, citesUserMessageId }, signal) {
      if (typeof projectId !== 'string' || !projectId || projectId === project) return { refused: 'invalid_project', reason: 'name another project' };
      if (typeof text !== 'string' || !text.trim()) return { refused: 'empty_text' };
      const cited = typeof citesUserMessageId === 'string' && citesUserMessageId
        ? await ownerMessageById({ request, project, id: citesUserMessageId, signal }) : undefined;
      if (!cited) return { refused: 'not_an_owner_message', reason: 'cites_user_message_id must name a message your owner wrote to you' };
      const quoted = textOf(cited).split('\n').map(line => '> ' + line).join('\n');
      // One handoff per (owner message, project): a retry is State's idempotent replay; a
      // different text for the same message and project is State's idempotency_conflict.
      const result = await request('/api/state/commands/send_director_message', {
        sender_project_id: project, director_identity: me, target_project_id: projectId,
        request_key: `handoff:${citesUserMessageId}:${projectId}`, subject: 'From your owner, via the steward',
        body: `${text.trim()}\n\nYour owner wrote (message ${citesUserMessageId}, ${cited.created_at}):\n${quoted}` }, signal)
        .catch(error => ({ error: String(error?.message ?? error) }));
      if (result.error) return /idempotency_conflict/.test(result.error)
        ? { refused: 'already_handed_off', reason: `message ${citesUserMessageId} was already passed to ${projectId} with other words` }
        : { refused: 'state_refused', reason: String(result.error).slice(0, 500) };
      const sent = result.result ?? result;
      await recordHandoff(stateDir, { at: new Date().toISOString(), project_id: projectId, cites_user_message_id: citesUserMessageId,
        message_id: sent.message?.message_id ?? null, delivery_ids: (sent.deliveries ?? []).map(d => d.delivery_id) })
        .catch(() => {});
      return { handed_off: true, project_id: projectId, cites_user_message_id: citesUserMessageId, ...sent };
    },
  };
}
