<script lang="ts">
  import { onMount } from 'svelte';
  import { listOwnerChannels, type OwnerChannel } from '../lib/api';
  import { t } from '../lib/i18n.svelte';
  import { channelAlertLine, channelAlertText, type ChannelAlertLine, type ChannelClock } from '../lib/channelAlert';
  // The line at the top of the steward's pane (design 7.9.2): the alert line the Channels tab
  // shows on a channel's card, for each channel the owner may have to act on - one that is down
  // past what its card can settle on its own, or whose recovery notice did not go out. Re-read
  // every REFRESH_MS; a failed read shows nothing (the Channels tab says why).
  const REFRESH_MS = 60_000;
  const SHOWN = new Set<ChannelAlertLine['key']>(['alerted', 'toldEarlier', 'gaveUp', 'noVia', 'unclaimed', 'upGaveUp', 'upUnclaimed']);
  let rows = $state<OwnerChannel[]>([]), clock = $state<ChannelClock | null>(null);
  const lines = $derived(clock ? rows.map(r => ({channel: r.channel, line: channelAlertLine(r, rows, clock!)}))
    .filter(({line}) => SHOWN.has(line.key)) : []);
  onMount(() => {
    let alive = true;
    const load = async () => {
      try {
        const result = await listOwnerChannels();
        if (alive) {
          rows = result?.channels ?? [];
          clock = result?.now && result.owner_tz ? {now: result.now, owner_tz: result.owner_tz} : null;
        }
      } catch { /* the Channels tab shows the error */ }
    };
    void load();
    const timer = setInterval(() => void load(), REFRESH_MS);
    return () => { alive = false; clearInterval(timer); };
  });
</script>
{#each lines as {channel, line} (channel)}
  <p class="channel-alert" data-steward-channel-alert={channel} role="status">
    <strong>{t('steward.chan.' + channel)}</strong>: {channelAlertText(line, clock!, t)}</p>
{/each}
<style>
  .channel-alert{margin:0 0 6px;padding:4px 10px;border-radius:6px;font-size:12px;
    background:var(--pico-card-sectioning-background-color);color:var(--pico-del-color);overflow-wrap:anywhere;}
</style>
