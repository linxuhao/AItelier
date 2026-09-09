# Persistent State DAG and workflow execution

## Project UI and migration preparation

The private project DAG viewer, exact-run historical graphs, stable source binding,
project/node dispatch holds and reference-only migration support are documented in
[state-project-ui-migration.md](state-project-ui-migration.md). The new source
binding can supersede the legacy source_project_id dependency before attempts
exist; historical execution IDs remain intact. The viewer is not a graph editor
and does not automatically migrate or approve existing work.

## External harness / State-only mode

State attempts now support either SkillFlow or your own director/subagent/CI
harness. External attempts have no dummy workflow, execution project or Run.
They use the same versioned contracts and evidence/acceptance rules. See
[external harness integration and the standalone State-only server](state-external-harness.md).
The workflow examples below describe the SkillFlow adapter, not a requirement
for every State DAG user.

## What owns what

AItelier now has an additive, long-lived State DAG layer. It does **not** replace
SkillFlow, the existing DPE configs, legacy task rows, or their UI.

| Layer | Responsibility | Not its responsibility |
|---|---|---|
| State project / node | Goal identity, requirement revision, acceptance contract, dependencies, evidence and acceptance receipts | Workflow step order or retries |
| State attempt | One attempt to satisfy one node revision, with an exact SkillFlow run OR external harness execution identity | A second execution engine |
| SkillFlow | Versioned workflow graphs, claims, steps, retries, loops, checkpoint decisions, cancellation and output production | Declaring a product goal permanently verified |
| Driver agent | Read the frontier, choose a goal and a workflow or external harness, inspect evidence and request verification | Keeping authoritative project state in conversation memory |

SkillFlow's graph can contain cycles. Only the **state dependency graph** is
required to be acyclic. The existing SkillFlow `capability` field is a tool grant;
it is not a verified product capability.

The legacy AItelier `runs.project_id` remains the execution-project key. A new
`state_projects.project_id` is a separate long-lived identity. `source_project_id`
on a state project points to an existing legacy project that owns its canonical
source repository. Each **workflow-backed** attempt receives a separate
`sg-<uuid>` execution project and exact SkillFlow run ID. An external attempt
has neither; it pins its harness/job identity and context instead. No legacy
project/task is automatically converted.

## Facets

Nodes carry a `facet` (design / contract / test / content / integration) and the
engine refuses a dependency edge that points at an implementation instead of a
contract or design fact — a `content` node is VERIFIED against its dependencies'
contracts, and real implementations compose only in `integration` nodes. Unfaceted
nodes are legacy and exempt, so a graph migrates one chain at a time; `facet_lint`
lists every violation with the fix. Rules, rationale and the migration recipe:
[design/state_facets.md](../design/state_facets.md).

## State and validity

Persistent node statuses are `OPEN`, `CANDIDATE`, `VERIFIED`, `STALE`, and
`SUPERSEDED`. `ready`, `blocked`, `in_progress`, and `closed` are **derived
readiness**, not workflow steps stored on a node.

A node is ready when its dependencies have current acceptance receipts and it
has no active attempt. Frontier responses are bounded, ranked summaries. Pull
`get_node` for the chosen goal's full contract, dependency receipts and recent
attempts; do not load every report into each agent context.

Each requirement change creates an immutable revision. Changing the goal,
contract or dependency list invalidates the node and all transitive dependents.
New evidence that corrects an accepted observation also invalidates those facts.
Historical evidence and receipts remain available. A superseded node is not
silently reopened; create a replacement and explicitly revise dependent edges.

A parent does not become verified just because its children are verified. Its own
integration/acceptance checks still apply. An acceptance is scoped to:

- exact node revision and contract hash;
- exact dependency acceptance-receipt snapshot;
- exact attempt and execution provenance (SkillFlow run/graph pin, or external harness identity/final observation);
- exact candidate Git commit or output-bundle digest;
- the latest passing evidence for every contract criterion.

This is version-scoped engineering assurance, **not a mathematical proof** and
not a permanent claim about every later source commit. Arbitrary Git edits do
not automatically revise a State DAG node. A driver must record changed
requirements/dependencies and obtain fresh validation for changed artifacts.

