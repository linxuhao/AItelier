<script lang="ts">
  import { push } from 'svelte-spa-router';
  import { getRepo, ApiError } from '../lib/api';
  import type { RepoDetail } from '../lib/api';
  import { formatTime, parseStatus, repoTypeLabel, formatTokens, cacheBadgeClass, formatTaskProgress } from '../lib/format';
  import { t } from '../lib/i18n.svelte';
  import { authStore } from '../stores/auth';
  import { setCurrentRepoPath } from '../stores/project';
  import RepoPanel from './RepoPanel.svelte';
  import WorkspaceBrowser from './WorkspaceBrowser.svelte';

  // ── Props (route params from svelte-spa-router) ──

  let { params = {} as Record<string, string> } = $props();
  let repoPath = $derived(decodeURIComponent(params.repoPath || ''));

  // ── State ──

  let repo = $state<RepoDetail | null>(null);
  let loading = $state(true);
  let error = $state<string | null>(null);
  let notFound = $state(false);

  // ── Derived ──

  let representativeProjectId = $derived(repo?.representative_project_id as string | undefined);
  let canWrite = $derived($authStore.permissionResolved && $authStore.canWrite);

  // ── Lifecycle ──

  $effect(() => {
    void params.repoPath;
    fetchRepo();
    const timer = setInterval(fetchRepo, 10000);
    return () => clearInterval(timer);
  });

  // ── Methods ──

  async function fetchRepo(): Promise<void> {
    if (!repoPath) {
      notFound = true;
      loading = false;
      return;
    }
    // Only show full loading on initial fetch; silent refresh otherwise.
    if (!repo) loading = true;
    error = null;
    notFound = false;
    try {
      const data = await getRepo(repoPath);
      repo = data;
    } catch (err: unknown) {
      if (err instanceof ApiError && err.status === 404) {
        notFound = true;
      } else {
        const msg = err instanceof Error ? err.message : 'Failed to load repository';
        error = msg;
      }
    } finally {
      loading = false;
    }
  }

  function navigateToProject(projectId: string): void {
    setCurrentRepoPath(repoPath);
    push('#/projects/' + encodeURIComponent(projectId));
  }
</script>

