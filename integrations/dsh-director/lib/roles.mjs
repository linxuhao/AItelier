/** The role table (roles.json): director, worker and the optional steward (plugin step 12),
 *  the one place the roles differ.
 *
 * Generic on purpose - no backend names or vocabulary here (see the dsh-director plugin
 * design): an adapter extends the parsed table, it never edits this file's rules.
 * core/dsh_driver.py `load_roles` parses the same file with the same rules; a
 * test compares both parses and both refusals. Unknown keys are refused at every
 * level, so a misspelled field is an error instead of a silently ignored one. */
import { readFileSync } from 'node:fs';

export const ROLE_NAMES = ['director', 'worker', 'steward'];
/** A table without these is refused; `steward` is optional (no steward, nothing changes). */
const REQUIRED_ROLES = ['director', 'worker'];
const TOP_KEYS = ['version', ...ROLE_NAMES];
const ROLE_KEYS = ['route', 'reasoning_effort', 'compaction', 'prompt', 'presented', 'forbidden', 'presented_only'];
/** The roles a host may run its root session as (HostConfig.rootRole). */
export const ROOT_ROLES = ['director', 'steward'];
const COMPACTION_KEYS = ['thresholdRatio', 'retainTokens'];
/** The efforts the completion bridge accepts (api/dsh_driver_routers.py Completion). */
const EFFORTS = ['off', 'low', 'high', 'max'];

const isObject = value => value !== null && typeof value === 'object' && !Array.isArray(value);

function exactKeys(value, keys, where, required = keys) {
  if (!isObject(value)) throw new Error(`roles: ${where} must be an object`);
  for (const key of Object.keys(value)) if (!keys.includes(key)) throw new Error(`roles: unknown key ${where}.${key}`);
  for (const key of required) if (!Object.hasOwn(value, key)) throw new Error(`roles: missing key ${where}.${key}`);
}

function names(value, where) {
  if (!Array.isArray(value) || value.some(name => typeof name !== 'string' || !/^[A-Za-z0-9_]+$/.test(name)))
    throw new Error(`roles: ${where} must be a list of tool names`);
  if (new Set(value).size !== value.length) throw new Error(`roles: ${where} names a tool twice`);
  return value;
}

/** Validate a parsed table; returns it unchanged. Throws naming the first bad key. */
export function validateRoles(table) {
  exactKeys(table, TOP_KEYS, 'table', ['version', ...REQUIRED_ROLES]);
  if (table.version !== 1) throw new Error('roles: version must be 1');
  for (const role of ROLE_NAMES) {
    const value = table[role];
    if (value === undefined && !REQUIRED_ROLES.includes(role)) continue;
    exactKeys(value, ROLE_KEYS, role, ROLE_KEYS.filter(key => key !== 'presented_only'));
    if (typeof value.route !== 'string' || !value.route) throw new Error(`roles: ${role}.route must be a non-empty string`);
    if (!EFFORTS.includes(value.reasoning_effort)) throw new Error(`roles: ${role}.reasoning_effort must be one of ${EFFORTS.join(', ')}`);
    exactKeys(value.compaction, COMPACTION_KEYS, role + '.compaction', ['thresholdRatio']);
    const { thresholdRatio, retainTokens } = value.compaction;
    if (typeof thresholdRatio !== 'number' || !(thresholdRatio > 0 && thresholdRatio <= 1))
      throw new Error(`roles: ${role}.compaction.thresholdRatio must be a number in (0, 1]`);
    if (retainTokens !== undefined && !(Number.isInteger(retainTokens) && retainTokens >= 0))
      throw new Error(`roles: ${role}.compaction.retainTokens must be a non-negative integer`);
    if (typeof value.prompt !== 'string' || !/^[A-Za-z0-9_.-]+\.md$/.test(value.prompt))
      throw new Error(`roles: ${role}.prompt must be a .md file name next to roles.json`);
    const presented = names(value.presented, role + '.presented'), forbidden = names(value.forbidden, role + '.forbidden');
    const both = presented.filter(name => forbidden.includes(name));
    if (both.length) throw new Error(`roles: ${role} both presents and forbids ${both.join(', ')}`);
    // presented_only: the presented list is the whole tool set; any other tool is refused.
    // Checked last, as core/dsh_driver.py does, so both parsers name the same first problem.
    if (Object.hasOwn(value, 'presented_only') && typeof value.presented_only !== 'boolean')
      throw new Error(`roles: ${role}.presented_only must be a boolean`);
  }
  return table;
}

/** Validate an EFFECTIVE table: a valid roles.json table that a host's roles() may
 *  have extended with, per role, `allowed_actions` ({tool: [action, ...]}) and
 *  `notes` ({tool: reason}). Everything else is held to validateRoles. */
export function validateEffectiveRoles(table) {
  if (!isObject(table)) throw new Error('roles: the effective table must be an object');
  const base = { ...table };
  for (const role of ROLE_NAMES) {
    if (!isObject(table[role])) continue;
    const { allowed_actions: actions, notes, ...rest } = table[role];
    base[role] = rest;
    if (actions !== undefined && (!isObject(actions) || Object.values(actions).some(list => !Array.isArray(list) || list.some(a => typeof a !== 'string'))))
      throw new Error(`roles: ${role}.allowed_actions must map tool names to lists of action names`);
    if (notes !== undefined && (!isObject(notes) || Object.values(notes).some(note => typeof note !== 'string')))
      throw new Error(`roles: ${role}.notes must map tool names to strings`);
  }
  validateRoles(base);
  return table;
}

/** Parse and validate roles.json text. */
export const parseRoles = text => validateRoles(JSON.parse(text));

/** Load and validate a role table file (default: the plugin's roles.json, ../roles.json). */
export const loadRoles = (file = new URL('../roles.json', import.meta.url)) => parseRoles(readFileSync(file, 'utf8'));

/** The one role rule: the root session runs as `rootRole` (the director unless the host
 *  says steward), every other session is a worker. */
export const roleOf = (sessionId, rootSession, rootRole = 'director') => sessionId === rootSession ? rootRole : 'worker';
