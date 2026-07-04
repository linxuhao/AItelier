<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import Router from 'svelte-spa-router';

  import AppBar from './views/AppBar.svelte';
  import Dashboard from './views/Dashboard.svelte';
  import Project from './views/Project.svelte';
  import Chat from './views/Chat.svelte';
  import Trace from './views/Trace.svelte';
  import Tracking from './views/Tracking.svelte';
  import Repositories from './views/Repositories.svelte';
  import Repository from './views/Repository.svelte';
  import NotificationPanel from './views/NotificationPanel.svelte';
  import CheckpointModal from './views/CheckpointModal.svelte';
  import ConfirmDialog from './views/ConfirmDialog.svelte';

  import { authStore } from './stores/auth';
  import { connectionStore } from './stores/connection';
  import { projectStore } from './stores/project';
  import { notificationStore } from './stores/notifications';
  import { checkpointStore, showCheckpoint } from './stores/checkpoint';

  import { whoami, getCheckpoint, getUserLang, setUserLang } from './lib/api';
  import { connect, on, off } from './lib/sse';
  import { syncInitialLang } from './stores/i18n';

  const routes = {
    '/': Dashboard,
    '/projects': Dashboard,
    '/chat': Chat, // the butler is standalone — the ONLY chat entry
    '/projects/:id': Project,
    // Project-level trace: no runId -> Trace targets the project id (the
    // backend's _resolve_run accepts project ids as run identifiers).
    '/projects/:id/trace': Trace,
    '/projects/:id/trace/:runId': Trace,
    '/tracking': Tracking,
    '/repos': Repositories,
    '/repos/:repoPath': Repository,
  };

  // SSE handler for checkpoint_reached: auto-open the CheckpointModal.
  // Skip "gather" (meta-conversation checkpoint handled in-chat by the butler).
  function _onCheckpointReached(event: Record<string, unknown>): void {
    const stepId = (event.step_id as string) || '';
    if (stepId === 'gather') return;
    const pid = (event.project_id as string) || '';
    if (!pid) return;
    // Fetch full checkpoint data, then show the modal
    getCheckpoint(pid).then((data) => {
      if (data && (data as Record<string, unknown>).checkpoint) {
        showCheckpoint(pid, data as Record<string, unknown>);
      }
    }).catch(() => { /* stale checkpoint, ignore */ });
  }

  onMount(async () => {
    // Fetch auth state on startup
    try {
      const user = await whoami();
      authStore.set({
        canWrite: user.can_write,
        email: user.email,
        permissionResolved: true,
      });
    } catch {
      authStore.set({
        canWrite: false,
        email: null,
        permissionResolved: true,
      });
    }

    // Sync browser language to backend on first visit
    syncInitialLang();

    // Connect SSE event stream
    connect();

    // Register checkpoint auto-open handler
    on('checkpoint_reached', _onCheckpointReached);
  });

  onDestroy(() => {
    off('checkpoint_reached', _onCheckpointReached);
  });
</script>

<AppBar />
<main id="view-container">
  <Router {routes} />
</main>
<NotificationPanel />
<CheckpointModal />
<ConfirmDialog />
