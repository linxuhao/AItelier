/** Keep MCP schemas in the registry, outside ordinary model requests. */
const metaTools = new Set(['search_tools', 'call_tool', 'run_code']);

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

export function installToolDiscovery(ctx, { defineTool, HarnessError }) {
  // Presentation only: restrictions would also deny execution, and root-local
  // registrations would miss fresh native children. Resolve each caller anew.
  ctx.on('system-prompt/assemble', async (_assembly, _context, next) => {
    const assembly = await next();
    return { ...assembly, tools: assembly.tools.filter(t => !t.name.startsWith('mcp__')) };
  });
  ctx.systemPrompt.section({ name: 'aitelier:tool-discovery', order: 3090, interpolate: false,
    text: 'MCP capabilities are available through search_tools(query) and call_tool(name, arguments). Search names/descriptions to retrieve only the relevant full parameter schemas; then use call_tool with the exact name and arguments. For AgentMCP image, OCR, PDF, audio or video work, for example search_tools({query:"agentmcp image",limit:3}), then call_tool({name:"<exact returned tool name>",arguments:{/* fields from its returned schema */}}). Search again after compaction if you need an earlier schema. Known names may be called without searching first. Native coding, coordination and wait tools remain directly callable. Discovery does not change your existing permissions.' });
  ctx.tools.register(defineTool({ name: 'search_tools',
    description: 'Find available tools by name or description and return their parameter schemas (default 5, maximum 10). Includes deferred MCP capabilities: State writes, pipelines, provider management, AgentMCP images/OCR/PDF/audio/video, devmcp and zvec. Use call_tool to invoke a result. The catalog follows live MCP updates and your own agent scope.',
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
