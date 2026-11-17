import { createHash } from 'node:crypto';

/** The AItelier side of the steward (plugin step 12, design-steward 4): its owner's
 *  conversation recorded into memory, the owner-message check, the read-only portfolio
 *  (which includes the steward's own inbox page, read whole) and the handoff, all over
 *  State reads and two State writes the steward is allowed (send_user_message is
 *  runtime.mjs's tool; send_director_message is the handoff).
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

/** The directorHost v1.2 operations the steward runs with on AItelier. */
export function stewardOps({ request, project, me }) {
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
      return { handed_off: true, project_id: projectId, cites_user_message_id: citesUserMessageId, ...(result.result ?? result) };
    },
  };
}
