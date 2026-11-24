/** dsh-director's built-in host: the smallest directorHost that implements every
 *  operation of the contract, on plain local files. Insert it after dsh-director
 *  in a profile and the director runs with no backend:
 *
 *    - { insert: [{ id: 'dsh-director', name: '<dir>/dsh-director/index.mjs' },
 *                 { id: 'director-local', name: '<dir>/dsh-director/hosts/local.mjs',
 *                   config: { root, rolesFile?, todo?: { module?, stateDirEnv?, python?, cwd? }, ... } }] }
 *
 *  `root`, `provider` and `model` are required (README.md has a whole profile). The rest
 *  has defaults: `rolesFile` the plugin's own roles.json (with director.md and worker.md
 *  next to it), `todo` the plugin's own todo package (python -m dsh_todo in todo/, state in
 *  DSH_TODO_STATE_DIR), `dshEntry` the real path of the running DSH entry (a global `dsh`
 *  symlink resolved), `sessionEnv` LOCAL_SESSION_ENV, which this plugin sets on its own
 *  process so every job a shell starts carries it (watch_process, idle_dormant); any of
 *  them can be overridden. At startup (install) the host checks what the plugin needs on
 *  this machine - not Windows, python3 with PyYAML for the todo package, git - and refuses
 *  to start naming what is missing (README.md: Linux only).
 *
 *  An external host (the backend adapter, or anyone's) replaces this plugin;
 *  it is never active next to one. It deliberately stays small: work items with a
 *  state and a holder, one notes file, an inbox directory of wake events and a
 *  progress log. No dependency graph, evidence, review or multi-director
 *  coordination - a deployment that needs those plugs in a host that has them.
 *
 *  Files under `root`:
 *    work.json       { "items": [{ "id", "title", "state", "holder"?, "node_key"? }] }
 *    notes.md        the director's notes (note_write appends, once per request_key)
 *    inbox/*.json|txt one wake event per file, id 'inbox:<file>'; delivered, then moved to inbox/done/
 *    progress.jsonl  one phase report per line
 *    questions.json  the director's questions to its owner: { "<id>": { text, context?, status
 *                    (open | answered | withdrawn), asked_at, answer?, answered_by?, answered_at?,
 *                    reason? } }; the owner answers with /answer (below) or by dropping
 *                    inbox/answer-<id>.json ({ "answer": "...", "answered_by"?: "..." }); an
 *                    answer without answered_by is recorded as owner:local
 *    proposals.json  the steward's proposals, with `projectsDir` only (below); each records the
 *                    request_key it was made under (a digest of its body: a retried propose is
 *                    answered with the proposal it made, never a second open card)
 *    proposals-seq.json the last proposal number given out: an id is never given twice, even
 *                    after a proposal is deleted from proposals.json by hand
 *    state/          the director's own state (todo plans)
 *
 *  Plugin step 15 (standalone DSH): the owner's commands, registered in DSH's command registry
 *  (ctx.commands; not registered when the process has none) - see local-commands.mjs:
 *  /questions, /answer <id> <text>, /proposals, /approve <id>, /decline <id> [reason], /profile,
 *  /purge <id>, /forget <id> [reason], /restore <id> (the last two wake the steward with one line,
 *  inbox/owner-memory-<ms>-<n>-<random hex>.txt). A DSH command carries no user identity, so whatever the owner decides through
 *  them is recorded as owner:local (answered_by, decided_by).
 *  With `projectsDir` (a directory whose every subdirectory is one local host root, its name
 *  the project id) the host also offers the steward `portfolio` (those projects, read-only) and
 *  `proposals` of the one kind it can execute itself: create_project, a new subdirectory with
 *  project.json, notes.md (the goal) and work.json (one work item per seed node). */
import { readFile, writeFile, appendFile, readdir, mkdir, rename, stat, rm } from 'node:fs/promises';
import { readFileSync, realpathSync } from 'node:fs';
import { execFile } from 'node:child_process';
import { dirname, join } from 'node:path';
import { createHash, randomBytes } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { WORK_STATES, isSettled } from '../lib/host.mjs';
import { registerOwnerCommands } from './local-commands.mjs';

export const name = 'dsh-director-local-host';
export const inject = [];

async function readJson(file, fallback) {
  try { return JSON.parse(await readFile(file, 'utf8')); }
  catch (error) { if (error.code === 'ENOENT') return fallback; throw error; }
}
async function writeJson(file, value) {
  await writeFile(file + '.tmp', JSON.stringify(value, null, 2) + '\n');
  await rename(file + '.tmp', file);
}
/** The variable that tags every process this DSH starts with the director's session id, when
 *  the config names none. Not DSH_-prefixed: DSH rebuilds the DSH_* namespace for every
 *  model shell call and drops ambient DSH_* values, so such a tag never reaches a job. */
export const LOCAL_SESSION_ENV = 'DIRECTOR_LOCAL_SESSION';
/** The DSH release this host is tested on (the repository's pin; README.md). Another one
 *  starts with a warning, not a refusal. */
export const TESTED_DSH_VERSION = '0.2.1-rc.5';
/** The plugin's python requirements (PyYAML, for the todo package). */
const REQUIREMENTS = fileURLToPath(new URL('../requirements.txt', import.meta.url));
/** The running DSH entry, its symlinks resolved: a global install starts from
 *  <prefix>/bin/dsh, a link from which DSH's own libraries cannot be found. */
