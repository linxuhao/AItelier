<script lang="ts">
  import { onMount } from 'svelte';
  import { listOwnerChannels, type OwnerChannel } from '../lib/api';
  import { t } from '../lib/i18n.svelte';
  import { channelAlertLine, channelAlertText } from '../lib/steward';
  // The line at the top of the steward's pane (design 7.9.2): the same down-alert line the
  // Channels tab shows on a channel's card, one per channel that has one, named. Re-read every
  // REFRESH_MS; a failed read shows nothing (the Channels tab says why).
  const REFRESH_MS = 60_000;
  let rows = $state<OwnerChannel[]>([]), now = $state(Date.now());
  const lines = $derived(rows.map(r => ({channel: r.channel, line: channelAlertLine(r, rows, now)}))
    .filter(({line}) => line !== null));
  onMount(() => {
    let alive = true;
    const load = async () => {
      try {
        const result = await listOwnerChannels();
        if (alive) { rows = result?.channels ?? []; now = Date.now(); }
      } catch { /* the Channels tab shows the error */ }
    };
    void load();
    const timer = setInterval(() => void load(), REFRESH_MS);
    return () => { alive = false; clearInterval(timer); };
  });
</script>
{#each lines as {channel, line} (channel)}
  <p class="channel-alert" data-steward-channel-alert={channel} role="status">
    <strong>{t('steward.chan.' + channel)}</strong>: {channelAlertText(line!, t)}</p>
{/each}
<style>
  .channel-alert{margin:0 0 6px;padding:4px 10px;border-radius:6px;font-size:12px;
    background:var(--pico-card-sectioning-background-color);color:var(--pico-del-color);overflow-wrap:anywhere;}
</style>
