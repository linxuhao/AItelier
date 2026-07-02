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

  let { projectId, canWrite = false }: { projectId: string; canWrite?: boolean } = $props();

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
    if (!window.confirm('Force sync discards local divergence (a backup branch is kept). Continue?')) return;
    run('Force sync', () => repoSync(projectId));
  }

  onMount(load);
</script>

<details class="workspace-section repo-panel" open>
  <summary><strong>Repository</strong>
    {#if status?.branch}<span class="ws-count">{status.branch as string}</span>{/if}
  </summary>

  {#if error}
    <p class="repo-error">{error}</p>
  {:else if !status}
    <p class="repo-muted">Loading repository status…</p>
  {:else if !status.is_git}
    <p class="repo-muted">Not a git repository{status.path ? ' — ' + status.path : ''}.</p>
  {:else}
    <div class="repo-grid">
      <span class="repo-label">Path</span><code>{status.path as string}</code>
      <span class="repo-label">Branch</span>
      <span>
        {status.branch as string}
        {#if status.dirty}<span class="repo-dirty">● {(status.dirty_count as number) || ''} uncommitted</span>{/if}
      </span>
      {#if status.remote_url}
        <span class="repo-label">Remote</span><code>{status.remote_url as string}</code>
      {/if}
      {#if status.upstream}
        <span class="repo-label">Upstream</span>
        <span>{status.upstream as string} · {(status.ahead as number) || 0} ahead, {(status.behind as number) || 0} behind</span>
      {/if}
    </div>

    {#if Array.isArray(status.commits) && (status.commits as unknown[]).length > 0}
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

    <div class="repo-actions">
      <a href={repoArchiveUrl(projectId)} class="repo-btn" download>Download .zip</a>
      {#if canWrite}
        <button class="repo-btn" disabled={!!busy} onclick={doCommit}>Commit</button>
        <button class="repo-btn" disabled={!!busy} onclick={() => run('Push', () => repoPush(projectId))}>Push</button>
        <button class="repo-btn" disabled={!!busy} onclick={() => run('Pull', () => repoPull(projectId))}>Pull</button>
        <button class="repo-btn" disabled={!!busy} onclick={doSync}>Force Sync</button>
        <button class="repo-btn" disabled={!!busy} onclick={() => run('Make PR', () => repoMakePR(projectId))}>Make PR</button>
        <button class="repo-btn" disabled={!!busy} onclick={doSetRemote}>Set Remote</button>
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
    font-size: 0.78rem;
    padding: 0.2rem 0.6rem;
    width: auto;
    margin: 0;
    border: 1px solid var(--pico-muted-border-color, #ccc);
    border-radius: 0.3rem;
    background: var(--pico-card-background-color, #fff);
    color: inherit;
    cursor: pointer;
    text-decoration: none;
    display: inline-block;
  }
  .repo-btn:hover:not(:disabled) {
    background: var(--pico-secondary-focus, rgba(128, 128, 128, 0.08));
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
</style>