const runningEntry = () => { try { return realpathSync(process.argv[1]); } catch { return process.argv[1]; } };
/** What the director reads about this host, after its role prompt (director root only). */
export const HOST_NOTE = `This director runs on the built-in local host: plain files under one root directory, no
dashboard, no steward and no other director. Where your role prompt speaks of the steward, a dashboard
or chat channels, none of them exist here.
- Work items are in work.json under that root. Record them with work_update (id; title; state working,
  input_required, completed, failed or cancelled; holder, empty to release) and keep notes with
  note_write: both come back in every context snapshot. A new task arrives as a file in inbox/; take it
  as a work item before you plan it. A work item's id is the attempt_id you plan, dispatch and finish
  under; node_key and context_hash are optional here (node_key defaults to the attempt_id).
- Your plans and the runtime's own records are in the state/ directory under that root, not in your
  working directory. Your role prompt's list of entries never to delete names another host's files; here
  never delete, move or overwrite anything under that root except through these tools.
- Your owner reads progress.jsonl and questions.json there and answers a question with the /answer
  command or a file inbox/answer-<id>.json; the answer comes back to you as an event.`;
/** Who answered when nothing says: the owner of this local host (plugin step 15). */
export const LOCAL_OWNER = 'owner:local';
/** What the standalone steward tells its owner to type (the commands in local-commands.mjs). */
const STEWARD_OWNER_COMMANDS = 'Your owner is in this DSH, with no Slack, WeChat or dashboard: they list your open questions with /questions and answer one with /answer <id> <text>';
const STEWARD_PROPOSAL_COMMANDS = '; they list your open proposals with /proposals and decide one with /approve <id> or /decline <id> [reason].'
  + ' When you ask them to decide a proposal, give them that command instead of a 同意/拒绝 proposal line';
/** With memory: memory_forget refuses here (no_owner_channel - a DSH command carries no owner
 *  message to cite), so the steward sends its owner to the commands that forget and restore. */
const STEWARD_MEMORY_COMMANDS = '; they see their resident profile with /profile, forget one memory entry with /forget <id> [reason]'
  + ' and bring it back with /restore <id>, or remove one from every version, for good, with /purge <id>.'
  + ' When memory_forget refuses with no_owner_channel, give them /forget <id> [reason] instead';
/** One page of a portfolio answer, as the backend adapter's steward host pages one (steward-host.mjs
 *  NODES_CHARS): as many entries as fit while the whole answer stays under this many characters,
 *  and at least one, so a walk always moves forward. */
export const PAGE_CHARS = 9000;
/** Open questions shown in portfolio.project: the newest, with their total. */
export const OPEN_QUESTIONS_SHOWN = 20;
/** A proposal whose request_key comes again in one of these states answers the retry with
 *  itself; after a decline, a failure or a withdrawal the same proposal makes a new card.
 *  Director 472 accepted this difference from steward-host.mjs, within these bounds:
 *  - steward-host replays a key whatever the status, because there the backend executes
 *    proposals and a replay must never execute twice; here the host executes create_project
 *    itself, and declined/failed/withdrawn let the same body make a new card, so the recovery
 *    advice "propose it again" works after a failure;
 *  - the key is a digest of kind, the stored payload, the trimmed reason and supersedes: a retry
 *    replays only when it repeats the same reason and supersedes verbatim; any change is a new card;
 *  - the two answers stay distinct: { proposed: false, duplicate: true, proposal_id, status }
 *    for a replay, { proposed: true, proposal_id, status: 'open' } for a new card. */
const REPLAYED = new Set(['open', 'executing', 'done', 'superseded']);
const isRecord = value => value !== null && typeof value === 'object' && !Array.isArray(value);
/** A local project id is a directory name: the backend's key rule, so a project keeps its id elsewhere. */
const PROJECT_ID = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;
const ANSWER_FILE = /^answer-([A-Za-z0-9._:-]{1,64})\.json$/;
/** A map's own entry (an id typed by the owner is never `__proto__`'s). */
const own = (map, key) => Object.hasOwn(map, key) ? map[key] : undefined;
const exists = path => stat(path).then(() => true, error => { if (error.code === 'ENOENT' || error.code === 'ENOTDIR') return false; throw error; });

/** Why `payload` is not a create_project the local host can execute, or undefined. */
function createProjectProblem(payload) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return 'payload must be an object';
  const { project_id: id, title, goal, seed_nodes: seeds = [] } = payload;
  if (typeof id !== 'string' || !PROJECT_ID.test(id)) return 'project_id must be 1-128 letters, digits, _ . - starting with a letter or digit';
  if (typeof title !== 'string' || !title.trim() || title.length > 400) return 'title must be 1-400 characters';
  if (typeof goal !== 'string' || !goal.trim() || goal.length > 4000) return 'goal must be 1-4000 characters';
  if (!Array.isArray(seeds) || seeds.length > 200) return 'seed_nodes must be a list of at most 200 nodes';
  const keys = new Set();
  for (const node of seeds) {
    if (!node || typeof node.key !== 'string' || !PROJECT_ID.test(node.key) || keys.has(node.key)) return 'every seed node needs its own key (letters, digits, _ . -)';
    if (typeof node.goal !== 'string' || !node.goal.trim()) return `seed node ${node.key} needs a goal`;
    if (!Array.isArray(node.acceptance) || !node.acceptance.length) return `seed node ${node.key} needs at least one acceptance check`;
    keys.add(node.key);
  }
  return undefined;
}

/** @param options - root, provider, model (required), sessionId, dshEntry, rolesFile, sessionEnv,
 *  permissionPreset, objective, holder, pollMs, todo: { python, cwd, module, stateDirEnv },
 *  projectsDir (step 15). */
