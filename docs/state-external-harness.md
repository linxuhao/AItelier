# State DAG with your own harness or director subagents

State DAG now treats **SkillFlow** and **external harnesses** as execution
adapters. Both use the same node revisions, dependency receipts, per-criterion
evidence and acceptance rules. A project may use either or mix them.

A director can create a State attempt, run its own subagents/CI/proof checker,
then submit their reports. No dummy workflow, synthetic SkillFlow run, execution
workspace or model configuration is required for an external attempt.

## What changes, and what does not

| Owner | Responsibility |
|---|---|
| AItelier State DAG | Goal/contract versions, DAG consistency, dependency validity, attempt identity, evidence and acceptance history |
| SkillFlow adapter (optional) | Run/step execution, graph version, checkpoint, operation/liveness observation |
| External harness | Spawn/coordinate subagents, choose tools, run tests/proof checks, stop/wait for workers, preserve exact outputs and report bytes |
| Authorized verifier/director | Report actual scoped evidence and accept the contract, not merely say "done" |

`workflow`, `execution_project_id`, `run_id`, `graph_version`, and `graph_digest`
are **null** on an external attempt. Its identity is `execution_kind=external`,
`harness`, `external_id`, and the authenticated `reporting_actor`. Graph pins
belong to the SkillFlow adapter, not to the universal meaning of VERIFIED.

An external completion is still only `CANDIDATE`. Required failing, skipped,
missing, wrong-artifact or stale evidence cannot become VERIFIED. A root goal
verified by a custom harness can unlock a workflow-backed child, and vice versa.
Changing a requirement or correcting an accepted outcome invalidates downstream
facts irrespective of which executor originally produced them.

## Agent onboarding and change waiting

Read the MCP prompt `state_graph_driver`, resource `aitelier://state/driver-guide`,
or the `driver_guide` field of `state_graph_help`. They share the same protocol;
clients need not support prompts to follow it. See [state-agent-driver.md](state-agent-driver.md).
Use the typed `wait_for_state_change` read action with a durable `after` cursor
instead of repeatedly asking a model to query status. Completion remains a
candidate; waiting never approves a checkpoint or verifies a goal.

## Register → execute externally → report → verify

Use the existing authenticated State APIs:

- MCP: `state_graph_help`, `state_graph_read`, `state_graph_write`.
- HTTP: `POST /api/state/commands/{action}` with the arguments as the JSON body.
- Internal MetaAgent: the same typed State tools; no special workflow is needed.
- Python embedding: `StateService` with an explicit database provider.

### 1. Register the external attempt first

MCP arguments:

```json
{
  "action": "start_external_attempt",
  "arguments": {
    "project_id": "my-project",
    "node_key": "growth.proficiency",
    "expected_revision": 1,
    "harness": "director-subagents",
    "external_id": "director-session-42/validation-job-7",
    "request_key": "proficiency-revision-1-attempt-1",
    "instruction": "Run the implementation and verifier subagents against the frozen acceptance contract."
  }
}
```

The response supplies `attempt_id`, the complete frozen `context`,
`context_hash`, dependency acceptance snapshots, and `observation_version=0`.
Pass that exact context to your harness. Registration reserves the goal and
records a caller-owned in-progress attempt; it **does not** launch your subagent
or prove that a remote process is alive.

A project/node hold or unmet dependency rejects registration. SkillFlow and
external attempts compete for the same single active-attempt slot per node.
Different independent nodes may still be handled concurrently. One harness may
use many subagents within this attempt to satisfy its criteria.

Register before dispatching remote workers. If the HTTP response is lost, repeat
with the same request key and identical payload. It returns the same attempt.
Reusing a key with changed arguments fails. External IDs are unique within a
project/node/harness, so a new implementation attempt must use a new execution
identity as well as a new request key. Sharing one external job across several
goals means registering a separately scoped attempt for each goal, not bypassing
any goal's contract or concurrency constraints.

### 2. Run your harness; report a settled candidate

The external system owns its work directory, worker budget, artifact retention
and actual verification. AItelier does not fetch report URLs, execute their
contents, mount your repository or remotely invoke shell commands.

After all relevant workers and artifact writes settle, submit:

