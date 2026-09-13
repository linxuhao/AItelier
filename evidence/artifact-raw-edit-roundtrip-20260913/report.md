# artifacts.read-edit-roundtrip@2 candidate report

Attempt: `attempt-b026b5b915e64fcbb077d742ed0ccce3`
Context hash: `ef1d154d82932b8682499ba2a8cbff9640f5e7ed1945e904030b0c15b04af1bb`
Contract hash: `e478ed6a3a35e0b3db6ca647df900ba00507d7cea219c5d7bd663398797e8d60`
Base: `d64967c7974012ca928918ea7945b374847a3e96`
Implementation commit: `df614111cebe8c7df3e95adfe9fb4bc4bed134b4`
Implementation tree: `15917fc785fb0a91b3fbff78cc2b586b81a900b4`
State evidence commit: `3dac3af12df8cd7e1558cfbdc79175596d8bb88b`
State evidence tree: `4abf7e0d5f104c58b5410fbbfeba57968dbb6352`

## Candidate change

The candidate delegates raw artifact reads, candidate routing, paging, deletion tombstones, and ownership to SkillFlow 1.5.75. It removes the earlier host filesystem override, which could fall back to a promoted artifact after staging deletion. The revision prompt preserves the first strict `old_str` miss, then requires one exact native raw reread with `source="self"` and one local exact edit. It explicitly tells the agent to page large files with `start_line`/`end_line` and, after a projected result, use the marker digest with a bounded `recall_observation` range. It forbids projections, line numbers, fuzzy matching, broad reruns, and whole-file rewrites.

The corrected native loop labels tool messages and keeps every provider-visible `recall_observation` response, including its JSON envelope, below 16 KiB. Range replies preserve exact contiguous source slices and continuation offsets. Grep replies clip very long lines around the match and stop before their complete envelope reaches the limit. The normal projection rule also applies to recall messages, so an oversized reply cannot bypass the provider boundary. SkillFlow remains the owner of read routing, staging, deletion visibility, and strict atomic edit execution.

## Real native-loop trace

The generated remote trace is `/home/linxuhao/.AItelier/workspaces/r4-native-loop-df61411/real-trace/native-loop-trace.json`, SHA-256 `2fc43106417a7397c94d17618e5be103204d20e5dcdfeef8c6e071fbf22e80be`. The committed [model-trace-deepseek-flash.json](model-trace-deepseek-flash.json) is a derived envelope with SHA-256 `581da0dfe225d2c9fef9488bcfa7da663f13ebc05a55600434b647db6ec1edba`; it retains the native events and provider responses while adding pricing provenance and a rewritten top-level result summary. The run exercised the implementation commit/tree above through the actual `PromptAssembler`, `PipelineEngine._run_native_step`, native SkillFlow tools, and one sticky `deepseek/deepseek-flash` gateway. The model produced this sequence:

1. `edit(report.md, old_str="STALE-PROJECTION")` failed strictly and the first failure was preserved.
2. `read(report.md, source="self", raw=true, start_line=695, end_line=705)` reread current staging bytes.
3. A single `artifact_dump` produced a 28,408-character result; the provider view projected it from 28,408 to 6,847 characters behind digest `1f259df09ebee8fd`.
4. `recall_observation(sha256="1f259df09ebee8fd", start=9500, end=10200)` returned exactly 700 characters from the prefix region; it did not contain the unique middle snippet. The model had already obtained that snippet from the bounded native raw page read in step 2.
5. One strict local edit replaced `MIDDLE-UNIQUE-🙂-value` with `MIDDLE-UNIQUE-🌙-value`; the final bytes match the expected single replacement and untouched regions remain byte-identical.
6. The ambiguous `read(missing/target.md, source="self", raw=true)` returned `a/target.md` and `b/target.md`; the model refused to guess and made no further edit.
7. It finished without calling `write`, `create`, or a whole-file rewrite.

The seven provider response IDs are retained in the trace and metrics: `49410d51-2aba-4e2f-b5cb-18f8f0c4f218`, `03213f12-7644-45ca-8e24-e9335eb89d04`, `2d103ce2-9cbd-4a83-8fad-673ebd34d6bb`, `31cfec7b-6fe6-47f1-ada2-3310efe01edd`, `6bb63d26-5450-4b28-9ff6-8396a9a49d56`, `02ff29f2-ffda-4358-ab0d-f80afd2ec4f6`, and `9e79b1dd-eaaf-49c1-ab81-c682b07a0381`. The assembled system/user payloads and all native trace events are retained in the content-addressed trace JSON. The route/pricing snapshot is `edeb94775cc3a8a6a164499ef3075b62178c73a298b25c68aeb947d8003852b7`, from the container's `/app/model_routes.json`.

The trace used 29,623 prompt tokens (27,392 cache-hit and 2,231 cache-miss) and 972 completion tokens. The DeepSeek note prices these at $0.003/M cache-hit input, $0.15/M cache-miss input, and $0.60/M output, with a 2x weekday peak multiplier. The 2026-09-13 Sunday trace is estimated at `$0.001000026` off peak; the exact formula and per-turn usage are in `metrics.json`.

## Deterministic coverage

`tests/unit/test_artifact_raw_edit_recovery.py` covers Markdown, JSON escaping, Unicode, tabs, LF/CRLF, exact raw round-trip, untouched-byte preservation, multiple-match atomic refusal, candidate-only deletion, native paging, and the first-failure prompt contract. `tests/integration/test_artifact_native_projection.py` drives the real SkillFlow and host native loop through a >16 KiB projection, bounded recall, exact edit, ambiguity refusal, and no whole rewrite.

Validation with the SkillFlow 1.5.75 source was:

```text
5 passed
git diff --check: clean
```

The prior production traces remain preserved in `metrics.json` with their first failures, model usage, and private source references.
