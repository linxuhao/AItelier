<script lang="ts">
  import { stateIssues, stateIssue } from '../lib/api';
  import type { StateIssue, StateIssueSummary } from '../lib/stateGraph';
  import { st } from '../lib/stateI18n.svelte';
  // Read-only: issues are triaged through the State tools, never from this view.
  interface Props { projectId: string; refresh?: number; onselect: (key: string) => void }
  const { projectId, refresh = 0, onselect }: Props = $props();
  let openOnly = $state(true), issues = $state<StateIssueSummary[]>([]), counts = $state<Record<string, number>>({});
  let next = $state(0), hasMore = $state(false), loading = $state(false), error = $state('');
  let expanded = $state(''), detail = $state<StateIssue | null>(null), detailError = $state('');
  let generation = 0;
  $effect(() => {
    const project = projectId, open = openOnly; void refresh;
    const version = ++generation; loading = true; error = '';
    stateIssues(project, open).then(r => {
      if (version !== generation) return;
      issues = r.issues; counts = r.status_counts; next = r.next_after; hasMore = r.has_more;
    }).catch(e => { if (version === generation) error = String(e.message ?? e); })
      .finally(() => { if (version === generation) loading = false; });
  });
  async function more() {
    const version = generation; loading = true;
    try { const r = await stateIssues(projectId, openOnly, next); if (version === generation) { issues = [...issues, ...r.issues]; next = r.next_after; hasMore = r.has_more; } }
    catch (e) { if (version === generation) error = String(e instanceof Error ? e.message : e); }
    finally { if (version === generation) loading = false; }
  }
  async function toggle(id: string) {
    if (expanded === id) { expanded = ''; detail = null; return; }
    expanded = id; detail = null; detailError = '';
    try { const d = await stateIssue(projectId, id); if (expanded === id) detail = d; }
    catch (e) { if (expanded === id) detailError = String(e instanceof Error ? e.message : e); }
  }
</script>

<section class="issues">
  <p class="note">{st('issuesNote')}</p>
  <div class="filters">
    <button class:active={openOnly} aria-pressed={openOnly} onclick={() => openOnly = true}>{st('issuesOpenOnly')} · {counts.open ?? 0}</button>
    <button class:active={!openOnly} aria-pressed={!openOnly} onclick={() => openOnly = false}>{st('issuesAll')} · {Object.values(counts).reduce((a, b) => a + b, 0)}</button>
  </div>
  {#if error}<p role="alert">{error}</p>{/if}
  {#if loading && !issues.length}<p aria-live="polite">{st('loading')}</p>{/if}
  {#if !loading && !error && !issues.length}<p class="muted">{st('noIssues')}</p>{/if}
  {#each issues as issue (issue.issue_id)}
    <article class="issue" class:contradicts={issue.contradicts_acceptance.length > 0}>
      <div class="row">
        <div>
          <p class="title"><span class="kind {issue.kind}">{st('kind_' + issue.kind)}</span> <strong>{issue.title}</strong></p>
          <small>{issue.status} · v{issue.version} · {issue.created_at} · <code>{issue.issue_id}</code></small>
          {#if issue.contradicts_acceptance.length}<p class="warn" role="note">⚠ {st('contradicts')}: {issue.contradicts_acceptance.join(', ')}</p>{/if}
          {#if issue.nodes.length}<div class="nodes">{#each issue.nodes as n (n.node_key)}
            <button class="outline" type="button" onclick={() => onselect(n.node_key)}>{n.node_key} · {n.status ?? '?'}</button>
          {/each}</div>{/if}
        </div>
        <button class="outline" onclick={() => toggle(issue.issue_id)}>{expanded === issue.issue_id ? st('hideIssue') : st('showIssue')}</button>
      </div>
      {#if expanded === issue.issue_id}
        {#if detailError}<p role="alert">{detailError}</p>
        {:else if !detail}<p aria-live="polite">{st('loading')}</p>
        {:else}
          <p class="body">{detail.body}</p>
          <small>{st('reportedBy')}: {detail.reported_by.director_identity} ({detail.reported_by.actor}){#if detail.source} · {st('issueSource')}: <code>{detail.source}</code>{/if}</small>
          {#if detail.resolution}
            <p class="resolution"><strong>{st('resolution')}: {detail.resolution.resolution}</strong>
              {#if detail.resolution.node_key} → <button class="link" type="button" onclick={() => onselect(detail!.resolution!.node_key!)}>{detail.resolution.node_key}{detail.resolution.node_revision ? ' r' + detail.resolution.node_revision : ''}</button>{/if}
              {#if detail.resolution.duplicate_of} → <code>{detail.resolution.duplicate_of}</code>{/if}
              <br/>{detail.resolution.reason}</p>
          {/if}
        {/if}
      {/if}
    </article>
  {/each}
  {#if hasMore}<button class="outline" disabled={loading} onclick={more}>{st('more')}</button>{/if}
</section>

<style>
  .issues { font-size:.82rem; }
  .note,.muted { font-size:.72rem; color:var(--pico-muted-color,#667085); }
  .filters { display:flex; gap:.4rem; margin:.5rem 0; }
  button { width:auto; margin:0; padding:.3rem .6rem; font-size:.75rem; }
  .filters button { background:transparent; color:var(--pico-color,#334155); border:1px solid var(--pico-muted-border-color,#ddd); }
  .filters .active { border-color:var(--pico-primary,#0066cc); color:var(--pico-primary,#0066cc); }
  .issue { padding:.75rem .9rem; margin:.6rem 0; overflow-wrap:anywhere; }
  .issue.contradicts { border-left:4px solid #bd433c; }
  .row { display:flex; justify-content:space-between; gap:1rem; align-items:flex-start; }
  .title { margin:0 0 .25rem; }
  .kind { font-size:.66rem; border:1px solid var(--pico-muted-border-color,#ddd); border-radius:4px; padding:.05rem .3rem; }
  .kind.defect { border-color:#bd433c; color:#bd433c; }
  .warn { color:#bd433c; font-size:.75rem; margin:.3rem 0; }
  .nodes { display:flex; flex-wrap:wrap; gap:.35rem; margin-top:.35rem; }
  .body { white-space:pre-wrap; margin:.6rem 0; }
  .resolution { margin:.5rem 0; }
  .link { background:none; border:0; padding:0; color:var(--pico-primary,#0066cc); }
  code { font-size:.7rem; white-space:normal; }
  @media(max-width:600px) { .row { flex-direction:column; } }
</style>