```json
{
  "action": "report_external_attempt",
  "arguments": {
    "attempt_id": "attempt-REPLACE_WITH_RETURNED_ID",
    "observation_id": "final-output-1",
    "expected_version": 0,
    "context_hash": "REPLACE_WITH_RETURNED_64_HEX_CONTEXT_HASH",
    "status": "candidate",
    "artifact": "REPLACE_WITH_EXACT_ARTIFACT_DIGEST",
    "artifact_kind": "sha256",
    "report_ref": "immutable-reports/job-7/final.json",
    "report_sha256": "REPLACE_WITH_ACTUAL_64_HEX_REPORT_HASH",
    "quiescent": true,
    "detail": "Implementation and verifier workers finished; output bytes are immutable."
  }
}
```

These placeholders are illustrative; send the actual returned IDs and hashes.
`artifact_kind=git-sha1` requires a 40-hex commit; `sha256` requires a 64-hex
artifact/bundle digest. Do not label a branch name or a mutable path as a digest.
The final report should identify the exact source/artifact and the dependency
receipts its checker used. For a repository outside AItelier, the external
harness is responsible for proving the code actually includes those dependencies;
the State server checks the frozen receipt scope, not remote Git ancestry.

Allowed progress reports are `running`, `paused`, `unknown`, `candidate`, and
`failed`, **not VERIFIED**. Every observation has an idempotency ID, an expected
version, the original context hash, and report reference/hash. Each successful
append increments the observation version. Repeated identical observations do
not append again; altered replay payloads and out-of-order versions are rejected.

`candidate` and `failed` require explicit `quiescent=true`. This is the external
reporter's statement that its workers/operations have settled, not AItelier's
own process inspection. A timeout or lost callback should be `unknown`; it keeps
the active slot. No expiry automatically assumes the workers stopped. The
harness must resolve and report that outcome. A project hold prevents new work,
but does not pretend to cancel or suppress observations from existing work.

A settled candidate's artifact cannot be silently replaced. Submit evidence for
that artifact, or begin a new attempt for changed code. A candidate may be
retracted with a new settled `failed` observation; this invalidates its previous
acceptance and downstream receipts. A failed/superseded attempt is not resumed
as a new execution. Completed observations whose goal/dependency versions are
stale are retained as superseded history, not applied to the new goal.

### 3. Submit actual per-criterion evidence

This is the **same** `record_evidence` command as for a workflow attempt:

```json
{
  "action": "record_evidence",
  "arguments": {
    "attempt_id": "attempt-REPLACE_WITH_RETURNED_ID",
    "evidence_id": "job-7-persistence-check",
    "criterion_id": "persistence",
    "verdict": "pass",
    "artifact": "REPLACE_WITH_THE_SAME_CANDIDATE_DIGEST",
    "report_ref": "immutable-reports/job-7/persistence.json",
    "report_sha256": "REPLACE_WITH_ACTUAL_REPORT_SHA256",
    "detail": "Actual test output from the harness's persistence-check subagent."
  }
}
```

Repeat for every required test/review/human/integration criterion. Reports from
the director's subagents may be used here, including to validate a SkillFlow
candidate. The submitting caller must already have State writer authorization;
putting a reviewer's name in a request does not grant it authority.

A completion report is not a substitute for the individual required checks.
Evidence can say `fail` or `skip`; neither passes acceptance. A correction uses a
new evidence ID and preserves the previous report. New evidence on an accepted
candidate invalidates its prior acceptance before it can be accepted again.

### 4. Accept the goal

```json
{
  "action": "verify_node",
  "arguments": {
    "project_id": "my-project",
    "node_key": "growth.proficiency",
    "expected_revision": 1,
    "attempt_id": "attempt-REPLACE_WITH_RETURNED_ID"
  }
}
```

The acceptance receipt includes immutable `provenance_json`. For an external
attempt it records the harness/execution ID, authenticated reporter, final
observation, artifact kind, context hash and report digest. For SkillFlow it
records the actual run/workflow/graph version. Both require current dependencies
and the latest eligible candidate's complete passing evidence.

`get_attempt`, `attempt_detail`, `recover_attempt`, `reconcile_attempt`, evidence
and `verify_node` work on external attempts without constructing an engine.
External recovery is a persisted observation read, not a remote worker restart.
Project `refresh_project` observes external records without invoking SkillFlow;
a mixed project still uses SkillFlow only for the workflow-backed attempts.

## Use only State DAG

A separate entry point serves the State HTTP APIs and three State MCP tools. It
does not import the normal application composition root, registry, scheduler,
workspace manager or SkillFlow. It creates only State tables, not legacy run/task
or SkillFlow tables.

From the installed AItelier environment:

