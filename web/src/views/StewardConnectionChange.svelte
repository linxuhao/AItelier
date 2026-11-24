<script lang="ts">
  import type { StewardProposal } from '../lib/api';
  import { t } from '../lib/i18n.svelte';
  // A connection_change proposal's card body (design 7.8.10, 7.8.10a): every field of the
  // connection and of each tool as "before -> after" (unchanged ones too), the tools' whole
  // definitions and the source's own words from the proposal's frozen snapshot - all plain
  // text, written by someone else's server or person. Each widening change is marked red.
  const { proposal, onconnect }: {
    proposal: StewardProposal;
    /** "Set the secret / sign in": open this connection on the connections tab. */
    onconnect?: (connId: string) => void;
  } = $props();
  type Row = {key: string; label: string; before: unknown; after: unknown; widen: boolean};
  const p = $derived(proposal.payload ?? {});
  const snap = $derived((p.snapshot ?? {}) as {connection?: Record<string, unknown> | null;
    tools?: Record<string, any>[]; source?: Record<string, any>});
  const widened = $derived(new Set<string>(Array.isArray(p.widened) ? p.widened : []));
  const isAdd = $derived(p.op === 'add');
  // The tools this change takes egress off (the backend's `egress_off`): dashboard approval only.
  const egressOff = $derived<string[]>(Array.isArray(p.egress_off) ? p.egress_off.map(String) : []);
  const before = $derived((snap.connection ?? {}) as Record<string, unknown>);
  function after(key: string): unknown {
    if (isAdd) return key === 'enabled' ? false : key === 'auth_identity' ? null : p[key] ?? null;
    return key in p ? p[key] : before[key];
  }
  const connectionRows = $derived<Row[]>([
    ['label', t('steward.connLabel')], ['url', t('steward.connUrl')], ['auth_kind', t('steward.connAuth')],
    ['header_name', t('steward.connHeaderName')], ['auth_identity', t('steward.connIdentity')],
    ['visibility', t('steward.connVisibility')], ['enabled', t('steward.connEnabled')],
  ].filter(([key]) => key !== 'header_name' || (isAdd ? p.auth_kind === 'header' : before.auth_kind === 'header'))
    .map(([key, label]) => ({key, label, before: isAdd ? null : before[key], after: after(key),
      widen: isAdd || widened.has(key)})));
  const toolFields: [string, string][] = [['allowed', 'steward.connAllowed'], ['outward', 'steward.connOutward'],
    ['egress', 'steward.connEgress'], ['blocked_args', 'steward.connBlocked'], ['daily_cap', 'steward.connCap']];
  function toolRows(tool: Record<string, any>): Row[] {
    const change = (Array.isArray(p.tools) ? p.tools : []).find((c: Record<string, unknown>) => c.raw_name === tool.raw_name) ?? {};
    return toolFields.map(([key, label]) => ({key, label: t(label), before: tool[key],
      after: key in change ? change[key] : tool[key], widen: widened.has(`tools.${tool.raw_name}.${key}`)}));
  }
  function show(value: unknown): string {
    if (value === null || value === undefined || value === '') return '—';
    if (typeof value === 'string') return value;
    return JSON.stringify(value);
  }
  const host = $derived.by(() => { try { return new URL(String(after('url') ?? '')).host; } catch { return ''; } });
  const result = $derived((proposal.result ?? {}) as Record<string, unknown>);
  const connectUrl = $derived(typeof result.connect_url === 'string' ? result.connect_url : '');
  const source = $derived(snap.source ?? {});
</script>
{#snippet rows(list: Row[])}
  <table class="changes">
    <tbody>
      {#each list as row (row.key)}
        <tr class:widen={row.widen} class:moved={show(row.before) !== show(row.after)} data-field={row.key} data-widen={row.widen ? '1' : '0'}>
          <th scope="row">{row.label}</th>
          <td class="plain" data-before>{show(row.before)}</td><td aria-hidden="true">→</td>
          <td class="plain" data-after>{show(row.after)}</td>
        </tr>
      {/each}
    </tbody>
  </table>
{/snippet}
<section class="connection-change" data-op={p.op} data-widens={p.widens ? '1' : '0'}>
  <p class="title"><strong>{t('steward.connOp.' + p.op)}</strong> <code>{p.conn_id}</code>{#if host} · <code data-host>{host}</code>{/if}</p>
  {#if p.widens}<p class="widen-note" data-widen-note>{t('steward.connWidens')}</p>{/if}
  {#if p.external_content}<p class="external" data-external>{t('steward.connExternal')}</p>{/if}
  {#if egressOff.length}<p class="external" data-egress-off>{t('steward.connEgressOff').replace('{tools}', egressOff.join(', '))}</p>{/if}
  {#if source.owner_message}
    <blockquote class="plain" data-source={source.kind}>
      {#if source.letter}
        <div data-source-letter>{t('steward.connSourceLetter').replace('{project}', String(source.letter.from_project_id))
          .replace('{at}', new Date(source.letter.created_at).toLocaleString())}: 『{source.letter.text}』</div>
      {/if}
      <div data-source-owner>{t('steward.connSourceOwner').replace('{channel}', String(source.owner_message.channel))
        .replace('{at}', new Date(source.owner_message.created_at).toLocaleString())}: 『{source.owner_message.text}』</div>
    </blockquote>
  {/if}
  {#if p.op !== 'remove' && p.op !== 'reenable'}{@render rows(connectionRows)}{/if}
  {#if p.op === 'remove'}<p class="widen-none" data-remove>{show(before.label)} · <code>{show(before.url)}</code></p>{/if}
  {#each snap.tools ?? [] as tool (tool.raw_name)}
    <div class="tool" data-tool={tool.raw_name}>
      <p><code>{tool.raw_name}</code> <small>{tool.state}</small></p>
      {@render rows(toolRows(tool))}
      <details open={toolRows(tool).some(r => r.widen)}>
        <summary>{t('steward.connDefinition')}</summary>
        <pre class="plain" data-definition>{JSON.stringify(tool.definition, null, 2)}</pre>
      </details>
    </div>
  {/each}
  {#if connectUrl && onconnect}
    <button type="button" class="outline" data-connect onclick={() => onconnect(String(p.conn_id))}>{t('steward.connGoConnect')}</button>
  {/if}
</section>
<style>
  .connection-change{margin:0 0 8px;}
  .title{margin:0 0 6px;}
  .widen-note,.external{margin:0 0 6px;color:var(--pico-del-color);font-size:12px;}
  blockquote{margin:0 0 8px;padding:6px 8px;font-size:12px;border-left:3px solid var(--pico-muted-border-color);}
  .changes{width:100%;margin:0 0 6px;font-size:12px;}
  .changes th{font-weight:normal;color:var(--pico-muted-color);white-space:nowrap;padding:1px 8px 1px 0;}
  .changes td{padding:1px 4px;overflow-wrap:anywhere;}
  tr.widen td[data-after],tr.widen th{color:var(--pico-del-color);font-weight:600;}
  .plain{white-space:pre-wrap;overflow-wrap:anywhere;}
  .tool{margin:0 0 6px;padding:6px 8px;border-radius:6px;background:var(--pico-card-sectioning-background-color);}
  .tool p{margin:0 0 4px;}
  pre{margin:4px 0 0;padding:6px 8px;font-size:11px;max-height:320px;overflow:auto;}
  button{width:auto;margin:4px 0 0;padding:5px 12px;font-size:13px;}
</style>
