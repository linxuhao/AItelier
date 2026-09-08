# State DAG running runs and usage counters

The graph tab (including the default project dashboard) has a compact related-run
panel. Its six plain, non-clickable counters are Total, Running, Finished, Failed,
Tokens and Cache. Only currently running related runs are listed as links to their
exact run pages. Finished/failed/paused/pending rows are not expanded here; the
existing run/attempt detail pages are unchanged.

## Scope and meanings

Membership is the union of bound State attempt run IDs and explicit historical
`kind=run` references. Exact run IDs are deduplicated across references, nodes and
attempt bindings. Sharing a repository does not automatically associate a run.
All linked identities are counted, not just the first page of attempt/history
results. External harness jobs are not fabricated into workflow runs, and their
unreported usage is explicitly excluded.

Running uses the actual workflow `running` status, not stale attempt state.
Finished means actual workflow `completed`, **not State VERIFIED**. Total also
includes paused and pending runs. When a linked run cannot be observed, its ID
stays in Total, other status counts are marked as lower bounds, and the panel
reports partial availability rather than claiming the project is idle.

Tokens is reported `prompt_tokens + completion_tokens`. Cache-hit tokens are
already part of prompt tokens, so they are never added again. Cache displays
`sum(hit) / (sum(hit) + sum(miss))`, plus the hit-token amount. It is a weighted
ratio over reported cache measurements, not an average of run percentages. A
provider that does not report cache is not assumed to have a zero hit rate.

No telemetry renders as `—`, not a fabricated zero. An empty workflow-run set
has zero workflow token usage; external usage remains outside this scope. A
partially measured token total has `*` and a reporting-coverage note. Invalid,
negative, malformed or missing usage counters are excluded from measurements.
Explicit zero counters remain distinguishable from absent data. Cache/input/
output definitions and exact numbers are available in the metric tooltips.

## API and refresh

```
GET /api/state/projects/{project_id}/run-summary
```

The shared State command is `project_run_summary` with `{project_id}`. It is a
private read operation on HTTP/MCP, using the same authorization as the project.
The response contains counts, only `running_runs`, measured usage/coverage and
observation time. No prompts, trace payloads, credentials or completed-run row
lists are returned.

Queries use the engine's trace-query adapter so per-project trace databases work
as well as the shared store. They never call reconciliation, dispatch, checkpoint
approval or State verification. No workflow observer is initialized for a project
with no linked workflow runs, preserving State-only/custom-harness operation.

The panel follows the existing visible-page refresh cadence (15 seconds) and
manual reload. Completed/failed runs disappear from the running list on the next
successful observation. Switching project or identity cancels stale responses;
a failed read shows an error rather than a successful empty result. Statistics
are observed snapshots, not an atomic transaction across the State and engine DBs.

## Tests and deployment

Backend tests cover true run statuses, cross-node/reference deduplication, more
than one reference page, unrelated projects, weighted cache counts, missing vs
zero measurements, malformed telemetry, per-project trace stores, private access,
State-only behavior and unchanged graph/events. Frontend tests assert exactly six
non-clickable metrics, running-only exact links, async races, permission loss,
refresh transitions, unknown data and clear error states. The real built-browser
suite clicks a running link, observes its later failure/removal, checks 390px
layout, and retains the existing State DAG paint/navigation checks.

Source delivery is not runtime deployment. Load the new backend route and rebuilt
frontend bundle together through the normal user-managed deployment process.
This feature adds no database tables and does not start gameplay work.
