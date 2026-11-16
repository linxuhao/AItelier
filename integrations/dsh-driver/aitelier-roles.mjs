/** AItelier's additions to the generic role table (design D3: the effective table
 *  is the repository roles.json plus these). They name State tools, so they live in
 *  the adapter, never in roles.json.
 *
 *  State writes from a worker: in the real aitelier MCP server almost every write
 *  is an `action` of ONE tool, state_graph_write. A worker may use only the
 *  allowlisted actions; everything else (and a missing/non-string action) is the
 *  director's. Two writes are separate tools and are the director's too:
 *  declare_driver_presence (marking the director offline lets others take over its
 *  claims) and answer_checkpoint (a human decision). The judging writes are also
 *  refused as bare tool names in case a server ever exposes them. */
const stateWriteTool = 'mcp__aitelier__state_graph_write';
const workerStateActions = ['report_issue'];
const directorOnlyStateTools = ['declare_driver_presence', 'answer_checkpoint', 'record_evidence', 'verify_node',
  'report_external_attempt', 'start_external_attempt', 'claim_node'].map(name => 'mcp__aitelier__' + name);
/** The adapter's own director tools (runtime.mjs registers them; plugin step 4 moved
 *  them out of the generic roles.json): presented to the director, and the user
 *  message is the director's alone. */
const adapterDirectorTools = ['state_read', 'send_user_message'];
const adapterDirectorOnly = ['send_user_message'];
export const workerJudgeRule = 'workers do not judge their own work; the director records State evidence'
  + ` (a subagent may only state_graph_write ${workerStateActions.join(' or ')})`;

/** The effective table: `table` (a validated roles.json) plus AItelier's additions.
 *  - director: its most-used write, state_graph_write, is presented (~8.3k calls in a
 *    measured week went through call_tool, plus ~500 searches to find it);
 *  - worker: the separate director-only State tools are forbidden, and
 *    state_graph_write is open only for the allowlisted actions.
 *  Returns a new object; `table` is not modified. */
export function roles(table) {
  const notes = Object.fromEntries([stateWriteTool, ...directorOnlyStateTools].map(name => [name, workerJudgeRule]));
  return { ...table,
    director: { ...table.director, presented: [...table.director.presented, ...adapterDirectorTools, stateWriteTool] },
    worker: { ...table.worker, forbidden: [...table.worker.forbidden, ...adapterDirectorOnly, ...directorOnlyStateTools],
      allowed_actions: { [stateWriteTool]: workerStateActions }, notes } };
}
