/** Keep MCP schemas in the registry, outside ordinary model requests. */
import { roleOf } from './roles.mjs';

const metaTools = new Set(['search_tools', 'call_tool', 'run_code']);

/** The request's tool list for one session: the tools its role presents. Everything
 *  else stays registered and callable - through call_tool, or directly by a known
 *  name - and is found with search_tools; only the role's `forbidden` tools are
 *  refused (see refusal). Which names a role presents is roles.json's business
 *  (chosen from measured use, 2026-11-07..14), extended by the adapter
 *  (aitelier-roles.mjs), never a list in this file. A tool missing there is
 *  deferred, never lost. */
export function presentTools(tools, role) {
  const presented = new Set(role.presented);
  return tools.filter(t => presented.has(t.name));
}

/** Why `role` may not run this call, or undefined. Refused: every tool the role
 *  forbids, and - for a tool listed in `allowed_actions` - any call whose `action`
 *  argument is not on that tool's list (a missing or non-string action included).
 *  `notes` adds the adapter's reason for one tool. */
export function refusal(role, exec) {
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
export function installToolDiscovery(ctx, { defineTool, HarnessError, rootSession, roles }) {
  const roleFor = sessionId => roles[roleOf(sessionId, rootSession)];
  // Keyed on the caller's session, not on a prompt. A global guard covers both
  // call_tool targets and a direct call of the (merely unlisted) name.
  ctx.tools.guard(exec => refusal(roleFor(exec.agent?.session?.id), exec));
  // Presentation only: restrictions would also deny execution, and root-local
  // registrations would miss fresh native children. Resolve each caller anew.
  ctx.on('system-prompt/assemble', async (_assembly, context, next) => {
    const assembly = await next();
    // DSH assembles with { agent, scope: agent }; the scope alone also names the caller.
    const caller = context?.agent ?? context?.scope;
    return { ...assembly, tools: presentTools(assembly.tools, roleFor(caller?.session?.id)) };
  });
  ctx.systemPrompt.section({ name: 'aitelier:tool-discovery', order: 3090, interpolate: false,
    text: 'MCP capabilities are available through search_tools(query) and call_tool(name, arguments). Search names/descriptions to retrieve only the relevant full parameter schemas; then use call_tool with the exact name and arguments. For AgentMCP image, OCR, PDF, audio or video work, for example search_tools({query:"agentmcp image",limit:3}), then call_tool({name:"<exact returned tool name>",arguments:{/* fields from its returned schema */}}). Search again after compaction if you need an earlier schema. Known names may be called without searching first. The everyday coding and coordination tools are listed directly; less-used ones (glob, web_fetch, web_search, interrupt_agent, goals, MCP resources and the rest) are not listed but are found with search_tools and called the same way. Discovery does not change your existing permissions.' });
  ctx.tools.register(defineTool({ name: 'search_tools',
    description: 'Find available tools by name or description and return their parameter schemas (default 5, maximum 10). Includes the less-used native tools that are not listed (glob, web_fetch, web_search, interrupt_agent, goals, MCP resources) and the deferred MCP capabilities: State writes, pipelines, provider management, AgentMCP images/OCR/PDF/audio/video, devmcp and zvec. Use call_tool to invoke a result. The catalog follows live MCP updates and your own agent scope.',
    parameters: { query: { type: 'string', required: true }, limit: { type: 'integer', description: '1–10; defaults to 5' } },
    output: { schema: { type: 'json' }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
    isConcurrencySafe: () => true,
    async execute({ query, limit }, exec) { return searchTools(ctx.tools.schemas(exec.agent), query, limit); },
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
