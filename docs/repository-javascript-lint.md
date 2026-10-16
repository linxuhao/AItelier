# Owned JavaScript syntax lint

The `eslint` backend parses the exact bytes of each selected JavaScript file.
A lint manifest explicitly maps `.js`, `.mjs` and/or `.cjs` to `eslint`; the
native loader retains its other backend and file-selection conventions.
This is a syntax check, not project-defined ESLint rules or formatting.
`.js` and `.mjs` use ESLint's module semantics; `.cjs` uses CommonJS. JSX and
TypeScript are not supported. An empty parsed program is valid. A glob that
selects no files is unmeasured and fails at the host lint boundary.

SkillFlow 1.5.91 supplies explicit `run_id`, `step_id`, and `project_root` to
the backend through native lint and StepValidator. The host does not infer
identity from a path or current session. Its existing launcher independently
checks the configured read-only run ownership record, exact repository root,
private per-run ticket, and matching source/snapshot bytes. Missing context,
artifact targets, aliases, links, nonregular inputs, unsupported extensions,
option-shaped path components and files over 8 MiB are refused.

Only the one input snapshot and trusted executor entry are mounted read-only
in the child. The original repository, configuration files, npm cache, host
launcher, Docker socket, database and credentials are absent. Execution uses
network none, UID/GID of the operator, 2 CPUs, 2 GiB, 512 PIDs, dropped
capabilities and no-new-privileges through the existing four-slot transport.
The fixed command uses `/opt/aitelier-node/bin/node` and
`/opt/aitelier-eslint/node_modules/eslint/bin/eslint.js` with
`--no-config-lookup --no-inline-config --stdin --stdin-filename input.<ext>`.
This disables project configs/plugins and inline rule changes explicitly.
The fixed input name avoids ignored-path warnings being counted as parsing.
The executor requires Node 24.22.0 and official ESLint 10.12.0. Missing tools,
version mismatches, timeouts and transport refusals fail; there is no npm/npx,
project binary, network installer, backend subprocess or direct Docker fallback.

The operator must supply an immutable `node_executor_image` containing that
reviewed package and enable the existing owned launcher. A Source change or
packaged tool does not activate a deployment. SDK 1.5.91 is an exact public
pin; the running SDK 1.5.90 deployment is not upgraded by this Source change.
The per-file 120-second deadline is not an aggregate lint quota or total wall
time guarantee. Existing acknowledged-container cleanup and disconnect
handling are reused. An unknown CREATE acknowledgment remains non-pass and
can leave a late container for observation; this change does not claim that
such containers are impossible.