export function localHost(options = {}) {
  const root = options.root;
  if (!root) throw new Error('directorHost refused: missing config: root (the local host keeps its files there)');
  const holder = options.holder ?? options.sessionId ?? 'director';
  const files = { work: join(root, 'work.json'), notes: join(root, 'notes.md'), inbox: join(root, 'inbox'),
    done: join(root, 'inbox', 'done'), progress: join(root, 'progress.jsonl'), noteKeys: join(root, 'note-keys.json'),
    questions: join(root, 'questions.json'), proposals: join(root, 'proposals.json'), proposalSeq: join(root, 'proposals-seq.json') };
  const questions = async () => readJson(files.questions, {});
  // questions.json is read-modified-written by ask, close and an arriving answer; one
  // at a time, so two asks in one director turn cannot lose one. The lock also guards
  // writeJson's single fixed tmp name (questions.json.tmp): without it two writers race
  // on that file and one fails with ENOENT, a louder failure than a lost update.
  let questionsLock = Promise.resolve();
  const locked = fn => { const run = questionsLock.then(fn); questionsLock = run.catch(() => {}); return run; };
  // proposals.json the same way: propose, withdraw and the owner's decisions, one at a time.
  let proposalsLock = Promise.resolve();
  const lockedProposals = fn => { const run = proposalsLock.then(fn); proposalsLock = run.catch(() => {}); return run; };
  const projectsDir = options.projectsDir;
  const items = async () => (await readJson(files.work, { items: [] })).items ?? [];
  let timer, finish, lastFailure = '';

  /** The owner's answer (plugin step 15, /answer): the answer file first - it is the wake event
   *  and survives a crash before questions.json is written; the sweep records it then - and
   *  the question after, so /questions reads it answered at once. An answer wins over a
   *  withdrawal, as on the sweep; a question is answered once. */
  const answer = ({ id, text, by = LOCAL_OWNER }) => locked(async () => {
    const question = own(await questions(), id);
    if (!question || !ANSWER_FILE.test(`answer-${id}.json`)) return { refused: 'unknown_question', reason: `no question ${id} was asked` };
    if (question.status === 'answered') return { refused: 'question_answered', reason: `${id} is answered already`, answer: question.answer };
    const file = join(files.inbox, `answer-${id}.json`);
    if (await exists(file)) return { refused: 'answer_waiting', reason: `an answer to ${id} is already waiting in the inbox` };
    await mkdir(files.inbox, { recursive: true });
    await writeFile(file + '.tmp', JSON.stringify({ answer: text, answered_by: by }) + '\n');
    await rename(file + '.tmp', file);
    const all = await questions();
    const at = new Date().toISOString();
    all[id] = { ...all[id], status: 'answered', answer: text, answered_by: by, answered_at: at };
    await writeJson(files.questions, all);
    return { answered: true, question_id: id, answered_by: by, answered_at: at };
  });

  // ---- projectsDir: the steward's portfolio and proposals (plugin step 15)
  const projectIds = async () => {
    let entries;
    try { entries = await readdir(projectsDir, { withFileTypes: true }); }
    catch (error) { if (error.code === 'ENOENT') return []; throw error; }
    return entries.filter(e => e.isDirectory() && PROJECT_ID.test(e.name)).map(e => e.name).sort();
  };
  const projectOf = async id => {
    if (typeof id !== 'string' || !PROJECT_ID.test(id)) return undefined;
    const dir = join(projectsDir, id);
    return await stat(dir).then(s => s.isDirectory(), () => false) ? dir : undefined;
  };
  const unknownProject = id => ({ refused: 'unknown_project', reason: `no project ${id} under ${projectsDir}` });
  // The whole progress log is read for its last line: one short line per phase report, so
  // a listing stays cheap; a host whose logs grow large keeps its own index.
  const lastPhase = async dir => {
    let text = '';
    try { text = await readFile(join(dir, 'progress.jsonl'), 'utf8'); } catch (error) { if (error.code !== 'ENOENT') throw error; }
    const last = text.trim().split('\n').at(-1);
    try { const { phase, at } = JSON.parse(last); return { phase, at }; } catch { return null; }
  };
  const questionsOf = async dir => Object.entries(await readJson(join(dir, 'questions.json'), {})).map(([id, q]) => ({ ...q, id }));
  const portfolio = {
    async projects() {
      // One project whose files do not parse (edited by hand) is reported as such; it does
      // not hide the others.
      return { projects: await Promise.all((await projectIds()).map(async id => {
        const dir = join(projectsDir, id);
        try {
          const meta = await readJson(join(dir, 'project.json'), {});
          return { project_id: id, title: meta.title ?? null, director: await lastPhase(dir),
            open_questions: (await questionsOf(dir)).filter(q => q.status === 'open').length };
        } catch (error) {
          return { project_id: id, error: 'unreadable', reason: String(error?.message ?? error).slice(0, 300) };
        }
      })) };
    },
    /** One project, its open work one page at a time (the backend adapter's shape): in work-item id
     *  order, as many as fit under PAGE_CHARS, then nodes_shown.open_next_after to pass back as
     *  open_after (null: there is no more, an empty list included). The cursor is the last id
     *  shown (a keyset), so an item that closes between two pages moves nothing else. */
    async project(id, _signal, { openAfter } = {}) {
      const dir = await projectOf(id);
      if (!dir) return unknownProject(id);
      const meta = await readJson(join(dir, 'project.json'), {});
      const work = (await readJson(join(dir, 'work.json'), { items: [] })).items ?? [];
      const counts = {};
      for (const item of work) counts[item.state] = (counts[item.state] ?? 0) + 1;
      const asked = (await questionsOf(dir)).filter(q => q.status === 'open')
        .sort((a, b) => String(b.asked_at ?? '').localeCompare(String(a.asked_at ?? '')) || a.id.localeCompare(b.id));
      const base = { project_id: id, title: meta.title ?? null, goal: meta.goal ?? null, director: await lastPhase(dir), work_counts: counts,
        open_work: [], open_questions: asked.slice(0, OPEN_QUESTIONS_SHOWN).map(({ id: qid, text, asked_at }) => ({ id: qid, text, asked_at })),
        open_questions_total: asked.length };
      // A cursor is the id a previous page ended on; anything else is refused by name, never
      // read as "start again".
      if (openAfter !== undefined && openAfter !== null && (typeof openAfter !== 'string' || !openAfter))
        return { ...base, open_work: { refused: 'invalid_open_after', reason: 'open_after is the open_next_after string a previous answer gave' } };
      const key = item => String(item.id);
      const open = work.filter(item => !isSettled(item.state)).sort((a, b) => (key(a) < key(b) ? -1 : key(a) > key(b) ? 1 : 0));
      const rest = openAfter ? open.filter(item => key(item) > openAfter) : open;
      const shown = () => ({ open_after: openAfter ?? null, open: base.open_work.length, open_total: open.length,
        open_remaining_after_page: rest.length - base.open_work.length,
        open_next_after: base.open_work.length < rest.length ? key(base.open_work.at(-1)) : null });
      for (const item of rest) {
        base.open_work.push(item);
        if (base.open_work.length > 1 && JSON.stringify({ ...base, nodes_shown: shown() }).length > PAGE_CHARS) { base.open_work.pop(); break; }
      }
      return { ...base, nodes_shown: shown() };
    },
    /** The local host keeps no owner/director conversation (it is in that director's DSH
     *  session); what it has is the questions and their answers, newest first (by when each
     *  was answered, else asked), one page at a time under PAGE_CHARS. next_cursor is the
     *  last message shown, to pass back as cursor; null (has_more false) means there is no
     *  more, an empty list included. A question answered between two pages moves to the
     *  top, so it is on the next walk's first page. */
    async conversation(id, cursor) {
      const dir = await projectOf(id);
      if (!dir) return unknownProject(id);
      const at = q => String(q.answered_at ?? q.asked_at ?? '');
      const order = (a, b) => at(b).localeCompare(at(a)) || a.id.localeCompare(b.id);
      let after;
      if (cursor !== undefined && cursor !== null && cursor !== '') {
        try { after = JSON.parse(Buffer.from(String(cursor), 'base64url').toString('utf8')); } catch { after = undefined; }
        if (!Array.isArray(after) || after.length !== 2 || !after.every(v => typeof v === 'string'))
          return { refused: 'invalid_cursor', reason: 'cursor is the next_cursor a previous answer gave' };
      }
      const asked = (await questionsOf(dir)).sort(order);
      const rest = after ? asked.filter(q => order(q, { id: after[1], answered_at: after[0] }) > 0) : asked;
      const page = { project_id: id, messages: [], messages_total: asked.length,
        note: 'the local host keeps only the questions to the owner and their answers; the conversation itself is in that director\'s DSH session' };
      const answer = () => {
        const more = page.messages.length < rest.length, last = page.messages.at(-1);
        return { ...page, remaining_after_page: rest.length - page.messages.length, has_more: more,
          next_cursor: more ? Buffer.from(JSON.stringify([at(last), last.id])).toString('base64url') : null };
      };
      for (const q of rest) {
        page.messages.push(q);
        if (page.messages.length > 1 && JSON.stringify(answer()).length > PAGE_CHARS) { page.messages.pop(); break; }
      }
      return answer();
    },
  };
  const proposalList = async () => readJson(files.proposals, {});
  /** A proposal's result, as a wake event for the steward: one file in its own inbox. */
  const wakeSteward = async proposal => {
    await mkdir(files.inbox, { recursive: true });
    // Encoded: a hand-edited id never names a file outside the inbox.
    const file = join(files.inbox, `proposal-${encodeURIComponent(proposal.id)}-${proposal.status}.json`);
    await writeFile(file + '.tmp', JSON.stringify({ proposal_id: proposal.id, kind: proposal.kind, status: proposal.status,
      decided_by: proposal.decided_by ?? null, ...(proposal.decline_reason ? { reason: proposal.decline_reason } : {}),
      ...(proposal.result ? { result: proposal.result } : {}) }) + '\n');
    await rename(file + '.tmp', file);
  };
  /** An owner's memory command (/forget, /restore) as a wake event: one line in its own inbox file.
   *  The counter starts again at a restart, so a random suffix keeps a wake from one process
   *  life from overwriting an undelivered one from the last, even within the same millisecond. */
  let wakes = 0;
  const wakeLine = async line => {
    await mkdir(files.inbox, { recursive: true });
    const file = join(files.inbox, `owner-memory-${Date.now()}-${++wakes}-${randomBytes(4).toString('hex')}.txt`);
    await writeFile(file + '.tmp', line + '\n');
    await rename(file + '.tmp', file);
  };
  /** create_project, all or nothing: built in a hidden directory, then renamed into place. */
  const createProject = async (proposalId, payload) => {
    const final = join(projectsDir, payload.project_id);
    if (await exists(final)) return { error: 'project_id_taken', reason: `project ${payload.project_id} already exists; nothing was written` };
    const building = join(projectsDir, `.${payload.project_id}.creating-${proposalId}`);
    try {
      await rm(building, { recursive: true, force: true });
      await mkdir(building, { recursive: true });
      await writeJson(join(building, 'project.json'), { project_id: payload.project_id, title: payload.title, goal: payload.goal,
        proposal_id: proposalId, created_at: new Date().toISOString() });
      await writeFile(join(building, 'notes.md'), payload.goal.trim() + '\n\n');
      const items = (payload.seed_nodes ?? []).map(node => ({ id: node.key, title: node.goal, state: 'working', acceptance: node.acceptance }));
      await writeJson(join(building, 'work.json'), { items });
      await rename(building, final);
      return { project_id: payload.project_id, root: final, work_items: items.length };
    } catch (error) {
      await rm(building, { recursive: true, force: true }).catch(() => {});
      return { error: 'create_failed', reason: String(error?.message ?? error).slice(0, 500) };
    }
  };
  const proposals = {
    kinds: ['create_project'],
    propose: ({ kind, payload, reason, supersedes }) => lockedProposals(async () => {
      if (kind !== 'create_project') return { refused: 'unknown_kind', kinds: ['create_project'], reason: 'the local host can only create a local project' };
      const problem = createProjectProblem(payload);
      if (problem) return { refused: 'invalid_payload', reason: problem };
      if (typeof reason !== 'string' || !reason.trim()) return { refused: 'reason_required', reason: 'say why, in your owner\'s terms' };
      const all = await proposalList();
      const stored = { project_id: payload.project_id, title: payload.title, goal: payload.goal, seed_nodes: payload.seed_nodes ?? [] };
      // The request key is the proposal's digest, as the backend adapter's (steward-host.mjs): a
      // retried call is answered with the proposal it made, not a second open card.
      const requestKey = 'proposal:' + createHash('sha256').update(JSON.stringify({ kind, payload: stored, reason: reason.trim(),
        ...(supersedes !== undefined ? { supersedes } : {}) })).digest('hex').slice(0, 40);
      const made = Object.entries(all).find(([, p]) => isRecord(p) && p.request_key === requestKey && REPLAYED.has(p.status));
      if (made) return { proposed: false, duplicate: true, proposal_id: made[0], status: made[1].status };
      if (supersedes !== undefined && own(all, supersedes)?.status !== 'open') return { refused: 'not_open', reason: `${supersedes} is not an open proposal` };
      // A number is never given twice: the last one given is kept apart from proposals.json
      // (written first), so a proposal deleted from it by hand does not free its id.
      // Director 472: because the seq file is written before proposals.json, a proposals.json
      // write that fails burns that number - the next proposal skips it, never reuses it.
      // Accepted: a gap in the numbers is harmless, a reused id is not.
      const numbers = Object.keys(all).map(key => /^p-(\d+)$/.exec(key)?.[1]).filter(Boolean).map(Number);
      const last = Math.max(0, Number((await readJson(files.proposalSeq, {})).last) || 0, ...numbers);
      const id = 'p-' + (last + 1);
      const at = new Date().toISOString();
      await mkdir(root, { recursive: true });
      await writeJson(files.proposalSeq, { last: last + 1 });
      if (supersedes !== undefined) all[supersedes] = { ...all[supersedes], status: 'superseded', superseded_by: id, decided_at: at };
      all[id] = { id, kind, payload: stored, reason: reason.trim(), status: 'open', created_at: at, request_key: requestKey,
        ...(supersedes !== undefined ? { supersedes } : {}) };
      await writeJson(files.proposals, all);
      return { proposed: true, proposal_id: id, status: 'open' };
    }),
    withdraw: ({ id, reason }) => lockedProposals(async () => {
      const all = await proposalList();
      const proposal = own(all, id);
      if (!proposal) return { refused: 'unknown_proposal', reason: `no proposal ${id}` };
      if (proposal.status === 'withdrawn') return { withdrawn: false, already_withdrawn: true, proposal_id: id };
      if (proposal.status !== 'open') return { refused: 'not_open', status: proposal.status };
      all[id] = { ...proposal, status: 'withdrawn', withdraw_reason: reason, decided_at: new Date().toISOString() };
      await writeJson(files.proposals, all);
      return { withdrawn: true, proposal_id: id };
    }),
  };
  /** The owner's decision (/approve, /decline): an open proposal only; an approval runs it now. */
  const decide = ({ id, approve, reason, by = LOCAL_OWNER }) => lockedProposals(async () => {
    const all = await proposalList();
    const proposal = own(all, id);
    if (!isRecord(proposal)) return { refused: 'unknown_proposal', reason: `no proposal ${id}` };
    if (proposal.status !== 'open') return { refused: 'not_open', reason: `${id} is ${proposal.status}`, status: proposal.status };
    // The map key is the id (a record's own id field could name another file in the inbox).
    const decided = { ...proposal, id, decided_at: new Date().toISOString(), decided_by: by };
    if (!approve) {
      all[id] = { ...decided, status: 'declined', ...(reason ? { decline_reason: reason } : {}) };
    } else {
      all[id] = { ...decided, status: 'executing' };
      await writeJson(files.proposals, all);
      // A record edited by hand into something no proposal could be fails by name; it does
      // not throw the approval away half-done.
      const problem = proposal.kind === 'create_project' ? createProjectProblem(proposal.payload) : `the local host cannot run ${proposal.kind}`;
      const result = problem ? { error: 'invalid_proposal', reason: problem } : await createProject(id, proposal.payload);
      all[id] = { ...all[id], status: result.error ? 'failed' : 'done', result };
    }
    // The wake file first, as /answer does: a crash before the final status leaves the
    // proposal open (decline) or executing (recovered and announced at the next start);
    // at worst the steward hears the result twice, never not at all.
    await wakeSteward(all[id]);
    await writeJson(files.proposals, all);
    return all[id];
  });
  /** A proposal a crash left executing: done when its project is there and names it, else failed. */
  const recoverProposals = () => lockedProposals(async () => {
    const all = await proposalList();
    for (const [key, proposal] of Object.entries(all).filter(([, p]) => isRecord(p) && p.status === 'executing')) {
      const id = proposal.payload?.project_id;
      // Only names the host itself could have built are touched: a hand-edited id never
      // reaches a path outside projectsDir.
      const safe = typeof id === 'string' && PROJECT_ID.test(id) && PROJECT_ID.test(key);
      const meta = safe ? await readJson(join(projectsDir, id, 'project.json'), {}).catch(() => ({})) : {};
      all[key] = meta.proposal_id === key
        ? { ...proposal, id: key, status: 'done', result: { project_id: id, root: join(projectsDir, id), recovered: true } }
        : { ...proposal, id: key, status: 'failed', result: { error: 'interrupted', reason: 'the run was interrupted; propose it again' } };
      if (safe) await rm(join(projectsDir, `.${id}.creating-${key}`), { recursive: true, force: true }).catch(() => {});
      await wakeSteward(all[key]);
      await writeJson(files.proposals, all);
    }
  });

  return {
    version: '1.0.0',
    capabilities: ['progress', 'workState', 'heldWork', 'roles', 'install', 'setup', 'attention', ...(projectsDir ? ['portfolio', 'proposals'] : [])],
    config: {
      sessionId: options.sessionId ?? 'director',
      provider: options.provider, model: options.model,
      stateDir: options.stateDir ?? join(root, 'state'),
      dshEntry: options.dshEntry ?? runningEntry(),
      rolesFile: options.rolesFile ?? fileURLToPath(new URL('../roles.json', import.meta.url)),
      sessionEnv: options.sessionEnv ?? LOCAL_SESSION_ENV,
      // This host prints nothing of its own when the director cannot start: the plugin prints
      // the first line of the reason. A plan here binds to a work item by its id alone.
      startupErrorDetail: true, planKeysOptional: true,
      ...(options.permissionPreset ? { permissionPreset: options.permissionPreset } : {}),
      todo: { python: 'python3', cwd: fileURLToPath(new URL('../todo', import.meta.url)), module: 'dsh_todo',
        stateDirEnv: 'DSH_TODO_STATE_DIR', ...(options.todo ?? {}) },
      idleReviewMs: options.idleReviewMs, dormantMinuteMs: options.dormantMinuteMs, watchIntervalMs: options.watchIntervalMs,
      clockToolGapMs: options.clockToolGapMs,
      dormantCompactMinTokens: options.dormantCompactMinTokens,
      maxActiveSubagents: options.maxActiveSubagents, reservedSlots: options.reservedSlots,
      // Plugin step 11: the owner's memory, passed through as given (absent: none).
      ...(options.memory ? { memory: options.memory } : {}),
      // Plugin step 12: run the root session as the steward (needs roles.json's steward).
      ...(options.rootRole ? { rootRole: options.rootRole } : {}),
    },
    /** The startup / post-compaction snapshot: the work items and the notes; for the steward
     *  also how its owner answers and decides here (step 15: steward.md speaks of Slack,
     *  WeChat and a dashboard, none of which a standalone DSH has). */
    async context(role) {
      let notes = '';
      try { notes = await readFile(files.notes, 'utf8'); } catch (error) { if (error.code !== 'ENOENT') throw error; }
      return JSON.stringify({ work: await items(), notes, ...(role === 'steward' ? { owner_commands: STEWARD_OWNER_COMMANDS
        + (projectsDir ? STEWARD_PROPOSAL_COMMANDS : '') + (options.memory ? STEWARD_MEMORY_COMMANDS : '') + '.' } : {}) });
    },
    async progress(body) {
      await mkdir(root, { recursive: true });
      await appendFile(files.progress, JSON.stringify({ at: new Date().toISOString(), ...body }) + '\n');
      // A failed model call (no key, an unknown model) is the commonest first error, and with no
      // dashboard it would sit unseen in progress.jsonl: its first line goes to stderr too, once
      // per distinct error.
      const failure = body?.phase === 'failed' && typeof body.error === 'string' ? body.error : '';
      if (failure && failure !== lastFailure) console.error('dsh-director failed: ' + failure.split('\n')[0].slice(0, 500));
      lastFailure = failure;
    },
    async workState(id) {
      const item = (await items()).find(entry => entry.id === id);
      return item && WORK_STATES.includes(item.state) ? item.state : 'unknown';
    },
    async heldWork() {
      return (await items()).filter(item => item.holder === holder && !['completed', 'failed', 'cancelled'].includes(item.state))
        .map(item => ({ ...item, node_key: item.node_key ?? item.id }));
    },
    /** The director's questions to its owner (plugin step 8), in questions.json. */
    attention: {
      ask: ({ id, text, context }) => locked(async () => {
        await mkdir(root, { recursive: true });
        const all = await questions();
        if (all[id]) return all[id].text === text ? { asked: false, duplicate: true, question_id: id, status: all[id].status }
          : { refused: 'question_id_reused', reason: `question ${id} already asks something else; give this question its own id` };
        all[id] = { text, ...(context ? { context } : {}), status: 'open', asked_at: new Date().toISOString() };
        await writeJson(files.questions, all);
        return { asked: true, question_id: id };
      }),
      close: ({ id, reason }) => locked(async () => {
        const all = await questions();
        const question = all[id];
        if (!question) return { refused: 'unknown_question', reason: `no question ${id} was asked` };
        if (question.status === 'answered') return { refused: 'question_answered', reason: `your owner already answered ${id}` };
        if (question.status === 'withdrawn') return { closed: false, already_closed: true, question_id: id };
        all[id] = { ...question, status: 'withdrawn', reason, withdrawn_at: new Date().toISOString() };
        await writeJson(files.questions, all);
        return { closed: true, question_id: id };
      }),
      async list() {
        return Object.entries(await questions()).filter(([, q]) => q.status === 'open').map(([id, q]) => ({ id, text: q.text }));
      },
    },
    /** The table as loaded, with this host's two recording tools shown to the director (they
     *  are registered in its scope only; without this they would be deferred behind search_tools). */
    roles: table => ({ ...table, director: { ...table.director, presented: [...table.director.presented, 'work_update', 'note_write'] } }),
    /** Before the director exists: refuse to start, naming each one, when something the plugin
     *  needs is missing here; warn on a DSH release this host is not tested on. */
    async install(api) {
      const problems = await prerequisiteProblems({ todo: api.todo, director: (options.rootRole ?? 'director') === 'director',
        memory: !!options.memory });
      if (problems.length) throw new Error('dsh-director local host cannot start here: ' + problems.join('; '));
      const version = dshVersion(api.config.dshEntry);
      if (version !== TESTED_DSH_VERSION)
        api.ctx.logger.warn(`dsh-director local host: tested on DSH ${TESTED_DSH_VERSION}, running ${version ?? 'an unknown version'}`);
    },
    /** The director records with two small tools: a work item's state, and a note. */
    async setup(scope, _subject, { defineTool, jsonOutput, runtimeSection }) {
      if ((options.rootRole ?? 'director') === 'director') {
        runtimeSection();
        scope.systemPrompt.section({ name: 'dsh-director:local-host', order: 12.5, interpolate: false, text: HOST_NOTE });
      }
      scope.tools.register(defineTool({ name: 'work_update',
        description: 'Director only. Create or update one work item in the local work list: id, and any of title, state (working, input_required, completed, failed, cancelled), holder (empty to release).',
        parameters: { id: { type: 'string', required: true }, title: { type: 'string' }, state: { type: 'string' }, holder: { type: 'string' } },
        output: jsonOutput,
        async execute(args) {
          if (args.state !== undefined && !WORK_STATES.includes(args.state) || args.state === 'unknown')
            return { refused: 'invalid_state', reason: `state must be one of ${WORK_STATES.filter(s => s !== 'unknown').join(', ')}` };
          await mkdir(root, { recursive: true });
          const list = await items();
          const at = list.findIndex(item => item.id === args.id);
          const item = { ...(at >= 0 ? list[at] : { id: args.id, state: 'working' }),
            ...Object.fromEntries(['title', 'state', 'holder'].filter(k => args[k] !== undefined).map(k => [k, args[k]])) };
          if (item.holder === '') delete item.holder;
          if (at >= 0) list[at] = item; else list.push(item);
          await writeJson(files.work, { items: list });
          return { item };
        },
      }));
      scope.tools.register(defineTool({ name: 'note_write',
        description: 'Director only. Append one paragraph to your notes; they are part of every startup and post-compaction snapshot. request_key makes it safe to retry: the same key with the same text writes once, the same key with other text is refused.',
        parameters: { text: { type: 'string', required: true }, request_key: { type: 'string', required: true } }, output: jsonOutput,
        async execute(args) {
          const text = String(args.text ?? '').trim();
          const key = String(args.request_key ?? '').trim();
          if (!text) return { refused: 'empty_note', reason: 'say something' };
          if (!key) return { refused: 'request_key_required', reason: 'give each note its own request_key' };
          await mkdir(root, { recursive: true });
          const keys = await readJson(files.noteKeys, {});
          const digest = createHash('sha256').update(text).digest('hex');
          if (keys[key] === digest) return { written: false, duplicate: true };
          if (keys[key]) return { refused: 'request_key_reused', reason: `request_key ${key} already wrote another note` };
          await appendFile(files.notes, text + '\n\n');
          keys[key] = digest;
          await writeJson(files.noteKeys, keys);
          return { written: true };
        },
      }));
    },
    /** Wake events: every file that lands in inbox/ is delivered, then moved to
     *  inbox/done/ (the move is the cursor). A crash before the move redelivers it under
     *  the same id; the director recognises it. */
    async events(api) {
      const { ctx, notify, agent, isDisposed } = api;
      await mkdir(files.done, { recursive: true });
      if (options.objective && !ctx.goals.get(agent)) ctx.goals.create(agent, { objective: options.objective });
      const sweep = async () => {
        const names = (await readdir(files.inbox, { withFileTypes: true })).filter(e => e.isFile() && !e.name.endsWith('.tmp'))
          .map(e => e.name).sort();
        for (const file of names) {
          if (isDisposed()) return;
          const path = join(files.inbox, file);
          const [text, { ino, mtimeMs }] = await Promise.all([readFile(path, 'utf8'), stat(path)]);
          const answered = ANSWER_FILE.exec(file)?.[1];
          if (answered) {
            // The owner's answer: recorded on the question (an answer wins over a
            // withdrawal) with who answered (step 15: owner:local when the file does not
            // say), then delivered naming the question, then moved like any event.
            let answer, by;
            try { ({ answer, answered_by: by } = JSON.parse(text)); } catch { answer = undefined; }
            const answeredBy = typeof by === 'string' && by.trim() ? by.trim() : LOCAL_OWNER;
            const all = await locked(async () => {
              const current = await questions();
              if (current[answered] && typeof answer === 'string') {
                current[answered] = { ...current[answered], status: 'answered', answer, answered_by: answeredBy,
                  answered_at: current[answered].answered_at ?? new Date().toISOString() };
                await writeJson(files.questions, current);
              }
              return current;
            });
            await notify('Owner answered (' + file + '):\n' + JSON.stringify({ question_id: answered, question: all[answered]?.text ?? null,
              answer: answer ?? null, answered_by: answeredBy, ...(all[answered] ? {} : { unknown_question: true }) }), { id: `inbox:${file}:${ino}:${mtimeMs}` });
            await rename(path, join(files.done, file));
            continue;
          }
          // Deliver first, then move (plugin step 7): a crash in between redelivers this
          // file under the same id, which the director recognises; moving first lost it.
          // The id is this file (name, inode, write time; the inode alone tells a later
          // file of the same name apart while the delivered one sits in done/).
          await notify('Director event (' + file + '):\n' + text, { id: `inbox:${file}:${ino}:${mtimeMs}` });
          await rename(join(files.inbox, file), join(files.done, file));
        }
      };
      if (projectsDir) await recoverProposals();
      await sweep();
      // Plugin step 15: the owner's commands, when this DSH has a command registry.
      // They are registered in the process-wide registry (every agent's UI lists them), so each
      // answers only in the root session's UI, where the owner talks to this director.
      const commands = ctx.get?.('commands');
      const isRoot = invoked => { const mine = api.agent?.session?.id, theirs = invoked?.session?.id;
        return invoked === api.agent || mine === undefined || theirs === undefined || mine === theirs; };
      const unregister = commands ? registerOwnerCommands(commands, { questions, answer, isRoot, memory: api.memory, wake: wakeLine,
        // Memory configured but not open: the memory commands say so instead of vanishing.
        ...(api.memory ? {} : api.memoryError || options.memory ? { memoryError: api.memoryError ?? 'it did not open (see the DSH log)' } : {}),
        ...(projectsDir ? { proposals: proposalList, decide } : {}) }) : [];
      const done = new Promise((resolve, reject) => {
        finish = resolve;
        // One sweep at a time: a tick that lands while a sweep still awaits a delivery
        // is skipped, or two sweeps would deliver the same file and race its move.
        let sweeping = false;
        timer = setInterval(() => {
          if (sweeping) return;
          sweeping = true;
          sweep().catch(error => { clearInterval(timer); reject(error); }).finally(() => { sweeping = false; });
        }, Number(options.pollMs) > 0 ? Number(options.pollMs) : 2000);
        timer.unref?.();
      });
      // A registry whose register returns no disposer leaves nothing to undo.
      return { done, stop: () => { clearInterval(timer); for (const off of unregister ?? []) if (typeof off === 'function') off(); finish?.(); } };
    },
    ...(projectsDir ? { portfolio, proposals } : {}),
  };
}

