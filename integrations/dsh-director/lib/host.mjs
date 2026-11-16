/** The directorHost contract, v1 (see ../host.d.ts and ../host.schema.json).
 *
 *  A host is whatever connects dsh-director to a work/memory system: the AItelier
 *  State adapter, the local-files reference host, or anyone else's. It is handed to
 *  the director through the cordis service `directorHost` (in process) - the
 *  interface is the product, the injection is only the wiring.
 *
 *  Loading REFUSES a host that cannot work, naming what is wrong, instead of
 *  degrading it to an empty one (design §9 MF3/MF4): a missing required operation,
 *  a version this director does not speak, or a capability it declares but does not
 *  implement. Optional operations that are absent degrade as `optionalDefaults`
 *  documents. */

export const HOST_MAJOR = 1;
export const REQUIRED_OPS = ['config', 'context', 'events'];
export const OPTIONAL_OPS = ['progress', 'workState', 'heldWork', 'roles', 'install', 'setup', 'texts', 'labels'];
/** The states a work item can be in (aligned with MCP Tasks / A2A task states). */
export const WORK_STATES = ['working', 'input_required', 'completed', 'failed', 'cancelled', 'unknown'];
/** States after which nothing of the work item may run again. */
export const SETTLED_WORK_STATES = new Set(['completed', 'failed', 'cancelled']);
const REQUIRED_CONFIG = ['sessionId', 'provider', 'model', 'stateDir', 'dshEntry', 'rolesFile', 'todo'];
const REQUIRED_TODO = ['module', 'stateDirEnv'];

/** Throws, naming every problem, unless `host` satisfies directorHost ^1. */
export function validateHost(host) {
  if (!host || typeof host !== 'object') throw new Error('directorHost missing: no host was provided');
  const problems = [];
  const version = typeof host.version === 'string' ? host.version : '';
  const major = Number(/^(\d+)\.\d+\.\d+/.exec(version)?.[1]);
  if (major !== HOST_MAJOR) problems.push(`expected directorHost ^${HOST_MAJOR}, host declares ${version || 'no version'}`);
  for (const op of REQUIRED_OPS) {
    const value = host[op];
    if (op === 'config' ? (!value || typeof value !== 'object') : typeof value !== 'function') problems.push(`missing required op: ${op}`);
  }
  if (host.config && typeof host.config === 'object') {
    for (const key of REQUIRED_CONFIG) if (host.config[key] === undefined || host.config[key] === '') problems.push(`missing config: ${key}`);
    if (host.config.todo && typeof host.config.todo === 'object')
      for (const key of REQUIRED_TODO) if (!host.config.todo[key]) problems.push(`missing config: todo.${key}`);
  }
  const declared = Array.isArray(host.capabilities) ? host.capabilities : [];
  for (const op of declared) {
    if (!OPTIONAL_OPS.includes(op)) problems.push(`unknown capability: ${op}`);
    else if (op === 'texts' || op === 'labels' ? (!host[op] || typeof host[op] !== 'object') : typeof host[op] !== 'function')
      problems.push(`capability declared but not implemented: ${op}`);
  }
  for (const op of OPTIONAL_OPS) if (host[op] !== undefined && !declared.includes(op)) problems.push(`implemented but not declared: ${op}`);
  if (problems.length) throw new Error('directorHost refused: ' + problems.join('; '));
  return host;
}

/** What an absent optional operation means. */
export const optionalDefaults = Object.freeze({
  progress: () => {},                  // status reports are dropped (the runtime still logs)
  workState: async () => 'unknown',    // no work item is known to have settled
  heldWork: async () => [],            // the director holds no external locks
  roles: table => table,               // the table as loaded
  install: async () => {},             // no host-scoped tools or sections of its own
  setup: async () => {},               // no director-scope tools, sections or hooks of its own
});

/** Whether a work state means nothing of that work may run again. */
export const isSettled = state => SETTLED_WORK_STATES.has(state);
