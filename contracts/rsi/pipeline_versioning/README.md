# Pipeline versioning contract (r2)

Status: SOURCE CANDIDATE for `rsi.pipeline-versioning.contract` r2. Acceptance
needs a fresh nonauthor readback@r2; the r1 readback does not carry over.

## Goal
A registered pipeline version is an immutable, content-addressed unit of graph,
resolved roles/settings and actual prompt/template bytes, which can be listed,
read and restored. `fake.py` is a standalone fake for downstream tests;
`native_provider.py` binds the same API to the accepted native frozen-bundle
provider (`core/pipeline_frozen_bundle.py`, ea100a7).

## Why r1 was re-derived
r1 (statement 1f469ef, retained with its 67 native diagnostics in the
`rsi-versioning-real-provider-proof-sol-20261016` evidence) declared a domain
the real host does not have: a caller-supplied generic `{nodes, edges}` graph
(the actual `PipelineGraph` parser raised `KeyError('name')` before any
effect), integer-only settings (actual resolved roles carry floats such as
`temperature: 0.7`), an append `seq` ledger, one global "current", and
`pin_run`/`finish_run` that would fabricate runs. r2 corrects the domain:

| r1 | r2 |
|---|---|
| caller passes a bundle dict | `capture(target)` reads an actually registered generated target |
| generic graph | the target's actual graph file (valid `PipelineGraph` schema) |
| integers only | finite JSON: finite floats admitted exactly, NaN/Infinity refused |
| `seq` insertion ledger | catalog = verified filesystem seals, ordered by version id |
| one global current, logical CAS | per-target whole-unit identity CAS (native) |
| `pin_run`/`finish_run`/`list_pins` | none: runs belong to the SDK; restore must preserve them |
| tool inventory set by caller | identities served by the actual ToolLoader |
| optional tamper hook | tampering is an on-disk fixture action for every provider |

## Identities (all distinct)
- **logical version id**: `"sha256:" + sha256(canonical_bytes(content))`. Content
  only; independent of target name, provenance, time and storage.
- **native bundle identity**: the frozen-bundle `identity`; also covers provenance
  (source paths/hashes, manifest hints, parent graph version/digest,
  `derived_from`, capabilities, tool-loader served code). Shown by `read` only.
- **physical seal name**: `gen_rsi_v<first 31 hex of logical id>_<first 16 hex of
  native identity>`; the native seal triple `<name>.yaml/.roles.json/.bundle.json`.
- **unit identity**: native `unit_identity` of a target's graph+roles files; the
  `expected_current` compare-and-swap token for `restore`. Not a version id.
- **graph version**: the SDK's integer content version of a registered graph;
  existing runs pin it. Not a version id.

## Logical content (result of `read`)
Exactly `{"graph", "roles", "tools"}`:
- `graph`: the target's graph mapping with only physical naming removed: its
  `name` and its own-config `steps[*].context[*].source.config` references (the
  scope native compile/adoption renames) become `"$self"`, and every
  `agent_config` its logical role name. `"$self"` is reserved.
- `roles`: `{logical_role: {"model", "tools", "settings", "prompt"}}` for exactly
  the roles the graph references; `settings` are all other resolved config
  values; `prompt` is the actual resolved text (template bytes or inline
  prompt) or `null`. Logical role name = physical name minus `<own name>__` and
  any number of adoption tags `rsi<16 hex>_`.
- `tools`: `{name: identity}` for every tool the unit depends on, identity as the
  actual ToolLoader serves it (for a directory tool `{"native": bool, "files":
  {file: sha256}}`).

Domain: null, bool, int, finite float, UTF-8 string, list, string-keyed object.
No coercion: `1`, `1.0` and `true` are different values; NaN, ±Infinity,
tuples, bytes and other objects refuse `MALFORMED_CONTENT`.

`canonical_bytes(content)` = UTF-8 of `json.dumps({"schema":
"rsi.pipeline_version.v2", **content}, sort_keys=True, separators=(",", ":"),
ensure_ascii=False, allow_nan=False)`; floats use Python's shortest round-trip
`repr`. Key order never matters; list order does. Exact bytes and ids are pinned
in `conformance_vectors.json`.

## API (provider object)
| call | result |
|---|---|
| `capture(target)` | `{target, version_id, created, physical_name}` |
| `list_versions()` | sorted list of logical version ids in the catalog |
| `read(version_id)` | `{version_id, content, native}` |
| `current(target)` | `{target, unit_identity, graph_version}` |
| `restore(target, version_id, *, expected_current)` | `{target, version_id, changed, previous_unit_identity, unit_identity}` |
| `canonical_bytes(content)`, `version_id_of(content)` | pure |

