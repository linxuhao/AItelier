/** The failure class an exiting driver has to name, and the one place a process-level
 *  exit is recorded (issue iss-6df19ff31a3b449b).
 *
 *  Measured on this host: two resident drivers ended within ten minutes of each
 *  other, each immediately after a completion-retry line that still had about
 *  165 s of its budget left, and the only records they left were a startup line
 *  reading `TypeError: fetch failed` and the adapter's own `exit: exit code 1`.
 *
 *  Three different failures look identical in that journal. A refused connection,
 *  a socket half-close, an answered 500 and a cancellation all end up as the same
 *  `exit code 1`, and a `TypeError: fetch failed` names neither the transport code
 *  nor the status that decided anything. An operator reading the journal cannot
 *  tell what happened, and the reader cannot even tell a deliberate exit from a
 *  crash.
 *
 *  So this module owns exactly two things:
 *
 *  - `failureClass(error)`: ONE name for a failure, in the vocabulary the
 *    completion retry line already writes (`HTTP 503`,
 *    `connection-level ECONNRESET after_send=true`), extended to the failures that
 *    layer deliberately does NOT retry - an answered 500, an abort, an undecodable
 *    body - because those are exactly the ones an exit record exists to tell apart.
 *    The retryable codes, statuses and half-close shapes are data here, and a
 *    witness checks them against the retry classifier so the two cannot drift.
 *  - `endDriverFailure(...)`: the single place a driver's process-level exit is
 *    decided and RECORDED. Both call sites (a failed boot, an event pump that
 *    ended) go through it, so they cannot drift apart, and the record exists
 *    BEFORE the process ends: the driver adapter's deferred exit line reads it and
 *    names the class instead of only the code.
 *
 *  A model completion that failed is NOT one of these. The completion layer's own
 *  retry contract decides when a completion has failed; what comes out of it is a
 *  failed TURN, reported through the agent's error path, and the process keeps
 *  serving. This module is reached only when the driver can no longer serve.
 */

/** Answered statuses that mean "nothing ran here" (the completion layer's retry
 *  set). Kept as data so this file cannot drift from the retry line it mirrors;
 *  the witness compares the two classifiers. */
export const RETRY_STATUSES = Object.freeze([502, 503, 504]);

/** The transport codes the completion layer retries, exactly. */
export const RETRY_TRANSPORT_CODES = Object.freeze(['ECONNREFUSED', 'ECONNRESET', 'EPIPE', 'UND_ERR_SOCKET']);

/** The undici messages naming a reset or a half-close on an already sent request. */
export const RETRY_TRANSPORT_SHAPES = Object.freeze(['socket hang up', 'terminated']);

/** The transport code on a failure, from its undici cause or from itself. */
function transportCode(error) {
  if (typeof error?.cause?.code === 'string') return error.cause.code;
  if (typeof error?.code === 'string') return error.code;
  return null;
}

/** The reset/half-close message this failure carries, or null. Matched as the whole
 *  message or its prefix, never as a substring: prose that mentions a body being
 *  "terminated" is not a dead connection. */
function transportShape(error) {
  for (const text of [error?.message, error?.cause?.message]) {
    if (typeof text !== 'string') continue;
    for (const shape of RETRY_TRANSPORT_SHAPES) {
      if (text === shape || text.startsWith(`${shape} `) || text.startsWith(`${shape}:`)) return shape;
    }
  }
  return null;
}

/** The class of one failure, as the label an operator reads on the exit record:
 *  `HTTP 500`, `connection-level ECONNRESET after_send=true`, `connection-level
 *  socket hang up after_send=true`, `abort`, `malformed body`, or the error's own
 *  name when nothing below it identifies the shape.
 *
 *  Order matters and is the retry classifier's order: a cancellation is an owner's
 *  decision whatever it raced, an answered status is a verdict about the request,
 *  and only then does a transport failure get looked at. `after_send` is carried
 *  for the retried failures only - it says whether the request bytes can already
 *  have reached the host - and a code outside that documented set keeps its own
 *  name without the label, because nothing here knows whether it was sent. */
export function failureClass(error) {
  if (error === undefined || error === null) return 'no cause';
  if (error.name === 'AbortError') return 'abort';
  if (typeof error.status === 'number') return `HTTP ${error.status}`;
  const code = transportCode(error);
  if (code) {
    return RETRY_TRANSPORT_CODES.includes(code)
      ? `connection-level ${code} after_send=${code !== 'ECONNREFUSED'}`
      : `connection-level ${code}`;
  }
  const shape = transportShape(error);
  if (shape) return `connection-level ${shape} after_send=true`;
  if (error instanceof SyntaxError || error.name === 'SyntaxError') return 'malformed body';
  return typeof error.name === 'string' && error.name ? error.name : 'Error';
}

/** The one slot this process records its exit cause in. A registered symbol, so the
 *  driver adapter (a separate plugin module) reads the same slot without either file
 *  importing the other's state. */
const SLOT = Symbol.for('dsh.exit-cause');

/** Record why this process is ending: `{where, error}`, readable by the exit line.
 *  A second record is refused rather than overwritten - the FIRST cause is the one
 *  that decided the exit, and a later one is a consequence of it. */
export function recordExitCause(error, where = 'driver') {
  if (globalThis[SLOT] === undefined) globalThis[SLOT] = Object.freeze({ where: String(where), error, class: failureClass(error) });
  return globalThis[SLOT];
}

/** The recorded cause of this process's exit, or undefined when nothing recorded
 *  one (an uncaught failure, or a clean end). */
export function exitCause() {
  return globalThis[SLOT];
}

/** The line a driver writes when it decides to end: where the failure arrived and
 *  which class it was. One line, no message body - the message can quote a
 *  credential, and the adapter's own exit line carries the redacted detail. */
export function exitCauseLine(record) {
  return `dsh exit cause: ${record.where}: ${record.class}`;
}

/**
 * The single decision that a resident driver's process ends. Records the class,
 * writes one line through `log` (and through the plugin logger when there is one),
 * then asks the launcher to exit 1.
 *
 * Returns the record, whose `class` names the failure. A caller that does not want
 * the process to end must not call this: reachable here means "the driver can no
 * longer serve" - its boot failed, or its event subscription ended - and a failed
 * turn is reported elsewhere.
 */
export function endDriverFailure({ ctx, where = 'driver', error, log } = {}) {
  const record = recordExitCause(error, where);
  const line = exitCauseLine(record);
  try { log?.(line); } catch { /* a reporter that fails must not change the decision */ }
  try { ctx?.logger?.error?.(line); } catch { /* nor must a logger */ }
  ctx?.get?.('appExit')?.(1);
  return record;
}

/** Boot failed: there is no event stream, so this process cannot serve. With `detail` (a host
 *  that prints no reason of its own, such as the local host) the first line of the error's
 *  message follows the exit line: a configuration refusal ("missing config: provider") names
 *  no credential, and without it the operator reads one word. */
export function startupFailure(ctx, error, { detail = false } = {}) {
  const reason = detail ? String(error?.message ?? error ?? '').split('\n')[0].slice(0, 1000) : '';
  return endDriverFailure({ ctx, where: 'dsh-director startup', error,
    log: line => { console.error(line); if (reason) console.error('dsh-director startup: ' + reason); } });
}

/** The event pump ended in an error: without its subscription this process cannot
 *  serve either. A delivery that failed is NOT this (see the runtime's pump). */
export function pumpFailure(ctx, error) {
  return endDriverFailure({ ctx, where: 'events pump', error, log: line => console.error(line) });
}
