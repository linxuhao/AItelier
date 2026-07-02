<script lang="ts">
  import { authStore } from '../stores/auth';
  import { connectionStore } from '../stores/connection';

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
