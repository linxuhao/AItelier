/** The steward's memory (plugin step 11, design section 3): a small git repository per
 *  owner, plus the resident profile, a literal grep and a ranked index.
 *
 *  Layout, under `root` (0700):
 *    <userId>/                 the git repository (0700, files 0600, core.sharedRepository=0600)
 *      log/<YYYY-MM>.jsonl     one MemoryEntry per line, deduplicated by id
 *      profile.md              the resident profile; its history is git's
 *      forgotten.jsonl         {id, at, reason, by, sha256, batch} | {id, at, restored} | {id, at, purged, batch}
 *    <userId>.lock             the process lock (O_EXCL, holds the pid)
 *    <userId>.spool.jsonl      writes that arrived during a purge
 *    <userId>.purge.jsonl      the purge journal (ids and phases only, never text)
 *    <userId>.ranked-pending.json   remote batches whose add failed
 *    <userId>.notices.jsonl    what the owner must be told (an interrupted purge), until acknowledged
 *    <userId>.profile-pending.json  who wrote a profile change whose commit failed (its author survives)
 *  The lock, spool, journal and pending file sit beside the repository, not in it, so the
 *  purge's directory swap can never move or drop them.
 *
 *  A write succeeds when its file is on disk (fsync); the git commit that follows can only
 *  degrade (status.git says why and since when) and the next commit takes the backlog. Logs
 *  name entry ids and lengths, never memory text. */
import { createHash } from 'node:crypto';
import { closeSync, constants, existsSync, fsyncSync, openSync, readFileSync, readdirSync, renameSync,
  rmSync, statSync, writeSync, chmodSync, mkdirSync, unlinkSync, linkSync } from 'node:fs';
import { join } from 'node:path';
import { Bm25Index, bm25Ranked, documentTokens, queryTokens } from './bm25.mjs';
import { DEFAULT_TIME_ZONE, dateTokens, dayOf, formatLine, formatProfileLine, parseAt } from './render.mjs';
import { memoryTimeZoneProblem } from '../host.mjs';
import { git, gitOk } from './git.mjs';
import { filterBlob, filterExportStream } from './purge-filter.mjs';

export const PROFILE_LIMIT = 4000;
export const TOP_K_MAX = 20;
/** memory_search (design 3.8.1): both answers together, rendered lines with their heads. */
export const SEARCH_CHARS = 6000;
/** memory_read: at most this many ids per call and this many characters of text in all. */
export const READ_IDS_MAX = 20;
export const READ_CHARS = 60000;
/** Design 3.3's budget. Crossing a line is logged once and stays on status.over_budget. */
export const BUDGET = Object.freeze({ entries: 100000, syncLoadMs: 3000, writeP95Ms: 200 });
const USER_ID = /^[A-Za-z0-9@_+-][A-Za-z0-9@._+-]{0,127}$/;
const ENTRY_ID = /^[A-Za-z0-9._:-]{1,160}$/;
const ROLES = ['owner', 'steward'];
const sha256 = text => createHash('sha256').update(text).digest('hex');
const month = at => String(at).slice(0, 7);
/** The purge phases before the directory swap: a journal that ends in one of them describes
 *  a repository nothing was swapped into yet, whatever the layout says afterwards. */
const PRE_SWAP_PHASES = new Set(['forgetting', 'forget_done', 'building', 'verified', 'rolling_back']);

export class MemoryUnavailable extends Error {
  constructor(message) { super(message); this.name = 'MemoryUnavailable'; }
}

function fsyncDir(dir) {
  const fd = openSync(dir, 'r');
  try { fsyncSync(fd); } finally { closeSync(fd); }
}

/** open → write → fsync → close: no long-lived handle, so a directory swap never leaves
 *  a write in a handle to the old directory. */
function appendDurable(file, text) {
  const created = !existsSync(file);
  const fd = openSync(file, constants.O_WRONLY | constants.O_APPEND | constants.O_CREAT, 0o600);
  try { writeSync(fd, text); fsyncSync(fd); } finally { closeSync(fd); }
  chmodSync(file, 0o600);
  if (created) fsyncDir(join(file, '..')); // a new file's name is on disk too, not only its bytes
}

function writeDurable(file, text) {
  const tmp = file + '.tmp-' + process.pid;
  const fd = openSync(tmp, 'w', 0o600);
  try { writeSync(fd, text); fsyncSync(fd); } finally { closeSync(fd); }
  chmodSync(tmp, 0o600);
  renameSync(tmp, file);
  fsyncDir(join(file, '..'));
}

function readLines(file) {
  if (!existsSync(file)) return [];
  return readFileSync(file, 'utf8').split('\n').filter(Boolean);
}

function parseLines(file) {
  const out = [];
  for (const line of readLines(file)) {
    try { out.push(JSON.parse(line)); } catch { /* a torn last line was never acknowledged */ }
  }
  return out;
}

/** Non-overlapping occurrences of `needle` in `text`. */
function occurrences(text, needle) {
  if (!needle) return 0;
  let n = 0;
  for (let i = text.indexOf(needle); i >= 0; i = text.indexOf(needle, i + needle.length)) n++;
  return n;
}

/** `text` without the lines that are a JSON row (optionally after a diff marker) naming
 *  one of `ids`. */
function withoutLinesNaming(text, ids) {
  return text.split('\n').filter(line => {
    const i = line.indexOf('{');
    if (i < 0 || ![...ids].some(id => line.includes(id))) return true;
    try { return !ids.has(JSON.parse(line.slice(i)).id); } catch { return true; }
  }).join('\n');
}

function pidAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try { process.kill(pid, 0); return true; } catch (error) { return error.code === 'EPERM'; }
}

/** Entry ids in number order (`t-9-…` before `t-10-…`), as memory_read orders a turn. */
const byId = (a = '', b = '') => a.localeCompare(b, 'en', { numeric: true });

export class MemoryStore {
  /** @param {object} o
   *  root, userId; ranked: a remote RankedIndex (absent: the built-in BM25);
   *  logger(event, fields); now(): ISO time; grepTimeoutMs (5000); syncMonths (12);
   *  backupRemote: a git remote URL pushed after each commit (failures only degrade);
   *  timeZone: the owner's IANA zone (UTC) - search's line dates and day groups, and the
   *  date words in the index (design 3.8.1); read once here, so a process never mixes two. */
  static async open(o) {
    const store = new MemoryStore(o);
    await store.#boot();
    return store;
  }

  constructor({ root, userId, ranked, logger = () => {}, now = () => new Date().toISOString(),
    grepTimeoutMs = 5000, syncMonths = 12, backupRemote, warnEntries = 200000, faults, budget = BUDGET, timeZone = DEFAULT_TIME_ZONE }) {
    if (typeof userId !== 'string' || !USER_ID.test(userId)) throw new Error('memory: userId must be 1-128 of letters, digits, @ . _ + - (not starting with .)');
    const zoneProblem = memoryTimeZoneProblem(timeZone);
    if (zoneProblem) throw new Error('memory: ' + zoneProblem);
    this.timeZone = timeZone;
    this.root = root; this.userId = userId; this.log = logger; this.now = now;
    this.grepTimeoutMs = grepTimeoutMs; this.syncMonths = syncMonths; this.backupRemote = backupRemote; this.warnEntries = warnEntries;
    this.dir = join(root, userId);
    this.lockFile = join(root, userId + '.lock');
    this.spoolFile = join(root, userId + '.spool.jsonl');
    this.journalFile = join(root, userId + '.purge.jsonl');
    this.pendingFile = join(root, userId + '.ranked-pending.json');
    this.noticesFile = join(root, userId + '.notices.jsonl');
    this.profilePendingFile = join(root, userId + '.profile-pending.json');
    this.budget = { ...BUDGET, ...budget };
    this.overBudget = new Map(); // kind -> line
    this.writeMs = [];           // the last 100 writes, fsync to commit
    this.entries = new Map();   // id -> entry (as written)
    this.forgotten = new Map(); // id -> last record
    this.index = new Bm25Index();
    this.remote = ranked;
    this.ranked = ranked ?? bm25Ranked(this.index, id => this.entries.get(id)?.text);
    this.coverage = 'all';
    this.gitState = { ok: true };
    this.purging = false;
    this.notices = [];
    this.queue = Promise.resolve();
    this.lockHeld = false;
    this.faults = faults; // test hooks: a crash point is a hook that throws or kills the process
  }

