/** Native DSH adapter over the host's existing sticky model routes/failover. */
/** Step 24.2b (design-steward 7.8.12a "不收图的路线"): the one line an image block becomes on a
 *  session whose route does not take images. Nothing of the image is read or sent. */
export const noImageLine = ref => `[图片 ${ref?.name || ref?.attachmentId || ref?.id || '?'}：本路线不收图，未发送]`;

/** Step 24.2c (design-steward 7.8.12a, 7.8.12b "八"): the line a file block becomes. A root role
 *  that may not read files (the steward forbids `read`) is told the truth: the file cannot be
 *  opened in that session, and a copy sent through the chat becomes a media ref read_document
 *  reads. Every other session keeps the native-file-tools line. */
export const fileLine = (ref, fileTools = true) => fileTools
  ? `[file: ${ref.name ?? ref.id}; use the native file tools to inspect this attachment]`
  : `[file: ${ref.name ?? ref.id}; files cannot be opened in this session - ask for it to be sent again through the chat, then read that media ref with read_document]`;

/** `images` resolves an image block to its request URL. An image block stays an image_url part of
 *  whatever role its message has, a role:'tool' result included (read_document's page images):
 *  measured 2026-11-23 (design-steward 7.8.12b step 0 #2, one call each through AIGateway with
 *  /completion's settings), ollamacloud, opencodego2 and opencodego3 deepseek-v4.1-flash and
 *  deepseek/deepseek-flash all read digits from an image in a tool message; opencodego was over
 *  its monthly quota, so unmeasured. `textOnly` is a session whose route does
 *  not take images: every image block becomes noImageLine instead, and nothing throws. An image
 *  block DSH marked `offloaded` is skipped: never read, never sent (7.8.12b "八" 4).
 *  `fileTools: false` is a session that cannot open files (fileLine). */
