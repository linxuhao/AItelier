/** This deployment's role table is dsh-director's roles.json (plugin step 4: one table); the parser and its
 *  rules are dsh-director's (../dsh-director/lib/roles.mjs). core/dsh_driver.py
 *  `load_roles` parses the same file with the same rules (tests/unit/test_dsh_roles.py). */
import { fileURLToPath } from 'node:url';
import { loadRoles as load } from '../dsh-director/lib/roles.mjs';

export { ROLE_NAMES, validateRoles, parseRoles, roleOf } from '../dsh-director/lib/roles.mjs';
export const rolesFile = fileURLToPath(new URL('../dsh-director/roles.json', import.meta.url));
export const loadRoles = (file = rolesFile) => load(file);
