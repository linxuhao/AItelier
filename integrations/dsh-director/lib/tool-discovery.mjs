/** Keep MCP schemas in the registry, outside ordinary model requests. */
import { roleOf } from './roles.mjs';

const metaTools = new Set(['search_tools', 'call_tool', 'run_code']);

/** The request's tool list for one session: the tools its role presents. Everything
 *  else stays registered and callable - through call_tool, or directly by a known
 *  name - and is found with search_tools; only the role's `forbidden` tools are
 *  refused (see refusal). Which names a role presents is roles.json's business
 *  (chosen from measured use, 2026-11-07..14), extended by the adapter
 *  (the backend adapter's role additions), never a list in this file. A tool missing there is
 *  deferred, never lost. */
export function presentTools(tools, role) {
  const presented = new Set(role.presented);
  return tools.filter(t => presented.has(t.name));
}

/** The prompt sections one session is shown. A presented_only role (the steward) has a
 *  closed tool set, so the guidance other tools register for themselves is only noise to
 *  it - and misleading noise: a steward told how to use bash, read, jobs or an MCP server
 *  believes it can (measured 2026-11-18: its self-check listed eleven tools and three MCP
 *  servers it cannot call). For such a role, judged against `shown` - the names of the
 *  tools this request actually lists for it - a tool's own section (`tool:<name>`, or
 *  `tool:<name>:<detail>`) stays only when that tool is shown, an MCP server's section
 *  (`mcp:<server>`) only when one of that server's tools (`mcp__<server>__*`) is, the MCP
 *  resource section only when a resource tool is, and the discovery section only when
 *  search_tools is. Every other section - the persona, the role prompt, the host's own -
 *  is kept; this is a rule over these section NAMES (DSH's own naming), so a section a
 *  plugin registers under another name reaches the steward. Every other role's sections
 *  are returned untouched (the same array). */
const RESOURCE_TOOLS = ['list_mcp_resources', 'list_mcp_resource_templates', 'read_mcp_resource'];
export function presentSections(sections, role, discoverySection, shown = role?.presented ?? []) {
  if (!role?.presented_only || !Array.isArray(sections)) return sections;
  const names = new Set(shown);
  return sections.filter(({ name }) => {
    if (name === discoverySection) return names.has('search_tools');
    if (name.startsWith('tool:')) return names.has(name.slice('tool:'.length).split(':')[0]);
    if (name === 'mcp-resource-servers') return RESOURCE_TOOLS.some(tool => names.has(tool));
    if (name.startsWith('mcp:')) {
      const prefix = `mcp__${name.slice('mcp:'.length)}__`;
      return [...names].some(tool => tool.startsWith(prefix));
    }
    return true;
  });
}

/** Why `role` may not run this call, or undefined. Refused: every tool the role
 *  forbids, and - for a tool listed in `allowed_actions` - any call whose `action`
 *  argument is not on that tool's list (a missing or non-string action included).
 *  `notes` adds the adapter's reason for one tool. `catalog` is the turn's host
 *  catalog (a Set of names) for a role with `catalog: "host"`. */
export function refusal(role, exec, catalog) {
  // A presented_only role (the steward) has exactly its presented tools, and - with a
  // host catalog - the names in this turn's catalog: any other name, called directly or
  // through a transport, is refused.
  if (role.presented_only && !role.presented.includes(exec.name) && !(role.catalog && catalog?.has(exec.name)))
    return `${exec.name} is not available to this role`;
  const actions = role.allowed_actions?.[exec.name];
  if (!role.forbidden.includes(exec.name) && !(actions && !actions.includes(exec.arguments?.action))) return undefined;
  const note = role.notes?.[exec.name];
  return `${exec.name}${actions ? `(${String(exec.arguments?.action)})` : ''} is the director's${note ? ': ' + note : ''}`;
}

export function searchTools(schemas, query, limit = 5) {
  const needle = query.trim().toLocaleLowerCase();
  if (!needle) throw new Error('query must not be empty');
  if (!Number.isInteger(limit) || limit < 1 || limit > 10) throw new Error('limit must be 1–10');
  const terms = [...new Set(needle.match(/[\p{L}\p{N}]+/gu) ?? [needle])];
  const ranked = schemas.filter(t => !metaTools.has(t.name)).map(tool => {
    const name = tool.name.toLocaleLowerCase(), description = tool.description.toLocaleLowerCase();
    const score = (name === needle ? 1000 : name.includes(needle) ? 100 : 0)
      + terms.reduce((n, term) => n + (name.includes(term) ? 10 : description.includes(term) ? 1 : 0), 0);
    return { tool, score };
  }).filter(hit => hit.score > 0).sort((a, b) => b.score - a.score || a.tool.name.localeCompare(b.tool.name));
  return { tools: ranked.slice(0, limit).map(({ tool }) => tool), total_matches: ranked.length,
    has_more: ranked.length > limit };
}

