/** Native DSH owns the durable objective and continuation counters.
 *
 *  `goal.md` is the mandate's source of truth. An ACTIVE goal whose objective
 *  differs from the rendering is re-seeded here, by the runtime, through the
 *  native edit API: the model-facing update_goal is limited to direct human
 *  turns, so no message could ever change a running director's mandate.
 *  `onSeed(view)` observes the applied edit. */
export function bindDriverGoal(goals, agent, objective, { onSeed } = {}) {
  let goal = goals.get(agent);
  if (!goal) {
    // No current goal can also mean the owner explicitly cleared it.
    if (agent.session.snapshotEvents().some(e => e.type === 'goal/change' && e.data.operation === 'clear')) return;
    return goals.create(agent, { objective });
  }
  // An enabled resident host is authorized to continue its active objective
  // after restart. Preserve explicit pause/block/completion and edited goals.
  // Only an active goal follows goal.md; paused/blocked/complete are explicit
  // human states. Native DSH trims objectives, so compare the trimmed text or
  // an unchanged goal would be re-edited (revision churn) on every start.
  if (goal.phase === 'active' && goal.objective !== objective.trim()) {
    // edit() keeps phase, activation, id and round counters; only the revision moves.
    goal = goals.edit(agent, { id: goal.id, revision: goal.revision }, { objective });
    onSeed?.(goal);
  }
  if (goal.phase !== 'active' || goal.activation !== 'disarmed') return goal;
  const ref = { id: goal.id, revision: goal.revision };
  if (goal.roundsStarted >= goal.maxGoalRounds) return goals.block(agent, ref, {
    code: 'round-limit', message: `Goal reached its configured limit of ${goal.maxGoalRounds} rounds.`,
  });
  return goals.resume(agent, ref);
}

/** A stable system section reads the goal itself, including after compaction. */
export function driverGoalSection(goals, agent) {
  const goal = goals.get(agent);
  if (!goal) return '';
  return `Native DSH driver goal (${goal.id}, ${goal.phase}):\n${goal.objective}`;
}
