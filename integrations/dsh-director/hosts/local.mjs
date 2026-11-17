/** dsh-director's built-in host: the smallest directorHost that implements every
 *  operation of the contract, on plain local files. Insert it after dsh-director
 *  in a profile and the director runs with no backend:
 *
 *    - { insert: [{ id: 'dsh-director', name: '<dir>/dsh-director/index.mjs' },
 *                 { id: 'director-local', name: '<dir>/dsh-director/hosts/local.mjs',
 *                   config: { root, rolesFile?, todo?: { module?, stateDirEnv?, python?, cwd? }, ... } }] }
 *
 *  Zero-config apart from `root`: `rolesFile` defaults to the plugin's own roles.json
 *  (with director.md and worker.md next to it) and `todo` to the plugin's own todo
 *  package (python -m dsh_todo in todo/, state in DSH_TODO_STATE_DIR); any of them
 *  can be overridden.
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
 *                    (open | answered | withdrawn), asked_at, answer?, reason? } }; the
 *                    owner answers by dropping inbox/answer-<id>.json ({ "answer": "..." })
 *    state/          the director's own state (todo plans) */
import { readFile, writeFile, appendFile, readdir, mkdir, rename, stat } from 'node:fs/promises';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { WORK_STATES } from '../lib/host.mjs';

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

/** @param options - root (required), sessionId, provider, model, dshEntry, rolesFile,
 *  objective, holder, pollMs, todo: { python, cwd, module, stateDirEnv }. */
export function localHost(options = {}) {
  const root = options.root;
  if (!root) throw new Error('directorHost refused: missing config: root (the local host keeps its files there)');
  const holder = options.holder ?? options.sessionId ?? 'director';
  const files = { work: join(root, 'work.json'), notes: join(root, 'notes.md'), inbox: join(root, 'inbox'),
    done: join(root, 'inbox', 'done'), progress: join(root, 'progress.jsonl'), noteKeys: join(root, 'note-keys.json'),
    questions: join(root, 'questions.json') };
  const questions = async () => readJson(files.questions, {});
  // questions.json is read-modified-written by ask, close and an arriving answer; one
  // at a time, so two asks in one director turn cannot lose one. The lock also guards
  // writeJson's single fixed tmp name (questions.json.tmp): without it two writers race
  // on that file and one fails with ENOENT, a louder failure than a lost update.
  let questionsLock = Promise.resolve();
  const locked = fn => { const run = questionsLock.then(fn); questionsLock = run.catch(() => {}); return run; };
  const items = async () => (await readJson(files.work, { items: [] })).items ?? [];
  let timer, finish;
  return {
    version: '1.0.0',
    capabilities: ['progress', 'workState', 'heldWork', 'setup', 'attention'],
    config: {
      sessionId: options.sessionId ?? 'director',
      provider: options.provider, model: options.model,
      stateDir: options.stateDir ?? join(root, 'state'),
      dshEntry: options.dshEntry ?? process.argv[1],
      rolesFile: options.rolesFile ?? fileURLToPath(new URL('../roles.json', import.meta.url)),
      sessionEnv: options.sessionEnv,
      todo: { python: 'python3', cwd: fileURLToPath(new URL('../todo', import.meta.url)), module: 'dsh_todo',
        stateDirEnv: 'DSH_TODO_STATE_DIR', ...(options.todo ?? {}) },
      idleReviewMs: options.idleReviewMs, dormantMinuteMs: options.dormantMinuteMs, watchIntervalMs: options.watchIntervalMs,
      dormantCompactMinTokens: options.dormantCompactMinTokens,
      maxActiveSubagents: options.maxActiveSubagents, reservedSlots: options.reservedSlots,
      // Plugin step 11: the owner's memory, passed through as given (absent: none).
      ...(options.memory ? { memory: options.memory } : {}),
      // Plugin step 12: run the root session as the steward (needs roles.json's steward).
      ...(options.rootRole ? { rootRole: options.rootRole } : {}),
    },
    /** The startup / post-compaction snapshot: the work items and the notes. */
    async context() {
      let notes = '';
      try { notes = await readFile(files.notes, 'utf8'); } catch (error) { if (error.code !== 'ENOENT') throw error; }
      return JSON.stringify({ work: await items(), notes });
    },
    async progress(body) {
      await mkdir(root, { recursive: true });
      await appendFile(files.progress, JSON.stringify({ at: new Date().toISOString(), ...body }) + '\n');
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
    /** The director records with two small tools: a work item's state, and a note. */
    async setup(scope, _subject, { defineTool, jsonOutput }) {
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
    async events({ ctx, notify, agent, isDisposed }) {
      await mkdir(files.done, { recursive: true });
      if (options.objective && !ctx.goals.get(agent)) ctx.goals.create(agent, { objective: options.objective });
      const sweep = async () => {
        const names = (await readdir(files.inbox, { withFileTypes: true })).filter(e => e.isFile() && !e.name.endsWith('.tmp'))
          .map(e => e.name).sort();
        for (const file of names) {
          if (isDisposed()) return;
          const path = join(files.inbox, file);
          const [text, { ino, mtimeMs }] = await Promise.all([readFile(path, 'utf8'), stat(path)]);
          const answered = /^answer-([A-Za-z0-9._:-]{1,64})\.json$/.exec(file)?.[1];
          if (answered) {
            // The owner's answer: recorded on the question (an answer wins over a
            // withdrawal), then delivered naming the question, then moved like any event.
            let answer;
            try { answer = JSON.parse(text).answer; } catch { answer = undefined; }
            const all = await locked(async () => {
              const current = await questions();
              if (current[answered] && typeof answer === 'string') {
                current[answered] = { ...current[answered], status: 'answered', answer, answered_at: current[answered].answered_at ?? new Date().toISOString() };
                await writeJson(files.questions, current);
              }
              return current;
            });
            await notify('Owner answered (' + file + '):\n' + JSON.stringify({ question_id: answered, question: all[answered]?.text ?? null,
              answer: answer ?? null, ...(all[answered] ? {} : { unknown_question: true }) }), { id: `inbox:${file}:${ino}:${mtimeMs}` });
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
      await sweep();
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
      return { done, stop: () => { clearInterval(timer); finish?.(); } };
    },
  };
}

export function apply(ctx, config) {
  ctx.provide('directorHost', localHost(config ?? {}));
}
