<script lang="ts">
  // Repository status panel — git snapshot of the project code repo with
  // writer actions. Ported from the vanilla _renderRepoStatusHtml section.
  import { onMount } from 'svelte';
  import {
    repoStatus,
    repoArchiveUrl,
    repoCommit,
    repoPush,
    repoPull,
    repoSync,
    repoSetRemote,
    repoMakePR,
  } from '../lib/api';
  import { formatTime } from '../lib/format';
  import { t } from '../lib/i18n.svelte';

  let { projectId, canWrite = false, compact = false }: { projectId: string; canWrite?: boolean; compact?: boolean } = $props();

  let status = $state<Record<string, unknown> | null>(null);
  let error = $state<string | null>(null);
  let actionMsg = $state<string | null>(null);
  let busy = $state<string | null>(null);

  async function load(): Promise<void> {
    if (!projectId) return;
    try {
      status = await repoStatus(projectId);
      error = null;
    } catch (err: unknown) {
      error = err instanceof Error ? err.message : 'Failed to load repo status.';
    }
  }

  async function run(name: string, fn: () => Promise<Record<string, unknown>>): Promise<void> {
    if (busy) return;
    busy = name;
    actionMsg = null;
    try {
      const res = await fn();
      actionMsg = (res && ((res.message as string) || (res.detail as string))) || name + ' done.';
      await load();
    } catch (err: unknown) {
      actionMsg = name + ' failed: ' + (err instanceof Error ? err.message : String(err));
    } finally {
      busy = null;
    }
  }

  function doCommit(): void {
    const msg = window.prompt('Commit message:');
    if (!msg) return;
    run('Commit', () => repoCommit(projectId, msg));
  }

  function doSetRemote(): void {
    const url = window.prompt('Remote URL (github https):', (status?.remote_url as string) || '');
    if (!url) return;
    run('Set remote', () => repoSetRemote(projectId, url));
  }

  function doSync(): void {
    const branch = window.prompt(
      'Force-sync: fetch and HARD RESET the working tree to origin/<branch>.\n' +
      'Local commits are discarded (a backup branch is created first).\n\n' +
      'Branch to sync from:', (status?.branch as string) || '');
    if (!branch) return;
    if (!window.confirm('This DISCARDS uncommitted changes and local commits, resetting to origin/' + branch + '. Continue?')) return;
    run('Force sync', () => repoSync(projectId, branch));
  }

  function doMakePR(): void {
    // Push the current work to a user-named feature branch, then PR it into
    // the base branch. Avoids the "PR head == base == main" dead end.
    const head = window.prompt('Branch name to push your current work to (the PR\'s source branch):');
    if (!head) return;
    const base = window.prompt('Base branch (merge into):', 'main');
    if (!base) return;
    if (head === base) {
      actionMsg = 'The feature branch and the base branch must differ.';
      return;
    }
    const title = window.prompt('Pull request title:');
    if (!title) return;
    const body = window.prompt('PR description (optional):') || '';
    run('Make PR', () => repoMakePR(projectId, { title, body, base, head }).then((res) => {
      if (res && res.url) window.open(res.url as string, '_blank', 'noopener');
      return res;
    }));
  }

  onMount(load);
</script>

