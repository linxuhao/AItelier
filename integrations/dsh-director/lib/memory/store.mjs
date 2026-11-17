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
 *  The lock, spool, journal and pending file sit beside the repository, not in it, so the
 *  purge's directory swap can never move or drop them.
 *
 *  A write succeeds when its file is on disk (fsync); the git commit that follows can only
 *  degrade (status.git says why and since when) and the next commit takes the backlog. Logs
 *  name entry ids and lengths, never memory text. */
import { createHash } from 'node:crypto';
import { closeSync, constants, existsSync, fsyncSync, openSync, readFileSync, readdirSync, renameSync,
  rmSync, statSync, writeSync, chmodSync, mkdirSync, unlinkSync } from 'node:fs';
import { join } from 'node:path';
import { Bm25Index, bm25Ranked } from './bm25.mjs';
import { git, gitOk } from './git.mjs';
import { filterBlob, filterExportStream } from './purge-filter.mjs';

export const PROFILE_LIMIT = 4000;
export const TOP_K_MAX = 20;
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

function pidAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try { process.kill(pid, 0); return true; } catch (error) { return error.code === 'EPERM'; }
}

function excerpt(text, query) {
  const at = query ? text.toLowerCase().indexOf(String(query).toLowerCase()) : -1;
  if (at < 0) return text.length > 160 ? text.slice(0, 160) + '…' : text;
  const start = Math.max(0, at - 60), end = Math.min(text.length, at + query.length + 60);
  return (start ? '…' : '') + text.slice(start, end) + (end < text.length ? '…' : '');
}

export class MemoryStore {
  /** @param {object} o
   *  root, userId; ranked: a remote RankedIndex (absent: the built-in BM25);
   *  logger(event, fields); now(): ISO time; grepTimeoutMs (5000); syncMonths (12);
   *  backupRemote: a git remote URL pushed after each commit (failures only degrade). */
  static async open(o) {
    const store = new MemoryStore(o);
    await store.#boot();
    return store;
  }

  constructor({ root, userId, ranked, logger = () => {}, now = () => new Date().toISOString(),
    grepTimeoutMs = 5000, syncMonths = 12, backupRemote, warnEntries = 200000, faults }) {
    if (typeof userId !== 'string' || !USER_ID.test(userId)) throw new Error('memory: userId must be 1-128 of letters, digits, @ . _ + - (not starting with .)');
    this.root = root; this.userId = userId; this.log = logger; this.now = now;
    this.grepTimeoutMs = grepTimeoutMs; this.syncMonths = syncMonths; this.backupRemote = backupRemote; this.warnEntries = warnEntries;
    this.dir = join(root, userId);
    this.lockFile = join(root, userId + '.lock');
    this.spoolFile = join(root, userId + '.spool.jsonl');
    this.journalFile = join(root, userId + '.purge.jsonl');
    this.pendingFile = join(root, userId + '.ranked-pending.json');
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
    if (notice) this.notices.push(notice);
    if (!existsSync(this.dir)) {
      const leftovers = readdirSync(this.root).filter(name => name.startsWith(this.userId + '.pre-purge-'));
      if (leftovers.length) throw new MemoryUnavailable(`memory: ${this.dir} is missing while ${leftovers.join(', ')} exists; refusing to start an empty repository over it`);
      await this.#init();
    }
    chmodSync(this.dir, 0o700);
    await this.#commitIfDirty('recover: uncommitted changes at boot');
    this.#load();
    await this.#replaySpool();
    await this.#replayPending(true);
  }

