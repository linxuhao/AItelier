/** Native DSH owns the durable objective and continuation counters. */
export function bindDriverGoal(goals, agent, objective) {
  const goal = goals.get(agent);
  if (!goal) {
    // No current goal can also mean the owner explicitly cleared it.
    if (agent.session.snapshotEvents().some(e => e.type === 'goal/change' && e.data.operation === 'clear')) return;
    return goals.create(agent, { objective });
  }
  // An enabled resident host is authorized to continue its active objective
  // after restart. Preserve explicit pause/block/completion and edited goals.
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