```sh
export AITELIER_STATE_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
python -m api.state_only --db /absolute/private/project-state.sqlite --port 4450
```

The token is read from the environment, not a command-line argument, and is not
logged. The database path is mandatory and absolute; there is no implicit
production database. Newly created files are private. By default the service
binds loopback. Apply TLS and controlled reverse-proxy access before exposing it
beyond a trusted machine. Use a dedicated State-only credential rather than
sharing an application/host administrative token.

Every private HTTP/MCP request, including schema and tool discovery, requires:

```http
Authorization: Bearer <AITELIER_STATE_TOKEN>
```

The State-only token is distinct from the full application's admin/external-MCP
headers. `/health` reveals only service mode/health; it does not expose projects.
MCP is at `/mcp/`. `--without-mcp` starts HTTP only. For non-local MCP hostnames,
configure the explicit `AITELIER_STATE_ALLOWED_HOSTS` allowlist; do not disable
transport origin/host checks to make a public setup work.

This entry point is a **headless State API/MCP service**, not a separate login UI
or workflow dashboard. The normal AItelier frontend now understands external
attempts; embeddings can reuse `api.state_http.create_state_router` with their
own authenticated frontend. Main AItelier's package dependencies have not been
split into a separately published minimal wheel: runtime independence is tested,
not a claim that the installer no longer downloads the SkillFlow package.

An in-process State-only integration can use:

```python
from core.state_database import StateDatabase
from core.state_service import StateService

service = StateService(
    StateDatabase('/absolute/private/project-state.sqlite'),
    actor='my-authenticated-integration',
)
```

Embedding code is responsible for authenticating that actor; do not populate it
from an untrusted JSON field. State-only mode has no legacy task import/source
project rows and cannot launch a workflow. It returns an explicit unavailable
error rather than creating a fake workflow or silently calling a model.

## UI and provenance

External work appears in the project's attempts and node evidence panels. The
card identifies external progress; details show the harness, external execution
ID, authenticated submitting identity, report hashes, quiescence declaration and
acceptance provenance. A null run ID is intentional and does not render a broken
workflow link or a missing-run error.

The global Runs page is still the actual **workflow-run history**. It does not
manufacture a run for every external validation. A project's Attempts tab is the
cross-executor history. This keeps the distinction between product facts,
execution attempts and the optional workflow UI.

## Trust and deployment boundaries

A report is an authorized attestation. A digest binds bytes/identity; it does not
prove the report is honest. An untrusted agent's word, `done`, a UI badge, or a
caller-supplied `quiescent=true` without actual worker coordination is not a
substitute for the contract's verifier. A formal-proof harness can register its
checker output; State DAG is not itself a universal proof kernel.

The authenticated reporting identity must match the external attempt owner for
progress reports. Harness/subagent names are provenance labels, not cryptographic
identities. A single shared bearer token represents one caller; two subagent
labels using it do not establish independent human approval or separate
permissions. Evidence/acceptance uses existing writer authority; per-project
roles, threshold signatures, verifier PKI and remote report retrieval are not
invented by this release.

Schema migration preserves old attempt IDs, sequence high-water marks, evidence,
receipts and indexes. The formerly NOT NULL workflow/execution fields require
one transactional table rebuild; external metadata/observations and receipt
provenance are added. The migration is idempotent and foreign-key checked; corrupt
or partial input fails without dropping the original data. Back up before a
normal deployment and coordinate State writers. Do not run mixed old/new State
writers after external attempts exist: old code does not understand these rows.
Rolling back code is not permission to delete external evidence or blindly
restore a database over later work. Development tests do not migrate production.

## Executable verification

```sh
python examples/external_harness_demo.py --report /tmp/external-demo.json
pytest tests/unit/test_state_external.py tests/integration/test_state_external_entrypoints.py
```

The demo runs a real pair of harness-owned Python subprocess checks. The first
artifact fails and acceptance returns 409; a corrected attempt passes and unlocks
its dependent. A fresh app reopens the database and a changed goal rejects old
evidence. No workflow tables, Run or SkillFlow import occurs. Demo artifacts are
temporary; production integrations must retain their immutable report files.

Tests also run an import firewall that forbids SkillFlow and full-host modules,
then exercise complete HTTP **and MCP** acceptance. Mixed external → SkillFlow →
external dependencies are separately tested with the real SkillFlow engine, as
are old-schema migration, concurrency, failed/skipped evidence, stale versions,
protected goals, observer ownership and incorrect replay.
