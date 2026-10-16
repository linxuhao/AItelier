# RSI pipeline benchmark: data interface and standalone fake

SOURCE CORRECTION CANDIDATE. Existing acceptance-only readback@r2 is retained;
focused fake/compile results belong to the Source report. Real-provider conformance
is unmeasured. This README is normative; literal shared vectors are reusable through
`test_contract.run_conformance(factory)`, where `factory() -> provider` exposes the
methods below. No real-provider result is implied by running the fake.

## What this is not
This is a data interface. It does **not** authenticate who executed anything, prove that private material stayed private, prove cost/duration/outcome truth, or authorize spend, eligibility or adoption. Caller strings are unauthenticated labels; no JSON id, digest or fake method proves real ownership, auth, privacy or accounting. The fake runs no model, evaluator, checker or scorer, loads no files or providers (the test harness reads only the vectors file), and imports no production scorer. The already-accepted pure scorer is a separate production subset and not a benchmark registry. A dict fake says nothing about real host behavior, and no real-provider binding may be inferred from it.

Only invented synthetic fixtures are used: 20 synthetic case ids and opaque placeholder private references. No real task, gold, holdout or private scoring text belongs here.

## Fixed format
- Schema string: `rsi.benchmark.v1`. Any other schema is refused (`unknown_schema`).
- A benchmark is `benchmark_id` + integer `version` >= 1 with `status` `draft` -> `published`. Id charset `[a-z0-9][a-z0-9._-]{0,63}`.
- Exactly 20 cases: 12 `train`, 8 `holdout`, disjoint (each case has one partition). Fixed family inventory `family-a..family-d`; every family must appear in each partition at publish.
- `arms`: 1..8 unique ids. 3 repetitions per arm per case (repetition 1..3).
- Frozen `point_total` (integer >= 1). Each case carries private `max_points` (integer >= 1); their sum must equal `point_total`. Normalization is exactly integer `{min:0,max:100}` (bool and float equivalents
  such as `False`, `0.0` or `100.0` are refused): case score = `100 * points / max_points`.
- Each case separates **public input** (`{prompt}`, safe to show) from **private references** (`{rubric_ref, checker_ref, max_points}`, opaque strings, never returned by public views).
- Publishing derives per-case `input_identity` (digest of public input), `checker_identity` (digest of private refs) and a `definition_digest`. Digests are `sha256:<64 hex>` of canonical JSON.

## Scoreboard row (append-only)
Fields, all required, no extras: `schema, benchmark_id, version, bundle_identity` (sha256 of the whole pipeline bundle), `runtime` (`route`, `endpoints` non-empty unique list, `effort`; pinned), `case_id, repetition, arm, run_receipt_id` (unique across the benchmark world), `input_identity, checker_identity` (must equal the published ones), `validity` (`VALID|INVALID|INFRASTRUCTURE|UNKNOWN_ACCOUNTING`), `outcome` (`scored|task_failure|none`), `raw_points, cost_usd, duration_s`.
- `VALID`: outcome `scored` (raw_points a finite number in `0..max_points`) or `task_failure` (raw_points must be null); `cost_usd` and `duration_s` must be known finite numbers >= 0.
- Non-`VALID` rows: outcome `none`, raw_points null; cost/duration may be null or known. They are retained, never dropped.
- Numbers must fit finite binary64 conversion; integer-valued fields remain integers,
  not coerced floats. Bool, NaN, Inf, negatives, overflowing integers and strings are
  refused (`invalid_value`). Aggregate cost/duration totals must also remain finite;
  otherwise aggregation refuses `invalid_value` without changing retained rows.
  Normalization divides points by max_points before multiplying by100 to avoid
  an overflowing intermediate for an otherwise bounded score.
- The fake adds `seq` and `recorded_by` (unauthenticated label) to stored rows.

