/**
 * Rendering helpers for durable trace entries.
 *
 * Lifted out of Trace.svelte so the full-page trace view and the run graph's
 * per-node pane read the SAME payload the same way. A trace payload has no
 * fixed shape -- it is whatever the step wrote -- so the un-shaping logic is
 * the only thing standing between the reader and a wall of JSON, and two
 * copies of it would drift.
 */
import { toEpochSeconds } from './format';

/** Collapse a multi-line value into one readable line. */
function oneLine(s: string, max = 160): string {
  const flat = s.replace(/\s+/g, ' ').trim();
  return flat.length > max ? flat.slice(0, max) + '…' : flat;
}

/**
 * Peel one layer off a tool preview that is a single-key JSON envelope.
 *
 * Several tools hand back `{"output": "{\"files\": []}"}` — the useful part
 * double-encoded inside a wrapper that says nothing. One peel, never more:
 * unwrapping recursively would start eating real structure.
 */
function unwrap(preview: string): string {
  const t = preview.trim();
  if (!t.startsWith('{')) return preview;
  try {
    const o = JSON.parse(t);
    const keys = o && typeof o === 'object' ? Object.keys(o) : [];
    if (keys.length === 1 && typeof o[keys[0]] === 'string') return o[keys[0]];
  } catch { /* not JSON — show it as written */ }
  return preview;
}

/** One line of chat-like text for a trace payload, whatever shape it took. */
export function extractPayloadText(payload: unknown): string {
  if (payload == null) return '';
  if (typeof payload === 'string') return payload;
  if (typeof payload === 'object') {
    const p = payload as Record<string, any>;
    const hasToolCalls = Array.isArray(p.tool_calls) && p.tool_calls.length;
    if (hasToolCalls || p.reasoning_content) {
      const parts: string[] = [];
      if (p.text) parts.push(p.text);
      if (p.reasoning_content) parts.push('[reasoning]\n' + p.reasoning_content);
      if (hasToolCalls) {
        p.tool_calls.forEach((tc: any, i: number) => {
          let name: string, args: string;
          if (typeof tc === 'string') {
            name = tc;
            args = (Array.isArray(p.tool_args) && p.tool_args[i]) || '';
          } else {
            name = tc.name;
            args = tc.arguments || '';
          }
          parts.push('→ ' + name + '(' + args + ')');
        });
      }
      return parts.join('\n\n');
    }
    // A prompt record is `{attempt, mode, system, user}` — the two halves of
    // what the model was actually asked. Without this it fell through to the
    // JSON dump below and the most readable record in the whole trace came out
    // as an escaped one-line blob.
    if (typeof p.user === 'string' && p.user) {
      const parts: string[] = [];
      if (typeof p.system === 'string' && p.system) parts.push('[system]\n' + p.system);
      parts.push('[user]\n' + p.user);
      return parts.join('\n\n');
    }
    const direct = p.content || p.text || p.message ||
      p.response || p.prompt || p.error;
    if (typeof direct === 'string' && direct) return direct;
    try { return JSON.stringify(payload, null, 2); } catch { return String(payload); }
  }
  return String(payload);
}

/** HH:MM:SS out of a trace timestamp, or the raw string when it has none. */
export function shortTime(ts: string | undefined | null): string {
  if (!ts) return '';
  // Trace `created_at` is a NAIVE SQLite datetime whose value is UTC. The old
  // regex just displayed the raw wall time, so every viewer saw UTC no matter
  // where they sat (invisible on this deployment only because the server's tz
  // happens to be UTC). `toEpochSeconds` already knows the format — it appends
  // the missing Z before parsing — so render its result in the VIEWER's zone.
  const epoch = toEpochSeconds(ts);
  if (epoch != null) {
    return new Date(epoch * 1000).toLocaleTimeString([], {
      hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit',
    });
  }
  const m = String(ts).match(/(\d{2}:\d{2}:\d{2})/);
  return m ? m[1] : String(ts);
}

