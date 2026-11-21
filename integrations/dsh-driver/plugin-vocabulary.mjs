/** The names this adapter really has - the vocabulary dsh-director must never carry -
 *  derived from the adapter's own files rather than listed (plugin-boundary.test.mjs,
 *  runtime.standalone.test.mjs). */
import { readFileSync } from 'node:fs';
import { roles as aitelierRoles } from './aitelier-roles.mjs';
import { labels } from './aitelier-texts.mjs';
import { loadRoles } from './roles.mjs';

const here = new URL('.', import.meta.url);

/** The adapter's own names, read from the adapter's own files. */
export function adapterVocabulary() {
  const runtime = readFileSync(new URL('runtime.mjs', here), 'utf8');
  const registered = [...runtime.matchAll(/defineTool\(\{ name: '([a-z_]+)'/g)].map(m => m[1]);
  const table = loadRoles(), effective = aitelierRoles(table);
  const added = new Set([
    ...effective.director.presented.filter(n => !table.director.presented.includes(n)),
    ...effective.worker.forbidden.filter(n => !table.worker.forbidden.includes(n)),
    ...Object.keys(effective.worker.allowed_actions ?? {}),
  ]);
  const prefixes = [...added].map(n => /^(mcp__[a-z0-9]+__)/.exec(n)?.[1]).filter(Boolean);
  const bare = [...added].map(n => n.replace(/^mcp__[a-z0-9]+__/, ''));
  const words = new Set([...registered, ...added, ...bare, ...prefixes, labels.runtimeEvent, labels.continue,
    labels.todoBrief, labels.context, labels.clock, labels.contextSection, labels.sectionPrefix + ':']);
  return [...words].filter(Boolean).sort();
}

/** A matcher for any of those names, starting a word or right after an MCP '__' separator:
 *  a name ending in a letter or digit as a whole word,
 *  a prefix ending in '__' or ':' wherever it starts a word. */
export const adapterPattern = () => new RegExp('(?:(?<![A-Za-z0-9_])|(?<=__))(?:' + adapterVocabulary()
  .map(w => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + (/[A-Za-z0-9]$/.test(w) ? '(?![A-Za-z0-9_])' : '')).join('|') + ')');