<details class="workspace-section repo-panel" class:repo-panel--compact={compact} open>
  {#if !compact}
    <summary><strong>{t('repo.title')}</strong>
      {#if status?.branch}<span class="ws-count">{status.branch as string}</span>{/if}
    </summary>
  {/if}

  {#if error}
    <p class="repo-error">{error}</p>
  {:else if !status}
    <p class="repo-muted">{t('repo.loading')}</p>
  {:else if !status.is_git}
    <p class="repo-muted">{t('repo.notGit')}{status.path ? ' — ' + status.path : ''}.</p>
  {:else}
    {#if compact}
      <div class="repo-compact-row">
        <span class="repo-label">{t('repo.branch')}</span>
        <span>
          {status.branch as string}
          {#if status.dirty}<span class="repo-dirty">● {(status.dirty_count as number) || ''} {t('repo.uncommitted')}</span>{/if}
        </span>
      </div>
    {:else}
      <div class="repo-grid">
        <span class="repo-label">{t('repo.path')}</span><code>{status.path as string}</code>
        <span class="repo-label">{t('repo.branch')}</span>
        <span>
          {status.branch as string}
          {#if status.dirty}<span class="repo-dirty">● {(status.dirty_count as number) || ''} {t('repo.uncommitted')}</span>{/if}
        </span>
        {#if status.remote_url}
          <span class="repo-label">{t('repo.remote')}</span><code>{status.remote_url as string}</code>
        {/if}
        {#if status.upstream}
          <span class="repo-label">{t('repo.upstream')}</span>
          <span>{status.upstream as string} · {(status.ahead as number) || 0} {t('repo.ahead')}, {(status.behind as number) || 0} {t('repo.behind')}</span>
        {/if}
      </div>
    {/if}

    {#if !compact && Array.isArray(status.commits) && (status.commits as unknown[]).length > 0}
      <ul class="repo-commits">
        {#each (status.commits as Record<string, unknown>[]) as c (c.hash as string)}
          <li>
            <code>{c.hash as string}</code>
            <span class="repo-subject" title={c.subject as string}>{c.subject as string}</span>
            <span class="repo-when">{formatTime(c.date as string)}</span>
          </li>
        {/each}
      </ul>
    {/if}

    <div class="repo-actions" class:repo-actions--compact={compact}>
      {#if !compact}
        <a href={repoArchiveUrl(projectId)} class="repo-btn" download>{t('repo.downloadZip')}</a>
      {/if}
      {#if canWrite}
        <button class="repo-btn repo-btn-green" disabled={!!busy} onclick={doCommit}>{t('repo.commit')}</button>
        <button class="repo-btn" disabled={!!busy} onclick={() => run('Push', () => repoPush(projectId))}>{t('repo.push')}</button>
        <button class="repo-btn" disabled={!!busy} onclick={() => run('Pull', () => repoPull(projectId))}>{t('repo.pull')}</button>
        {#if !compact}
          <button class="repo-btn repo-btn-red" disabled={!!busy} onclick={doSync}>{t('repo.forceSync')}</button>
          <button class="repo-btn repo-btn-purple" disabled={!!busy} onclick={doMakePR}>{t('repo.makePr')}</button>
          <button class="repo-btn repo-btn-amber" disabled={!!busy} onclick={doSetRemote}>{t('repo.setRemote')}</button>
        {/if}
      {/if}
    </div>
    {#if busy}
      <p class="repo-muted">{busy}…</p>
    {:else if actionMsg}
      <p class="repo-muted">{actionMsg}</p>
    {/if}
  {/if}
</details>

<style>
  .workspace-section {
    margin-top: 1rem;
    border: 1px solid var(--pico-muted-border-color, #e0e0e0);
    border-radius: 0.4rem;
    padding: 0.5rem 0.75rem;
    background: var(--pico-card-background-color, #fff);
  }
  .workspace-section summary {
    cursor: pointer;
  }
  .ws-count {
    margin-left: 0.5rem;
    font-size: 0.8rem;
    color: var(--pico-muted-color, #888);
    font-family: monospace;
  }
  .repo-grid {
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: 0.15rem 0.75rem;
    margin: 0.5rem 0;
    font-size: 0.85rem;
    align-items: baseline;
  }
  .repo-grid code {
    font-size: 0.78rem;
    word-break: break-all;
  }
  .repo-label {
    color: var(--pico-muted-color, #888);
    font-size: 0.75rem;
    text-transform: uppercase;
  }
  .repo-dirty {
    color: #b8860b;
    margin-left: 0.5rem;
    font-size: 0.8rem;
  }
  .repo-commits {
    list-style: none;
    margin: 0.25rem 0 0.5rem;
    padding: 0;
    font-size: 0.8rem;
  }
  .repo-commits li {
    display: flex;
    gap: 0.5rem;
    align-items: baseline;
    padding: 0.1rem 0;
    border-bottom: 1px dashed var(--pico-muted-border-color, #eee);
  }
  .repo-commits code {
    font-size: 0.75rem;
    flex-shrink: 0;
  }
  .repo-subject {
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .repo-when {
    color: var(--pico-muted-color, #888);
    flex-shrink: 0;
    font-size: 0.75rem;
  }
  .repo-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
    margin-top: 0.5rem;
    padding-top: 0.5rem;
    border-top: 1px solid var(--pico-muted-border-color, #eee);
  }
  .repo-btn {
    /* Colored outline buttons (the vanilla UI used Pico's `outline` class);
       per-action accents via --repo-btn-color. */
    --repo-btn-color: var(--pico-primary, #0172ad);
    font-size: 0.78rem;
    padding: 0.2rem 0.6rem;
    width: auto;
    margin: 0;
    border: 1px solid var(--repo-btn-color);
    border-radius: 0.3rem;
    background: transparent;
    color: var(--repo-btn-color);
    cursor: pointer;
    text-decoration: none;
    display: inline-block;
    font-weight: 600;
  }
  .repo-btn:hover:not(:disabled) {
    background: var(--repo-btn-color);
    color: #fff;
  }
  .repo-btn:disabled {
    opacity: 0.5;
    cursor: default;
  }
  .repo-btn-green {
    --repo-btn-color: #2e7d32;
  }
  .repo-btn-red {
    --repo-btn-color: #c62828;
  }
  .repo-btn-purple {
    --repo-btn-color: #6a1b9a;
  }
  .repo-btn-amber {
    --repo-btn-color: #b8860b;
  }
  .repo-muted {
    color: var(--pico-muted-color, #888);
    font-size: 0.8rem;
    margin: 0.4rem 0 0;
  }
  .repo-error {
    color: #b00;
    font-size: 0.85rem;
    margin: 0.5rem 0 0;
  }
  .repo-panel--compact {
    padding: 0.3rem 0.5rem;
    font-size: 0.78rem;
    margin-top: 0;
  }
  .repo-panel--compact summary {
    display: none;
  }
  .repo-compact-row {
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
    margin: 0.15rem 0;
    font-size: 0.85rem;
  }
  .repo-panel--compact .repo-commits {
    display: none;
  }
  .repo-panel--compact .repo-actions {
    margin-top: 0.2rem;
    padding-top: 0.2rem;
    border-top: none;
    gap: 0.25rem;
  }
</style>