/**
 * The one line a COLLAPSED entry shows.
 *
 * Written against the payload shapes the pipeline actually writes (surveyed
 * from a live dpe_game run): tool calls carry `params`/`files`, results carry
 * `preview`/`written`/`keys`/`error`, prompts carry `system`+`user`, steps
 * carry `next_node`/`label`, lifecycle carries `status`+`detail`.
 *
 * The rule that matters: NEVER lead with a bare `{`. Falling back to
 * pretty-printed JSON spent the first of two visible lines on an opening brace
 * and the second on whichever field happened to be first — which is what made
 * the pane read as garbage rather than as a history.
 */
export function traceSummary(entry: Record<string, any>): string {
  const p = entry?.payload;
  if (p == null) return '';
  if (typeof p !== 'object') return oneLine(String(p));

  // What the agent asked a tool to do.
  if (p.params != null) {
    try { return oneLine(JSON.stringify(p.params)); } catch { /* fall through */ }
  }
  if (Array.isArray(p.files)) return oneLine(p.files.join(', '));
  // ...and what came back.
  if (typeof p.preview === 'string' && p.preview) return oneLine(unwrap(p.preview));
  if (p.written != null) {
    return oneLine(Array.isArray(p.written) ? p.written.join(', ') : String(p.written));
  }
  if (Array.isArray(p.keys)) return oneLine(p.keys.join(', '));
  if (typeof p.error === 'string' && p.error) return oneLine(p.error);

  if (p.prompt_tokens != null || p.total_tokens != null) {
    return `in ${p.prompt_tokens ?? 0} · out ${p.completion_tokens ?? 0}`;
  }

  // The conversation itself: the model's own words, else the tools it reached
  // for, else its reasoning. A turn that only called a tool has an EMPTY text,
  // and that is the commonest response record of all — left to the key=value
  // fallback the single most interesting category read the worst.
  if (typeof p.text === 'string' && p.text) return oneLine(p.text);
  if (Array.isArray(p.tool_calls) && p.tool_calls.length) {
    const names = p.tool_calls
      .map((tc: any) => (typeof tc === 'string' ? tc : tc?.name))
      .filter(Boolean);
    if (names.length) return '→ ' + names.join(', ');
  }
  if (typeof p.reasoning_content === 'string' && p.reasoning_content) {
    return oneLine(p.reasoning_content);
  }
  if (typeof p.user === 'string' && p.user) return oneLine(p.user);

  // Engine records: where the run went next.
  if (typeof p.label === 'string' && p.label) return oneLine(p.label);
  if (typeof p.next_node === 'string' && p.next_node) return '→ ' + p.next_node;
  if (typeof p.status === 'string' && p.status) {
    return oneLine([p.status, p.detail].filter(Boolean).join(' · '));
  }

  // Last resort: compact key=value. Empty/false/null fields are dropped rather
  // than printed — a record whose every field is empty says nothing, and the
  // head row already names it.
  const bits = Object.entries(p)
    .filter(([, v]) => v != null && v !== '' && v !== false
      && !(Array.isArray(v) && v.length === 0))
    .map(([k, v]) => k + '=' + oneLine(
      typeof v === 'object' ? JSON.stringify(v) : String(v), 40));
  return bits.join(' · ');
}

/** "route → provider/model" for a token_usage payload, else null.
 *  This is the ACTUAL endpoint that served the turn (post-failover), which is
 *  why it lives on usage rows and never on the claim row: a claim happens
 *  before the gateway binds, so anything shown there would be a guess. */
export function modelBinding(payload: unknown): string | null {
  if (!payload || typeof payload !== 'object') return null;
  const p = payload as Record<string, any>;
  if (!p.served_by) return null;
  // Display the MODEL only, never the provider: this renders on a public
  // page, and which reseller served a given model is commercial information
  // some providers do not want advertised. The full served_by stays in the
  // trace payload for the operator.
  const model = String(p.served_by).includes('/')
    ? String(p.served_by).split('/', 2)[1]
    : String(p.served_by);
  const route = p.model_route && p.model_route !== model && p.model_route !== p.served_by
    ? p.model_route + ' \u2192 ' : '';
  return route + model;
}
