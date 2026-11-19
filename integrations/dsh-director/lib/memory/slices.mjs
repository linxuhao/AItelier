/** Turns for the memory's long synchronous work: one slice per event-loop iteration (plugin
 *  step 21 perf follow-up, design 3.8.2's 20 ms). Loops that yield with setImmediate of
 *  their own all run in the same check phase, back to back with no timer or I/O between:
 *  a vector scan's 5 ms slice, grep's output parsing and grep's fork (12-15 ms at 100k
 *  entries) added up to 23 ms in one iteration. Here the waiting loops take turns: each
 *  iteration resumes one of them, which runs until `due()` and then waits for its next turn
 *  behind the others. */
export const SLICE_MS = 5;
const waiting = [];
let deadline = 0, scheduled = false;

function schedule() {
  if (scheduled) return;
  scheduled = true;
  setImmediate(tick);
}

function tick() {
  scheduled = false;
  const next = waiting.shift();
  if (waiting.length) schedule();
  deadline = performance.now() + SLICE_MS;
  next?.();
}

/** Wait for a turn: it comes in a later loop iteration, one waiter per iteration, in order. */
export function turn() {
  return new Promise(resolve => { waiting.push(resolve); schedule(); });
}

/** Whether this turn's time is used up. */
export const due = () => performance.now() > deadline;
