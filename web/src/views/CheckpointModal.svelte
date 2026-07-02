<script lang="ts">
  import { tick } from 'svelte';
  import { onDestroy } from 'svelte';
  import { checkpointStore, hideCheckpoint } from '../stores/checkpoint';
  import { approveCheckpoint, rejectCheckpoint, getCheckpoint } from '../lib/api';
  import { renderMarkdown } from '../lib/markdown';
  import { escapeHtml } from '../lib/format';

  // ── Local state (Svelte 5 runes) ──────────────────────────────────

  let feedback = $state('');
  let feedbackMode = $state(false);
  let submitting = $state(false);
  let errorMsg = $state('');
  let contentLoading = $state(false);
  let loadedContentHtml = $state('');
  let loadedLabel = $state('');

  let staleTimer: ReturnType<typeof setInterval> | null = null;

  // ── Derived ───────────────────────────────────────────────────────

  let storeVal = $derived($checkpointStore);
  let visible = $derived(storeVal.visible);
  let runId = $derived(storeVal.runId);
  let checkpointData = $derived(storeVal.checkpointData);
  let projectId = $derived(runId);

  // ── Lifecycle ─────────────────────────────────────────────────────

  onDestroy(() => {
    stopStalePolling();
  });

  // ── Reactive: auto-load content when visible becomes true ──────────

  let prevVisible = false;

  $effect(() => {
    const v = visible;
    const pid = projectId;

    if (v && pid) {
      // Reset local state for new checkpoint
      feedback = '';
      feedbackMode = false;
      submitting = false;
      errorMsg = '';
      loadedContentHtml = '';
      const data = checkpointData;
      loadedLabel = data?.label ? String(data.label) : '';

      // Start stale polling
      startStalePolling(pid);

      // Load content
      loadContent(pid, data);
    }

    if (!v && prevVisible) {
      stopStalePolling();
      feedback = '';
      feedbackMode = false;
      submitting = false;
      errorMsg = '';
      loadedContentHtml = '';
      loadedLabel = '';
    }

    prevVisible = v;
  });

  // ── Stale polling ─────────────────────────────────────────────────

  function startStalePolling(pid: string): void {
    stopStalePolling();
    staleTimer = setInterval(() => checkStale(pid), 5000);
  }

  function stopStalePolling(): void {
    if (staleTimer !== null) {
      clearInterval(staleTimer);
      staleTimer = null;
    }
  }

  async function checkStale(pid: string): Promise<void> {
    if (submitting) return;
    try {
      const data = await getCheckpoint(pid);
      if (!visible) {
        stopStalePolling();
        return;
      }
      if (!data || !data.checkpoint) {
        stopStalePolling();
        hideCheckpoint();
      }
    } catch (err: unknown) {
      const status = (err as any)?.status;
      if (status === 404) {
        if (visible) {
          stopStalePolling();
          hideCheckpoint();
        }
      }
    }
  }

  // ── Content rendering ─────────────────────────────────────────────

  function renderCheckpointContent(data: Record<string, unknown>): string {
    const stepOutput = (data.step_output || data.stepOutput || {}) as Record<string, unknown>;
    let files: Record<string, unknown> = {};
    let rejectionHistory: unknown[] | null = null;

    if (stepOutput.files && typeof stepOutput.files === 'object' && !Array.isArray(stepOutput.files)) {
      files = stepOutput.files as Record<string, unknown>;
    } else if (typeof stepOutput === 'object' && !Array.isArray(stepOutput)) {
      let hasFileContent = false;
      for (const key of Object.keys(stepOutput)) {
        if (key !== 'rejection_history' && key !== 'files') {
          if (typeof stepOutput[key] === 'string' && (stepOutput[key] as string).length > 10) {
            hasFileContent = true;
            break;
          }
        }
      }
      if (hasFileContent) {
        files = stepOutput as Record<string, unknown>;
      } else {
        files = ((stepOutput.files as Record<string, unknown>) || {}) as Record<string, unknown>;
      }
    }

    if (stepOutput.rejection_history && Array.isArray(stepOutput.rejection_history)) {
      rejectionHistory = stepOutput.rejection_history;
    }

    let html = '';

    // Rejection history banner
    if (rejectionHistory && rejectionHistory.length > 0) {
      const latest = rejectionHistory[rejectionHistory.length - 1] as Record<string, unknown>;
      const lastFeedback = String(latest?.user_feedback || latest?.reason || '');
      html += '<div class="cp-revision-note"><strong>Revised ' + rejectionHistory.length + ' time(s)</strong>';
      if (lastFeedback) {
        html += '<div class="cp-revision-feedback">Last feedback: ' + escapeHtml(lastFeedback) + '</div>';
      }
      html += '</div>';
    }

    // Filter file keys
    const fileKeys = Object.keys(files).filter(fname => {
      if (fname.charAt(0) === '.' || fname === '_snapshot.json' || fname.indexOf('instruction') === 0) {
        return false;
      }
      return true;
    });

    if (fileKeys.length === 0) {
      html += '<p class="empty-state">(No file output to review)</p>';
      return html;
    }

    for (const fname of fileKeys) {
      const fcontent = String(files[fname] || '');
      html += renderFileSection(fname, fcontent);
    }

    return html;
  }

  function renderFileSection(fname: string, fcontent: string): string {
    const MAX_FILE_SIZE = 50 * 1024;
    const isLarge = fcontent.length > MAX_FILE_SIZE;
    let displayContent = fcontent;
    if (isLarge) {
      displayContent = fcontent.slice(0, MAX_FILE_SIZE) + '\n\n[File truncated — showing first 50KB]';
    }

    const isMarkdown = fname.toLowerCase().endsWith('.md');
    let sectionHtml = '';

    sectionHtml += '<div class="cp-file-header">' + escapeHtml(fname) + '</div>';

    if (isMarkdown) {
      try {
        sectionHtml += '<div class="cp-md-content">' + renderMarkdown(displayContent) + '</div>';
      } catch {
        sectionHtml += '<pre class="cp-code-block"><code>' + escapeHtml(displayContent) + '</code></pre>';
      }
    } else {
      sectionHtml += '<pre class="cp-code-block"><code>' + escapeHtml(displayContent) + '</code></pre>';
    }

    if (isLarge) {
      sectionHtml += '<div class="cp-truncated-note">(File truncated — showing first 50KB)</div>';
    }

    return sectionHtml;
  }

  async function loadContent(pid: string, data: Record<string, unknown> | null): Promise<void> {
    const needsFetch = !data || !data.step_output;

    if (needsFetch) {
      contentLoading = true;
      loadedContentHtml = '';
      try {
        const fetched = await getCheckpoint(pid);
        if (!visible) return;
        if (!fetched || !fetched.checkpoint) {
          stopStalePolling();
          hideCheckpoint();
          return;
        }
        if (fetched.label) {
          loadedLabel = String(fetched.label);
        }
        loadedContentHtml = renderCheckpointContent(fetched as Record<string, unknown>);
      } catch (err: unknown) {
        if (!visible) return;
        const status = (err as any)?.status;
        if (status === 404) {
          stopStalePolling();
          hideCheckpoint();
          return;
        }
        loadedContentHtml = '<p class="cp-error-text">Failed to load checkpoint data: ' + escapeHtml((err as Error)?.message || 'Unknown error') + '</p>';
      } finally {
        contentLoading = false;
      }
    } else {
      if (data.label) {
        loadedLabel = String(data.label);
      }
      loadedContentHtml = renderCheckpointContent(data);
    }
  }

  // ── Handlers ──────────────────────────────────────────────────────

  async function handleApprove(): Promise<void> {
    if (submitting || !projectId) return;

    submitting = true;
    errorMsg = '';
    try {
      const data = await approveCheckpoint(projectId, feedback || undefined) as any;
      if (data && data.status === 'already_advanced') {
        stopStalePolling();
        hideCheckpoint();
        return;
      }
      stopStalePolling();
      hideCheckpoint();
    } catch (err: unknown) {
      if ((err as any)?.message?.indexOf('already_advanced') !== -1) {
        stopStalePolling();
        hideCheckpoint();
        return;
      }
      errorMsg = 'Approve failed: ' + ((err as Error)?.message || 'Unknown error');
    } finally {
      submitting = false;
    }
  }

  function handleRejectClick(): void {
    if (submitting || !projectId) return;

    if (!feedbackMode) {
      feedbackMode = true;
      errorMsg = '';
      requestAnimationFrame(() => {
        const ta = document.getElementById('cp-feedback') as HTMLTextAreaElement | null;
        ta?.focus();
      });
      return;
    }

    const trimmedFeedback = feedback.trim();
    if (!trimmedFeedback) {
      errorMsg = 'Feedback is required — describe what needs to change.';
      return;
    }

    submitRejection(trimmedFeedback);
  }

  async function submitRejection(fb: string): Promise<void> {
    if (submitting || !projectId) return;
    submitting = true;
    errorMsg = '';
    try {
      const data = await rejectCheckpoint(projectId, fb) as any;
      if (data && data.status === 'already_advanced') {
        stopStalePolling();
        hideCheckpoint();
        return;
      }
      stopStalePolling();
      hideCheckpoint();
    } catch (err: unknown) {
      if ((err as any)?.message?.indexOf('already_advanced') !== -1) {
        stopStalePolling();
        hideCheckpoint();
        return;
      }
      errorMsg = 'Reject failed: ' + ((err as Error)?.message || 'Unknown error');
    } finally {
      submitting = false;
    }
  }

  function cancelFeedback(): void {
    feedbackMode = false;
    feedback = '';
    errorMsg = '';
  }

  function handleDismiss(): void {
    if (feedbackMode) {
      cancelFeedback();
      return;
    }
    hideCheckpoint();
  }

  function handleKeydown(e: KeyboardEvent): void {
    if (e.key === 'Escape') {
      e.preventDefault();
      handleDismiss();
    }
  }

  function handleDialogClose(): void {
    stopStalePolling();
    if (visible) {
      hideCheckpoint();
    }
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<!-- `open` is one-way: `visible` is $derived from the store (not bindable),
     and Svelte only allows bind:open on <details> anyway. Dialog-initiated
     closes (Escape) sync back through on:close → handleDialogClose. -->
<dialog
  id="checkpoint-modal"
  open={visible}
  onclose={handleDialogClose}
>
  <article>
    <header>
      <h2 id="cp-label">{loadedLabel || 'Checkpoint'}{projectId ? ': ' + projectId : ''}</h2>
      <button class="close outline" onclick={handleDismiss} aria-label="Close">&times;</button>
    </header>

    <div id="cp-content" class="cp-content">
      {#if contentLoading}
        <p class="cp-loading">Loading checkpoint data…</p>
      {:else if loadedContentHtml}
        {@html loadedContentHtml}
      {:else}
        <p>Checkpoint reached for run <code>{projectId}</code>.</p>
      {/if}
    </div>

    <footer>
      {#if errorMsg}
        <div class="cp-error">{errorMsg}</div>
      {/if}

      {#if feedbackMode}
        <textarea
          id="cp-feedback"
          bind:value={feedback}
          placeholder="Describe what needs to change…"
          disabled={submitting}
        ></textarea>
        <div class="cp-feedback-buttons">
          <button class="outline" onclick={cancelFeedback} disabled={submitting}>
            Cancel
          </button>
          <button class="contrast" onclick={handleRejectClick} disabled={submitting}>
            {submitting ? 'Submitting…' : 'Submit Rejection'}
          </button>
        </div>
      {:else}
        <button
          id="cp-approve"
          class="contrast"
          onclick={handleApprove}
          disabled={submitting}
        >
          {submitting ? 'Approving…' : 'Approve'}
        </button>
        <button
          id="cp-reject"
          class="outline"
          onclick={handleRejectClick}
          disabled={submitting}
        >
          Request Changes
        </button>
      {/if}
    </footer>
  </article>
</dialog>

<style>
  :global(dialog#checkpoint-modal) {
    width: min(90vw, 720px);
    max-height: 80vh;
  }

  :global(dialog#checkpoint-modal article) {
    max-height: 75vh;
    display: flex;
    flex-direction: column;
  }

  :global(dialog#checkpoint-modal article > header) {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-shrink: 0;
  }

  :global(dialog#checkpoint-modal article > header button.close) {
    font-size: 1.5rem;
    line-height: 1;
    border: none;
    background: none;
    cursor: pointer;
    padding: 0.25rem 0.5rem;
  }

  :global(.cp-content) {
    flex: 1;
    overflow-y: auto;
    padding: 0.5rem 0;
    min-height: 100px;
  }

  :global(.cp-loading) {
    color: var(--muted-color, #888);
    font-style: italic;
  }

  :global(.cp-error) {
    width: 100%;
    color: var(--del-color, #d04040);
    font-size: 0.85rem;
    margin-top: 0.5rem;
    padding: 0.5rem;
    background-color: rgba(208, 64, 64, 0.08);
    border-radius: 0.25rem;
  }

  :global(.cp-error-text) {
    color: var(--del-color, #d04040);
  }

  :global(.cp-revision-note) {
    margin-bottom: 1rem;
    padding: 0.5rem;
    border-left: 3px solid #d49b1a;
    background-color: rgba(212, 155, 26, 0.08);
    border-radius: 0.25rem;
    font-size: 0.85rem;
    line-height: 1.5;
  }

  :global(.cp-revision-feedback) {
    margin-top: 0.25rem;
    color: var(--muted-color, #888);
  }

  :global(.cp-file-header) {
    margin-top: 1rem;
    margin-bottom: 0.25rem;
    font-size: 0.85rem;
    font-weight: 600;
    color: var(--primary, #0066cc);
  }

  :global(.cp-md-content) {
    line-height: 1.6;
    font-size: 0.9rem;
  }

  :global(.cp-code-block) {
    margin: 0;
    padding: 0.75rem;
    background-color: var(--code-background-color, #f5f5f5);
    border-radius: 0.4rem;
    overflow-x: auto;
    font-size: 0.85rem;
    line-height: 1.5;
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 400px;
    overflow-y: auto;
  }

  :global(.cp-code-block code) {
    font-family: 'SF Mono', 'Consolas', 'Liberation Mono', monospace;
  }

  :global(.cp-truncated-note) {
    font-size: 0.8rem;
    font-style: italic;
    color: var(--muted-color, #888);
    margin-top: 0.25rem;
  }

  textarea#cp-feedback {
    width: 100%;
    min-height: 80px;
    margin-bottom: 0.5rem;
  }

  .cp-feedback-buttons {
    display: flex;
    gap: 0.5rem;
    justify-content: flex-end;
  }

  :global(dialog#checkpoint-modal article > footer) {
    flex-shrink: 0;
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    align-items: flex-start;
  }
</style>