export function openaiMessages(options, images = null, { textOnly = false, fileTools = true } = {}) {
  const messages = options.system ? [{ role: 'system', content: options.system }] : [];
  for (const message of options.messages) {
    const text = [], reasoning = [], calls = [], parts = [];
    for (const block of message.content) {
      if (block.type === 'text') text.push(block.text);
      else if (block.type === 'reasoning') reasoning.push(block.text);
      else if (block.type === 'tool-call') calls.push({ id: block.id, type: 'function', function: { name: block.name, arguments: block.arguments } });
      else if (block.type === 'image' && block.offloaded === true) continue;
      else if (block.type === 'image' && textOnly) text.push(noImageLine(block.attachment));
      else if (block.type === 'image') {
        if (!images) throw new Error('driver bridge cannot forward an image without the durable attachment service');
        const url = images(block.attachment);
        if (!url) throw new Error(`driver bridge cannot forward image attachment ${block.attachment?.attachmentId ?? '?'}: no request bytes`);
        parts.push({ type: 'image_url', image_url: { url } });
      } else if (block.type === 'file') text.push(fileLine(block.attachment, fileTools));
      else throw new Error(`Unsupported driver message block: ${block.type}`);
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


/** The largest image one request carries, in raw bytes (readImageRequest's `maxBytes`). */
export const REQUEST_IMAGE_MAX_BYTES = 4 * 1024 * 1024;
/** Base64 length of raw bytes, padding included (DSH dsh-llm `base64Length`). */
const base64Length = bytes => Math.ceil(bytes / 3) * 4;

/** Resolve every image block in this turn to a data URL through the durable
 *  attachment service. The adapter cannot read bytes itself, and a silent
 *  downgrade to a text placeholder would tell the model a picture exists while
 *  never sending it — so an unresolvable image is a loud error, not a hint.
 *  An `offloaded` block is not read (7.8.12b "八" 4). */
async function requestImageUrls(ctx, options, signal) {
  const refs = [];
  for (const message of options.messages) for (const block of message.content) {
    if (block?.type === 'image' && block.attachment && block.offloaded !== true) refs.push(block.attachment);
  }
  if (!refs.length) return new Map();
  const attachments = ctx.get('attachments');
  if (!attachments) throw new Error('driver bridge cannot forward images: the durable attachment service is not mounted');
  const urls = new Map();
  for (const ref of refs) {
    const info = await attachments.readImageRequest(ref,
      { width: 2048, height: 2048, maxBytes: REQUEST_IMAGE_MAX_BYTES }, signal);
    urls.set(ref.attachmentId ?? ref.id,
      `data:${info.mediaType};base64,${Buffer.from(info.data).toString('base64')}`);
  }
  return urls;
}

/** Step 24.2b (design-steward 7.8.12a "路线能力"): what a route may be sent when nothing says more. */
const TEXT_ONLY = Object.freeze({ root: Object.freeze(['text']), child: Object.freeze(['text']) });
const takesImage = list => Array.isArray(list) && list.includes('image');
/** `measured: false`: the backend's route table has no `_input` yet, so nothing was measured
 *  (OPS-1). Image input then stays as it was before 24.2b - declared, and sent as image_url - and
 *  only the new wake images stay placeholder lines. */
const unmeasured = routes => routes?.measured === false;
/** What resolveModel declares (7.8.12a "路线能力"): DSH does not say which session asks, so image
 *  input only when the root's route AND the children's route both take images - or nothing was
 *  measured yet. */
export const declaresImage = routes => unmeasured(routes) || (takesImage(routes?.root) && takesImage(routes?.child));
/** Whether a wake carries its images as native blocks: the same intersection resolveModel declares
 *  (DSH replaces every image block with a text line before stream() when the model is declared
 *  text-only, so a block on a mixed route would arrive as neither image nor media_call hint), and
 *  never before the routes were measured. */
export const wakeTakesImages = routes => !unmeasured(routes) && declaresImage(routes);

/** The dashboard flag of the steward's standing route check (design-steward 7.8.12b "九"). */
export const STEWARD_ROUTE_FLAG = '管家路线不收图';

/** Step 24.2c (7.8.12b "九", 上线后常驻的检查): the steward process's route-input, checked on
 *  every read (every pre-step read goes through it). An answer with `measured: false`, or a root
 *  or child side without `image`, writes one log line - again only when the reason changes or
 *  `everyMs` (10 min) has passed - and raises the dashboard flag through `flag(text)`; an answer
 *  that takes images again clears it with `flag(undefined)`. The flag changes only on a change,
 *  so a step writes nothing. The answer itself is passed on untouched. */
export function routeStandingCheck(routeInput, { log = () => {}, flag = () => {}, now = Date.now, everyMs = 600000 } = {}) {
  let logged, loggedAt = -Infinity, flagged;
  return async function checkedRouteInput(signal) {
    const routes = await routeInput(signal);
    const reasons = [...(unmeasured(routes) ? ['measured:false'] : []),
      ...(takesImage(routes?.root) ? [] : ['root text-only']), ...(takesImage(routes?.child) ? [] : ['child text-only'])];
    const reason = reasons.join(', ');
    if (reason && (reason !== logged || now() - loggedAt >= everyMs)) {
      logged = reason; loggedAt = now();
      log(`steward route takes no images (${reason}): photos and PDF pages reach it as text lines only`);
    }
    if (reason !== (flagged ?? '')) {
      flagged = reason || undefined;
      if (!reason) { logged = undefined; log('steward route takes images again'); }
      flag(reason ? `${STEWARD_ROUTE_FLAG}（${reason}）` : undefined);
    }
    return routes;
  };
}

/** The backend's `GET /api/dsh-drivers/{project}/route-input` ({root, child}), read at most once
 *  per `ttlMs` (60 s) and shared by every session of this process. `read(signal)` is the request.
 *  A read that fails answers the last good value, or text-only when there is none, and is tried
 *  again on the next call: an unknown route never takes images. */
export function routeInputCache(read, { ttlMs = 60000, now = Date.now, log = () => {} } = {}) {
  let value = null, at = -Infinity, pending = null;
  return function routeInput(signal) {
    if (value && now() - at < ttlMs) return Promise.resolve(value);
    pending ??= (async () => {
      try {
        const answer = await read(signal);
        value = Object.freeze({ root: takesImage(answer?.root) ? ['text', 'image'] : ['text'],
          child: takesImage(answer?.child) ? ['text', 'image'] : ['text'], ...(unmeasured(answer) ? { measured: false } : {}) });
        at = now();
        return value;
      } catch (error) {
        log(`route-input read failed (${String(error?.message ?? error).slice(0, 200)}); ${value ? 'keeping the last answer' : 'treating both routes as text-only'}`);
        return value ?? TEXT_ONLY;
      } finally { pending = null; }
    })();
    return pending;
  };
}

// ---- Step 24.2c R-M3 (design-steward 7.8.12b "八" 4): only images the model has seen are offloaded ----

/** DSH dsh-llm `offloadedImagePrefixCount` (lib/index.js:741-759, 0.2.2-rc.5; unchanged since 0.2.1-rc.5), copied verbatim
 *  because DSH does not export it; a test compares it with DSH `requiredImageOffload`. */
export function offloadedImagePrefixCount(lengths, budget) {
  const total = lengths.reduce((sum, bytes) => sum + bytes, 0);
  const excessCount = budget.maxImages === void 0 ? 0 : Math.max(0, lengths.length - budget.maxImages);
  const excessBytes = budget.maxBytes === void 0 ? 0 : Math.max(0, total - budget.maxBytes);
  if (excessCount === 0 && excessBytes === 0) return 0;
  const countQuantum = budget.countQuantum ?? 1;
  const byteQuantum = budget.byteQuantum ?? 1;
  const removeCount = excessCount === 0 ? 0 : Math.ceil(excessCount / countQuantum) * countQuantum;
  const removeBytes = excessBytes === 0 ? 0 : Math.ceil(excessBytes / byteQuantum) * byteQuantum;
  let count = 0;
  let removedBytes = 0;
  for (const imageBytes of lengths) {
    if (count >= removeCount && (removeBytes === 0 || (byteQuantum === 1 ? removedBytes >= removeBytes : removedBytes > removeBytes))) break;
    removedBytes += imageBytes;
    count += 1;
  }
  return count;
}

/** The line an offloaded image becomes, from its attachment name alone, so every generation is
 *  byte-identical (prefix cache). A page image (`<ref>#p<n>.jpg`) and a library image
 *  (`<ref>.<ext>`) say how to look again; anything else says it cannot be seen again. */
export function offloadedImagePlaceholder(ref) {
  const name = typeof ref?.name === 'string' ? ref.name : '';
  const page = /^(m_[0-9a-f]+)#p(\d+)\.jpg$/.exec(name);
  if (page) return `[第 ${page[2]} 页图已移出上下文（文字仍在上面）；要再看请 read_document(ref=${page[1]}, pages=${page[2]})]`;
  const image = /^(m_[0-9a-f]+)\.(png|jpe?g|gif|webp)$/.exec(name);
  if (image) return `[图片 ${image[1]} 已移出上下文；要再看请 read_document(ref=${image[1]})]`;
  return `[一张图已移出上下文（${name || ref?.attachmentId || '?'}），不能再看]`;
}

/** The base64 length of every image the model has already seen: the image blocks of the `user`
 *  and `tool` messages before the last `assistant` message (the same two kinds DSH
 *  `offloadOldestImages` walks), measured on the data URL actually sent. Everything after that
 *  assistant message is the latest input and never counts. */
function seenImageLengths(messages, urls) {
  let last = -1;
  messages.forEach((message, index) => { if (message.role === 'assistant') last = index; });
  const lengths = [];
  for (const message of messages.slice(0, Math.max(0, last))) {
    if (message.role !== 'user' && message.role !== 'tool') continue;
    for (const block of message.content) {
      if (block?.type !== 'image' || block.offloaded === true) continue;
      const url = urls.get(block.attachment?.attachmentId ?? block.attachment?.id);
      lengths.push(url.length - url.indexOf(',') - 1);
    }
  }
  return lengths;
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

/** The transport failures a completion is retried over — the documented set,
 *  exactly. `ECONNREFUSED` `ECONNRESET` `EPIPE` `UND_ERR_SOCKET`
 *
 *  Kept as data, and the classifier reads this data, so the set cannot drift
 *  from the sentence above it. Being an undici `TypeError` is NOT by itself a
 *  reason to retry: a `TypeError` whose cause is a malformed body — an
 *  undecodable payload, an invalid header, a body that cannot be serialised —
 *  is a verdict about the request, and repeating it only asks the backend the
 *  same unanswerable question again. Only a known transport code, or one of the
 *  half-close shapes below, can mean "the connection died, the request may be
 *  replayable". */
export const RETRYABLE_TRANSPORT_CODES = Object.freeze(['ECONNREFUSED', 'ECONNRESET', 'EPIPE', 'UND_ERR_SOCKET']);

/** The undici messages that name a reset or a half-close on an already sent
 *  request, kept as data and documented shapes: `socket hang up` `terminated`.
 *  Matched as the whole message or as its prefix (`terminated: other side
 *  closed`), never as a substring, so prose about a body cannot be mistaken for
 *  a dead connection. */
export const RETRYABLE_TRANSPORT_SHAPES = Object.freeze(['socket hang up', 'terminated']);

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

/** The transport message that names a reset or half-close, or null. */
function transportShape(error) {
  for (const text of [error?.message, error?.cause?.message]) {
    if (typeof text !== 'string') continue;
    for (const shape of RETRYABLE_TRANSPORT_SHAPES) {
      if (text === shape || text.startsWith(`${shape} `) || text.startsWith(`${shape}:`)) return shape;
    }
  }
  return null;
}

/** The transport code on an error, from its undici cause or from itself. */
function transportCode(error) {
  if (typeof error?.cause?.code === 'string') return error.cause.code;
  if (typeof error?.code === 'string') return error.code;
  return null;
}

/** The failure class worth retrying, as the label the retry log carries, or
 *  null when this error must surface at once. A retry is only safe because the
 *  whole response is buffered before anything is yielded: no block has been
 *  delivered, so a repeat can never duplicate a tool call. Anything the backend
 *  actually ANSWERED with a verdict (400/401/429/...), an abort, or an
 *  undecodable body is that verdict — including a `TypeError` whose cause is a
 *  malformed body, which is why this looks at the transport CODE and at the
 *  documented half-close SHAPES rather than at `instanceof TypeError`.
 *
 *  `after_send` states whether the request bytes can already have reached the
 *  backend. `ECONNREFUSED` is the only retried failure where they cannot — no
 *  socket was ever established — so a refusal is labelled `after_send=false`.
 *  Every other retried failure happened on a connection that existed, and the
 *  request is written before a reset can be observed, so the backend may
 *  already have executed this completion (and billed it) while this process saw
 *  only a dead socket. Such a failure is still retried DELIBERATELY: a turn
 *  that died without an answer costs one repeated provider call, which is
 *  strictly cheaper than a fatal turn. The log line says `after_send=true` so
 *  that the possible second server-side execution is visible to an operator on
 *  stderr, rather than discoverable only in the bill. */
export function retryableClass(error) {
  if (!error) return null;
  if (error.name === 'AbortError') return null;
  if (error instanceof SyntaxError || error.name === 'SyntaxError') return null;
  if (typeof error.status === 'number') return RETRYABLE_STATUSES.has(error.status) ? `HTTP ${error.status}` : null;
  const code = transportCode(error);
  if (code && RETRYABLE_TRANSPORT_CODES.includes(code)) {
    return `connection-level ${code} after_send=${code !== 'ECONNREFUSED'}`;
  }
  const shape = transportShape(error);
  if (shape) return `connection-level ${shape} after_send=true`;
  return null;
}

/** The caller's cancellation, always as an error named `AbortError` — the name
 *  DSH and every caller already test for. A custom abort reason is kept as the
 *  cause instead of replacing the shape. */
function abortError(signal) {
  const reason = signal?.reason;
  if (reason instanceof Error && reason.name === 'AbortError') return reason;
  const error = new Error('This operation was aborted');
  error.name = 'AbortError';
  if (reason !== undefined) error.cause = reason;
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

/** Jitter bounds for one retry pause, as a fraction of the exponential base it
 *  is drawn from. A pause is never shorter than `min` times that base (so a
 *  retry cannot become a hot loop) and never longer than it (so the declared
 *  ceiling, `maxDelayMs`, stays a real ceiling). Both bounds are asserted by a
 *  witness, and the mapping is exact, not a clamp applied after the fact. */
export const RETRY_JITTER = Object.freeze({ min: 0.5, max: 1 });

/** The pause actually slept before a retry: `base` scaled by jitter inside
 *  RETRY_JITTER. A finite sample outside [0, 1] is treated as the top of the
 *  range so a broken random source can only ever lengthen a pause, never turn
 *  it into a spin. */
function jitteredPause(base, random) {
  const span = RETRY_JITTER.max - RETRY_JITTER.min;
  const sample = Number(random());
  const unit = Number.isFinite(sample) ? Math.min(1, Math.max(0, sample)) : 1;
  return Math.round(base * (RETRY_JITTER.min + span * unit));
}

/** One line per retried attempt on stderr. This is the default because the call
 *  site that matters (runtime.mjs `boot`) runs headless inside the driver
 *  process: a retry nobody can see is indistinguishable from a hang, and the
 *  retry's cost — a second provider call — is exactly what an operator needs to
 *  be able to read off stderr. The line states the attempt number, the failure
 *  class (`HTTP 503`, or `connection-level <code> after_send=<bool>`), the pause
 *  actually slept in ms and the budget left. */
export function stderrRetryLog(line) {
  process.stderr.write(`${line}\n`);
}

/** One completion request, retried inside a single wall-clock budget.
 *
 *  The budget starts at the FIRST FAILURE, not at the call: a slow but
 *  successful first attempt (20-minute completions are normal for the root
 *  director) must leave the whole window available to recover from a transport
 *  failure that happens later. Backoff starts at `delayMs`, doubles up to
 *  `maxDelayMs`, and every pause is jittered inside RETRY_JITTER. A retry is
 *  only started when the pause it needs fits inside the budget that is left, so
 *  the last failed attempt never sleeps first — a pause that would consume the
 *  remaining budget is skipped and the failure surfaces immediately.
 *
 *  `now`, `sleep`, `random` and `log` are injectable so the witnesses can pin
 *  the deadline, the pauses, the jitter bounds and the log lines on a fake
 *  clock instead of on wall-clock luck. */
async function completionWithRetry(request, path, body, signal, options) {
  const { budgetMs, delayMs, maxDelayMs, log, now = Date.now, sleep = sleepFor, random = Math.random } = options;
  let deadline = null, attempt = 0, wait = delayMs;
  for (;;) {
    attempt += 1;
    try {
      return await request(path, body, signal);
    } catch (error) {
      const failure = retryableClass(error);
      // An abort that coincides with a connection error is the owner cancelling
      // this turn, not a socket bug: the transport TypeError must not win that
      // race, or the caller sees a raw undici object (or its own plain-Error
      // reason) instead of an abort. A real verdict that arrived anyway — an
      // HTTP status, an undecodable body — stays more informative than the
      // cancellation, so only the non-verdicts are replaced.
      const verdict = typeof error?.status === 'number' || error?.name === 'SyntaxError' || error instanceof SyntaxError;
      if (signal?.aborted && !verdict) throw abortError(signal);
      if (!failure) throw error;
      // The clock starts here, at the first failure, not when the call started.
      if (deadline === null) deadline = now() + budgetMs;
      const remaining = deadline - now();
      const pause = jitteredPause(Math.min(wait, maxDelayMs), random);
      // No room for another attempt: do not sleep a pause that could not be
      // followed by a request. The failure surfaces now.
      if (pause <= 0 || pause >= remaining) throw error;
      log(`dsh completion attempt ${attempt} failed (${failure}); retrying in ${pause}ms with ${remaining}ms of the retry budget left`);
      await sleep(pause, signal);
      wait = Math.min(wait * 2, maxDelayMs);
    }
  }
}

/** The root director's default effort: the configured one when valid, else max. */
export const rootReasoningEffort = value => REASONING_EFFORTS.includes(value) ? value : 'max';

/** Root completions default to rootEffort (the director plans and judges); native
 *  children default to workerEffort. The runtime passes both from roles.json. An
 *  explicit DSH reasoningEffort is passed through.
 *
 *  Step 24.2c: `imageRetain` is the root role's roles.json `image_retain` (the steward's); with it
 *  the root session keeps at most that many already-seen images (R-M3) and asks DSH to offload
 *  the rest with IMAGE_OFFLOAD_REQUIRED. Its byte cap must hold one request image, or a large
 *  photo would be offloaded the step after it was first seen: checked here, at install.
 *  `rootReadsFiles` is false for a root role that forbids `read` (fileLine). */
export function installHostModel(ctx, { LlmAdapter, LlmError, projectOffloadedImages }, { request, project, session, rootEffort = 'max',
  workerEffort = 'high', retry = {}, routeInput = async () => TEXT_ONLY, imageRetain, rootReadsFiles = true }) {
  if (imageRetain) {
    if (!(imageRetain.max_base64_bytes >= base64Length(REQUEST_IMAGE_MAX_BYTES)))
      throw new Error(`image_retain.max_base64_bytes ${imageRetain.max_base64_bytes} is under one request image (${base64Length(REQUEST_IMAGE_MAX_BYTES)} base64 bytes)`);
    if (typeof LlmError !== 'function' || typeof projectOffloadedImages !== 'function')
      throw new Error('image_retain needs DSH LlmError and projectOffloadedImages');
  }
  const retain = imageRetain && { maxImages: imageRetain.max_images, countQuantum: imageRetain.count_quantum,
    maxBytes: imageRetain.max_base64_bytes, byteQuantum: imageRetain.byte_quantum };
  const retryOptions = { budgetMs: retry.budgetMs ?? modelRetryBudgetMs(), delayMs: retry.delayMs ?? RETRY_DELAY_MS,
    maxDelayMs: retry.maxDelayMs ?? RETRY_MAX_DELAY_MS, log: retry.log ?? stderrRetryLog,
    ...(retry.now ? { now: retry.now } : {}), ...(retry.sleep ? { sleep: retry.sleep } : {}),
    ...(retry.random ? { random: retry.random } : {}) };
  class HostAdapter extends LlmAdapter {
    async resolveModel(provider, model, signal) {
      // Step 24.2b (7.8.12a "路线能力"): DSH does not say which session asks, so image input is
      // declared only when the root's route AND the children's route both take images. Otherwise
      // the file tool refuses read_image (the tool stays; the call is refused). A table with no
      // `_input` measured nothing: declared as before 24.2b (declaresImage).
      const routes = await routeInput(signal);
      return { provider, id: model, name: model, context: { contextWindow: 1000000 }, defaultMaxTokens: 32768,
        inputModalities: declaresImage(routes) ? ['text', 'image'] : ['text'] };
    }
    async *stream(options) {
      // stream() knows its session: that session's own route decides. A route without images
      // reads no image byte (the attachment service is not even asked) and sends one line per
      // image block instead (noImageLine) - never an image_url, never a throw.
      const routes = await routeInput(options.signal);
      const root = (options.sessionId ?? session) === session;
      const textOnly = !unmeasured(routes) && !takesImage(root ? routes.root : routes.child);
      // R-M3 (7.8.12b "八" 4), the root session of a role with image_retain only: offloaded images
      // become their fixed placeholder lines, and when the images already seen exceed the budget
      // the call fails before any request, naming how many of the oldest DSH must offload.
      const messages = root && retain ? projectOffloadedImages(options.messages, offloadedImagePlaceholder) : options.messages;
      const imageUrls = textOnly ? new Map() : await requestImageUrls(ctx, { messages }, options.signal);
      if (root && retain && !textOnly) {
        const seen = seenImageLengths(messages, imageUrls);
        const offloadImages = offloadedImagePrefixCount(seen, retain);
        if (offloadImages > 0) throw new LlmError(`the root session keeps at most ${retain.maxImages} seen images`
          + ` (${retain.maxBytes} base64 bytes); ${seen.length} are seen, offload the ${offloadImages} oldest`, 'IMAGE_OFFLOAD_REQUIRED', { offloadImages });
      }
      // One signal for the whole call: a retry lives inside the same 30-minute
      // bound as the attempt it replaces, not a fresh one.
      const signal = options.signal ?? AbortSignal.timeout(1800000);
      const result = await completionWithRetry(request, `/api/dsh-drivers/${encodeURIComponent(project)}/completion`, {
        session_id: options.sessionId ?? session,
        messages: openaiMessages({ system: options.system, messages }, (ref) => imageUrls.get(ref?.attachmentId ?? ref?.id),
          { textOnly, fileTools: !root || rootReadsFiles }),
        tools: options.tools?.map(t => ({ type: 'function', function: { name: t.name, description: t.description, parameters: t.parameters } })),
        max_tokens: options.maxTokens ?? 32768,
        reasoning_effort: options.reasoningEffort ?? (root ? rootEffort : workerEffort),
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
