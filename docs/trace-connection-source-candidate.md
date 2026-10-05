# Coordinated trace connection source candidate

This source-only change requires the unpublished SkillFlow `1.5.81+trace1`
candidate based on official SDK commit `bb41782832b807f2bb2a1b21eb4a48fd29bc4382`.
It is not a public package release or deployment.

The SDK scopes raw trace connections with `trace_connection(project_id)`,
covering the entire SQL transaction or cursor materialization under the engine
RLock. Idle SQLite connections use a 32-entry LRU; actual nested borrows pin
entries until release. Invalidation waits for active borrows to release.

The host recovery-decision transaction and DPE retained-conversation read now
use this scope. The recovery test's trigger setup/cleanup does likewise.
This removes the two production raw borrows before/without the engine lock.
Existing durable recovery decisions, output rows, failure fencing and DPE replay
semantics remain the focused regression controls. Reads now serialize with
engine transactions, which is necessary for safe reuse and eviction.

Both the SDK and host source bundles must be reviewed together. The candidate
pin records the coordinated dependency and is unavailable from public PyPI;
installation or release requires a later separately authorized delivery step.
