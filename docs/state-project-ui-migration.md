# State Project UI, exact-run graphs, and migration preparation

## Navigation and ownership

**Navigation update:** `/` and `/projects` now open the selected long-lived project's
State DAG directly. Primary navigation is Projects / Runs / Pipelines / Chat;
repository tools live under `/repos`. See [the dashboard UX guide](project-dashboard-ux.md)
for current routes, visible fact/attempt badges and the white-label fix.
The catalog and all project/goal/run permalinks below remain supported.
This is the long-lived product project, not the old execution-project row.

- `#/state-projects`: private project catalog with goal counts and dispatch policy.
- `#/state-projects/:id`: State DAG, project attempts, evidence/history.
- `#/state-projects/:id/nodes/:nodeKey`: link to a specific goal.
- `#/state-projects/for-repo/:encodedPath`: related projects for a source checkout.
- `#/state-runs/:runId`: exact workflow run, its pinned graph, and project/goal backlinks.

Existing `#/projects/:executionId` and trace links remain valid. Expanded old
execution details show related State Project links. Expanded repository cards
query the private State API for their associated projects; they do not create a
second graph or place private goals inside the public repository response.

One State Project has many nodes; each node can have many attempts. Each attempt
binds one SkillFlow run and may choose a different workflow. Different nodes can
be active simultaneously; one node still has at most one active attempt. Old
`sg-*` execution project IDs are implementation detail, not new products.

## Display semantics

The graph is a separate State DAG adapter on top of the existing SVG/layout
foundation, not a reuse of workflow step status folding. Edges point from
**prerequisite to dependent**. The display separates:

1. Fact status: OPEN / CANDIDATE / VERIFIED / STALE / SUPERSEDED.
2. Readiness: ready / blocked / held / in_progress / closed.
3. Latest attempt: reserved / running / paused / completed candidate / failed etc.

Only VERIFIED uses the verified styling. A workflow that completed with failing
acceptance remains a candidate; its evidence can visibly fail without the graph
claiming the goal is complete. Reference links never certify a goal.

The page supports domain filtering (the first segment of a dotted node key),
search, selected-goal neighbourhood, zoom and scrolling. These are display
filters, not new dependency edges. Links crossing the filtered boundary are
counted explicitly. Views above 60 visible goals ask the reader to narrow the
scope instead of silently rendering an arbitrary subset. Full contracts,
attempts and evidence are loaded only for the selected goal. A late response for
a previously selected goal cannot overwrite the new selection.

The Runs tab is a cursor-paginated project aggregation, not the old endpoint
that only listed a single execution project's runs. Evidence is inspectable
within an attempt, and exact-run links lead to the original run/trace identity.
A failed reload is shown as an error, not as an empty successful project.

The header and workspace adapt to narrow screens. The global AppBar wraps its
navigation and measures its height so the chat area does not keep using a fixed
desktop-height assumption when the header spans several rows.

## Refresh is explicit

**Reload view** only reads stored State DAG observations. **Sync run results** is
an explicit, authorized command that observes a bounded batch of existing bound
runs. It does not launch, stop, approve or verify anything. A failure observing
one run is reported without hiding the rest. Large projects expose a next-batch
button. The page shows its observation timestamp/event sequence.

This release does not add State DAG SSE fan-out or an autonomous planning
scheduler. Existing workflow execution remains in SkillFlow, and the Driver can
continue calling `reconcile_attempt`. Merely opening a page cannot start work.

## Correct historical Workflow Graphs

When `PipelineGraph` receives a run ID, it now requests:

```text
GET /api/runs/{exact_run_id}/graph
```

The response is projected from the run's pinned graph version. The host checks
that the stored historical graph, its digest, and the run's version/digest agree.
A missing or inconsistent version yields 409, not the latest graph. A project
alias yields 404; this endpoint requires the exact run identity.

The projection includes structural IDs, types, transitions, loop membership and
checkpoint positions. It excludes prompts, context, tool parameter values,
output values and private goal text. Current manifest labels and addon
attribution are deliberately not applied to a historical run; unknown labels
fall back to stable node IDs. The config catalog still displays the current
config graph because it is a config view, not a historical run view.

## Private project projection APIs

All `/api/state` reads and commands retain writer authorization. These extra
queries are available through the same typed `state_graph_help/read/write`
contracts used by MCP and the internal Driver:

```text
project_catalog(repo_path?, after?, limit?)
project_overview(project_id)
project_attempts(project_id, after?, limit?)
references(project_id, node_key?, after?, limit?)
run_owners(run_id)
attempt_detail(attempt_id)
```

Convenience REST routes:

```text
GET /api/state/projects?repo_path=...&after=...&limit=100
GET /api/state/projects/{id}/overview
GET /api/state/projects/{id}/nodes/{node_key}
GET /api/state/projects/{id}/attempts?after=0&limit=30
GET /api/state/projects/{id}/references
GET /api/state/attempts/{id}/detail
GET /api/state/runs/{id}/owners
POST /api/state/commands/refresh_project
```