/** What this machine lacks for the plugin, one sentence each (empty: nothing). The director
 *  needs python3 with PyYAML for its todo package (`python -m dsh_todo templates` is run once)
 *  and git for plans and their delivery; a steward needs git for its memory, when it has one. */
export async function prerequisiteProblems({ todo, director = true, memory = false, platform = process.platform, git = 'git' }) {
  const problems = [];
  if (platform === 'win32') problems.push('Windows is not supported (the todo package needs fcntl and workers need a POSIX shell); run it on Linux');
  if (director) {
    // The CLI lists a template it cannot load with that template's error, so a missing PyYAML
    // shows as an error on every template, not as a failed run.
    const noYaml = text => /No module named '?yaml/.test(text);
    const yamlProblem = `PyYAML is missing for ${todo.python} (pip install -r ${REQUIREMENTS})`;
    try {
      const { code, result } = await todo.run(['templates'], undefined, 60000);
      if (code !== 0) problems.push(`the todo package refused to list its templates: ${JSON.stringify(result).slice(0, 300)}`);
      else if ((result?.templates ?? []).some(t => noYaml(String(t?.error ?? '')))) problems.push(yamlProblem);
    } catch (error) {
      const text = String(error?.message ?? error);
      if (/spawn .*ENOENT/.test(text)) problems.push(`python not found: ${todo.python} (install python3, or set todo.python)`);
      else if (noYaml(text)) problems.push(yamlProblem);
      else problems.push(`the todo package does not run with ${todo.python}: ${text.split('\n')[0].slice(0, 300)}`);
    }
  }
  if (director || memory) {
    const missing = await new Promise(resolve => execFile(git, ['--version'], error => resolve(error)));
    if (missing) problems.push(`git not found (${missing.code === 'ENOENT' ? `no ${git} on PATH` : String(missing.message).split('\n')[0]}): install git`);
  }
  return problems;
}

/** The version of the DSH whose entry is `entry` (its package.json above it), or undefined. */
export function dshVersion(entry) {
  try {
    for (let dir = dirname(realpathSync(entry)); dir !== dirname(dir); dir = dirname(dir)) {
      let manifest;
      try { manifest = JSON.parse(readFileSync(join(dir, 'package.json'), 'utf8')); } catch { continue; }
      if (manifest.name === '@deepseek-ai/dsh') return manifest.version;
    }
  } catch { /* an entry that does not resolve has no version */ }
  return undefined;
}

export function apply(ctx, config) {
  const host = localHost(config ?? {});
  // Every process this DSH starts - a job the director's or a worker's shell runs, detached or
  // not - carries the director's session in its environment: watch_process accepts it and
  // idle_dormant sees it (lib/core.mjs processSession, backgroundProcesses).
  const { sessionEnv, sessionId } = host.config;
  const before = process.env[sessionEnv];
  process.env[sessionEnv] = sessionId;
  ctx.on('dispose', () => { if (before === undefined) delete process.env[sessionEnv]; else process.env[sessionEnv] = before; });
  ctx.provide('directorHost', host);
}