## Workflow-backed attempts: completion is only a candidate

The supported lifecycle is:

```text
ready goal → reserved intent → launching → bound SkillFlow run
                                   │             │
                        unknown outcome     running / paused
                                                 │
                                  completed and operations quiet
                                                 │
                                              candidate
                                                 │
                         all required scoped evidence passes
                                                 │
                                              VERIFIED
```

A failed workflow keeps its history and allows a new attempt. A paused workflow
stays active. Unknown engine state, missing graph history, or admitted operations
never count as completion. An old attempt that completes after its inputs changed
is superseded, not applied to the new goal revision.

`request_key` is an idempotency key **within a node**. Reusing the same request and
payload returns the same attempt; reusing it with different arguments is an
error. A new request key means a genuinely new attempt, not a network retry.
Only one active attempt per node is supported in this version.

A launch intent is saved before calling the existing `start_config_run` path.
The exact execution-project key permits recovery when the run was created but
its response was lost. A launch with no observable result is held as `unknown`;
recovery does not blindly dispatch another worker. `retire_reservation` only
retires a still-undispatched `reserved` intent. It cannot pretend a launching,
unknown, running or paused worker has stopped. An uncertain launch without a
recoverable run needs operator investigation; there is deliberately no
force-reset command that assumes effects have ended.

The source/seed/output/driver contract is pinned before dispatch. Later edits to
a current config manifest cannot redirect an old attempt's artifact lookup.
For a code-backed dependency, its accepted commit must be present in the source
ancestry before launching, and in the actual candidate ancestry before acceptance.
A separately validated feature branch may therefore need integration before a
code-dependent goal can use it. Squash/cherry-pick equivalence is not guessed.

## Trust boundary for evidence

`record_evidence` is an **authorized verifier attestation**. The verifier supplies
a report reference, a SHA-256 of the report, an exact artifact identity and a
`pass`, `fail`, or `skip` verdict. The service validates the scope, contract,
latest-attempt identity and completeness. It does not execute an arbitrary
command contained in the report or infer a pass from model prose.

This version does not cryptographically establish that a remote verifier told
the truth, fetch arbitrary report URLs, or run every test from an uploaded JSON
file. Keep verifier credentials controlled, use real test/review reports, and
retain their immutable bytes in an evidence store. A false attestation from an
authorized writer is outside this trust boundary. Do not treat an agent's
`done` message, a passing process exit alone, or `skip` as a passing criterion.
The offline demo below really runs its tests, including an intentional failure.

The transport derives reviewer identity from the authenticated request or the
internal driver's owner. A body cannot override `reviewer`. State project data
is private to authorized writers; there is not yet a separate per-state-project
ACL or a distinct cryptographic verifier role. Normal host writer authority is
therefore administrative authority over this state graph.

## Driver interfaces

External MCP and the internal MetaAgent expose the same three tools:

1. `state_graph_help()` returns the exact argument schemas and trust boundary.
2. `state_graph_read(action, arguments)` performs a finite typed query.
3. `state_graph_write(action, arguments)` performs a finite typed command.

These are explicit allowlists, not reflection, SQL, filesystem or shell RPC.
Unknown actions, extra fields and incorrect argument types are rejected.
MCP domain errors use `isError: true`. State-data MCP reads require writer
authorization even though other legacy metadata reads may be public.

Read actions: `list_projects`, `get_graph`, `get_node`, `frontier`, `events`,
`get_attempt`, `list_attempts`, `evidence`.

Write actions: `create_project`, `add_nodes`, `revise_node`, `split_node`,
`supersede_node`, `start_attempt`, `start_external_attempt`, `report_external_attempt`,
`recover_attempt`, `reconcile_attempt`,
`retire_reservation`, `record_evidence`, `verify_node`, `import_tasks`.

The MCP prompt `state_graph_driver` describes the intended loop. Workflow
completion is observed by the driver using `reconcile_attempt`; this version
does not add an autonomous project-planning scheduler or auto-acceptance hook.
State inspection/planning remains available without composing a working
SkillFlow executor; only execution and acceptance operations require it.