No private goal fields were added to anonymous repository/config projections.
State data remains administrative-writer scoped; this does not introduce a
separate per-project ACL or cryptographic verifier role.

## Stable source binding and migration holds

A State Project may now bind an existing source checkout without depending on
the continued existence of a historical execution-project row:

```json
{"action":"bind_source","arguments":{
  "project_id":"product","repo_path":"/absolute/existing/source",
  "expected_revision":0
}}
```

The path must be a Git repository root. Its absolute path and Git common-directory
identity are recorded and rechecked when used. After any attempts exist, this
binding cannot be retargeted; a source migration requires an explicit future
provenance-preserving procedure. This is one canonical source binding, not a
full many-repository resource graph.

A project dispatch policy is `active`, `hold` or `archive`. `set_dispatch` requires
an expected policy revision and a reason. A node can additionally have its own
hold using `set_node_hold` with its hold revision. Holds affect both frontier
selection and reservation/launch admission; they are not decorative UI badges.
An existing attempt may remain in_progress while a hold forbids new dispatch.

Holds do not cancel already admitted work. A protected historical run that is
paused/running/unknown cannot have its node hold released until the service
observes a terminal run with no admitted operations. Adding another protected
reference increments the hold revision even when it is already held, preventing
a stale client from silently releasing a newly protected task. Project release
does not remove a node's independent protection.

## Historical / external work is reference-only

`add_reference` records immutable run/commit/report/note references with label,
provenance actor, observed status and optional artifact/report hashes. Transport
identity is separately recorded. Reusing a reference ID with different content
is refused; corrections are new references, not rewritten history.

For an exact SkillFlow run, the service checks its identity; an active run
requires protection. It remains the same legacy run, with its original execution
project and checkpoint. No attempt is invented and no output is adopted. A
manual Codex/Claude delivery can be referenced by commit/report without pretending
it was a SkillFlow run. This is **not yet** an external-executor attempt adapter
or automatic acceptance of hand-written reports.

## Migration preparation and rehearsal

The generic preparation CLI is deliberately non-destructive:

```sh
python scripts/preview_state_migration.py /path/to/manifest.json \
  --report /path/to/new-preview-report.json
```

It has no `--apply`, production DB, launch or approval option. It checks the
canonical source's exact commit and clean status, checks pinned report hashes,
then stages the manifest in a TemporaryDirectory database. A successful second
staging of the identical manifest is idempotent; different input or modified
shadow state is refused. The temporary database is discarded after the report.
The report is created exclusively rather than overwriting prior evidence.

A format-1 manifest contains:

- long-lived project ID/title and canonical source path/40-hex Git commit;
- project hold/archive policy and explicit reason (active import is forbidden);
- node specs using existing strict goal/acceptance/dependency contracts;
- immutable historical references, additional node holds, excluded canceled runs;
- pinned evidence file paths/SHA-256 and unresolved design assumptions.

Every initial node remains OPEN and held, with zero attempts, zero acceptance
receipts, and an empty ready frontier. A historical completed task does not turn
VERIFIED. Excluded canceled runs cannot also be imported as current-goal run
references. Active/unknown external run references require protection. Source
or report changes stop the rehearsal rather than silently using new evidence.

For the prepared game migration, the private manifest and evidence snapshots
live outside the source repository under the workspace director/migrations
area. They are not bundled into the frontend or committed to a potentially
public source repository. Actual production migration remains a separate action
following review of the mapping, assumptions, protected legacy runs and snapshot
freshness. Do not manually release all holds just to make the frontier green.

## Verification and rollout

Component/layout tests cover goal-state semantics, boundaries, private access,
selection retention, asynchronous request races, explicit sync, exact run links
and missing history. `tests/browser/state_project_smoke.py` exercises built SPA,
real isolated State API/SQLite/SkillFlow and Chromium, including desktop/mobile,
failed evidence, current-vs-pinned graph mismatch, anonymous denial and error
states. Its optional `--migration-manifest` shows the real prepared goal manifest
in the temporary service without touching production data.

Run the tests in an isolated development environment:

```sh
pytest tests/integration/test_state_portfolio.py tests/unit/test_state_migration.py
cd web && npm ci && npm test && npm run build
# From repository root, with Playwright installed only in the test environment:
python tests/browser/state_project_smoke.py --report-dir /tmp/new-state-browser-report
```

Source integration is not deployment. The frontend bundle is baked into the
normal backend image outside the live source mount; loading the new UI normally
requires rebuilding/deploying the frontend-bearing image, not merely refreshing
the browser or restarting an unchanged image. Python module changes also need
the normal backend reload/restart. This implementation does not perform that
production operation. Additive State metadata can remain during code rollback;
never drop audit/evidence tables as a rollback shortcut.
