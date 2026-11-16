/** The backend vocabulary dsh-director must never carry, shared by the boundary gate
 *  (test/boundary.test.mjs, over the plugin's files) and the standalone scenario
 *  (standalone/scenario.mjs, over the text every model request actually carried).
 *  The tokens are assembled from pieces so that this file does not contain them. */
const word = (...parts) => parts.join('');
export const name = word('ait', 'elier');
export const entity = word('Sta', 'te');
export const route = word('/', 'api/');
export const toolShape = word('sta', 'te_');
export const vocabulary = [
  [new RegExp(name, 'i'), 'backend name'],
  [new RegExp('\\b' + entity + '\\b'), 'backend entity word'],
  [new RegExp(route), 'backend route'],
  // (?<![a-z]) not \b: in an MCP name the prefix ends with an underscore, a word character.
  [new RegExp('(?<![a-z])' + toolShape + '(?:read|graph_[a-z_]+|query|commands)\\b'), 'backend tool name'],
];
/** The reasons a text carries the backend vocabulary (empty when it carries none). */
export const leaks = text => vocabulary.filter(([pattern]) => pattern.test(text)).map(([, reason]) => reason);