  /** Serialize every repository change (the in-process half of the write lock). */
  #locked(fn) {
    const run = this.queue.then(fn);
    this.queue = run.catch(() => {});
    return run;
  }

  async #boot() {
    mkdirSync(this.root, { recursive: true, mode: 0o700 });
    chmodSync(this.root, 0o700);
    this.#acquireLock();
    const notice = await this.#recoverPurge();
    // f7: the notice outlives this process: it is kept beside the repository until the
    // owner acknowledges it, shown on status and in every snapshot meanwhile.
    // A boot that recovers the same purge again (a crash loop) adds no second copy.
    if (notice && !parseLines(this.noticesFile).some(n => n.text === notice))
      appendDurable(this.noticesFile, JSON.stringify({ at: this.now(), text: notice }) + '\n');
    this.notices = parseLines(this.noticesFile).map(n => n.text);
    if (!existsSync(this.dir)) {
      const leftovers = readdirSync(this.root).filter(name => name.startsWith(this.userId + '.pre-purge-'));
      if (leftovers.length) throw new MemoryUnavailable(`memory: ${this.dir} is missing while ${leftovers.join(', ')} exists; refusing to start an empty repository over it`);
      await this.#init();
    }
    chmodSync(this.dir, 0o700);
    await this.#commitIfDirty('recover: uncommitted changes at boot');
    // (a) An ordinary forget is not journalled (`#dropFromWorkTree`), so a crash between its
    // records and its strip leaves the id in the log AND in forgotten.jsonl with nothing to
    // finish it: #recoverPurge above finishes and reports only the forgets of a purge it knows
    // about. So the ordinary window is closed here, from the state this boot load reads, and
    // the strip it makes is committed at once, like any other boot repair. (A restore that died
    // before its `restored` record leaves the same two lines; the surviving record there still
    // says forgotten, so this is also the reading the disk supports.)
    const finished = this.#finishForget(this.#load(), { loaded: true });
    if (finished) await this.#commitIfDirty('recover: finished an interrupted forget at boot');
    await this.#replaySpool();
    await this.#replayPending(true);
  }

  /** f6: the takeover of a dead holder's lock is a rename to a name of our own (atomic: of
   *  two processes taking over, one rename finds nothing), and what was renamed is checked:
   *  if it is not the dead holder's lock - someone took it over meanwhile - it is put back
   *  and we are refused. Only then O_EXCL. A lock is ours only while it names our pid. */
  #acquireLock() {
    const readHolder = file => { try { return Number(readFileSync(file, 'utf8').trim()); } catch (error) { if (error.code === 'ENOENT') return undefined; throw error; } };
    for (let attempt = 0; attempt < 5; attempt++) {
      try {
        const fd = openSync(this.lockFile, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL, 0o600);
        try { writeSync(fd, String(process.pid)); fsyncSync(fd); } finally { closeSync(fd); }
        this.lockHeld = true;
        return;
      } catch (error) {
        if (error.code !== 'EEXIST') throw error;
      }
      const holder = readHolder(this.lockFile);
      if (holder === undefined) continue;                       // released meanwhile: try again
      if (pidAlive(holder)) throw new MemoryUnavailable(`memory: locked by process ${holder}`);
      this.faults?.beforeLockTakeover?.();
      const aside = `${this.lockFile}.stale-${process.pid}-${attempt}`;
      try { renameSync(this.lockFile, aside); }
      catch (error) { if (error.code === 'ENOENT') continue; throw error; } // another process took it first
      this.faults?.afterLockAside?.();
      const moved = readHolder(aside);
      if (moved !== holder) {
        // Not the stale lock we judged: a live takeover happened in between. Put it back -
        // with link, which fails rather than replace a lock a third process made meanwhile.
        try { linkSync(aside, this.lockFile); } catch (error) { if (error.code !== 'EEXIST') throw error; }
        unlinkSync(aside);
        throw new MemoryUnavailable(`memory: locked by process ${moved}`);
      }
      unlinkSync(aside);
      this.log('memory/lock-taken-over', { stale_pid: holder });
    }
    throw new MemoryUnavailable('memory: could not take the lock');
  }

  /** Release the process lock (the store is unusable afterwards); a lock that no longer
   *  names this process is someone else's and is left alone. */
  async close() {
    await this.queue;
    if (!this.lockHeld) return;
    this.lockHeld = false;
    try { if (Number(readFileSync(this.lockFile, 'utf8').trim()) === process.pid) unlinkSync(this.lockFile); } catch {}
  }

  /** The owner has seen the notices: they leave status and the snapshot. */
  acknowledgeNotices() {
    if (existsSync(this.noticesFile)) writeDurable(this.noticesFile, '');
    this.notices = [];
  }

  async #init(dir = this.dir) {
    mkdirSync(dir, { mode: 0o700 });
    chmodSync(dir, 0o700);
    await gitOk(dir, ['init', '-q', '--shared=0600', '-b', 'main']);
    mkdirSync(join(dir, 'log'), { mode: 0o700 });
    writeDurable(join(dir, 'log', '.keep'), '');
    writeDurable(join(dir, 'profile.md'), '');
    writeDurable(join(dir, 'forgotten.jsonl'), '');
    await gitOk(dir, ['add', '-A']);
    await gitOk(dir, ['commit', '-q', '-m', 'init (runtime)']);
  }

  #logFiles() {
    const logDir = join(this.dir, 'log');
    if (!existsSync(logDir)) return [];
    return readdirSync(logDir).filter(n => /^\d{4}-\d{2}\.jsonl$/.test(n)).sort().reverse();
  }

  /** Read the repository into memory. Returns the ids the log AND forgotten.jsonl both name:
   *  an ordinary forget appends its records before it strips the log lines, so a crash in
   *  between leaves exactly that, and no journal row says so (#boot finishes them). */
  #load() {
    const started = performance.now();
    this.entries.clear(); this.forgotten.clear(); this.index = new Bm25Index();
    if (!this.remote) this.ranked = bm25Ranked(this.index, id => this.entries.get(id)?.text);
    // forgotten holds what is forgotten NOW: a restore record takes its id out again.
    for (const row of parseLines(join(this.dir, 'forgotten.jsonl'))) {
      if (row.restored) this.forgotten.delete(row.id); else this.forgotten.set(row.id, row);
    }
    const files = this.#logFiles();
    const cutoff = this.#cutoffMonth();
    const later = [];
    for (const name of files) {
      const rows = parseLines(join(this.dir, 'log', name));
      for (const e of rows) this.entries.set(e.id, e);
      if (name.slice(0, 7) >= cutoff) for (const e of rows) this.index.add(e.id, this.#docTokens(e), e.at);
      else later.push(rows);
    }
    if (this.entries.size > this.warnEntries) this.log('memory/large', { entries: this.entries.size });
    this.#overBudget('entries', this.entries.size > this.budget.entries,
      `${this.entries.size} entries > ${this.budget.entries} (design 3.3: 100,000 entries within 150 MB)`);
    const loadMs = Math.round(performance.now() - started);
    this.#overBudget('sync_load', loadMs > this.budget.syncLoadMs, `synchronous load ${loadMs} ms > ${this.budget.syncLoadMs} ms`);
    // A remote that traces hits to turns must know every turn, forgotten ones included.
    if (this.remote?.learnBatches) {
      const batches = new Set([...this.entries.values()].map(e => e.batch));
      for (const r of this.forgotten.values()) batches.add(r.batch);
      batches.delete(undefined);
      this.remote.learnBatches([...batches]);
    }
    if (later.length) {
      this.coverage = 'since ' + cutoff;
      const next = () => {
        const rows = later.shift();
        if (rows) for (const e of rows) if (this.entries.has(e.id)) this.index.add(e.id, this.#docTokens(e), e.at);
        if (later.length) setImmediate(next); else this.coverage = 'all';
      };
      setImmediate(next);
    } else this.coverage = 'all';
    // Still in the log and recorded as forgotten: a forget that died in the window between its
    // two writes. A restored id has left `forgotten`, and a purged one is not listed any more
    // (its records carry no sha256), so neither belongs here.
    return [...this.forgotten.values()]
      .filter(r => !r.purged && r.sha256 && this.entries.has(r.id))
      .map(r => r.id);
  }

  /** The index's token list for an entry (design 3.5): its text's tokens, then the date words
   *  of its time in the owner's zone. Every add and every remove of the index takes the list
   *  from here, so a remove takes back exactly what the add put in (the zone is fixed for the
   *  process). The text itself is never changed: grep, memory_read and the log never see the
   *  date words. */
  #docTokens(e) {
    return [...documentTokens(e.text), ...dateTokens(e.at, this.timeZone)];
  }

  #cutoffMonth() {
    const d = new Date(this.now());
    d.setUTCDate(1);
    d.setUTCMonth(d.getUTCMonth() - (this.syncMonths - 1));
    return d.toISOString().slice(0, 7);
  }

  // ---------------------------------------------------------------- git

  async #commitIfDirty(message, dir = this.dir) {
    const status = await git(dir, ['status', '--porcelain']);
    if (status.code === 0 && !status.stdout.length) return true;
    return this.#commit(message, dir);
  }

  /** Commit everything; a failure only degrades (it is reported, and the next commit takes it). */
  async #commit(message, dir = this.dir) {
    try {
      await this.#commitPendingProfile(dir);
      await gitOk(dir, ['add', '-A']);
      const staged = await git(dir, ['diff', '--cached', '--quiet']);
      if (staged.code === 0) { this.gitState = { ok: true }; return true; }
      await gitOk(dir, ['commit', '-q', '--no-verify', '-m', message]);
      this.gitState = { ok: true };
      await git(dir, ['gc', '--auto', '-q']);
      if (this.backupRemote) {
        const push = await git(dir, ['push', '-q', this.backupRemote, 'main'], { timeoutMs: 30000 });
        if (push.code !== 0) this.log('memory/backup-push-failed', { reason: push.stderr.trim().slice(0, 200) });
      }
      return true;
    } catch (error) {
      if (this.gitState.ok) this.gitState = { ok: false, since: this.now(), reason: String(error.message).slice(0, 300) };
      else this.gitState.reason = String(error.message).slice(0, 300);
      this.log('memory/commit-failed', { since: this.gitState.since, reason: this.gitState.reason });
      return false;
    }
  }

  /** write-f1: a profile change whose own commit failed keeps its author. The next commit,
   *  whichever write triggers it, first commits profile.md alone under the recorded message;
   *  only a change nobody recorded (an edit on disk) is the owner's. Throws like gitOk. */
  async #commitPendingProfile(dir = this.dir) {
    if (!existsSync(this.profilePendingFile)) return false;
    let pending;
    try { pending = JSON.parse(readFileSync(this.profilePendingFile, 'utf8')); } catch { pending = undefined; }
    const text = existsSync(join(dir, 'profile.md')) ? readFileSync(join(dir, 'profile.md'), 'utf8') : '';
    const dirty = await git(dir, ['status', '--porcelain', '--', 'profile.md']);
    if (pending?.sha256 === sha256(text) && dirty.code === 0 && dirty.stdout.length) {
      await gitOk(dir, ['add', '--', 'profile.md']);
      await gitOk(dir, ['commit', '-q', '--no-verify', '-m', pending.message, '--', 'profile.md']);
    }
    // Committed now, or the file moved on since (the owner edited it, or it was committed):
    // either way the record has done its work.
    unlinkSync(this.profilePendingFile);
    return true;
  }

  /** Wait for every queued write and commit (tests, shutdown). */
  flush() { return this.#locked(() => {}); }

  /** iss-a81f: a budget line crossed is logged once and kept on status.over_budget. */
  #overBudget(kind, over, line) {
    if (!over) { this.overBudget.delete(kind); return; }
    if (!this.overBudget.has(kind)) this.log('memory/over-budget', { kind, line });
    this.overBudget.set(kind, line);
  }

  #recordWriteMs(ms) {
    this.writeMs.push(ms);
    if (this.writeMs.length > 100) this.writeMs.shift();
    if (this.writeMs.length < 20) return;
    const p95 = [...this.writeMs].sort((a, b) => a - b)[Math.floor(this.writeMs.length * 0.95)];
    this.#overBudget('write_p95', p95 > this.budget.writeP95Ms,
      `write p95 ${Math.round(p95)} ms > ${this.budget.writeP95Ms} ms over the last ${this.writeMs.length} writes (on disk to committed)`);
  }

  statusLine() {
    return {
      git: this.gitState.ok ? 'ok' : `commit failing since ${this.gitState.since}: ${this.gitState.reason}`,
      index: `${this.index.size} entries`,
      ...(this.purging ? { purge: 'in progress: writes are spooled' } : {}),
      ...(this.notices.length ? { notices: this.notices } : {}),
      ...(this.pendingStuck?.length ? { ranked_pending_stuck: this.pendingStuck } : {}),
      ...(this.overBudget.size ? { over_budget: [...this.overBudget.values()] } : {}),
    };
  }

  // ---------------------------------------------------------------- writes

  /** One owner/steward turn (design 3.3 写入来源): entry ids derive from the turn id and
   *  the message's position, so a replay writes nothing twice. Resolves once on disk. */
  recordTurn(turnId, messages) {
    if (typeof turnId !== 'string' || !ENTRY_ID.test('t-' + turnId)) throw new Error('memory: turn id must be letters, digits, . _ : -');
    const at = this.now();
    const entries = messages.map((m, i) => {
      if (!ROLES.includes(m.role)) throw new Error('memory: role must be owner or steward');
      // `part` (plugin step 12): a stable name for the message within its turn, for a host
      // whose turn grows message by message (an owner message now, the reply later); by
      // default the position.
      const id = `t-${turnId}-${m.part ?? i}`;
      if (!ENTRY_ID.test(id)) throw new Error('memory: message part must be letters, digits, . _ : -');
      return { id, at: m.at ?? at, role: m.role, source: 'conversation', text: String(m.text), batch: turnId };
    }).filter(e => e.text.trim());
    return this.#writeEntries(entries, `turn ${turnId} (runtime)`);
  }

  /** The most recent conversation turns, verbatim, oldest first (design-steward 7.3 layer 3):
   *  up to `n` turns and `maxChars` characters, dropping whole turns from the oldest end; a
   *  newest turn longer than the bound alone is kept with its oldest lines cut and says so. */
  recentTurns(n = 10, maxChars = 12000) {
    const turns = new Map();
    for (const e of this.entries.values()) {
      if (e.source !== 'conversation') continue;
      if (!turns.has(e.batch)) turns.set(e.batch, []);
      turns.get(e.batch).push(e);
    }
    const ordered = [...turns.values()].map(rows => rows.sort((a, b) => (a.at > b.at ? 1 : a.at < b.at ? -1 : a.id.localeCompare(b.id))))
      .sort((a, b) => (a[0].at > b[0].at ? 1 : a[0].at < b[0].at ? -1 : 0)).slice(-n)
      .map(rows => rows.map(e => `[${e.at}] ${e.role}: ${e.text}`).join('\n'));
    while (ordered.length > 1 && ordered.join('\n\n').length > maxChars) ordered.shift();
    if (ordered.length === 1 && ordered[0].length > maxChars) ordered[0] = '[…earlier part of this turn cut]\n' + ordered[0].slice(-maxChars);
    return ordered;
  }

  /** Whether an entry id was ever written here: live, forgotten or purged (none of those is
   *  written again). A host syncing a conversation stops paging at a message it knows. */
  known(id) { return this.entries.has(id) || this.forgotten.has(id); }

  /** memory_note: one explicit entry. */
  note(text, role = 'steward') {
    const value = String(text ?? '').trim();
    if (!value) return Promise.resolve({ refused: 'empty_note' });
    if (value.length > 8000) return Promise.resolve({ refused: 'note_too_long', limit: 8000 });
    const at = this.now();
    const id = 'n-' + sha256(at + '\0' + value).slice(0, 20);
    return this.#writeEntries([{ id, at, role, source: 'note', text: value, batch: id }], `note ${id} (${role})`)
      .then(r => ({ ...r, id }));
  }

  async #writeEntries(entries, message) {
    if (!entries.length) return { written: 0 };
    if (this.purging) {
      // Design 3.6: during a purge a write goes to the spool; on disk there is success.
      appendDurable(this.spoolFile, JSON.stringify({ entries, message }) + '\n');
      this.log('memory/spooled', { ids: entries.map(e => e.id) });
      return { written: entries.length, spooled: true };
    }
    let fresh;
    const started = performance.now();
    const written = this.#locked(async () => {
      if (this.purging) return { spool: true };
      fresh = this.#appendEntries(entries);
      return { written: fresh.length };
    });
    const result = await written;
    if (result.spool) return this.#writeEntries(entries, message);
    // The commit runs after the caller has its answer (it never delays a reply).
    if (fresh.length) {
      this.#locked(() => this.#commit(message).then(() => this.#recordWriteMs(performance.now() - started)));
      this.#remoteAdd(fresh);
    }
    return result;
  }

  #appendEntries(entries) {
    // A replay never brings back what was forgotten: a forgotten id is not written again.
    const fresh = entries.filter(e => !this.entries.has(e.id) && !this.forgotten.has(e.id));
    const byMonth = new Map();
    for (const e of fresh) {
      if (!byMonth.has(month(e.at))) byMonth.set(month(e.at), []);
      byMonth.get(month(e.at)).push(e);
    }
    for (const [m, rows] of byMonth) appendDurable(join(this.dir, 'log', m + '.jsonl'), rows.map(e => JSON.stringify(e)).join('\n') + '\n');
    for (const e of fresh) { this.entries.set(e.id, e); this.index.add(e.id, this.#docTokens(e), e.at); }
    if (fresh.length) this.log('memory/written', { ids: fresh.map(e => e.id), lengths: fresh.map(e => e.text.length) });
    return fresh;
  }

  async #replaySpool() {
    const rows = parseLines(this.spoolFile);
    if (!rows.length) { if (existsSync(this.spoolFile)) writeDurable(this.spoolFile, ''); return; }
    await this.#locked(async () => {
      const fresh = [];
      for (const row of rows) fresh.push(...this.#appendEntries(row.entries ?? []));
      await this.#commit(`spool: ${fresh.length} entries written during a purge (runtime)`);
      writeDurable(this.spoolFile, '');
      // f4: a spooled write was acknowledged like any other, so it reaches the remote too.
      this.#remoteAdd(fresh);
      this.log('memory/spool-replayed', { entries: fresh.length });
    });
  }

  // ---------------------------------------------------------------- remote ranked index

  #readPending() {
    try { return JSON.parse(readFileSync(this.pendingFile, 'utf8')); } catch { return {}; }
  }

  #remoteAdd(entries) {
    if (!this.remote) return;
    const batches = new Map();
    for (const e of entries) { if (!batches.has(e.batch)) batches.set(e.batch, []); batches.get(e.batch).push(e); }
    for (const [id, rows] of batches) {
      this.remote.add({ id, entries: rows }).catch(error => {
        const pending = this.#readPending();
        pending[id] = { since: pending[id]?.since ?? this.now(), boot_failures: pending[id]?.boot_failures ?? 0 };
        writeDurable(this.pendingFile, JSON.stringify(pending));
        this.log('memory/remote-add-failed', { batch: id, reason: String(error.message).slice(0, 200) });
      });
    }
  }

  /** Replay remote adds that failed (at boot, and before each search). Three failed boot
   *  replays put a batch on status.ranked_pending_stuck and in the log. */
  async #replayPending(atBoot = false) {
    if (!this.remote || !existsSync(this.pendingFile)) return;
    const pending = this.#readPending();
    for (const [id, row] of Object.entries(pending)) {
      const rows = [...this.entries.values()].filter(e => e.batch === id);
      if (!rows.length) { delete pending[id]; continue; }
      try { await this.remote.add({ id, entries: rows }); delete pending[id]; }
      catch (error) {
        if (atBoot) row.boot_failures = (row.boot_failures ?? 0) + 1;
        if (row.boot_failures >= 3) this.log('memory/remote-pending-stuck', { batch: id, boot_failures: row.boot_failures });
      }
    }
    writeDurable(this.pendingFile, JSON.stringify(pending));
    this.pendingStuck = Object.entries(pending).filter(([, r]) => r.boot_failures >= 3).map(([id]) => id);
  }

  // ---------------------------------------------------------------- profile

  async #profileVersion(text) {
    const dirty = await git(this.dir, ['status', '--porcelain', '--', 'profile.md']);
    if (dirty.code !== 0 || dirty.stdout.length) return 'uncommitted:' + sha256(text);
    const last = await git(this.dir, ['log', '-1', '--format=%H', '--', 'profile.md']);
    return last.stdout.toString().trim() || 'uncommitted:' + sha256(text);
  }

  /** The resident profile and its version. An edit made on disk (standalone use) is
   *  committed first, as the owner's. */
  profile() { return this.#locked(() => this.#profileRead()); }

  async #profileRead() {
    {
      await this.#commitPendingProfile().catch(() => {});
      const dirty = await git(this.dir, ['status', '--porcelain', '--', 'profile.md']);
      if (dirty.code === 0 && dirty.stdout.length && !existsSync(this.profilePendingFile)) {
        await gitOk(this.dir, ['add', '--', 'profile.md']).catch(() => {});
        await git(this.dir, ['commit', '-q', '--no-verify', '-m', 'profile (owner): edited on disk', '--', 'profile.md']);
      }
      const text = existsSync(join(this.dir, 'profile.md')) ? readFileSync(join(this.dir, 'profile.md'), 'utf8') : '';
      return { text, version: await this.#profileVersion(text), limit: PROFILE_LIMIT };
    }
  }

  /** Replace the whole profile (design 3.4); `by` is steward | owner. */
  async updateProfile(text, expectedVersion, reason, by = 'steward') {
    if (this.purging) return { refused: 'purge_in_progress', reason: 'a purge is running; try again when it ends' };
    const value = String(text ?? '');
    if (value.length > PROFILE_LIMIT) return { refused: 'profile_too_long', limit: PROFILE_LIMIT, length: value.length, reason: 'shorten it; it is never truncated' };
    const why = String(reason ?? '').trim();
    if (!why) return { refused: 'reason_required' };
    return this.#locked(async () => {
      // Checked inside the lock: the owner's edit can never be overwritten by a stale version.
      const current = await this.#profileRead();
      // The version names the content: an "uncommitted:<sha256>" handed out while commits
      // were failing still names the same text once that text is committed (write-f1).
      const same = expectedVersion === current.version || expectedVersion === 'uncommitted:' + sha256(current.text);
      if (!same) return { refused: 'version_conflict', current };
      writeDurable(join(this.dir, 'profile.md'), value);
      const message = `profile (${by}): ${why.slice(0, 200)}`;
      writeDurable(this.profilePendingFile, JSON.stringify({ message, sha256: sha256(value), at: this.now() }));
      await this.#commit(message);
      this.log('memory/profile', { by, length: value.length });
      return { updated: true, version: await this.#profileVersion(value), status: this.statusLine() };
    });
  }

  // ---------------------------------------------------------------- forget / restore

  /** Remove these ids' lines from log/<YYYY-MM>.jsonl. Returns how many files it rewrote. */
  #stripLogLines(ids) {
    const drop = new Set(ids);
    const files = new Set();
    for (const id of drop) { const e = this.entries.get(id); if (e) files.add(month(e.at)); }
    let changed = 0;
    for (const m of files) {
      const file = join(this.dir, 'log', m + '.jsonl');
      if (!existsSync(file)) continue;
      const lines = readLines(file);
      const kept = lines.filter(line => { try { return !drop.has(JSON.parse(line).id); } catch { return true; } });
      if (kept.length === lines.length) continue;
      writeDurable(file, kept.length ? kept.join('\n') + '\n' : '');
      changed++;
    }
    return changed;
  }

  /** Level 1 out of the working tree: the forgotten records are appended BEFORE the log lines
   *  go. That order is the point: a crash in between leaves the id in BOTH places, which the
   *  next boot finishes (`#finishForget`), while the other order leaves a window where the id
   *  is in neither and the text is gone with no record that it ever existed. Returns the
   *  entries it dropped. */
  async #dropFromWorkTree(ids, { reason, by }) {
    const at = this.now();
    const rows = [];
    for (const id of ids) {
      const e = this.entries.get(id);
      if (e) rows.push({ id: e.id, at, reason, by, sha256: sha256(e.text), batch: e.batch });
    }
    if (!rows.length) return [];
    if (this.faults?.beforeForgetRecords) await this.faults.beforeForgetRecords();
    appendDurable(join(this.dir, 'forgotten.jsonl'), rows.map(r => JSON.stringify(r)).join('\n') + '\n');
    if (this.faults?.beforeForgetStrip) await this.faults.beforeForgetStrip();
    this.#stripLogLines(rows.map(r => r.id));
    const dropped = [];
    for (const r of rows) {
      const e = this.entries.get(r.id);
      if (!e) continue;
      this.entries.delete(e.id); this.index.remove(e.id, this.#docTokens(e));
      this.forgotten.set(e.id, { id: e.id, at, by, batch: e.batch });
      dropped.push(e);
    }
    return dropped;
  }

  /** Forget (design 3.6 level 1): out of the working tree, grep and BM25; recoverable
   *  from history. `by` names who asked (owner, or steward citing an owner message). */
  async forget(ids, { reason, by }) {
    if (this.purging) return { refused: 'purge_in_progress', reason: 'a purge is running; try again when it ends' };
    const why = String(reason ?? '').trim();
    if (!why) return { refused: 'reason_required' };
    if (!Array.isArray(ids) || !ids.length || ids.some(id => typeof id !== 'string')) return { refused: 'ids_required' };
    return this.#locked(async () => {
      const live = [], unknown = [], already = [];
      for (const id of new Set(ids)) {
        if (this.entries.has(id)) live.push(id);
        else (this.forgotten.has(id) ? already : unknown).push(id);
      }
      const dropped = await this.#dropFromWorkTree(live, { reason: why, by });
      let remote = this.remote ? (this.remote.remove ? 'removed' : this.remote.batchOf ? 'filtered_by_turn' : 'unsupported') : 'local';
      if (dropped.length && this.remote?.remove) {
        try { await this.remote.remove(dropped.map(e => e.id)); }
        catch (error) { remote = 'remove_failed: ' + String(error.message).slice(0, 200); }
      }
      if (dropped.length) await this.#commit(`forget ${dropped.map(e => e.id).join(',')} (${by})`);
      this.log('memory/forgotten', { ids: dropped.map(e => e.id), by });
      return { forgotten: dropped.map(e => e.id), already, unknown, remote_forget: remote,
        note: 'the text stays in git history until purged' + (remote === 'unsupported' ? '; the remote index may still return it' : ''),
        status: this.statusLine() };
    });
  }

  /** The forgotten entries an owner can restore. */
  forgottenList() {
    return [...this.forgotten.values()].filter(r => !r.purged).map(r => ({ id: r.id, at: r.at, by: r.by }));
  }

  /** The entry's last committed form, from history: the newest commit holding a log line
   *  with this id (and, when given, this sha256 of its text). */
  async #fromHistory(id, digest, dir = this.dir) {
    const commits = (await gitOk(dir, ['log', '--all', '--format=%H', '--', 'log'])).stdout.toString().split('\n').filter(Boolean);
    const needle = JSON.stringify({ id }).slice(1, -1);
    for (const sha of commits) {
      const r = await git(dir, ['grep', '-F', '-z', '--no-color', '-e', needle, sha, '--', 'log']);
      for (const record of r.stdout.toString('utf8').split('\n').filter(Boolean)) {
        const line = record.split('\0').at(-1);
        try {
          const e = JSON.parse(line);
          if (e.id === id && typeof e.text === 'string' && (!digest || sha256(e.text) === digest)) return e;
        } catch {}
      }
    }
    return undefined;
  }

  async restore(id, by = 'owner') {
    if (this.purging) return { refused: 'purge_in_progress' };
    return this.#locked(async () => {
      const record = this.forgotten.get(id);
      if (!record) return { refused: 'not_forgotten' };
      if (record.purged) return { refused: 'purged', reason: 'a purged entry cannot be restored' };
      const full = parseLines(join(this.dir, 'forgotten.jsonl')).filter(r => r.id === id && r.sha256).at(-1);
      const e = await this.#fromHistory(id, full?.sha256);
      if (!e) return { refused: 'not_in_history', reason: 'the entry was never committed, so git cannot return it' };
      this.forgotten.delete(id);
      this.#appendEntries([e]);
      appendDurable(join(this.dir, 'forgotten.jsonl'), JSON.stringify({ id, at: this.now(), restored: true, by }) + '\n');
      await this.#commit(`restore ${id} (${by})`);
      if (this.remote) this.#remoteAdd([e]);
      return { restored: id, status: this.statusLine() };
    });
  }

  // ---------------------------------------------------------------- search

  /** memory_search (design 3.5, 3.8.1): grep and the ranked index, both, each with its source.
   *  Each answer gives its hits' whole entries as `text` - grouped by the owner's calendar
   *  day, every line dated - and the machine-readable `hits` beside it (memory_read takes
   *  `hits[].id`). Both answers share one budget (SEARCH_CHARS); a hit is whole or listed in
   *  `omitted`, never cut and never dropped. */
  async search(query, topK = 10) {
    const q = String(query ?? '');
    if (!q.trim()) return { refused: 'empty_query' };
    const k = Math.max(1, Math.min(TOP_K_MAX, Number.isInteger(topK) ? topK : 10));
    const [grep, ranked] = await Promise.all([this.#grep(q, k), this.#ranked(q, k)]);
    const answers = [{ name: 'grep', items: grep.items, omitted: [] }];
    if (ranked.items) answers.push({ name: 'ranked', items: ranked.items, omitted: [] });
    const used = this.#place(answers);
    const out = answer => ({ text: this.#grouped(answer), hits: answer.items.map(it => it.hit), omitted: answer.omitted });
    const { items: _g, ...grepHead } = grep;
    const { items: _r, filtered_forgotten, remote_forget, ...rankedHead } = ranked;
    return { query: q,
      grep: { ...grepHead, ...out(answers[0]) },
      ranked: ranked.items ? { ...rankedHead, ...out(answers[1]), filtered_forgotten, ...(remote_forget ? { remote_forget } : {}) } : ranked,
      // The entries left to read: one both answers had to omit is one entry, counted once.
      budget: `${used}/${SEARCH_CHARS} 字 · omitted ${new Set(answers.flatMap(a => a.omitted.map(o =>
        o.line !== undefined ? 'profile:' + o.line : o.id ?? 'ref:' + o.ref))).size}`,
      status: this.statusLine() };
  }

  /** The line of a search item; `see` names the answer that shows the entry whole. */
  #line(item, see) {
    return item.profileLine !== undefined ? formatProfileLine(item.profileLine, item.entry.text)
      : formatLine(item.entry, this.timeZone, { dateOnly: item.hit.date_only === true, see });
  }

  /** Design 3.8.1's one budget: the two answers in turn by rank (grep 1, ranked 1, grep 2, …).
   *  What fits goes in whole; what does not is listed in that answer's `omitted` and the next
   *  one is tried (a shorter one later may still fit). An entry the other answer already
   *  shows whole costs only its line head with `→ 见 grep` / `→ 见 ranked`. The budget counts
   *  the rendered lines, heads included (not the newlines between them). Returns the
   *  characters used. */
  #place(answers) {
    const whole = new Map(); // entry id -> the answer showing it whole
    let used = 0;
    const longest = Math.max(...answers.map(a => a.items.length));
    for (let i = 0; i < longest; i++) for (const answer of answers) {
      const item = answer.items[i];
      if (!item) continue;
      const id = item.profileLine === undefined ? item.entry.id : undefined;
      const see = id !== undefined ? whole.get(id) : undefined;
      const line = this.#line(item, see);
      if (used + line.length <= SEARCH_CHARS) {
        used += line.length; item.line = line;
        item.hit.shown = see ? 'see_' + see : 'full';
        if (id !== undefined && !see) whole.set(id, answer.name);
      } else {
        item.hit.shown = 'omitted';
        answer.omitted.push(item.profileLine !== undefined ? { id: 'profile', line: item.profileLine, length: item.entry.text.length }
          : { ...(id !== undefined ? { id } : { ref: item.hit.ref }), at: item.entry.at, length: item.entry.text.length });
      }
    }
    return used;
  }

  /** An answer's placed lines as text: grouped by the owner's calendar day, groups apart by a
   *  blank line, no titles. ranked orders the groups by their best rank, grep newest day
   *  first; the profile's lines are a group of their own after grep's days, in line order;
   *  lines without a usable time are one group, last of the days. In a group: oldest first,
   *  then by id. */
  #grouped(answer) {
    const groups = new Map();
    answer.items.forEach((item, rank) => {
      if (item.line === undefined) return;
      const key = item.profileLine !== undefined ? '\u0002profile' : dayOf(item.entry.at, this.timeZone) ?? '\u0001undated';
      if (!groups.has(key)) groups.set(key, { key, best: rank, rows: [] });
      groups.get(key).rows.push(item);
    });
    const tail = key => (key === '\u0002profile' ? 2 : key === '\u0001undated' ? 1 : 0);
    const order = answer.name === 'grep'
      ? (a, b) => tail(a.key) - tail(b.key) || (a.key < b.key ? 1 : a.key > b.key ? -1 : 0)
      : (a, b) => tail(a.key) - tail(b.key) || a.best - b.best;
    const time = item => { const ms = parseAt(item.entry.at); return Number.isNaN(ms) ? -Infinity : ms; };
    const inGroup = (a, b) => (a.profileLine !== undefined ? a.profileLine - b.profileLine
      : time(a) - time(b) || byId(a.entry.id, b.entry.id));
    return [...groups.values()].sort(order).map(g => g.rows.sort(inGroup).map(item => item.line).join('\n')).join('\n\n');
  }

  /** memory_read (design 3.5): the whole entries a search named. `profile` is the resident
   *  profile. withTurn adds the other live entries of the same turns, marked context, after
   *  every entry asked for. A forgotten entry answers `forgotten`; a purged or unknown id
   *  answers `not_found` - forgotten and purged text is as invisible here as in search
   *  (an id is not checked to come from a search: the memory is the steward's own). The texts are whole up to
   *  READ_CHARS in all: an entry past that is listed in `omitted` (read it next), and an
   *  entry longer than the limit on its own comes in pieces, `cut.next_offset` naming where
   *  the next piece starts (read that id alone with that offset). */
  async read(ids, { withTurn = false, offset = 0 } = {}) {
    if (!Array.isArray(ids) || !ids.length || ids.some(id => typeof id !== 'string' || !id)) return { refused: 'ids_required' };
    const wanted = [...new Set(ids)];
    if (wanted.length > READ_IDS_MAX) return { refused: 'too_many_ids', limit: READ_IDS_MAX };
    if (!Number.isInteger(offset) || offset < 0) return { refused: 'bad_offset' };
    if (offset && (wanted.length !== 1 || withTurn)) return { refused: 'offset_needs_one_id', reason: 'offset continues one entry: pass that id alone, without with_turn' };
    const items = [], forgotten = [], notFound = [], seen = new Set(), turns = [];
    // The entries asked for first, in the order asked, never marked context.
    for (const id of wanted) {
      if (id === 'profile') {
        const p = await this.profile();
        seen.add(id); items.push({ id, text: p.text, version: p.version });
        continue;
      }
      const e = this.entries.get(id);
      if (!e) { (this.forgotten.has(id) && !this.forgotten.get(id).purged ? forgotten : notFound).push(id); continue; }
      seen.add(id); items.push(this.#readable(e));
      if (withTurn && e.batch && !turns.includes(e.batch)) turns.push(e.batch);
    }
    // Then the rest of their turns, each turn in time order.
    for (const batch of turns) {
      const turn = [...this.entries.values()].filter(x => x.batch === batch && !seen.has(x.id))
        .sort((a, b) => (a.at > b.at ? 1 : a.at < b.at ? -1 : a.id.localeCompare(b.id, 'en', { numeric: true })));
      for (const x of turn) { seen.add(x.id); items.push({ ...this.#readable(x), context: true }); }
    }
    if (offset && items.length && offset >= items[0].text.length) return { refused: 'offset_past_end', length: items[0].text.length };
    const entries = [], omitted = [];
    let used = 0;
    for (const item of items) {
      const text = offset ? item.text.slice(offset) : item.text;
      if (used + text.length <= READ_CHARS) { entries.push({ ...item, text, ...(offset ? { offset } : {}) }); used += text.length; }
      else if (!entries.length) {
        entries.push({ ...item, text: text.slice(0, READ_CHARS), ...(offset ? { offset } : {}),
          cut: { next_offset: offset + READ_CHARS, length: item.text.length } });
        used = READ_CHARS;
      } else omitted.push({ id: item.id, length: item.text.length });
    }
    return { entries, ...(omitted.length ? { omitted } : {}), ...(forgotten.length ? { forgotten } : {}),
      ...(notFound.length ? { not_found: notFound } : {}), status: this.statusLine() };
  }

  #readable(e) {
    return { id: e.id, at: e.at, role: e.role, source: e.source, text: e.text, ...(e.batch && e.batch !== e.id ? { turn: e.batch } : {}) };
  }

  /** grep's items, newest first, at most k: `{hit, entry}` for a log line (the entry as the
   *  log holds it) and `{hit, entry: {text}, profileLine}` for a line of the profile. */
  async #grep(query, k) {
    const source = 'grep · 字面匹配 · 本地记忆日志';
    const needle = JSON.stringify(query).slice(1, -1);
    const r = await git(this.dir, ['grep', '-F', '-i', '-I', '-n', '-z', '--no-color', '--untracked', '-e', needle, '--', 'log', 'profile.md'],
      { timeoutMs: this.grepTimeoutMs, onSpawn: pid => { this.lastGrepPid = pid; } });
    const items = [];
    const lower = query.toLowerCase();
    for (const record of r.stdout.toString('utf8').split('\n')) {
      const [file, line, ...rest] = record.split('\0');
      if (!file || rest.length === 0) continue;
      const content = rest.join('\0');
      if (file === 'profile.md') {
        items.push({ at: '', profileLine: Number(line), entry: { text: content }, hit: { id: 'profile', line: Number(line) } });
        continue;
      }
      let e;
      try { e = JSON.parse(content); } catch { continue; }
      if (!e?.text || !e.text.toLowerCase().includes(lower)) continue;
      items.push({ at: e.at, entry: e, hit: { id: e.id, at: e.at, role: e.role } });
    }
    items.sort((a, b) => (b.at > a.at ? 1 : b.at < a.at ? -1 : 0));
    return { source, complete: !r.killed && (r.code === 0 || r.code === 1),
      ...(r.killed ? { reason: `grep killed after ${this.grepTimeoutMs} ms; partial` } : r.code > 1 ? { reason: r.stderr.trim().slice(0, 200) } : {}),
      items: items.slice(0, k) };
  }

  /** Design 3.8.1: a local ranked hit that shares no word with the query and only meets it
   *  in its date words is that day's record, not one close in meaning. */
  #dateOnly(words, e) {
    const text = new Set(documentTokens(e.text));
    if (words.some(t => text.has(t))) return false;
    const dates = new Set(dateTokens(e.at, this.timeZone));
    return words.some(t => dates.has(t));
  }

  /** The ranked items, best first, or {source, error} (a remote that failed: no answer).
   *  Local hits are read from `entries` (live entries only). A remote hit whose text is a
   *  live local entry's - any entry, not only one grep found (a hit close in meaning rarely
   *  holds the query's words) - is shown as that entry and carries its id, which memory_read
   *  takes; one with no local twin is the remote's own line, without an id. */
  async #ranked(query, k) {
    const source = this.ranked.name;
    if (this.remote) await this.#replayPending().catch(() => {});
    let hits;
    try { hits = await this.ranked.search(query, k); }
    catch (error) { return { source, error: String(error?.message ?? error).slice(0, 500) }; }
    let filtered = 0;
    let remoteForget;
    if (this.remote) {
      if (this.remote.batchOf) {
        const gone = new Set([...this.forgotten.values()].filter(r => r.batch).map(r => r.batch));
        hits = hits.filter(h => { const b = h.ref && this.remote.batchOf(h.ref); if (b && gone.has(b)) { filtered++; return false; } return true; });
      } else if (!this.remote.remove) remoteForget = 'unsupported';
    }
    const items = [];
    if (this.remote) {
      // Identical texts: the newest entry by `at` (its turn is the one with_turn brings).
      // One pass over the entries, only on a remote search that returned hits.
      const localTexts = new Map();
      if (hits.length) for (const e of this.entries.values()) {
        const seen = localTexts.get(e.text);
        if (!seen || e.at >= seen.at) localTexts.set(e.text, e);
      }
      const taken = new Set(); // a second remote hit on the same entry stays the remote's own line
      for (const h of hits) {
        const text = String(h.text ?? '');
        const local = localTexts.get(text);
        if (local && !taken.has(local.id)) {
          taken.add(local.id);
          items.push({ entry: local, hit: { id: local.id, ref: h.ref, at: h.at, role: local.role, score: h.score } });
        } else items.push({ entry: { at: h.at, role: 'remote', text }, hit: { ref: h.ref, at: h.at, role: 'remote', score: h.score } });
      }
    } else {
      const words = queryTokens(query);
      for (const h of hits) {
        const e = this.entries.get(h.ref);
        if (!e) continue; // the index and the entries change together; never a text we cannot show
        items.push({ entry: e, hit: { id: e.id, ref: e.id, at: e.at, role: e.role, score: h.score,
          ...(this.#dateOnly(words, e) ? { date_only: true } : {}) } });
      }
    }
    return { source, coverage: this.remote ? 'remote' : this.coverage, items,
      filtered_forgotten: filtered, ...(remoteForget ? { remote_forget: remoteForget } : {}) };
  }

  // ---------------------------------------------------------------- purge

  #journal(row) {
    appendDurable(this.journalFile, JSON.stringify({ ...row, at: this.now() }) + '\n');
  }

  /** Drop a build directory that will not be swapped in (the caller journals first). */
  #discardBuild(plan) {
    if (!existsSync(plan.new_dir)) return;
    rmSync(plan.new_dir, { recursive: true, force: true }); fsyncDir(this.root);
  }

  /** Put the old repository back: journal the intent, then the two renames, then drop the
   *  build. Shared by the in-purge rollback and by recovery, so both leave the same rows and
   *  a crash in either window is describable from the layout alone.
   *
   *  The build is dropped BEFORE the first rename, not after the second: a repository, an old
   *  copy and a build at once (an interrupted delete of a build leaves exactly that) made that
   *  rename throw a bare ENOTEMPTY, and a rollback's job is to be one decision on every boot.
   *  Every caller holds this.dir, so the build here is never the only copy; a build that is
   *  already gone makes this a no-op. */
  async #rollbackSwap(plan, { reason, fromLayout } = {}) {
    this.#journal({ ...plan, phase: 'rolling_back',
      ...(reason ? { reason: String(reason).slice(0, 500) } : {}), ...(fromLayout ? { from_layout: fromLayout } : {}) });
    if (this.faults?.beforeRollback) await this.faults.beforeRollback();
    this.#discardBuild(plan);
    renameSync(this.dir, plan.new_dir); fsyncDir(this.root);
    if (this.faults?.duringRollback) await this.faults.duringRollback();
    renameSync(plan.pre_purge_dir, this.dir); fsyncDir(this.root);
    if (this.faults?.afterRollback) await this.faults.afterRollback();
    this.#discardBuild(plan);
  }

  /** Purge (design 3.6 level 2): owner only, never a steward tool. Build a filtered copy,
   *  verify it, swap it in, verify again, then delete the old repository. */
  async purge(ids, { by = 'owner' } = {}) {
    if (!Array.isArray(ids) || !ids.length) return { refused: 'ids_required' };
    if (this.purging) return { refused: 'purge_in_progress' };
    this.purging = true;
    let result;
    try {
      result = await this.#locked(async () => {
        const stamp = this.now().replace(/[^0-9]/g, '').slice(0, 17);
        const purgeId = 'purge-' + stamp;
        const plan = { purge_id: purgeId, ids, dir: this.dir, new_dir: join(this.root, `${this.userId}.purge-new-${stamp}`),
          pre_purge_dir: join(this.root, `${this.userId}.pre-purge-${stamp}`) };
        // 1. forget first, committed (a purge never runs on an uncommitted tree)
        const live = ids.filter(id => this.entries.has(id));
        const known = ids.filter(id => this.entries.has(id) || this.forgotten.has(id));
        if (known.length !== ids.length) return { refused: 'unknown_ids', unknown: ids.filter(id => !known.includes(id)) };
        if (live.length) {
          // Journalled before the first destructive edit: a crash inside step 1 has to be
          // recognisable at the next boot, and step 1 IS a forget, the remote included.
          this.#journal({ ...plan, phase: 'forgetting' });
          const dropped = await this.#dropFromWorkTree(live, { reason: 'purge', by });
          if (dropped.length && this.remote?.remove) {
            try { await this.remote.remove(dropped.map(e => e.id)); }
            catch (error) { this.log('memory/remote-forget-failed', { ids: dropped.map(e => e.id), reason: String(error.message).slice(0, 200) }); }
          }
        }
        if (!await this.#commitIfDirty(`forget ${ids.join(',')} (${by}, before purge)`)) return { refused: 'commit_failing', status: this.statusLine() };
        this.#journal({ ...plan, phase: 'forget_done' });
        // 2. build
        this.#journal({ ...plan, phase: 'building' });
        if (this.faults?.beforeBuild) await this.faults.beforeBuild();
        try { await this.#buildFiltered(plan.new_dir, new Set(ids)); }
        catch (error) { rmSync(plan.new_dir, { recursive: true, force: true }); this.#journal({ ...plan, phase: 'done', outcome: 'rolled_back', reason: String(error.message).slice(0, 300) }); return { purged: false, reason: 'build failed: ' + error.message }; }
        // 3. verify
        const failures = await this.verifyPurged(plan.new_dir, this.dir, new Set(ids));
        if (failures.length) {
          rmSync(plan.new_dir, { recursive: true, force: true });
          this.#journal({ ...plan, phase: 'done', outcome: 'rolled_back', reason: failures.join('; ').slice(0, 500) });
          return { purged: false, reason: 'verification failed; nothing changed', failures };
        }
        this.#journal({ ...plan, phase: 'verified' });
        if (this.faults?.afterVerify) await this.faults.afterVerify();
        // 4. swap, in two renames
        renameSync(this.dir, plan.pre_purge_dir); fsyncDir(this.root);
        if (this.faults?.afterRenameOld) await this.faults.afterRenameOld();
        this.#journal({ ...plan, phase: 'renamed_old' });
        if (this.faults?.betweenRenames) await this.faults.betweenRenames();
        renameSync(plan.new_dir, this.dir); fsyncDir(this.root);
        if (this.faults?.afterRenameNew) await this.faults.afterRenameNew();
        this.#journal({ ...plan, phase: 'renamed_new' });
        if (this.faults?.afterSwap) await this.faults.afterSwap();
        // 5. verify again, at the final path
        const again = await this.verifyPurged(this.dir, plan.pre_purge_dir, new Set(ids));
        if (again.length) {
          await this.#rollbackSwap(plan, { reason: again.join('; ') });
          this.#journal({ ...plan, phase: 'done', outcome: 'rolled_back', reason: again.join('; ').slice(0, 500) });
          this.#load();
          return { purged: false, reason: 'the re-check after the swap failed; the old repository is back', failures: again };
        }
        this.#journal({ ...plan, phase: 'rechecked' });
        // 6. delete the only recoverable copy
        this.#journal({ ...plan, phase: 'deleting_old' });
        if (this.faults?.beforeDelete) await this.faults.beforeDelete();
        rmSync(plan.pre_purge_dir, { recursive: true, force: true }); fsyncDir(this.root);
        this.#journal({ ...plan, phase: 'done', outcome: 'completed' });
        this.#load();
        this.log('memory/purged', { ids });
        return { purged: true, ids, not_reached: this.#notReached(ids), status: this.statusLine() };
      });
    } finally {
      this.purging = false;
      await this.#replaySpool();
    }
    if (result && 'status' in result) result.status = this.statusLine();
    return result;
  }

  #notReached(ids) {
    const out = [];
    if (this.remote) out.push(`the remote index (${this.remote.name}) keeps its copy; delete ids ${ids.join(', ')} there`);
    if (this.backupRemote) out.push('the backup remote still holds the old history until it is force-pushed (--force-with-lease)');
    out.push('any other clone of this repository is outside the plugin');
    return out;
  }

  async #buildFiltered(newDir, ids) {
    const exported = await gitOk(this.dir, ['fast-export', '--all', '--signed-tags=strip', '--reencode=yes'], { timeoutMs: 600000 });
    const stream = filterExportStream(exported.stdout, ids, this.faults?.filterLine);
    mkdirSync(newDir, { mode: 0o700 });
    await gitOk(newDir, ['init', '-q', '--shared=0600', '-b', 'main']);
    await gitOk(newDir, ['fast-import', '--quiet'], { input: stream, timeoutMs: 600000 });
    await gitOk(newDir, ['checkout', '-q', '-f', 'main']);
    await gitOk(newDir, ['reflog', 'expire', '--expire=now', '--all']);
    await gitOk(newDir, ['gc', '-q', '--prune=now']);
    this.#tightenTree(newDir);
  }

  #tightenTree(dir) {
    for (const name of readdirSync(dir, { recursive: true })) {
      const path = join(dir, name);
      const st = statSync(path);
      chmodSync(path, st.isDirectory() ? 0o700 : st.mode & 0o200 ? 0o600 : 0o400);
    }
  }

  /** Step 3 and 5 checks; [] when the purged repository is what it must be. */
  async verifyPurged(newDir, oldDir, ids) {
    const failures = [];
    const fsck = await git(newDir, ['fsck', '--full']);
    if (fsck.code !== 0) failures.push('fsck: ' + fsck.stderr.trim().slice(0, 200));
    const unreachable = await git(newDir, ['fsck', '--unreachable', '--no-reflogs']);
    if (unreachable.stdout.toString().trim()) failures.push('unreachable objects remain');
    for (const p of ['.git/refs/original', '.git/refs/stash', '.git/lost-found']) if (existsSync(join(newDir, p))) failures.push(p + ' exists');
    const holds = buffer => buffer.toString('utf8').split('\n').some(line => {
      const i = line.indexOf('{');
      if (i < 0) return false;
      try {
        const row = JSON.parse(line.slice(line.startsWith('+') || line.startsWith('-') || line.startsWith(' ') ? 1 : i));
        return ids.has(row.id) && (typeof row.text === 'string' || row.reason !== undefined || row.sha256 !== undefined);
      } catch { return false; }
    });
    const logp = await git(newDir, ['log', '-p', '--all', '--format=']);
    if (holds(logp.stdout)) failures.push('git log -p --all still holds a purged entry');
    const all = await git(newDir, ['cat-file', '--batch-all-objects', '--batch']);
    if (holds(all.stdout)) failures.push('an object still holds a purged entry');
    // iss-e4eb: the id check alone is blind to a text that lost its id. So the text itself is
    // counted too: outside lines naming a purged id, the new repository may hold it only as
    // often as the old one did (the same words said in another entry, the profile, an id).
    // Each side is split once: the lines still naming a purged id are the id check's
    // business (they carry no text, or the id check already failed) and are set aside.
    const outside = text => withoutLinesNaming(text, ids);
    const sides = [
      ['git log -p --all', outside((await git(oldDir, ['log', '-p', '--all', '--format='])).stdout.toString('utf8')), outside(logp.stdout.toString('utf8'))],
      ['cat-file --batch-all-objects', outside((await git(oldDir, ['cat-file', '--batch-all-objects', '--batch'])).stdout.toString('utf8')), outside(all.stdout.toString('utf8'))],
    ];
    for (const id of ids) {
      const e = await this.#fromHistory(id, undefined, oldDir);
      if (!e) continue; // never committed: nothing of it can be in history
      const forms = [...new Set([e.text, JSON.stringify(e.text).slice(1, -1)])];
      for (const [name, before, after] of sides) {
        const allowed = forms.reduce((n, f) => n + occurrences(before, f), 0);
        const found = forms.reduce((n, f) => n + occurrences(after, f), 0);
        if (found > allowed) failures.push(`${name} still holds the text of ${id} (${found} times, ${allowed} outside purged lines)`);
      }
    }
    const count = async d => (await git(d, ['rev-list', '--all', '--count'])).stdout.toString().trim();
    if (await count(newDir) !== await count(oldDir)) failures.push('commit counts differ');
    const files = async d => (await git(d, ['ls-files'])).stdout.toString().split('\n').filter(Boolean).sort().join('\n');
    const oldFiles = await files(oldDir);
    if (await files(newDir) !== oldFiles) failures.push('tracked files differ');
    for (const f of oldFiles.split('\n').filter(Boolean)) {
      const want = filterBlob(existsSync(join(oldDir, f)) ? readFileSync(join(oldDir, f)) : Buffer.alloc(0), ids);
      const have = existsSync(join(newDir, f)) ? readFileSync(join(newDir, f)) : Buffer.alloc(0);
      if (!want.equals(have)) failures.push(`working tree differs at ${f}`);
    }
    const profile = async d => (await git(d, ['log', '--reverse', '--format=', '-p', '--', 'profile.md'])).stdout.toString();
    if (await profile(newDir) !== await profile(oldDir)) failures.push('profile history differs');
    if (this.faults?.verify) failures.push(...await this.faults.verify(newDir));
    return failures;
  }

  /** Is this a complete repository, i.e. one we would dare to put back in place? A copy whose
   *  delete had begun is not: git still calls it a repository while its log files are gone. */
  async #completeRepository(dir) {
    if (!existsSync(join(dir, '.git'))) return false;
    const head = await git(dir, ['rev-parse', '--verify', 'HEAD']);
    if (head.code !== 0 || !head.stdout.toString().trim()) return false;
    const fsck = await git(dir, ['fsck', '--connectivity-only', '--no-dangling']);
    if (fsck.code !== 0) return false;
    const files = (await git(dir, ['ls-files'])).stdout.toString().split('\n').filter(Boolean);
    if (!files.length) return false;
    return files.every(f => existsSync(join(dir, f)));
  }

  /** Step 1 is a forget, and its records are appended before the log lines go: a crash in
   *  between leaves an id in the log AND in forgotten.jsonl. Finish the removal here, so a
   *  forgotten entry is never searchable again. Called with the ids a purge's journal names
   *  (recovery) and with the ids the boot load found in both places (an ordinary forget, whose
   *  window no journal describes); `loaded` says that load has already run on this layout.
   *  Returns how many ids it finished. */
  #finishForget(ids, { loaded = false } = {}) {
    if (!loaded) this.#load(); // a directory swap since the last load makes it worthless
    const both = ids.filter(id => this.entries.has(id) && this.forgotten.has(id));
    if (!both.length) return 0;
    this.#stripLogLines(both);
    for (const id of both) { const e = this.entries.get(id); this.entries.delete(id); this.index.remove(id, this.#docTokens(e)); }
    this.log('memory/forget-finished', { ids: both });
    return both.length;
  }

  /** Boot: finish or roll back a purge the journal shows unfinished (design 3.6).
   *
   *  WHICH action is possible is decided from the directories that exist, never from the
   *  journal phase alone: a journal row is written after its step, so a crash can leave a
   *  step done and its row missing. The journal supplies only the two things the layout
   *  cannot say: whether a delete of the old copy had begun (that row is written BEFORE the
   *  delete, which is why a torn old copy is never mistaken for a repository) and the label
   *  of a finish whose disk state is the same either way. Every layout this store can leave
   *  behind therefore has exactly one action, and none of them needs a human to edit the
   *  journal. Returns the notice for the owner. */
  async #recoverPurge() {
    const rows = parseLines(this.journalFile);
    const last = rows.at(-1);
    if (!last || last.phase === 'done') return undefined;
    const plan = rows.filter(r => r.purge_id === last.purge_id)[0] ?? last;
    const has = p => existsSync(p);
    const dir = has(plan.dir), pre = has(plan.pre_purge_dir), fresh = has(plan.new_dir);
    const layout = `${dir ? 'repo' : 'no-repo'}/${pre ? 'old' : 'no-old'}/${fresh ? 'build' : 'no-build'}`;
    const ids = new Set(plan.ids ?? []);
    const dropOld = async () => {
      if (last.phase !== 'deleting_old') this.#journal({ ...plan, phase: 'deleting_old', from_layout: layout });
      if (this.faults?.recoveryBeforeDelete) await this.faults.recoveryBeforeDelete();
      rmSync(plan.pre_purge_dir, { recursive: true, force: true }); fsyncDir(this.root);
    };
    let outcome;
    if (!dir && !pre) {
      throw new MemoryUnavailable(`memory: purge ${plan.purge_id} left neither ${plan.dir} nor ${plan.pre_purge_dir} (${layout}); there is nothing to put back`);
    } else if (!dir) {
      // The old repository was renamed aside and the new one never arrived: it is the only copy.
      this.#journal({ ...plan, phase: 'rolling_back', from_layout: layout });
      renameSync(plan.pre_purge_dir, plan.dir); fsyncDir(this.root);
      this.#discardBuild(plan); outcome = 'rolled_back';
    } else if (pre) {
      if (last.phase === 'deleting_old' || !await this.#completeRepository(plan.pre_purge_dir)) {
        // The delete had begun, or the old copy is already torn: never put it back.
        await dropOld(); outcome = 'completed';
      } else if (last.phase === 'rolling_back') {
        // The rollback was already decided before the crash: finish it instead of deciding
        // again, so two verdicts on the same bytes can never disagree across a boot.
        await this.#rollbackSwap(plan, { fromLayout: layout });
        outcome = 'rolled_back';
      } else {
        const failures = await this.verifyPurged(plan.dir, plan.pre_purge_dir, ids);
        if (failures.length) {
          await this.#rollbackSwap(plan, { reason: failures.join('; '), fromLayout: layout });
          outcome = 'rolled_back';
        } else { await dropOld(); outcome = 'completed'; }
      }
    } else if (fresh) {
      // The repository in place is the old one and the build is what is left over (a rollback
      // whose renames ran, or a build that was never swapped in): discard the build.
      this.#journal({ ...plan, phase: 'rolling_back', from_layout: layout });
      this.#discardBuild(plan); outcome = 'rolled_back';
    } else {
      // Only the repository is left: the old one (nothing was swapped) or the new one (the
      // delete finished). The layout cannot say which; the last phase can.
      outcome = PRE_SWAP_PHASES.has(last.phase) ? 'rolled_back' : 'completed';
    }
    const finished = this.#finishForget(plan.ids ?? []);
    this.#journal({ ...plan, phase: 'done', outcome, recovered_from: last.phase, layout,
      ...(finished ? { forget_finished: finished } : {}) });
    this.log('memory/purge-recovered', { purge_id: plan.purge_id, from: last.phase, layout, outcome, forget_finished: finished });
    return `上次彻底删除（${plan.purge_id}，条目 ${plan.ids.join(', ')}）被中断，结果为：${outcome === 'completed' ? '已完成' : '已回退，未删除'}`;
  }
}