There is deliberately no create/pin/finish/run API.

## Behaviour
- **capture** catalogs what an owned generated target serves now. If a seal of
  that logical id exists it returns `created=false` and writes nothing (no new
  physical seal even when native provenance differs, e.g. after a restore).
  Otherwise it seals once through native `seal`. It never changes the target.
- **list/read** re-verify seals (native `verify_sealed` + projection + naming);
  results are fresh copies that callers own. Reading needs no live tools.
- **restore** changes only the named target, only through native
  `adopt_bundle` (CAS + before-change descriptor + one registration
  transaction). If the target already serves that logical content it returns
  `changed=false` and changes nothing. A change never creates history, never
  touches other targets, and keeps every existing paused/running/completed run
  on its SDK graph-version pin with its old resolved prompt bytes (adoption
  retains old roles beside the new bundle-specific roles).
- **history is immutable**: no delete, overwrite or re-key.

## Refusals (loud, before any effect)
Every refusal is an exception with a stable `code`; a refused call leaves seals,
targets, registrations, graph versions, manifests and runs unchanged.
`MALFORMED_CONTENT`, `MALFORMED_VERSION_ID` (whole string `sha256:` + 64
lowercase hex), `MALFORMED_TARGET` (whole string `gen_[a-z0-9][a-z0-9_]{0,59}`,
not `gen_rsi_*`), `MALFORMED_EXPECTED_CURRENT` (64 lowercase hex),
`UNKNOWN_TARGET`, `UNKNOWN_VERSION`, `TAMPERED_VERSION`, `PROVIDER_UNAVAILABLE`,
`PROVIDER_CHANGED`, `CONFLICT`, `UNSUPPORTED_CONTENT` (a unit whose native
seal/adoption would not reproduce its logical content), `NATIVE_REFUSED` (any
other native refusal, message preserved).

Order. capture: target form, known target, providers, content domain, catalog
integrity (tampered), seal. restore: version form, target form, CAS form, known
target, known version, tamper, CAS, no-op, adoption reproduces content,
providers, adoption. `list_versions` refuses `TAMPERED_VERSION` if any catalog
seal fails verification; `read` of another version still works.

## Ownership
- Host-owned (outside this API): tools, ordinary target registration, runs and
  their lifecycle, the SDK graph-version pins.
- Provider-owned: seals it writes (immutable once written) and the adoption it
  performs on the named target. The native binding keeps no state beyond its
  host objects: no version, pin, run or sequence ledger.

## Conformance
`test_contract.run_conformance(make_world)`; `make_world() -> (api, fixture)`.
The fixture performs host actions outside the API: `install_tool`,
`remove_tool`, `register` (ordinary generated config; the vector also states the
content it resolves to, which only the fake uses), `start_run`, `pause_run`,
`complete_run`, `observe_runs` (status, pinned graph version, resolved prompts),
`claim_prompt`, `tamper_seal`, `snapshot` (all observable host state) and
`snapshot_outside(target, since=None)` (all of it except the target's own
files/graph/manifest and target roles absent from `since`).
Vectors: `units` (registered input, `$self` = target), `contents`, pinned
`ids`/`canonical`, `same_id`, `distinct_ids`, `domain_refusals`, `scenarios`.
Step grammar: `expect` is `{error}`, `{result}` (exact, typed) or
`{result_subset}`; `unchanged: true` requires an identical snapshot;
`only_changes: <target>` requires an identical `snapshot_outside` (runs, seals,
tools, other targets and every pre-existing role, retained old roles included)
and a changed full snapshot; `record`
saves a result field as `$rec:<name>`; `$id:K`, `$content:K`, `$pin:<run>`,
`$zero`; `{"$float": "nan"|"inf"|"-inf"}`, `{"$tuple": [...]}`,
`{"$bytes": "..."}` encode non-JSON values. Every refusal is snapshot-checked.
There are no optional scenarios: the same vectors run on every provider.

## Not covered
Concurrency beyond the native `sf._lock` exclusion, boot/restart of the
catalog, candidate derivation (`derive_candidate`), builtin (non-generated)
targets, retirement/deletion, normal runtime/deployment, model or evaluator
execution. Fixture proof is SDK 1.5.92 in an isolated container, not the
critical SDK 1.5.94 runtime.
