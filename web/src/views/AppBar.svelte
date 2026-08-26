<script lang="ts">
  import { authStore } from '../stores/auth';
  import { connectionStore } from '../stores/connection';
  import { notifPanelOpen, notifUnread } from '../stores/notifications';
  import { langStore, setLang } from '../stores/i18n';
  import { t } from '../lib/i18n.svelte';

  const LANG_OPTIONS: { code: string; label: string }[] = [
    { code: 'en', label: 'English' },
    { code: 'zh-CN', label: '简体中文' },
    { code: 'zh-TW', label: '繁體中文' },
    { code: 'ja', label: '日本語' },
    { code: 'ko', label: '한국어' },
    { code: 'fr', label: 'Français' },
    { code: 'de', label: 'Deutsch' },
    { code: 'es', label: 'Español' },
  ];

  function onLangChange(e: Event): void {
    const sel = e.target as HTMLSelectElement;
    setLang(sel.value);
  }

  function toggleNotifications(): void {
    notifPanelOpen.update((v) => !v);
  }

  let connectionText = $derived(
    $connectionStore.connectionOk
      ? t('appbar.connected')
      : t('appbar.reconnecting') + ($connectionStore.reconnectAttempt > 0 ? ` (${t('dashboard.attempt').replace('{n}', String($connectionStore.reconnectAttempt))})` : '…'),
  );
</script>

<header id="app-bar">
  <nav>
    <ul>
      <li><strong>AItelier</strong></li>
    </ul>
    <ul>
      <li><a href="#/projects">{t('appbar.dashboard')}</a></li>
      <li><a href="#/chat">{t('appbar.chat')}</a></li>
      {#if $authStore.canWrite}
        <li><a href="#/tracking">{t('appbar.tracking')}</a></li>
      {/if}
    </ul>
    <ul>
      <li>
        <!-- The mark is inlined rather than fetched: the page must stay
             self-contained, and a remote icon is one more thing that can fail
             to load for a public reader. -->
        <a
          class="gh-link"
          href="https://github.com/linxuhao/aitelier"
          target="_blank"
          rel="noopener noreferrer"
          title={t('appbar.github')}
          aria-label={t('appbar.github')}
        >
          <svg viewBox="0 0 16 16" width="18" height="18" aria-hidden="true">
            <path fill="currentColor" d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z"/>
          </svg>
        </a>
      </li>
      <li>
        <select
          class="lang-select"
          value={$langStore}
          onchange={onLangChange}
          title="Language"
          aria-label="Language"
        >
          {#each LANG_OPTIONS as opt}
            <option value={opt.code}>{opt.label}</option>
          {/each}
        </select>
      </li>
      <li>
        <button
          class="notif-bell"
          class:open={$notifPanelOpen}
          onclick={toggleNotifications}
          title={t('appbar.notifTitle')}
          aria-label={t('appbar.notifLabel')}
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
  .gh-link {
    display: inline-flex;
    align-items: center;
    padding: 0.2rem 0.4rem;
    border-radius: 0.4rem;
    color: inherit;
  }
  .gh-link:hover {
    background: var(--pico-secondary-focus, rgba(128, 128, 128, 0.12));
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
  .lang-select {
    font-size: 0.875rem;
    padding: 0.15rem 0.35rem;
    border: 1px solid var(--pico-muted-border-color, #ccc);
    border-radius: 0.3rem;
    background: transparent;
    color: inherit;
    cursor: pointer;
  }
</style>