  #acquireLock() {
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        const fd = openSync(this.lockFile, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL, 0o600);
        try { writeSync(fd, String(process.pid)); fsyncSync(fd); } finally { closeSync(fd); }
        this.lockHeld = true;
        return;
      } catch (error) {
        if (error.code !== 'EEXIST') throw error;
        const holder = Number(readFileSync(this.lockFile, 'utf8').trim());
        if (holder !== process.pid && pidAlive(holder)) throw new MemoryUnavailable(`memory: locked by process ${holder}`);
        this.log('memory/lock-taken-over', { stale_pid: holder });
        unlinkSync(this.lockFile);
      }
    }
    throw new MemoryUnavailable('memory: could not take the lock');
  }

  /** Release the process lock (the store is unusable afterwards). */
  async close() {
    await this.queue;
    if (this.lockHeld) { try { unlinkSync(this.lockFile); } catch {} this.lockHeld = false; }
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

  #load() {
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
      if (name.slice(0, 7) >= cutoff) for (const e of rows) this.index.add(e.id, e.text, e.at);
      else later.push(rows);
    }
    if (this.entries.size > this.warnEntries) this.log('memory/large', { entries: this.entries.size });
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
        if (rows) for (const e of rows) if (this.entries.has(e.id)) this.index.add(e.id, e.text, e.at);
        if (later.length) setImmediate(next); else this.coverage = 'all';
      };
      setImmediate(next);
    } else this.coverage = 'all';
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

  /** Wait for every queued write and commit (tests, shutdown). */
  flush() { return this.#locked(() => {}); }

  statusLine() {
    return {
      git: this.gitState.ok ? 'ok' : `commit failing since ${this.gitState.since}: ${this.gitState.reason}`,
      index: `${this.index.size} entries`,
      ...(this.purging ? { purge: 'in progress: writes are spooled' } : {}),
      ...(this.notices.length ? { notices: this.notices } : {}),
      ...(this.pendingStuck?.length ? { ranked_pending_stuck: this.pendingStuck } : {}),
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
      return { id: `t-${turnId}-${i}`, at: m.at ?? at, role: m.role, source: 'conversation', text: String(m.text), batch: turnId };
    }).filter(e => e.text.trim());
    return this.#writeEntries(entries, `turn ${turnId} (runtime)`);
  }

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
    const written = this.#locked(async () => {
      if (this.purging) return { spool: true };
      fresh = this.#appendEntries(entries);
      return { written: fresh.length };
    });
    const result = await written;
    if (result.spool) return this.#writeEntries(entries, message);
    // The commit runs after the caller has its answer (it never delays a reply).
    if (fresh.length) {
      this.#locked(() => this.#commit(message));
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
    for (const e of fresh) { this.entries.set(e.id, e); this.index.add(e.id, e.text, e.at); }
    if (fresh.length) this.log('memory/written', { ids: fresh.map(e => e.id), lengths: fresh.map(e => e.text.length) });
    return fresh;
  }

  async #replaySpool() {
    const rows = parseLines(this.spoolFile);
    if (!rows.length) { if (existsSync(this.spoolFile)) writeDurable(this.spoolFile, ''); return; }
    await this.#locked(async () => {
      let n = 0;
      for (const row of rows) n += this.#appendEntries(row.entries ?? []).length;
      await this.#commit(`spool: ${n} entries written during a purge (runtime)`);
      writeDurable(this.spoolFile, '');
      this.log('memory/spool-replayed', { entries: n });
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
      const dirty = await git(this.dir, ['status', '--porcelain', '--', 'profile.md']);
      if (dirty.code === 0 && dirty.stdout.length) {
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
      if (expectedVersion !== current.version) return { refused: 'version_conflict', current };
      writeDurable(join(this.dir, 'profile.md'), value);
      await this.#commit(`profile (${by}): ${why.slice(0, 200)}`);
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
      this.entries.delete(e.id); this.index.remove(e.id, e.text);
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

  /** memory_search (design 3.5): grep and the ranked index, both, each with its source. */
  async search(query, topK = 10) {
    const q = String(query ?? '');
    if (!q.trim()) return { refused: 'empty_query' };
    const k = Math.max(1, Math.min(TOP_K_MAX, Number.isInteger(topK) ? topK : 10));
    const [grep, ranked] = await Promise.all([this.#grep(q, k), this.#ranked(q, k)]);
    const grepIds = new Set(grep.hits.map(h => h.id));
    const grepTexts = new Map(grep.hits.filter(h => h.text).map(h => [h.text, h.id]));
    if (ranked.hits) {
      const rankedIds = new Set();
      for (const h of ranked.hits) {
        const id = this.remote ? grepTexts.get(h.text) : h.ref;
        if (id && grepIds.has(id)) { h.also_in_grep = true; rankedIds.add(id); }
      }
      for (const h of grep.hits) if (rankedIds.has(h.id)) h.also_in_ranked = true;
    }
    for (const h of grep.hits) delete h.text;
    for (const h of ranked.hits ?? []) delete h.text;
    return { query: q, grep, ranked, status: this.statusLine() };
  }

  async #grep(query, k) {
    const source = 'grep · 字面匹配 · 本地记忆日志';
    const needle = JSON.stringify(query).slice(1, -1);
    const r = await git(this.dir, ['grep', '-F', '-i', '-I', '-n', '-z', '--no-color', '--untracked', '-e', needle, '--', 'log', 'profile.md'],
      { timeoutMs: this.grepTimeoutMs, onSpawn: pid => { this.lastGrepPid = pid; } });
    const hits = [];
    const lower = query.toLowerCase();
    for (const record of r.stdout.toString('utf8').split('\n')) {
      const [file, line, ...rest] = record.split('\0');
      if (!file || rest.length === 0) continue;
      const content = rest.join('\0');
      if (file === 'profile.md') { hits.push({ id: 'profile', at: '', line: Number(line), excerpt: excerpt(content, query) }); continue; }
      let e;
      try { e = JSON.parse(content); } catch { continue; }
      if (!e?.text || !e.text.toLowerCase().includes(lower)) continue;
      hits.push({ id: e.id, at: e.at, role: e.role, excerpt: excerpt(e.text, query), text: e.text });
    }
    hits.sort((a, b) => (b.at > a.at ? 1 : b.at < a.at ? -1 : 0));
    return { source, hits: hits.slice(0, k), complete: !r.killed && (r.code === 0 || r.code === 1),
      ...(r.killed ? { reason: `grep killed after ${this.grepTimeoutMs} ms; partial` } : r.code > 1 ? { reason: r.stderr.trim().slice(0, 200) } : {}) };
  }

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
    return { source, coverage: this.remote ? 'remote' : this.coverage,
      hits: hits.map(h => ({ ref: h.ref, at: h.at, score: h.score, excerpt: excerpt(h.text ?? '', query), text: h.text })),
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
   *  a crash in either window is describable from the layout alone. */
  async #rollbackSwap(plan, { reason, fromLayout } = {}) {
    this.#journal({ ...plan, phase: 'rolling_back',
      ...(reason ? { reason: String(reason).slice(0, 500) } : {}), ...(fromLayout ? { from_layout: fromLayout } : {}) });
    if (this.faults?.beforeRollback) await this.faults.beforeRollback();
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
    const stream = filterExportStream(exported.stdout, ids);
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
   *  forgotten entry is never searchable again. Returns how many ids it finished. */
  #finishForget(ids) {
    this.#load();
    const both = ids.filter(id => this.entries.has(id) && this.forgotten.has(id));
    if (!both.length) return 0;
    this.#stripLogLines(both);
    for (const id of both) { const e = this.entries.get(id); this.entries.delete(id); this.index.remove(id, e.text); }
    this.log('memory/purge-recovered-forget', { ids: both });
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