/** @param roles - the effective role table (roles.json plus the adapter's additions). */
/** Generic wording; a host whose tools need naming (its MCP servers, say) passes its own. */
export const defaultDiscoveryTexts = Object.freeze({
  sectionName: 'dsh-director:tool-discovery',
  section: 'More tools are available through search_tools(query) and call_tool(name, arguments). Search names/descriptions to retrieve only the relevant full parameter schemas; then use call_tool with the exact name and arguments. Search again after compaction if you need an earlier schema. Known names may be called without searching first. The everyday coding and coordination tools are listed directly; less-used ones are not listed but are found with search_tools and called the same way. Discovery does not change your existing permissions.',
  searchDescription: 'Find available tools by name or description and return their parameter schemas (default 5, maximum 10). Includes the less-used native tools that are not listed and every deferred MCP capability. Use call_tool to invoke a result. The catalog follows live MCP updates and your own agent scope.',
});

/** `catalog(sessionId)` (step 24.1a, design-steward 7.8.7): for a role with
 *  `catalog: "host"`, the Set of tool names this turn may find and call beyond the
 *  presented ones (undefined: none yet). Such a role searches only its catalog, and on
 *  its host a call from a session that is not its root - a missing session included,
 *  which roleOf would read as a worker - is refused for search, call and any MCP tool. */
export function installToolDiscovery(ctx, { defineTool, HarnessError, rootSession, rootRole = 'director', roles, texts = {}, catalog }) {
  const wording = { ...defaultDiscoveryTexts, ...Object.fromEntries(Object.entries(texts).filter(([, v]) => v !== undefined)) };
  const roleFor = sessionId => roles[roleOf(sessionId, rootSession, rootRole)];
  const hostCatalog = roles[rootRole]?.catalog === 'host';
  const catalogFor = sessionId => (sessionId === rootSession ? catalog?.(sessionId) : undefined) ?? new Set();
  // Keyed on the caller's session, not on a prompt. A global guard covers both
  // call_tool targets and a direct call of the (merely unlisted) name.
  ctx.tools.guard(exec => {
    const session = exec.agent?.session?.id;
    if (hostCatalog && session !== rootSession && (metaTools.has(exec.name) || exec.name.startsWith('mcp__')))
      return `${exec.name} is not available outside the ${rootRole}'s own session`;
    const role = roleFor(session);
    return refusal(role, exec, role.catalog ? catalogFor(session) : undefined);
  });
  // Presentation only: restrictions would also deny execution, and root-local
  // registrations would miss fresh native children. Resolve each caller anew.
  ctx.on('system-prompt/assemble', async (_assembly, context, next) => {
    const assembly = await next();
    // DSH assembles with { agent, scope: agent }; the scope alone also names the caller.
    const caller = context?.agent ?? context?.scope;
    const role = roleFor(caller?.session?.id);
    // A host-catalog role whose host gives no catalog (the standalone local host) has
    // nothing to discover: search_tools and call_tool are not shown to it at all.
    const tools = presentTools(assembly.tools, role).filter(t => !(role.catalog && !catalog && metaTools.has(t.name)));
    return { ...assembly, tools, sections: presentSections(assembly.sections, role, wording.sectionName, tools.map(t => t.name)) };
  });
  ctx.systemPrompt.section({ name: wording.sectionName, order: 3090, interpolate: false, text: wording.section });
  ctx.tools.register(defineTool({ name: 'search_tools',
    description: wording.searchDescription,
    parameters: { query: { type: 'string', required: true }, limit: { type: 'integer', description: '1–10; defaults to 5' } },
    output: { schema: { type: 'json' }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
    isConcurrencySafe: () => true,
    async execute({ query, limit }, exec) {
      const session = exec.agent?.session?.id;
      const schemas = ctx.tools.schemas(exec.agent);
      if (!roleFor(session).catalog) return searchTools(schemas, query, limit);
      // A host-catalog role finds only this turn's catalog: never an unlisted native tool,
      // another connection, or a connection this conversation may not use.
      const allowed = catalogFor(session);
      return searchTools(schemas.filter(s => allowed.has(s.name)), query, limit);
    },
  }));
  const outcomes = new Map();
  ctx.tools.register(defineTool({ name: 'call_tool',
    description: 'Invoke an available tool by its exact name and JSON arguments, including deferred MCP tools found with search_tools. Uses your own agent identity and the original validation, permissions, cancellation and result handling. A known name can be called directly. Cannot target search_tools, call_tool or run_code.',
    parameters: { name: { type: 'string', required: true }, arguments: { type: 'object', additionalProperties: true, required: true } },
    output: { schema: { type: 'json' }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
    async execute({ name, arguments: args }, exec) {
      if (metaTools.has(name)) throw new Error('call_tool must target an end tool, not another discovery/execution transport');
      const result = await ctx.tools.execute({ name, arguments: args, agent: exec.agent,
        callId: exec.callId + ':call_tool', rootCallId: exec.rootCallId, parent: exec.token, signal: exec.signal });
      outcomes.set(exec.token, result);
      for (const context of result.additionalContexts ?? []) exec.deferContext(context);
      if (result.isError) {
        const error = new HarnessError(result.error.message, result.error.info?.code ?? 'TOOL_ERROR');
        error.name = result.error.info?.name ?? 'ToolError';
        throw error;
      }
      if (result.concludesTurn) exec.concludeTurn();
      return result.value;
    },
    // Preserve text/images rather than stringify an MCP envelope or native value.
    // Later result policies still own redaction/blocking of this outer call.
    projectContent(exec) { return outcomes.get(exec.token)?.content; },
    finalizeContent(exec) { outcomes.delete(exec.token); },
  }));
}