### Example: create and decompose a project

First use the existing project-list interface to identify the canonical source
project. Replace `existing-source-project` below with its exact legacy ID.

```json
{"action":"create_project","arguments":{
  "project_id":"shrimp-state",
  "title":"Shrimp game",
  "source_project_id":"existing-source-project"
}}
```

Create dependencies in one transaction, including forward references:

```json
{"action":"add_nodes","arguments":{
  "project_id":"shrimp-state",
  "nodes":[
    {"key":"monthly-actions","goal":"Each player can plan two bounded monthly actions",
     "dependencies":["proficiency"],
     "acceptance":[
       {"id":"budget","kind":"test","description":"Reject a third action without double charging"},
       {"id":"coop-month","kind":"integration","description":"All same-month encounters finish before advancing"}
     ]},
    {"key":"proficiency","goal":"Progress is saved independently per player and training target",
     "dependencies":[],
     "acceptance":[
       {"id":"persistence","kind":"test","description":"Save/load preserves exact progress and overflow"},
       {"id":"review","kind":"review","description":"Source, failure cases and report are reviewed"}
     ]}
  ]
}}
```

Query the compact selection surface:

```json
{"action":"frontier","arguments":{"project_id":"shrimp-state","limit":10}}
```

Then pull `get_node` with `project_id` and `node_key` for full context. Unknown,
self-referential, duplicate, cross-project or cyclic dependencies roll back the
entire command. `split_node` adds children and revises the parent's dependency
list atomically while retaining the parent's acceptance contract.

### Launch, reconnect and observe

Use `list_pipelines` to select an appropriate existing, **seeded** workflow.
For example, `coding_impl` accepts an approved plan and has its existing code and
test execution; a self-contained generated workflow can also be selected.

```json
{"action":"start_attempt","arguments":{
  "project_id":"shrimp-state","node_key":"proficiency",
  "expected_revision":1,"workflow":"coding_impl",
  "request_key":"proficiency-attempt-1",
  "instruction":"Implement the goal and its explicit acceptance cases. Do not change unrelated gameplay."
}}
```

Keep the returned `attempt_id` and `run_id`. Use existing `wait_for_run`, trace,
output and checkpoint tools for the workflow. The state launch bridge always
attaches the existing driver with `auto_approve=False`; it never answers a human
checkpoint. SkillFlow still determines graph/checkpoint semantics: review gates
must be declared on actual workflow transitions, not assumed from a final step's
name.

After context loss or a lost response, use `get_node`/`list_attempts` and then
`recover_attempt` with the exact attempt ID. Do not invent another request key.
`reconcile_attempt` observes the current run and derives an artifact for a
completed, quiet candidate. For code, the run-owned tree must be committed and
clean. This version supports 40-hex SHA-1 Git commits; SHA-256-format Git repositories
are retained with an explicit unsupported-format message, not misread as output bundles. For output-only work, the declared output step must contain a nonempty,
bounded set of regular files; symlinks and missing artifacts are refused.

Some existing DPE graphs require finalized meta-conversation outputs in the
execution project's workspace. A fresh node attempt does not fabricate those
outputs or mark them approved. Such a workflow is refused with a prerequisite
message. Use a self-contained seeded workflow for node delivery, or prepare its
real producer artifacts through the existing supported pipeline path. The
legacy DPE entry flow remains available unchanged.

### Record real evidence and accept

Replace placeholders with the exact candidate artifact and actual report digest:

```json
{"action":"record_evidence","arguments":{
  "attempt_id":"attempt-REPLACE",
  "evidence_id":"proficiency-persistence-check-1",
  "criterion_id":"persistence","verdict":"pass",
  "artifact":"EXACT_CANDIDATE_GIT_SHA_OR_BUNDLE_DIGEST",
  "report_ref":"immutable-evidence/proficiency/persistence.json",
  "report_sha256":"EXACT_64_HEX_SHA256_OF_REPORT",
  "detail":"Real persistence test report; no skipped cases."
}}
```

Record the required review separately. Then:

