/** One tool call made by a tool: call_tool, run_confirmed and media_call run their target this way.
 *  Since DSH 0.2.2, ctx.tools.execute runs a model step's whole batch; a nested call goes through
 *  the shared pipeline's three stages as DSH's own run_code does: prepare (the call's policy and
 *  every guard), dispatch (the tool body), complete (post-execute and the materialized result,
 *  { isError, value, content, error, additionalContexts, concludesTurn }).
 *  `ToolExecutionPipeline` is @deepseek-ai/dsh-tools/pipeline's, from the DSH that runs the plugin. */
export function nestedToolCaller(ctx, ToolExecutionPipeline) {
  const pipeline = new ToolExecutionPipeline(ctx);
  return async input => {
    const prepared = await pipeline.prepare(input);
    if (prepared.kind !== 'dispatch') return pipeline.complete(prepared.exec, prepared);
    const dispatched = await pipeline.dispatch(prepared.exec);
    return pipeline.complete(prepared.exec, { ...dispatched, exec: prepared.exec });
  };
}
