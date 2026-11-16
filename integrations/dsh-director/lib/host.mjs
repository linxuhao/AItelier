/** The directorHost contract, v1 (see ../host.d.ts).
 *
 *  A host is whatever connects dsh-director to a work/memory system: the
 *  backend adapter, the local-files reference host, or anyone else's. It is handed to
 *  the director through the cordis service `directorHost` (in process) - the
 *  interface is the product, the injection is only the wiring.
 *
 *  Loading REFUSES a host that cannot work, naming what is wrong, instead of
 *  degrading it to an empty one (design §9 MF3/MF4): a missing required operation,
 *  a version this director does not speak, or a capability it declares but does not
 *  implement. Optional operations that are absent degrade as `optionalDefaults`
 *  documents. */

import { readFileSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';

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
  const config = host.config;
  const isText = value => typeof value === 'string' && value.length > 0;
  if (config && typeof config === 'object' && !Array.isArray(config)) {
    // Every required field must be a non-empty string - present is not enough.
    for (const key of REQUIRED_CONFIG.filter(k => k !== 'todo'))
      if (config[key] === undefined || config[key] === null || config[key] === '') problems.push(`missing config: ${key}`);
      else if (!isText(config[key])) problems.push(`config: ${key} must be a non-empty string, got ${JSON.stringify(config[key])}`);
    // The role table must be a readable file with the director's and the worker's
    // prompts next to it (refused here, not as an ENOENT at boot).
    if (isText(config.rolesFile)) {
      let table;
      try { table = JSON.parse(readFileSync(config.rolesFile, 'utf8')); }
      catch (error) { problems.push(`config: rolesFile ${config.rolesFile} is not a readable role table (${error.code ?? error.message})`); }
      for (const role of ['director', 'worker']) {
        const prompt = table?.[role]?.prompt;
        if (table && (typeof prompt !== 'string' || !existsSync(join(dirname(config.rolesFile), prompt))))
          problems.push(`config: rolesFile ${role} prompt ${prompt} is not next to ${config.rolesFile}`);
      }
    }
    const todo = config.todo;
    if (todo === undefined || todo === null) problems.push('missing config: todo');
    else if (typeof todo !== 'object' || Array.isArray(todo)) problems.push(`config: todo must be an object, got ${JSON.stringify(todo)}`);
    else {
      for (const key of REQUIRED_TODO)
        if (todo[key] === undefined || todo[key] === null || todo[key] === '') problems.push(`missing config: todo.${key}`);
        else if (!isText(todo[key])) problems.push(`config: todo.${key} must be a non-empty string, got ${JSON.stringify(todo[key])}`);
      // Optional, but typed: python defaults to python3 and cwd to the process's own
      // directory; anything else given must be a usable path string.
      for (const key of ['python', 'cwd'])
        if (todo[key] !== undefined && !isText(todo[key])) problems.push(`config: todo.${key} must be a non-empty string when given`);
    }
    for (const key of ['sessionEnv', 'projectId', 'permissionPreset'])
      if (config[key] !== undefined && !isText(config[key])) problems.push(`config: ${key} must be a non-empty string when given`);
  } else if (config !== undefined && config !== null && typeof config !== 'function') {
    problems.push('config: must be an object');
  }
  // capabilities is required (host.d.ts): an array of the optional op names implemented.
  let declared = [];
  if (!Array.isArray(host.capabilities)) problems.push(`missing capabilities: an array of the optional ops this host implements (got ${host.capabilities === undefined ? 'none' : JSON.stringify(host.capabilities)})`);
  else if (host.capabilities.some(op => typeof op !== 'string')) problems.push('capabilities: every entry must be an op name string');
  else if (new Set(host.capabilities).size !== host.capabilities.length)
    problems.push(`capabilities: duplicate entries (${[...new Set(host.capabilities.filter((op, i) => host.capabilities.indexOf(op) !== i))].join(', ')})`);
  else declared = host.capabilities;
  for (const op of declared) {
    if (!OPTIONAL_OPS.includes(op)) problems.push(`unknown capability: ${op}`);
    else if (op === 'texts' || op === 'labels' ? (!host[op] || typeof host[op] !== 'object') : typeof host[op] !== 'function')
      problems.push(`capability declared but not implemented: ${op}`);
  }
  if (Array.isArray(host.capabilities))
    for (const op of OPTIONAL_OPS) if (host[op] !== undefined && !declared.includes(op)) problems.push(`implemented but not declared: ${op}`);
  if (problems.length) throw new Error('directorHost refused: ' + problems.join('; '));
  return host;
}

/** What an absent optional operation means. */
export const optionalDefaults = Object.freeze({
  progress: () => {},                  // status reports are dropped (the runtime still logs)
  workState: async () => 'unknown',    // no work item is known to have settled
  heldWork: async () => [],            // the director holds no external locks
  roles: table => table,               // the table as loaded (a host's roles() must return its table synchronously)
  install: async () => {},             // no host-scoped tools or sections of its own
  setup: async () => {},               // no director-scope tools, sections or hooks of its own
});

/** Whether a work state means nothing of that work may run again. */
export const isSettled = state => SETTLED_WORK_STATES.has(state);
