<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { push } from 'svelte-spa-router';
  import { listRepos } from '../lib/api';
  import type { RepoItem } from '../lib/api';
  import { formatTime } from '../lib/format';
  import { t } from '../lib/i18n.svelte';

  // ── State ──

  let repos = $state<RepoItem[]>([]);
  let loading = $state(true);
  let error = $state<string | null>(null);
  let pollTimer = $state<ReturnType<typeof setInterval> | null>(null);

  // ── Derived ──

  let empty = $derived(!loading && !error && repos.length === 0);

  // ── Lifecycle ──

  onMount(async () => {
    await fetchRepos();
    pollTimer = setInterval(fetchRepos, 10000);
  });

  onDestroy(() => {
    if (pollTimer !== null) {
      clearInterval(pollTimer);
    }
  });

  // ── Methods ──

  async function fetchRepos(): Promise<void> {
    loading = true;
    error = null;
    try {
      const data = await listRepos();
      repos = data;
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to load repositories';
      error = msg;
    } finally {
      loading = false;
    }
  }

  function navigateToRepo(repoPath: string): void {
    push('#/repos/' + encodeURIComponent(repoPath));
  }

  function repoTypeLabel(repoType: string): string {
    switch (repoType) {
      case 'new': return 'new';
      case 'existing': return 'existing';
      case 'clone': return 'clone';
      default: return repoType;
    }
  }
</script>

<section id="view-repos">
  <!-- Breadcrumb -->
  <nav aria-label="breadcrumb">
    <ul>
      <li><a href="#/projects">{t('appbar.dashboard')}</a></li>
      <li>{t('repos.title')}</li>
    </ul>
  </nav>

  <!-- Page header -->
  <header class="repos-header">
    <h2>{t('repos.title')}</h2>
  </header>

  <!-- Loading state -->
  {#if loading}
    <article aria-busy="true">
      <p>{t('repos.loading')}</p>
    </article>
  {:else if error}
    <!-- Error state -->
    <article class="error-state">
      <header>
        <h3>{t('repos.failedToLoad')}</h3>
      </header>
      <p>{error}</p>
      <button onclick={fetchRepos}>{t('repos.retry')}</button>
    </article>
  {:else if empty}
    <!-- Empty state -->
    <article class="empty-state">
      <p>{t('repos.noRepos')}</p>
      <p><a href="#/projects">{t('repos.backToDashboard')}</a></p>
    </article>
  {:else}
    <!-- Repo list table -->
    <figure>
      <table class="repo-table">
        <thead>
          <tr>
            <th>{t('repo.pageTitle')}</th>
            <th>{t('repo.labelPath')}</th>
            <th>{t('repo.labelType')}</th>
            <th>{t('repo.labelProjectCount')}</th>
            <th>{t('dashboard.colLastUpdate')}</th>
          </tr>
        </thead>
        <tbody>
          {#each repos as repo}
            <tr class="repo-row" onclick={() => navigateToRepo(repo.repo_path)}>
              <td>
                <a href="#/repos/{encodeURIComponent(repo.repo_path)}"
                   onclick={(e) => { e.preventDefault(); navigateToRepo(repo.repo_path); }}>
                  {repo.repo_name}
                </a>
              </td>
              <td>
                <code>{repo.repo_path}</code>
              </td>
              <td>
                <kbd>{repoTypeLabel(repo.repo_type)}</kbd>
              </td>
              <td>
                <mark>{t('repos.count').replace('{n}', String(repo.project_count))}</mark>
              </td>
              <td>
                <span class="timestamp">{formatTime(repo.last_activity)}</span>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </figure>
  {/if}
</section>

<style>
  .repos-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: var(--pico-spacing, 1rem);
  }

  .repos-header h2 {
    margin: 0;
  }

  /* ── Repo table ── */
  .repo-row {
    cursor: pointer;
    transition: background 0.15s;
  }

  .repo-row:hover {
    background: var(--pico-table-row-hover-background, rgba(128, 128, 128, 0.05));
  }

  .repo-row a {
    font-weight: 600;
  }

  .repo-row code {
    font-size: 0.825rem;
    color: var(--pico-muted-color, #888);
  }

  .repo-row kbd {
    font-size: 0.75rem;
    padding: 0.1rem 0.4rem;
  }

  .repo-row mark {
    font-size: 0.75rem;
    padding: 0.1rem 0.4rem;
    background: var(--pico-primary-background, #1095c1);
    color: var(--pico-primary-inverse, #fff);
    border-radius: var(--pico-border-radius, 4px);
  }

  /* ── Timestamp ── */
  .timestamp {
    font-size: 0.875rem;
    color: var(--pico-muted-color, #888);
  }

  /* ── Empty / error states ── */
  .empty-state,
  .error-state {
    text-align: center;
    padding: 2rem 1rem;
  }
</style>
