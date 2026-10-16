# Pipeline versioning contract (v1, statement phase)

Status: STATEMENT CANDIDATE. Nothing here has been compiled, executed or independently
read back. The fake and vectors are unrun. The fake is intentionally standalone: it does
not register, boot, roll back or touch any real pipeline, and it does not stand in for the
real provider (the real `ConfigRegistry` is a dataclass, not a dict, and is not modelled).

## Goal
A registered pipeline version is an immutable, content-addressed bundle of graph, roles
and templates that can be listed, read and restored.

## Bundle (input to `capture`, output of `read`)
Exactly three keys, no others:
- `graph`: object. Whole pipeline graph as pure data.
- `roles`: object `{role_name: {settings, tools, prompt}}`.
  - `settings`: object holding every *resolved* setting (defaults already applied).
  - `tools`: list of `{name, identity}`; names unique within a role; `identity` is an opaque
    frozen reference to the tool (for example a content digest or pinned tag).
  - `prompt`: the actual prompt text. Pointers/paths are refused (`MALFORMED_BUNDLE`).
- `templates`: object `{template_name: text}` (actual text, not paths).

Values may be null, bool, int, string, list, or string-keyed object. Floats, tuples and
anything else are refused (`MALFORMED_BUNDLE`), as are non-UTF-8-encodable strings.
No YAML is involved; callers parse YAML elsewhere.

## Version id
`version_id = "sha256:" + hex(sha256(canonical_bytes(bundle)))`.

`canonical_bytes` is the ASCII bytes of `json.dumps(payload, sort_keys=True,
separators=(",", ":"), ensure_ascii=True)` where
`payload = {"schema": "rsi.pipeline_version.v1", "graph": graph, "roles": R, "templates": T}`;
`R[role] = {"prompt_b64": base64(prompt UTF-8 bytes), "settings": settings,
"tools": [{"identity", "name"} sorted by name]}`; `T[name] = base64(text UTF-8 bytes)`.
Consequences: key order and tool order never matter; any byte change to a prompt, template,
setting, graph value or tool identity changes the id; prompts are hashed as bytes, never by
pointer. Exact bytes for four bundles are pinned in `conformance_vectors.json`.

## API (provider object)
| call | result |
|---|---|
| `capture(bundle, *, expected_current=<unset>)` | `{version_id, created, previous_version_id, current_version_id}` |
| `list_versions()` | `[{version_id, seq, is_current}]` in creation order, `seq` from 1 |
| `read(version_id)` | `{version_id, bundle}` |
| `restore(version_id, *, expected_current=<unset>)` | `{version_id, previous_version_id, changed}` |
| `current_version()` | version id or `None` |
| `pin_run(run_id)` / `finish_run(run_id)` / `run_pin(run_id)` / `list_pins()` | run pinning, below |
| `version_id_of(bundle)`, `canonical_form(bundle)` | pure; used by vectors |
| `set_available_tools({name: identity})` | fake/test seam for the host tool inventory |

`expected_current` (optional): a version id or `None` (meaning "nothing selected yet").
If given and it differs from the actual current, the call refuses `CONFLICT`.

## Behaviour
- **No-op edit creates no version.** `capture` of a bundle whose id already exists
  returns `created=false`, adds no entry and keeps `seq`. It still selects that version as
  current (equal to current means no change at all). Reordered keys/tools are no-ops.
- **History is append-only.** There is no delete or overwrite. A version's content never
  changes after capture.
- **Restore selects only.** `restore` changes the current selection; it does not create a
  version, edit history, or touch any run pin. Restoring the current version is allowed and
  returns `changed=false`.
- **Pins.** `pin_run` records the current version for a run (`NO_CURRENT_VERSION` if none,
  `DUPLICATE_RUN` if reused). A pin never changes afterwards: capture, restore and
  `finish_run` leave it as is (finish only sets `state` to `finished`; the pin is retained).
  `list_pins()` returns `{run_id: {version_id, state}}`; `state` is `running|finished`.
- **Tools.** `capture` and `restore` require every tool in every role to exist with the
  same identity: otherwise `TOOL_UNAVAILABLE` / `TOOL_IDENTITY_MISMATCH`. Reading a stored
  version does not require tools (history stays inspectable).
- **Integrity.** Every `read`/`restore`/duplicate `capture` re-hashes stored content;
  mismatch refuses `TAMPERED_VERSION`.

## Errors (loud refusal)
Every refusal is an exception with a stable string attribute `code`; no silent default,
no partial effect: a refused call leaves versions, current selection and pins unchanged.
`MALFORMED_BUNDLE`, `MALFORMED_VERSION_ID` (not `sha256:` + 64 lowercase hex, or not a
string), `UNKNOWN_VERSION`, `TAMPERED_VERSION`, `TOOL_UNAVAILABLE`,
`TOOL_IDENTITY_MISMATCH`, `CONFLICT`, `NO_CURRENT_VERSION`, `UNKNOWN_RUN`, `DUPLICATE_RUN`.

Check order for `restore`: id form, unknown, tamper, expected_current, tools. For `capture`:
bundle validity, expected_current, tools, tamper (when the id exists).

## Ownership
- The store owns history. Callers own what they pass in (deep-copied on entry) and what
  they get back (deep-copied on exit); mutating either never alters stored history.
- Run pins belong to the run that created them; restore cannot repin a running run.
- The host owns the tool inventory; versions only hold frozen tool references.

## Conformance
`conformance_vectors.json` holds literal bundles, exact canonical bytes and scenarios.
Grammar: bundle keys name entries in `bundles`; `$id:K` = provider id of bundle K,
`$bundle:K` = that bundle, `$zero` = `zero_id`, `$null` = null. `expect` is `{error: CODE}`,
`{result: X}` (exact) or `{result_subset: X}` (dict subset); no `expect` means "must succeed".
Ops: capture, capture_then_mutate_input, mutate_read, list, read, current, restore,
set_tools, tamper, pin_run, finish_run, run_pin, list_pins. A scenario with `requires:
["tamper_hook"]` is skipped by providers lacking that capability (skipped, not passed).
A real provider passes the same vectors by calling
`test_contract.run_conformance(factory)`, `factory(available_tools) -> provider`; it
returns a list of failure strings, empty meaning conforming.

## Not covered
Real registration/adoption/restart, filesystem or registry storage, YAML parsing, boot and
rollback safety, concurrency, persistence across processes.