<section id="view-repo">
  <!-- Breadcrumb -->
  <nav aria-label="breadcrumb">
    <ul>
      <li><a href="#/projects">{t('appbar.dashboard')}</a></li>
      <li><a href="#/repos">{t('repos.title')}</a></li>
      {#if repo}
        <li>{repo.repo_name}</li>
      {:else}
        <li>{t('repo.pageTitle')}</li>
      {/if}
    </ul>
  </nav>

  <!-- Loading state (only on initial load, not re-fetches) -->
  {#if loading && !repo}
    <article aria-busy="true">
      <p>{t('repo.pageLoading')}</p>
    </article>
  {:else if notFound}
    <!-- 404 state -->
    <article class="error-state">
      <h3>{t('repo.notFound')}</h3>
      <p><a href="#/repos">{t('repo.backToRepos')}</a></p>
    </article>
  {:else if error}
    <!-- Error state -->
    <article class="error-state">
      <header>
        <h3>{t('repo.failedToLoad')}</h3>
      </header>
      <p>{error}</p>
      <button onclick={fetchRepo}>{t('repos.retry')}</button>
      <p><a href="#/repos">{t('repo.backToRepos')}</a></p>
    </article>
  {:else if repo}
    <!-- Repo metadata header -->
    <header class="repo-header">
      <h2>{repo.repo_name}</h2>
    </header>

    <figure>
      <table class="repo-metadata">
        <tbody>
          <tr>
            <th>{t('repo.labelPath')}</th>
            <td><code>{repo.repo_path}</code></td>
          </tr>
          <tr>
            <th>{t('repo.labelType')}</th>
            <td><kbd>{repoTypeLabel(repo.repo_type)}</kbd></td>
          </tr>
          <tr>
            <th>{t('repo.labelUrl')}</th>
            <td class="url-cell">{repo.repo_url || '\u2014'}</td>
          </tr>
          <tr>
            <th>{t('repo.labelProjectCount')}</th>
            <td><mark>{repo.project_count}</mark></td>
          </tr>
        </tbody>
      </table>
    </figure>

    <!-- Project list -->
    <section class="repo-projects">
      <h3>{t('repo.projectsInRepo')}</h3>
      {#if repo.projects && repo.projects.length > 0}
        <figure>
          <table>
            <thead>
              <tr>
                <th>{t('dashboard.colProject')}</th>
                <th>{t('dashboard.colStatus')}</th>
                <th>{t('dashboard.colTasks')}</th>
                <th>{t('dashboard.colLastUpdate')}</th>
              </tr>
            </thead>
            <tbody>
              {#each repo.projects as project}
                {@const parsed = parseStatus(project.status)}
                <tr
                  class="project-row"
                  onclick={() => navigateToProject(project.project_id)}
                >
                  <td>
                    <a
                      href="#/projects/{encodeURIComponent(project.project_id)}"
                      onclick={(e) => { e.preventDefault(); navigateToProject(project.project_id); }}
                    >
                      {project.name}
                    </a>
                  </td>
                  <td>
                    <span class="status-badge {parsed.className}" title={parsed.text}>
                      {parsed.icon} {parsed.text}
                    </span>
                    {#if project.cache_stats && (project.cache_stats as Record<string, number>).hit_ratio != null}
                      {@const cs = project.cache_stats as Record<string, number>}
                      <span
                        class="cache-inline-badge {cacheBadgeClass(cs.hit_ratio)}"
                        title={t('chat.cacheHitRatio')}
                      >
                        Cache {(cs.hit_ratio * 100).toFixed(1)}%{cs.total_tokens != null ? ' · ' + formatTokens(cs.total_tokens) : ''}
                      </span>
                    {/if}
                  </td>
                  <td>
                    <span class="task-progress">{formatTaskProgress(project)}</span>
                  </td>
                  <td>
                    <span class="timestamp">{formatTime(project.updated_at)}</span>
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </figure>
      {:else}
        <p><small>No projects in this repository.</small></p>
      {/if}
    </section>

    <!-- RepoPanel (git operations) — guarded by representative project ID -->
    {#if representativeProjectId}
      <section class="repo-panel-section">
        <RepoPanel projectId={representativeProjectId} {canWrite} />
      </section>

      <!-- WorkspaceBrowser root="code" — guarded by representative project ID -->
      <section class="workspace-section">
        <WorkspaceBrowser
          projectId={representativeProjectId}
          root="code"
          title="Repository Code"
        />
      </section>
    {:else}
      <!-- No representative project available -->
      <article class="error-state">
        <p>{t('repo.notFound')}</p>
        <p><a href="#/repos">{t('repo.backToRepos')}</a></p>
      </article>
    {/if}
  {:else}
    <!-- Fallback — should not normally be reached -->
    <article aria-busy="true">
      <p>{t('repo.pageLoading')}</p>
    </article>
  {/if}
</section>

<style>
  .repo-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: var(--pico-spacing, 1rem);
  }

  .repo-header h2 {
    margin: 0;
  }

  /* ── Repo metadata table ── */
  .repo-metadata {
    width: auto;
    min-width: 40%;
  }

  .repo-metadata th {
    white-space: nowrap;
    padding-right: 1.5rem;
    font-weight: 600;
    color: var(--pico-muted-color, #888);
  }

  .repo-metadata code {
    font-size: 0.875rem;
  }

  .repo-metadata kbd {
    font-size: 0.75rem;
    padding: 0.1rem 0.4rem;
  }

  .repo-metadata mark {
    font-size: 0.75rem;
    padding: 0.1rem 0.4rem;
    background: var(--pico-primary-background, #1095c1);
    color: var(--pico-primary-inverse, #fff);
    border-radius: var(--pico-border-radius, 4px);
  }

  .url-cell {
    font-size: 0.875rem;
    word-break: break-all;
    color: var(--pico-muted-color, #888);
  }

  /* ── Project table ── */
  .repo-projects {
    margin-top: var(--pico-spacing, 1rem);
  }

  .repo-projects h3 {
    margin-bottom: var(--pico-spacing, 0.5rem);
  }

  .project-row {
    cursor: pointer;
    transition: background 0.15s;
  }

  .project-row:hover {
    background: var(--pico-table-row-hover-background, rgba(128, 128, 128, 0.05));
  }

  .project-row a {
    font-weight: 600;
  }

  .status-badge {
    font-size: 0.875rem;
    color: var(--pico-muted-color, #888);
  }

  .timestamp {
    font-size: 0.875rem;
    color: var(--pico-muted-color, #888);
  }

  /* ── Sections ── */
  .repo-panel-section,
  .workspace-section {
    margin-top: calc(var(--pico-spacing, 1rem) * 1.5);
  }

  /* ── Error state ── */
  .error-state {
    text-align: center;
    padding: 2rem 1rem;
  }
</style>
