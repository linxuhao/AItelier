/** The steward's basic capabilities (step 24.4, design-steward 7.8.24 "三", "七"): the system
 *  prompt section that says which jobs the steward can do and with which tool, rendered from the
 *  table the owner set (`basics`, carried on the host's connection cache, lib/catalog.mjs).
 *
 *  - The fixed list: the ids, their order and their English keywords (`desc`) are the backend
 *    list's, a design revision to change; this file keeps only what the section says about each
 *    (its native words, its host tools and the name a host tool is checked by).
 *  - renderBasics is pure: the same table and registry give the same bytes, in an owner turn and
 *    a group turn alike. It writes only this file's constants and the capability ids - never a
 *    connection's label, a connection tool's name or description, or anything an upstream server
 *    wrote: a connection entry is always `search_tools("basics:<id>")`. Health (a connection's last
 *    error, a failed self-check) is not in it; only what the table's configuration says
 *    (`available`) and whether a host tool is registered here.
 *  - No table (none set, or a host without one): no section at all (`basicsSection`). */
import { GROUP_TOOLS } from './group-turns.mjs';

/** The fixed list, in display and render order. `native`: what "native" means for the job (its
 *  words, the tools those words name; memory's words carry their own turns); `host`: the host
 *  tools an entry may name, each with the registered name that says it is there. */
export const BASICS = Object.freeze([
  { id: 'vision', desc: 'image understanding, see images', native: { text: 'you see images in your wake', tools: [] } },
  { id: 'ocr', desc: 'text recognition, OCR, image text', native: { text: 'you read the text in the images you see', tools: [] } },
  { id: 'pdf', desc: 'PDF reading, PDF text', native: { text: 'read_document', tools: ['read_document'] } },
  { id: 'office_read', desc: 'Word Excel PowerPoint reading, docx xlsx pptx', host: { read_document: 'make_document' } },
  { id: 'office_write', desc: 'Word Excel PowerPoint creation, docx xlsx pptx', host: { make_document: 'make_document' } },
  { id: 'transcribe', desc: 'speech recognition, transcription, audio text' },
  { id: 'tts', desc: 'speech synthesis, voice message, TTS' },
  { id: 'draw', desc: 'image generation, picture, drawing' },
  { id: 'music', desc: 'music generation, song' },
  { id: 'sfx', desc: 'sound effect, SFX' },
  { id: 'video', desc: 'video generation, video editing' },
  { id: 'search', desc: 'web search, search engine' },
  { id: 'fetch', desc: 'web page, URL reading, fetch' },
  { id: 'email', desc: 'email, mail, inbox' },
  { id: 'calendar', desc: 'calendar, events, appointments',
    missing: 'Reminders (schedule_*, group_reminder_set) are your own and are not a calendar.' },
  { id: 'cloud_files', desc: 'cloud drive, files, storage' },
  { id: 'memory', desc: 'memory, notes, past conversations',
    native: { text: 'memory_* in owner turns, family_memory_* in group turns', tools: [], turns: false } },
  { id: 'code', desc: 'code execution, Python, calculation', host: { calculate: 'calculate' } },
].map(spec => Object.freeze(spec)));

/** Every host tool an entry may name (a proposal's snapshot names only these, steward-host). */
export const HOST_TOOLS = Object.freeze([...new Set(BASICS.flatMap(spec => Object.keys(spec.host ?? {})))]);

export const BASICS_INTRO = 'Your basic capabilities (your owner set this table on the dashboard; it is authoritative; try entries in order, a later one only when an earlier one cannot be used):';
export const NOT_SET_INTRO = 'Not set yet (find tools with search_tools as you always have; you may propose a route): ';
const BOTH = ' (owner and group turns)', OWNER = ' (owner turns only)', UNAVAILABLE = ' (unavailable right now)';

/** The table's rows that are set, by id (an id this list does not know is ignored). */
const setRows = basics => {
  const rows = new Map();
  for (const row of Array.isArray(basics?.rows) ? basics.rows : [])
    if (row && typeof row.id === 'string' && Array.isArray(row.route) && !rows.has(row.id)) rows.set(row.id, row.route);
  return rows;
};

/** Whether the table has a row this list renders. */
export const hasBasics = basics => BASICS.some(spec => setRows(basics).has(spec.id));

/** One entry's words, or undefined for an entry this capability cannot have (never rendered). */
function entryText(spec, entry, isRegistered) {
  const turns = (names, available) => !available || !names.every(isRegistered) ? UNAVAILABLE
    : names.every(name => GROUP_TOOLS.has(name)) ? BOTH : OWNER;
  if (entry?.kind === 'native' && spec.native) return `native - ${spec.native.text}`
    + (spec.native.turns === false ? '' : turns(spec.native.tools, entry.available !== false));
  if (entry?.kind === 'host' && spec.host && Object.hasOwn(spec.host, entry.tool))
    return entry.tool + (entry.available === false || !isRegistered(spec.host[entry.tool]) ? UNAVAILABLE
      : GROUP_TOOLS.has(entry.tool) ? BOTH : OWNER);
  if (entry?.kind === 'conn') return `connection tool - search_tools("basics:${spec.id}")`
    + (entry.available !== true ? UNAVAILABLE : entry.visibility === 'public' ? BOTH : OWNER);
  return undefined;
}

/** The section's text ('' without a set row). @param basics the cache's `basics`
 *  ({rows: [{id, route: [{kind, tool?, visibility?, available}]}]}) @param registered the
 *  names registered on this host (an iterable; undefined: not checked). */
export function renderBasics(basics, registered) {
  const rows = setRows(basics);
  const names = registered === undefined ? undefined : new Set(registered);
  const isRegistered = name => names === undefined || names.has(name);
  const lines = [], unset = [];
  for (const spec of BASICS) {
    const route = rows.get(spec.id);
    if (route?.length === 1 && route[0]?.kind === 'missing') {
      lines.push(`- ${spec.id} (${spec.desc}): missing.${spec.missing ? ' ' + spec.missing : ''}`);
      continue;
    }
    const entries = (route ?? []).map(entry => entryText(spec, entry, isRegistered)).filter(Boolean);
    if (!entries.length) { unset.push(`${spec.id} (${spec.desc})`); continue; }
    lines.push(`- ${spec.id} (${spec.desc}): ${entries.map((text, i) => `${i + 1}) ${text}`).join('; ')}.`);
  }
  if (!lines.length) return '';
  return [BASICS_INTRO, ...lines, ...(unset.length ? [NOT_SET_INTRO + unset.join(', ') + '.'] : [])].join('\n');
}

/** Keeps the section `name` registered on `scope` exactly while the table has a row (call it
 *  before each step): without one nothing is registered, so the prompt is today's byte for
 *  byte. Its text is read at each assembly. @param basics () => the cache's `basics`
 *  @param registered (assembly context) => the names registered for that caller. */
export function basicsSection(scope, { name, order = 13, basics, registered }) {
  let dispose;
  return () => {
    const present = hasBasics(basics());
    if (present && !dispose) dispose = scope.systemPrompt.section({ name, order, interpolate: false,
      text: context => renderBasics(basics(), registered(context)) }) ?? (() => {});
    else if (!present && dispose) { dispose(); dispose = undefined; }
  };
}