## Methods
All refusals raise an exception with stable `.code` (the fake uses `BenchmarkError`);
nothing is silently coerced or ignored. Refusals write nothing. Returned structures
are deep copies. Inputs are acyclic JSON data, not arbitrary Python objects. Objects
have string keys; nonempty text must be UTF-8 encodable. IDs and SHA identities match
the entire string, including refusal of a final newline. Malformed IDs are
`invalid_value`; well-formed absent benchmark/case/arm IDs keep their `unknown_*` code.
Runtime endpoint elements are validated as text before duplicate detection. The
append and aggregate(expected) entrypoints enforce the same runtime validator.
- `create_draft(caller, header)`: header has exactly `schema, benchmark_id, version, arms, families, cases, point_total, normalization`. Records `caller` as draft owner. Errors: `unknown_schema`, `unknown_field`, `invalid_value`, `duplicate` (id+version already exists in any status).
- `set_public_input(caller, id, version, case_id, {prompt})`, `set_private_refs(caller, id, version, case_id, refs)`: owner-only on drafts. Errors: `unknown_benchmark`, `immutable` (published), `ownership`, `unknown_case`, `unknown_field`, `invalid_value`. Setting again before publish replaces the draft value.
- `publish(caller, id, version)`: owner-only; requires every case to have public and private material, family coverage per partition and the point sum. Errors: `incomplete`, `immutable`, `ownership`. Irreversible; there is no unpublish and no edit afterwards.
- `get_definition(id, version)`: public view only (never private refs or `max_points`). Any caller.
- `get_private_refs(caller, id, version)`: creator-only (label match, not authentication). Error `ownership`.
- `list_benchmarks()`: id/version/status, sorted.
- `append_receipt(caller, row)`: requires a published benchmark (`not_published`). Refuses unknown schema/benchmark/case/arm/repetition (`unknown_*`), unknown/missing fields, identity differences (`identity_mismatch`), a reused `run_receipt_id` (`duplicate_run_receipt`) and a second row for the same (bundle, runtime, arm, case, repetition) cell (`duplicate_cell`). Never replaces, edits or deletes; returns the new `seq`. Retained rows are not corrected: a repeat attempt needs a new cell identity (e.g. a different pinned bundle/runtime) and cannot overwrite.
- `list_receipts(id, version)`, `scoreboard_snapshot(id, version)`: ordered rows, `row_count`, `snapshot_digest`; an earlier snapshot's rows are always a prefix of a later one.
- `aggregate(id, version, expected)`: `expected` maps **every** arm to `{bundle_identity, runtime}`. Requires published (`not_published`); unknown arm -> `unknown_arm`; malformed -> `invalid_value`.

## Aggregate validity (revision r2)
The earlier wording "invalid runs are excluded" is revised: a missing or non-`VALID` result is never excluded, never scored 0, and never leaves a smaller subset to score. The matrix is `arms x 20 cases x 3 repetitions` (120 cells for two arms), each cell matching the expected bundle and runtime identity exactly. If any cell is missing (including cells whose rows carry a different bundle/runtime) or non-`VALID` (invalid, infrastructure, unknown accounting), `aggregate` returns `status: "INVALID"` with `missing_cells`, `invalid_cells` (retained row receipt ids) and `arms: null`: no statistic at all. Only a complete matrix returns `status: "COMPLETE"` with per-arm, per-partition mean normalized score (case mean over repetitions, then mean over cases), total cost and duration. The only fixed zero is a known settled actual task failure (`VALID` + `task_failure`), scored 0 points. Statistics from an incomplete matrix are not meaningful and must not be computed elsewhere from this data.

## Ownership and trust summary
Owner = the label that created the draft; it controls drafts and private refs. Receipt recorders are unconstrained labels. Real systems must supply authentication, private-material custody, receipt provenance and spend authority; this interface and fake do not.

## Shared vectors
`conformance_vectors.json` is literal: fixture, expected errors and expected numbers (derived by hand, not by the fake). Covered: fixed inventory/partition/repeats, publication immutability, incomplete publish refusal, public/private separation, ownership, unknown inputs, fixed identities, append-only and duplicate rejection, finite known accounting, incomplete and invalid matrices reported INVALID, genuine task-failure zero, exact bundle/runtime identity matching. Any other implementation claiming this contract must pass the same vectors through
`run_conformance(factory)`. The harness compares refusal codes and checks that
refusals leave public/private definition and receipt views unchanged. The returned
failure list is empty only for the exercised vectors; a future real-provider binding
remains a separate required proof.
