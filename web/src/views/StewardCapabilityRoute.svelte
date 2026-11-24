<script lang="ts">
  import type { StewardProposal, StewardRouteEntry } from '../lib/api';
  import { t } from '../lib/i18n.svelte';
  // A capability_route proposal's card body (design 7.8.24 "五"): "capability: entry 1 -> entry 2",
  // the tool descriptions the steward judged by as frozen when it proposed (written by the
  // upstream server: plain text only), and the source - the owner's own message read from the
  // database, or "found by tool search", which only this card can approve.
  const { proposal }: {proposal: StewardProposal} = $props();
  type CardTool = {conn_id?: string; raw_name?: string; description?: string};
  const p = $derived(proposal.payload ?? {});
  const capId = $derived(String(p.cap_id ?? ''));
  const capName = $derived.by(() => {
    const key = 'steward.cap.' + capId;
    return t(key) === key ? capId : t(key);
  });
  // The resolved entries the backend froze; the steward's own strings when there are none.
  const entries = $derived<(StewardRouteEntry | string)[]>(Array.isArray(p.entries) ? p.entries
    : Array.isArray(p.route) ? p.route : []);
  const card = $derived<CardTool[]>(Array.isArray(p.card) ? p.card : []);
  const source = $derived((p.source ?? {}) as {kind?: string; owner_message?: {channel?: string; created_at?: string; text?: string}});
  function entryText(entry: StewardRouteEntry | string): string {
    if (typeof entry === 'string') return entry;
    if (entry.kind === 'native') return t('steward.capEntryNative');
    if (entry.kind === 'missing') return t('steward.capEntryMissing');
    if (entry.kind === 'host') return entry.tool;
    return entry.conn_id + ' / ' + entry.raw_name;
  }
</script>
<section class="capability-route" data-cap-id={capId} data-source={source.kind ?? ''}>
  <p class="title"><strong data-cap-name>{capName}</strong>: <span data-cap-route>{entries.map(entryText).join(' → ')}</span></p>
  <dl>
    <dt>{t('steward.capPropRoute')}</dt>
    <dd><ol>{#each entries as entry, i (i)}<li data-cap-entry={i}>{entryText(entry)}</li>{/each}</ol></dd>
    {#if card.length}
      <dt>{t('steward.capPropDescriptions')}</dt>
      <dd>
        {#each card as tool, i (i)}
          <div class="tool" data-cap-card={tool.raw_name}>
            <code>{tool.conn_id} / {tool.raw_name}</code>
            <pre class="plain" data-cap-description>{tool.description ?? ''}</pre>
          </div>
        {/each}
      </dd>
    {/if}
  </dl>
  {#if source.kind === 'owner_message'}
    <blockquote class="plain" data-cap-source="owner_message">
      {t('steward.capPropSourceOwner')}{#if source.owner_message}: 『{source.owner_message.text}』
        <small>{source.owner_message.channel}{#if source.owner_message.created_at} · {new Date(source.owner_message.created_at).toLocaleString()}{/if}</small>{/if}
    </blockquote>
  {:else if source.kind === 'tool_search'}
    <p class="search" data-cap-source="tool_search">{t('steward.capPropSourceSearch')}</p>
  {/if}
</section>
<style>
  .capability-route{margin:0 0 8px;}
  .title{margin:0 0 6px;}
  dl{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:2px 10px;margin:0 0 6px;font-size:12px;}
  dt{color:var(--pico-muted-color);font-weight:normal;}dd{margin:0;overflow-wrap:anywhere;}
  ol{margin:0;padding-left:18px;}
  .tool{margin:0 0 4px;padding:4px 6px;border-radius:6px;background:var(--pico-card-sectioning-background-color);}
  .plain{white-space:pre-wrap;overflow-wrap:anywhere;}
  pre{margin:2px 0 0;padding:4px 6px;font-size:11px;max-height:200px;overflow:auto;}
  blockquote{margin:0 0 8px;padding:6px 8px;font-size:12px;border-left:3px solid var(--pico-muted-border-color);}
  .search{margin:0 0 6px;color:var(--pico-del-color);font-size:12px;}
</style>
