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

/** Reasoning efforts the completion bridge accepts. */
export const REASONING_EFFORTS = ['off', 'low', 'high', 'max'];

/** Retry budget for one completion call, in milliseconds, when nothing overrides it. */
const MODEL_RETRY_DEFAULT_MS = 180000;
const RETRY_DELAY_MS = 500;
const RETRY_MAX_DELAY_MS = 8000;
/** Answered responses that mean "nothing ran here": the backend was replaced
 *  mid-request, so the same completion can be asked for again. */
const RETRYABLE_STATUSES = new Set([502, 503, 504]);

/** The wall-clock budget a completion call may spend retrying a connection-level
 *  failure. `AITELIER_DSH_MODEL_RETRY_SEC` is seconds and may be fractional;
 *  unset, non-numeric or negative falls back to the 180 s default, and `0`
 *  disables the retry entirely (one request, exactly as before). */
export function modelRetryBudgetMs(raw = process.env.AITELIER_DSH_MODEL_RETRY_SEC) {
  if (raw === undefined || raw === null) return MODEL_RETRY_DEFAULT_MS;
  const text = String(raw).trim();
  if (text === '') return MODEL_RETRY_DEFAULT_MS;
  const seconds = Number(text);
  if (!Number.isFinite(seconds) || seconds < 0) return MODEL_RETRY_DEFAULT_MS;
  return seconds * 1000;
}

/** The failure class worth retrying, or null when this error must surface at
 *  once. A retry is only safe because the whole response is buffered before
 *  anything is yielded: no block has been delivered, so a repeat can never
 *  duplicate a tool call. Anything the backend actually ANSWERED with a verdict
 *  (400/401/429/...), an abort, or an undecodable body is that verdict. */
function retryableClass(error, signal) {
  if (!error || signal?.aborted) return null;
  if (error.name === 'AbortError') return null;
  if (error instanceof SyntaxError || error.name === 'SyntaxError') return null;
  if (typeof error.status === 'number') return RETRYABLE_STATUSES.has(error.status) ? `HTTP ${error.status}` : null;
  if (error instanceof TypeError || error.name === 'TypeError') {
    return `connection-level (${error.cause?.code ?? error.cause?.name ?? error.message})`;
  }
  return null;
}

function abortError(signal) {
  const reason = signal?.reason;
  if (reason instanceof Error) return reason;
  const error = new Error('This operation was aborted');
  error.name = 'AbortError';
  error.cause = reason;
  return error;
}

/** Wait `ms`, or reject the moment the caller aborts - a retry must never hold
 *  a cancelled turn open. */
function sleepFor(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) { reject(abortError(signal)); return; }
    const aborted = () => { clearTimeout(timer); reject(abortError(signal)); };
    const timer = setTimeout(() => { signal?.removeEventListener('abort', aborted); resolve(); }, ms);
    signal?.addEventListener('abort', aborted, { once: true });
  });
}

/** One completion request, retried inside a single wall-clock budget. Backoff
 *  starts at `delayMs` and doubles up to `maxDelayMs`; the pause is clipped to
 *  whatever the budget has left, so no retry is ever started past the deadline
 *  and the last one waits out the remaining budget rather than overrunning it. */
async function completionWithRetry(request, path, body, signal, { budgetMs, delayMs, maxDelayMs, log }) {
  const deadline = Date.now() + budgetMs;
  let attempt = 0, wait = delayMs;
  for (;;) {
    attempt += 1;
    try {
      return await request(path, body, signal);
    } catch (error) {
      const failure = retryableClass(error, signal);
      if (!failure) throw error;
      const pause = Math.min(wait, maxDelayMs, deadline - Date.now());
      if (pause <= 0) throw error;
      log(`dsh completion attempt ${attempt} failed (${failure}); retrying in ${pause}ms with ${deadline - Date.now()}ms of the retry budget left`);
      await sleepFor(pause, signal);
      if (Date.now() >= deadline) throw error;
      wait = Math.min(wait * 2, maxDelayMs);
    }
  }
}

/** The root director's default effort: the configured one when valid, else max. */
export const rootReasoningEffort = value => REASONING_EFFORTS.includes(value) ? value : 'max';

/** Root completions default to rootEffort (the director plans and judges); native
 *  children default to high. An explicit DSH reasoningEffort is passed through. */
export function installHostModel(ctx, { LlmAdapter }, { request, project, session, rootEffort = 'max', retry = {} }) {
  const retryOptions = { budgetMs: retry.budgetMs ?? modelRetryBudgetMs(), delayMs: retry.delayMs ?? RETRY_DELAY_MS,
    maxDelayMs: retry.maxDelayMs ?? RETRY_MAX_DELAY_MS, log: retry.log ?? (() => {}) };
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
      // One signal for the whole call: a retry lives inside the same 30-minute
      // bound as the attempt it replaces, not a fresh one.
      const signal = options.signal ?? AbortSignal.timeout(1800000);
      const result = await completionWithRetry(request, `/api/dsh-drivers/${encodeURIComponent(project)}/completion`, {
        session_id: options.sessionId ?? session,
        messages: openaiMessages(options, (ref) => imageUrls.get(ref?.attachmentId ?? ref?.id)),
        tools: options.tools?.map(t => ({ type: 'function', function: { name: t.name, description: t.description, parameters: t.parameters } })),
        max_tokens: options.maxTokens ?? 32768,
        reasoning_effort: options.reasoningEffort ?? ((options.sessionId ?? session) === session ? rootEffort : 'high'),
      }, signal, retryOptions);
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
