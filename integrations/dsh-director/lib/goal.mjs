/** The director's standing goal (moved from dsh-driver/goal.mjs, plugin design step 2). */

/** Below this many remaining goal rounds an active resident goal's cap is raised at start. */
export const ROUND_TOP_UP_BELOW = 64;
/** The raised cap leaves this many rounds: maxGoalRounds = roundsStarted + ROUND_TOP_UP_TO. */
export const ROUND_TOP_UP_TO = 256;

/** Native DSH owns the durable objective and continuation counters.
 *
 *  The `objective` the host renders is the mandate's source of truth (whichever
 *  template the host loaded: a deployment's own mandate, else its default). An
 *  ACTIVE goal whose objective
 *  differs from the rendering is re-seeded here, by the runtime, through the
 *  native edit API: the model-facing update_goal is limited to direct human
 *  turns, so no message could ever change a running director's mandate.
 *
 *  The same edit tops up the round cap. A resident director spends a counted
 *  native goal round only when a start re-arms it (IdleReview disarms it again at
 *  idle and wakes it with uncounted messages), so roundsStarted counts starts,
 *  and DSH's default cap of 256 would otherwise block a healthy director for
 *  good after its 256th restart. The counter itself is never reset: only
 *  goals.clear does that, and a clear is the owner's stop signal (see below).
 *  `onSeed(after, before)` observes the applied edit (views before/after it). */
export function bindDriverGoal(goals, agent, objective, { onSeed } = {}) {
  let goal = goals.get(agent);
  if (!goal) {
    // No current goal can also mean the owner explicitly cleared it.
    if (agent.session.snapshotEvents().some(e => e.type === 'goal/change' && e.data.operation === 'clear')) return;
    return goals.create(agent, { objective });
  }
  // An enabled resident host is authorized to continue its active objective
  // after restart. Preserve explicit pause/block/completion and edited goals.
  // Only an active goal follows the template; paused/blocked/complete are explicit
  // human states. Native DSH trims objectives, so compare the trimmed text or
  // an unchanged goal would be re-edited (revision churn) on every start.
  if (goal.phase === 'active') {
    const request = {};
    if (goal.objective !== objective.trim()) request.objective = objective;
    if (goal.maxGoalRounds - goal.roundsStarted < ROUND_TOP_UP_BELOW) request.maxGoalRounds = goal.roundsStarted + ROUND_TOP_UP_TO;
    if (Object.keys(request).length) {
      // edit() keeps phase, activation, id and round counters; only the revision moves.
      const before = goal;
      goal = goals.edit(agent, { id: goal.id, revision: goal.revision }, request);
      onSeed?.(goal, before);
    }
  }
  if (goal.phase !== 'active' || goal.activation !== 'disarmed') return goal;
  return goals.resume(agent, { id: goal.id, revision: goal.revision });
}

/** A stable system section reads the goal itself, including after compaction. */
export function driverGoalSection(goals, agent) {
  const goal = goals.get(agent);
  if (!goal) return '';
  return `Native DSH driver goal (${goal.id}, ${goal.phase}):\n${goal.objective}`;
}
