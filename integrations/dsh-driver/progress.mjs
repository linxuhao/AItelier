/**
 * Bounding a real-DSH scenario by the progress it OBSERVES, not by the clock.
 *
 * The two real-DSH scenarios (runtime.smoke.test.mjs:73 and
 * runtime.todo.test.mjs:342) used a wall-clock budget for the question "did the
 * child make progress" - a 45 s / 90 s harness timeout plus 35 s / 75 s poll
 * deadlines. A wall budget cannot tell a slow-but-correct runtime from a stuck
 * one. On a shared host the same 35 s is generous at loadavg 1 and short at
 * loadavg 8, so the clock - not the runtime - decides the verdict, and the red it
 * prints names a duration instead of the signal that never came. Measured
 * 2026-11-07 on an untouched base tree under load: the smoke scenario failed 8 of
 * 9 paired draws and passed 9 of 9 unloaded, and the red said only "scenario did
 * not finish".
 *
 * A watch is therefore defined by MILESTONES - named predicates over the
 * scenario's own observations (a native child's tool result, a runtime handback,
 * a compaction, a State re-injection, the director's own completion) - and it
 * bounds only SILENCE: the quiet window restarts every time a milestone that was
 * not reached before is reached. A runtime that keeps making progress passes
 * however long it takes; a runtime whose watched progress stops arriving fails
 * after `quietMs` and says which watch expired and what it last saw move.
 *
 * Two rules keep the bound honest:
 *
 *   - Milestones must be MONOTONE: once met, a milestone stays met, because the
 *     helper records the first time it saw it. `met` therefore has to describe
 *     observed history ("the child's deliver item passed") and never a momentary
 *     state ("no children are live right now") - a momentary predicate can be
 *     true once, early, and false for ever after, which would let a stuck
 *     scenario through on a single lucky poll.
 *   - Only FORWARD signals belong in a watch. The runtime's idle traffic -
 *     activity posts, heartbeats, the periodic idle review, "Director idle."
 *     turns - is not progress and must never keep a stuck scenario alive, so a
 *     milestone counts the FIRST review / the FIRST dispatch, never the latest
 *     one or a raw event count.
 */

/**
 * How long a watched scenario may go with NO milestone reached.
 *
 * Not a budget for the scenario: a slow-but-correct runtime passes however long
 * the whole thing takes. Measured on the shared host at loadavg ~8, the longest
 * legitimate silence between two milestones of either scenario was a few seconds
 * (a native child booting its harness, or one compaction summarization turn);
 * 60 s is more than an order of magnitude above that, so a loaded box cannot
 * redden a runtime that is still moving. Choosing this too small re-introduces
 * exactly the load sensitivity this file exists to remove, which is why it is
 * stated once here rather than per scenario.
 */
export const PROGRESS_QUIET_MS = 60000;

/**
 * Wait for a scenario's observed progress. Resolves when every milestone has been
 * reached, or returns early when the scenario's process exits or the runtime
 * reports a failure (the caller asserts those separately, with better output).
 *
 * `message` is always populated: it names the last milestone that was observed
 * (and when, relative to the start and to the moment the watch gave up), and
 * every milestone that was never reached - so a red says which watch expired
 * instead of how long it waited.
 *
 * DSH_PROGRESS_TRACE=1 prints each milestone, with its offset, to stderr. That is
 * how the quiet window stays a measurement rather than a guess: a change to the
 * runtime or the host load shows up as a new longest-gap number.
 */
export async function watchProgress({
  what,                 // what the scenario is waiting for; used in the red
  milestones,           // [{ name, met }] - monotone, all of them required
  quietMs = PROGRESS_QUIET_MS,
  pollMs = 100,
  running = () => true, // the scenario's process is still alive
  failed = () => false, // the runtime has reported a failure
  trace = Boolean(process.env.DSH_PROGRESS_TRACE),
}) {
  const started = Date.now();
  const reached = new Map();            // name -> ms after the start, in observation order
  const observe = now => {
    for (const { name, met } of milestones) {
      if (reached.has(name) || !met()) continue;
      reached.set(name, now - started);
      if (trace) console.error(`[progress] ${what}: ${name} at +${now - started} ms`);
    }
  };
  const describe = (now, unmet, stopped) => {
    const timeline = [...reached].map(([name, at]) => `"${name}" +${at} ms`).join(', ');
    const last = [...reached].at(-1);
    const where = stopped ? `${what}: ${stopped}` : `progress watch expired: no milestone for ${now - started - (last?.[1] ?? 0)} ms`;
    const observed = last
      ? `last progress observed was "${last[0]}" at +${last[1]} ms, ${now - started - last[1]} ms before giving up`
      : 'no watched milestone was ever observed';
    const missing = unmet.length === milestones.length
      ? `never reached any of: ${unmet.map(({ name }) => `"${name}"`).join(', ')}`
      : `${unmet.length} of ${milestones.length} milestones never reached: ${unmet.map(({ name }) => `"${name}"`).join(', ')}`;
    return `${where} (scenario ran ${now - started} ms); ${observed}; ${missing}; reached so far: ${timeline || 'none'}`;
  };
  let quietSince = started;
  for (;;) {
    const now = Date.now();
    const before = reached.size;
    observe(now);
    if (reached.size > before) quietSince = now;
    const unmet = milestones.filter(({ name }) => !reached.has(name));
    if (!unmet.length) return { done: true, reached, elapsed: now - started };
    const stopped = failed() ? 'the runtime reported a failure'
      : !running() ? 'the scenario process exited before the scenario finished' : null;
    if (stopped || now - quietSince >= quietMs) {
      return { done: false, reached, elapsed: now - started, message: describe(now, unmet, stopped) };
    }
    await new Promise(resolve => setTimeout(resolve, pollMs));
  }
}
