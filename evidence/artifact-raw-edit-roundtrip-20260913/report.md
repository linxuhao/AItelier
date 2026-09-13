# artifacts.read-edit-roundtrip@2 candidate report

Attempt: `attempt-5124ee635412490b815bb4b5e52eca15`
Context hash: `3498727c2ae1588467b450dfe72d26f7c17706078cd5135a9cfc9a1e2a1970ac`
Contract hash: `e478ed6a3a35e0b3db6ca647df900ba00507d7cea219c5d7bd663398797e8d60`
Base: `d64967c7974012ca928918ea7945b374847a3e96`

## Candidate change

The candidate delegates raw artifact reads, candidate routing, paging, deletion
tombstones, and ownership to SkillFlow 1.5.75. It removes the earlier host
filesystem override, which could have fallen back to a promoted artifact after
staging deletion. The revision prompt treats the first strict `old_str` miss as
preserved failure evidence, then requires one exact native raw reread with
`source="self"` and one local exact edit; it forbids projections, line numbers,
fuzzy matching, and whole-file rewrites. SkillFlow's existing strict edit
executor remains unchanged, including its zero/multiple-match refusal and
atomic write behavior.

## Real trace diagnosis

The raw trace databases were copied read-only from the private production
workspace and retained beside this report at `_raw_trace_sources/`. Their
SHA-256 values and source paths are in `metrics.json`; the databases themselves
are intentionally not committed because they contain private game prompts and
responses.

Run `4a2d71bf` has eight artifact edit failures: seven `edit_design` misses in
step 2, followed by one `edit_tasks_manifest` miss in step 3. The first errors
are retained at trace sequences 527 and 877. The model requested raw reads only
after the repeated step-2 misses (two) and reread once after the step-3 miss;
one step-3 exact multiline retry succeeded. A later full manifest write was
also recorded. Run `2742f7a4` has seven consecutive step-2 `edit_design`
misses; two raw-read requests appeared before the final misses, with no local
successful retry, followed by a later full manifest write. These are real
traces with inexpensive model routes including Qwen Flash, DeepSeek Flash, and
GLM Flash; usage counts are preserved in `metrics.json`.

The trace shows that models already attempted a `raw=true` spelling in places,
but the deployed read surface did not make that contract explicit or host-owned.
The candidate closes that gap and changes the recovery instruction at the
owning prompt/dispatch layers. It does not weaken exact matching or ambiguity
handling.

## Live candidate trace

The immutable raw trace is [model-trace-deepseek-flash.json](model-trace-deepseek-flash.json)
with SHA-256
`f2153f13bc78900cc3bc0362eed95a5fbef5d4f6cdc3c62f8ee2efbef36b8531`.
It ran against an isolated candidate/promoted workspace using SkillFlow 1.5.75
and one sequence-bound `deepseek/deepseek-flash` gateway. The real model
produced the required sequence: turn 1 exact edit miss, turn 2
`read(source="self", raw=true)` of the current candidate, turn 3 exact local
edit success, turn 4 ambiguous basename read returning both candidates, then
turn 5 stopped without a whole-file write. All five turns stayed on the same
served endpoint. Usage totals were 5,035 prompt tokens (3,584 cache-hit and
1,451 cache-miss) and 607 output tokens.

Pricing provenance is the container's `/app/model_routes.json` DeepSeek Flash
note: $0.003/M cache-hit input, $0.15/M cache-miss input, and $0.60/M output,
with a 2x weekday peak multiplier. The 2026-09-13 Sunday trace is estimated at
`$0.000592602` off-peak using the formula recorded in `metrics.json`.

## Deterministic coverage

`tests/unit/test_artifact_raw_edit_recovery.py` covers Markdown, JSON escaping,
Unicode, tabs, LF/CRLF, exact raw round-trip, untouched-byte preservation,
multiple-match atomic refusal, candidate-only deletion, native paging, and the
first-failure recovery prompt. With the downloaded SkillFlow 1.5.75 wheel on
`PYTHONPATH`, all four tests pass. `python3 -m py_compile` and diff checks also
pass. The repository's normal pytest collection remains blocked by its local
missing `mcp` dependency; the test was run directly against the deployed
SkillFlow 1.5.75 source instead.
