/** Native DSH adapter over the host's existing sticky model routes/failover. */
export function openaiMessages(options) {
  const messages = options.system ? [{ role: 'system', content: options.system }] : [];
  for (const message of options.messages) {
    const text = [], reasoning = [], calls = [];
    for (const block of message.content) {
      if (block.type === 'text') text.push(block.text);
      else if (block.type === 'reasoning') reasoning.push(block.text);
      else if (block.type === 'tool-call') calls.push({ id: block.id, type: 'function', function: { name: block.name, arguments: block.arguments } });
      else if (block.type === 'file' || block.type === 'image') {
        const ref = block.attachment;
        text.push(`[${block.type}: ${ref.name ?? ref.id}; use the native file tools to inspect this attachment]`);
      } else throw new Error(`Unsupported driver message block: ${block.type}`);
    }
    if (message.role === 'developer') throw new Error('Driver route does not support incremental tool declarations');
    messages.push({ role: message.role, content: text.join('\n'),
      ...(reasoning.length ? { reasoning_content: reasoning.join('\n') } : {}),
      ...(calls.length ? { tool_calls: calls } : {}),
      ...(message.role === 'tool' ? { tool_call_id: message.toolCallId } : {}),
    });
  }
  return messages;
}

export function installHostModel(ctx, { LlmAdapter }, { request, project, session }) {
  class HostAdapter extends LlmAdapter {
    async resolveModel(provider, model) {
      return { provider, id: model, name: model, context: { contextWindow: 1000000 }, defaultMaxTokens: 32768 };
    }
    async *stream(options) {
      const result = await request(`/api/dsh-drivers/${encodeURIComponent(project)}/completion`, {
        session_id: options.sessionId ?? session, messages: openaiMessages(options),
        tools: options.tools?.map(t => ({ type: 'function', function: { name: t.name, description: t.description, parameters: t.parameters } })),
        max_tokens: options.maxTokens ?? 32768, reasoning_effort: options.reasoningEffort ?? 'high',
      }, options.signal ?? AbortSignal.timeout(1800000));
      const blocks = [];
      if (result.reasoning_content) blocks.push({ type: 'reasoning', text: result.reasoning_content });
      if (result.text) blocks.push({ type: 'text', text: result.text });
      for (const call of result.tool_calls) blocks.push({ type: 'tool-call', id: call.id, name: call.function.name, arguments: call.function.arguments });
      for (const [index, block] of blocks.entries()) {
        yield { type: 'block-start', index, blockType: block.type };
        if (block.type === 'tool-call') yield { type: 'tool-call-delta', index, id: block.id, name: block.name, argumentsDelta: block.arguments };
        else yield { type: block.type === 'reasoning' ? 'reasoning-delta' : 'text-delta', index, text: block.text };
        yield { type: 'block-end', index, block };
      }
      const usage = result.usage;
      yield { type: 'usage', usage: { inputTokens: Math.max(0, (usage.prompt_tokens ?? 0) - (usage.cache_hit_tokens ?? 0)),
        outputTokens: usage.completion_tokens ?? 0, ...(usage.cache_hit_tokens == null ? {} : { cacheReadTokens: usage.cache_hit_tokens }) } };
      yield { type: 'finish', reason: { kind: result.truncated ? 'max-tokens' : result.tool_calls.length ? 'tool-calls' : 'stop' } };
    }
  }
  ctx.llm.registerAdapter(['aitelier'], new HostAdapter());
}
