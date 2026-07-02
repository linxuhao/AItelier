<script>
  import { onMount } from 'svelte';
  import Router from 'svelte-spa-router';

  import AppBar from './views/AppBar.svelte';
  import Dashboard from './views/Dashboard.svelte';
  import Project from './views/Project.svelte';
  import Chat from './views/Chat.svelte';
  import Trace from './views/Trace.svelte';
  import Tracking from './views/Tracking.svelte';
  import NotificationPanel from './views/NotificationPanel.svelte';
  import CheckpointModal from './views/CheckpointModal.svelte';
  import ConfirmDialog from './views/ConfirmDialog.svelte';

  import { authStore } from './stores/auth';
  import { connectionStore } from './stores/connection';
  import { projectStore } from './stores/project';
  import { notificationStore } from './stores/notifications';
  import { checkpointStore } from './stores/checkpoint';

  import { whoami } from './lib/api';
  import { connect } from './lib/sse';

  const routes = {
    '/': Dashboard,
    '/projects': Dashboard,
    '/projects/:id': Project,
    '/projects/:id/chat': Chat,
    '/projects/:id/trace/:runId': Trace,
    '/tracking': Tracking,
  };

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

    // Connect SSE event stream
    connect();
  });
</script>

<AppBar />
<main id="view-container">
  <Router {routes} />
</main>
<NotificationPanel />
<CheckpointModal />
<ConfirmDialog />
