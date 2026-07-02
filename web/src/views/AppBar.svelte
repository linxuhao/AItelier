<script lang="ts">
  import { authStore } from '../stores/auth';
  import { connectionStore } from '../stores/connection';
  import { notifPanelOpen, notifUnread } from '../stores/notifications';

  function toggleNotifications(): void {
    notifPanelOpen.update((v) => !v);
  }

  let connectionText = $derived(
    $connectionStore.connectionOk
      ? '● Connected'
      : `○ Reconnecting${$connectionStore.reconnectAttempt > 0 ? ` (attempt ${$connectionStore.reconnectAttempt})` : '\u2026'}`,
  );
</script>

<header id="app-bar">
  <nav>
    <ul>
      <li><strong>AItelier</strong></li>
    </ul>
    <ul>
      <li><a href="#/projects">Dashboard</a></li>
      <li><a href="#/chat">Chat</a></li>
      {#if $authStore.canWrite}
        <li><a href="#/tracking">Tracking</a></li>
      {/if}
    </ul>
    <ul>
      <li>
        <button
          class="notif-bell"
          class:open={$notifPanelOpen}
          onclick={toggleNotifications}
          title="Pipeline notifications"
          aria-label="Notifications"
        >
          🔔
          {#if $notifUnread > 0}
            <span class="notif-bell-badge">{$notifUnread > 99 ? '99+' : $notifUnread}</span>
          {/if}
        </button>
      </li>
      <li>
        <span
          class="connection-status"
          class:connected={$connectionStore.connectionOk}
          class:disconnected={!$connectionStore.connectionOk}
        >
          {connectionText}
        </span>
      </li>
    </ul>
  </nav>
</header>

<style>
  .notif-bell {
    position: relative;
    background: none;
    border: none;
    font-size: 1rem;
    cursor: pointer;
    padding: 0.2rem 0.4rem;
    line-height: 1;
    border-radius: 0.4rem;
  }
  .notif-bell:hover,
  .notif-bell.open {
    background: var(--pico-secondary-focus, rgba(128, 128, 128, 0.12));
  }
  .notif-bell-badge {
    position: absolute;
    top: -0.3rem;
    right: -0.35rem;
    background: #c62828;
    color: #fff;
    font-size: 0.62rem;
    font-weight: 700;
    padding: 0.05rem 0.28rem;
    border-radius: 1rem;
    min-width: 1rem;
    text-align: center;
  }
  .connection-status {
    font-size: 0.875rem;
  }
  .connection-status.connected {
    color: var(--pico-color-green-500, #090);
  }
  .connection-status.disconnected {
    color: var(--pico-color-orange-500, #c90);
  }
</style>