```json
{"action":"verify_node","arguments":{
  "project_id":"shrimp-state","node_key":"proficiency",
  "expected_revision":1,"attempt_id":"attempt-REPLACE"
}}
```

Missing/failed/skipped criteria, a wrong artifact, a stale requirement or
dependency receipt, or a superseded attempt prevents acceptance. A newer
attempt supersedes old candidates for acceptance purposes. Verification retries
with the same evidence are idempotent. A new correcting evidence record is
append-only and invalidates previous acceptance; it does not erase the first
failure.

### Change requirements

```json
{"action":"revise_node","arguments":{
  "project_id":"shrimp-state","node_key":"proficiency",
  "expected_revision":1,"reason":"Owner changed the growth rule",
  "goal":"Updated progression rule with explicit revised scope"
}}
```

This creates revision 2 and marks dependent facts stale. A late completion from
revision 1 cannot certify revision 2. Use `events` with `after` and `limit` for an
incremental audit view, not a full transcript dump.

## REST and SDK

All `/api/state` routes use the existing writer authorization dependency,
including GETs. MCP's external token is not automatically a REST admin token;
use the host's supported authenticated channel for each transport.

```text
GET  /api/state/schema
GET  /api/state/projects
GET  /api/state/projects/{project_id}
GET  /api/state/projects/{project_id}/frontier?limit=30
GET  /api/state/attempts/{attempt_id}
POST /api/state/query/{read_action}       JSON body = arguments only
POST /api/state/commands/{write_action}   JSON body = arguments only
```

The shared Python implementation is `StateGraphStore`, `StateAttempts`, and
`StateService` in `core/state_*.py`. Libraries require explicit DB objects;
there is no production-default path in these modules. `StateService` adds
standard launch composition and real artifact derivation. Low-level
`StateAttempts.record_evidence` is a trusted integration API, not an untrusted
report verifier.

`import_tasks(project_id, source_project_id)` is an explicit one-time snapshot of
legacy tasks. Original tasks are not deleted or modified. Old completed tasks
become unverified candidates, superseded tasks stay superseded, and dependency
errors roll back the import. Replace imported placeholder criteria with concrete
acceptance requirements and revalidate; old task completion is not evidence.

## Offline demonstration and tests

```sh
python examples/state_graph_demo.py --report /tmp/state-demo.json
pytest tests/unit/test_state_graph.py tests/unit/test_state_attempts.py \
       tests/integration/test_state_graph_entrypoints.py
```

The demo uses temporary SQLite databases, real SkillFlow claims, real Git
commits, and Python subprocess tests. Its first implementation intentionally
fails a test while the workflow completes. The goal remains unverified. A
corrected attempt passes and unlocks the dependent goal. A new driver object
reopens the same state, completes the dependent goal, and an upstream requirement
revision subsequently invalidates its historical acceptance. It makes no LLM,
network, production database or deployment calls.

## Storage, rollout and recovery

The new `state_*` tables are additive and initialized on first state-service
construction. Existing `runs`, `tasks`, SkillFlow tables, task-loop behavior and
public UI are not migrated. This release adds API/MCP/driver access, not a visual
graph editor. State event/revision/evidence/receipt tables reject UPDATE/DELETE
through SQLite triggers; this is integrity protection, not protection from a
privileged operator replacing the entire database.

Keep report bytes outside disposable worktrees. Worktree deletion and branch
integration are separate from goal verification; this feature does not change
the existing reaper or turn a PR creation into a goal acceptance.

Deploy only a reviewed code revision and restart/reload the backend through the
normal deployment procedure. A commit on disk does not prove a running worker
loaded it. Back up the database before deployment; rollback can return to older
code while leaving the additive `state_*` tables intact. Do not drop evidence
history during rollback. Work already running remains governed by its SkillFlow
run identity and pinned graph; do not resume a vanished worktree against a
shared source checkout.

Current boundaries: one active attempt per goal; dependencies stay inside one
state project; no automatic Git-change impact analysis, PR status monitoring,
remote evidence signature verification, arbitrary cross-config seed copying,
visual editor, or automatic whole-game state import. These are explicit future
extensions, not hidden promises of this implementation.
