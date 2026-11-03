/** Native DSH adapter over the host's existing sticky model routes/failover. */
export function openaiMessages(options, images = null) {
  const messages = options.system ? [{ role: 'system', content: options.system }] : [];
  for (const message of options.messages) {
    const text = [], reasoning = [], calls = [], parts = [];
    for (const block of message.content) {
      if (block.type === 'text') text.push(block.text);
      else if (block.type === 'reasoning') reasoning.push(block.text);
      else if (block.type === 'tool-call') calls.push({ id: block.id, type: 'function', function: { name: block.name, arguments: block.arguments } });
      else if (block.type === 'image') {
        if (!images) throw new Error('driver bridge cannot forward an image without the durable attachment service');
        const url = images(block.attachment);
        if (!url) throw new Error(`driver bridge cannot forward image attachment ${block.attachment?.attachmentId ?? '?'}: no request bytes`);
        parts.push({ type: 'image_url', image_url: { url } });
      } else if (block.type === 'file') {
        const ref = block.attachment;
        text.push(`[file: ${ref.name ?? ref.id}; use the native file tools to inspect this attachment]`);
      } else throw new Error(`Unsupported driver message block: ${block.type}`);
    }
    if (message.role === 'developer') throw new Error('Driver route does not support incremental tool declarations');
    const joined = text.join('\n');
    const content = parts.length ? [{ type: 'text', text: joined }, ...parts] : joined;
    messages.push({ role: message.role, content,
      ...(reasoning.length ? { reasoning_content: reasoning.join('\n') } : {}),
      ...(calls.length ? { tool_calls: calls } : {}),
      ...(message.role === 'tool' ? { tool_call_id: message.toolCallId } : {}),
    });
  }
  return messages;
}


/** Resolve every image block in this turn to a data URL through the durable
 *  attachment service. The adapter cannot read bytes itself, and a silent
 *  downgrade to a text placeholder would tell the model a picture exists while
 *  never sending it — so an unresolvable image is a loud error, not a hint. */
async function requestImageUrls(ctx, options, signal) {
  const refs = [];
  for (const message of options.messages) for (const block of message.content) {
    if (block?.type === 'image' && block.attachment) refs.push(block.attachment);
  }
  if (!refs.length) return new Map();
  const attachments = ctx.get('attachments');
  if (!attachments) throw new Error('driver bridge cannot forward images: the durable attachment service is not mounted');
  const urls = new Map();
  for (const ref of refs) {
    const info = await attachments.readImageRequest(ref,
      { width: 2048, height: 2048, maxBytes: 4 * 1024 * 1024 }, signal);
    urls.set(ref.attachmentId ?? ref.id,
      `data:${info.mediaType};base64,${Buffer.from(info.data).toString('base64')}`);
  }
  return urls;
}

export function installHostModel(ctx, { LlmAdapter }, { request, project, session }) {
  class HostAdapter extends LlmAdapter {
    async resolveModel(provider, model) {
      return { provider, id: model, name: model, context: { contextWindow: 1000000 }, defaultMaxTokens: 32768,
        // The aitelier route forwards OpenAI-style image_url parts to litellm, and the
        // host model behind it answers them (verified 2026-11-03: a 960x704 frame was
        // read as its on-screen heading). Without this the file tool refuses read_image.
        inputModalities: ['text', 'image'] };
    }
    async *stream(options) {
      const imageUrls = await requestImageUrls(ctx, options, options.signal);
      const result = await request(`/api/dsh-drivers/${encodeURIComponent(project)}/completion`, {
        session_id: options.sessionId ?? session,
        messages: openaiMessages(options, (ref) => imageUrls.get(ref?.attachmentId ?? ref?.id)),
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
