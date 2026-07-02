<script lang="ts">
  import { notificationStore, clearNotifications, type NotificationEntry } from '../stores/notifications';
  import { formatTime } from '../lib/format';
  import { onMount } from 'svelte';

  // ── State ──

  let collapsed = $state(false);
  let notifListEl: HTMLDivElement | undefined = $state();

  // ── Event type → CSS class mapping ──

  const _TYPE_CLASS: Record<string, string> = {
    error: 'notif-error',
    warning: 'notif-warning',
    info: 'notif-info',
    success: 'notif-success',
  };

  function typeClass(type: string): string {
    return _TYPE_CLASS[type] || 'notif-info';
  }

  // ── Auto-scroll to latest ──

  $effect(() => {
    // Read to create dependency
    void $notificationStore.length;
    if (notifListEl && !collapsed) {
      requestAnimationFrame(() => {
        if (notifListEl) {
          notifListEl.scrollTop = 0;
        }
      });
    }
  });

  function handleClear(e: Event): void {
    e.stopPropagation();
    clearNotifications();
  }
</script>

<aside id="notification-panel" class="notification-panel" class:collapsed>
  <!-- Header (click to toggle collapse) -->
  <div class="notif-header" on:click={() => collapsed = !collapsed} role="button" tabindex="0" on:keydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); collapsed = !collapsed; } }}>
    <span class="notif-title">Notifications</span>
    {#if $notificationStore.length > 0}
      <span class="notif-badge">{Math.min($notificationStore.length, 100)}</span>
    {/if}
    <span class="notif-toggle">{collapsed ? '\u25B6' : '\u25BC'}</span>
    {#if $notificationStore.length > 0 && !collapsed}
      <button class="notif-clear-btn" on:click={handleClear} title="Clear all notifications">&times;</button>
    {/if}
  </div>

  {#if !collapsed}
    <div class="notif-list" bind:this={notifListEl}>
      {#if $notificationStore.length === 0}
        <p class="notif-empty">No notifications</p>
      {:else}
        {#each $notificationStore as notif (notif.id)}
          <div class="notif-entry {typeClass(notif.type)}">
            <div class="notif-entry-header">
              <span class="notif-type-badge">{notif.type}</span>
              <span class="notif-timestamp">{formatTime(notif.timestamp)}</span>
            </div>
            <div class="notif-message">{notif.message}</div>
          </div>
        {/each}
      {/if}
    </div>
  {/if}
</aside>

<style>
  .notification-panel {
    position: fixed;
    top: 3.5rem;
    right: 0.5rem;
    width: 320px;
    max-height: calc(100vh - 5rem);
    background: var(--pico-card-background-color, #fff);
    border: 1px solid var(--pico-muted-border-color, #ddd);
    border-radius: 0.5rem;
    box-shadow: 0 4px 12px rgba(0,0,0,0.15);
    z-index: 100;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  .notification-panel.collapsed {
    width: auto;
    min-width: 160px;
  }

  .notif-header {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.5rem 0.75rem;
    background: var(--pico-primary-background, #0066cc);
    color: var(--pico-primary-inverse, #fff);
    cursor: pointer;
    user-select: none;
    flex-shrink: 0;
  }

  .notif-title {
    font-weight: 600;
    font-size: 0.9rem;
    flex: 1;
  }

  .notif-badge {
    background: var(--pico-primary-inverse, #fff);
    color: var(--pico-primary-background, #0066cc);
    font-size: 0.75rem;
    font-weight: 700;
    padding: 0.1rem 0.4rem;
    border-radius: 1rem;
    min-width: 1.2rem;
    text-align: center;
  }

  .notif-toggle {
    font-size: 0.7rem;
    opacity: 0.8;
  }

  .notif-clear-btn {
    background: none;
    border: none;
    color: inherit;
    font-size: 1.1rem;
    cursor: pointer;
    padding: 0 0.15rem;
    line-height: 1;
    opacity: 0.8;
  }

  .notif-clear-btn:hover {
    opacity: 1;
  }

  .notif-list {
    flex: 1;
    overflow-y: auto;
    max-height: calc(100vh - 10rem);
    padding: 0.25rem 0;
  }

  .notif-empty {
    text-align: center;
    padding: 1rem;
    opacity: 0.6;
    font-size: 0.85rem;
  }

  .notif-entry {
    padding: 0.4rem 0.75rem;
    border-bottom: 1px solid var(--pico-muted-border-color, #eee);
    font-size: 0.85rem;
  }

  .notif-entry:last-child {
    border-bottom: none;
  }

  .notif-entry-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.2rem;
  }

  .notif-type-badge {
    font-size: 0.7rem;
    font-weight: 600;
    text-transform: uppercase;
    padding: 0.05rem 0.35rem;
    border-radius: 0.2rem;
  }

  .notif-timestamp {
    font-size: 0.75rem;
    opacity: 0.6;
  }

  .notif-message {
    line-height: 1.3;
    word-wrap: break-word;
  }

  /* Color coding by type */
  .notif-error .notif-type-badge {
    background: #c62828;
    color: #fff;
  }
  .notif-error {
    border-left: 3px solid #c62828;
  }

  .notif-warning .notif-type-badge {
    background: #f9a825;
    color: #000;
  }
  .notif-warning {
    border-left: 3px solid #f9a825;
  }

  .notif-info .notif-type-badge {
    background: #1565c0;
    color: #fff;
  }
  .notif-info {
    border-left: 3px solid #1565c0;
  }

  .notif-success .notif-type-badge {
    background: #2e7d32;
    color: #fff;
  }
  .notif-success {
    border-left: 3px solid #2e7d32;
  }
</style>
