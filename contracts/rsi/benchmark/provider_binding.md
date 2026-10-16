# Production benchmark data binding

`core.pipeline_benchmark.BenchmarkStore(path)` is the first production data provider
for this interface. The trusted host supplies an explicit private JSON document
path; no default/global store, application registry or runtime is initialized.
Definitions and their private opaque references, immutable publication identities
and append-only scoreboard rows are business data in that document. An ordinary
file lock serializes document mutations; validated writes use atomic replacement.
Reopening the same path, including from another process, reads the same definitions
and receipt history. Refused mutations leave document bytes unchanged. There is no
operation-owner ledger, service, executor, campaign, budget admission or adoption.
The filesystem and caller labels are trusted; this API is not an authentication or
private-file access boundary. Atomic replacement does not claim power-loss recovery.

The existing README/fake/vectors/harness remain byte-identical. The same
`run_conformance(factory)` accepts either `BenchmarkFake` or a factory creating
BenchmarkStore instances at distinct owned paths. It never converts the real store
into the fake or loads contract implementation code from production. The Source
unit test supplies that factory and also checks actual persistence/reopen/refusal.

The accepted `core.pipeline_optimization` module is included byte-identically from
5c90420a2a7c40e64e562ce06930c85fb8becb9c. Constants and its exact normalized-score
validation/known-task-failure zero are reused. Normalization uses exact rational
arithmetic for finite JSON numbers (floats use their decimal repr), then display
statistics become floats; finite binary64 input and total-accounting bounds remain.
The full scorer manifest/API is deliberately not misrepresented as this broader
contract: it fixes two arms and arm-wide checker IDs; this data format supports1–8
arms and per-case checker IDs. No schema coercion, renamed arm, fabricated checker
identity, threshold change or holdout selection result is introduced. This provider
reports complete matrix means and accounting only, and grants no eligibility.

This is Source data-interface conformance, not existing normal registry integration,
installed deployment, actual evaluation, receipt truth, secrecy or publication.
Public views are projections, not authorization to disclose real case IDs/scores.
Conformance inputs are invented fixture data only. Real callers must establish
execution identity, accounting truth, private custody and spend/adoption authority
outside this data interface before making any actual experiment.
