# Project-first dashboard and readable State DAG cards

## Navigation

The default route `/` (and `/projects`) is now a **project dashboard**, not the
legacy execution-project/repository catalog. It selects a permitted State
Project and displays its DAG directly. A project switcher and `Browse projects`
link remain available. The last selected project ID is a local browser preference,
validated against the current authorized project-list response before use; it is
not an authorization mechanism. Empty, failed, signed-out and changed-identity
states do not retain another user's project data.

Primary navigation: **Projects → Runs → Pipelines → Chat**, with existing Tracking
access unchanged. These are separate screens and separate fetch paths:

| Route | Meaning |
|---|---|
| `/`, `/projects` | Selected long-lived project's State DAG |
| `/state-projects` | Project directory, including paging |
| `/state-projects/:id[/nodes/:nodeKey]` | Stable project/goal permalinks |
| `/runs` | Actual workflow execution history |
| `/runs/project/:projectId` | Actual runs linked through this State Project's attempts |
| `/pipelines` | Current reusable workflow definitions and workflow notes |
| `/repos` | Repository/Git tools and legacy execution-project management |
| `/projects/:executionId` and existing trace routes | Backwards-compatible execution detail |
| `/state-runs/:runId` | Exact run and its pinned historical workflow graph |

Repository pages link to State Projects without creating duplicate graphs. The
legacy `UnifiedDashboard` component remains for compatibility; routed repository
tools use repository-only mode and do not fetch the pipeline/run catalog.

The homepage's source path and dispatch-hold explanation can be expanded. Its
headers and metrics are compact enough that the first goal cards are visible
on the desktop's initial viewport, rather than below a page of metadata. A
project hold is still visible; collapsing its explanation does not release it.

## Why nodes were white

The original card had real `<text>` elements and populated status fields. But
`g[role=button]` picked up Pico's button-scoped `--pico-color` (white), and the
SVG text used that variable over a white card. DOM-only visibility/content tests
missed this: a node could contain the correct text while rendering blank.

The component now captures theme ink/surface variables outside the button scope,
uses explicit card text/label paints, and retains keyboard button semantics.
Its SVG dimensions are explicit; global responsive-SVG styles cannot silently
shrink a large graph until the text is unreadable. Scrolling and zoom remain
available. Long mixed CJK/Latin titles use two bounded lines, with full labels
available in the title and selected-node detail.

Each card separately displays:

- Goal title/key and requirement revision.
- **Fact status**, such as OPEN, CANDIDATE, VERIFIED or STALE.
- **Readiness**, such as ready, dependency-blocked, held or in progress.
- **Latest attempt**, such as RUNNING, PAUSED, FAILED or CANDIDATE; absent attempts
  say `Not started` rather than leaving an empty row.

A completed attempt is not rendered as a verified goal. Only the goal's actual
VERIFIED state uses the accepted-fact styling. No project or run status is changed
by displaying the badge. Dashboard polling reloads persisted state every 15
seconds only while the page is visible; it never invokes `refresh_project`,
launches work or answers checkpoints. The explicit observation-sync button keeps
its existing meaning. Persisted observations may lag an executor until the
existing driver reconciles them; the page still identifies its snapshot.

## Actual run history

The pre-existing `/api/runs` endpoint aggregates **legacy execution projects**;
using it as a one-row-per-Run feed would hide multiple workflows or retries under
the same execution project. It remains unchanged for compatibility.

New private read-only endpoint:

```
GET /api/run-history?q=&status=&workflow=&state_project_id=&offset=0&limit=50
```

It projects each actual SkillFlow run exactly once, includes repository-free and
authoring runs, applies the existing owner scope before adding State ownership,
then filters and pages results. Maximum page size is 100; search/filter lengths
are bounded. Returned fields identify the run, execution workspace, workflow,
status, step, timestamps, graph version and optional State Project/node/attempt.
Prompts, input values, trace payloads, outputs and credentials are not included.

It requires the existing writer authorization, does not initialize State tables
on a legacy-only database, and does not reconcile or drive workflows. Missing
State metadata does not erase legacy/standalone engine runs. The run screen uses
exact run IDs for graph links; historical labels are not taken from a current
workflow definition.

## Verification and rollout

Tests include the former regression suite plus explicit tests for default-DAG
mounting, permitted project selection, late identity responses, sign-out, errors,
separate page queries, duplicate/paginated run identities, repository-free runs,
owner filtering, anonymous denial and non-mutating state refresh.

`tests/browser/state_node_paint.py` checks the built app with the full Pico CSS:
light/dark themes, normal/hover/focus, nonzero painted label bounds, contrast at
least 4.5:1, readable actual card size and label containment. The browser smoke
suite traverses homepage → runs → pipeline definition → project, checks desktop
first-screen geometry and 390px widths for all three primary screens, and keeps
existing pinned-run/recovery/anonymous scenarios.

These checks use an isolated real SQLite/SkillFlow test app, not game acceptance.
The original white-node reproduction was captured from the live site; a new
bundle still needs the normal user-managed deployment after source delivery.
No Docker socket, SSH permission, active-gameplay-run control, migration rewrite
or checkpoint approval is required or added by this UI change.


## External attempts

State goals may also be verified by your own harness/subagents without a
workflow. The project Attempts view and node evidence details show execution
kind, harness/job identity, authenticated reporter, immutable observations and
acceptance provenance. No missing-run error or synthetic workflow link is
created. The global Runs page remains actual SkillFlow execution history;
project Attempts is the cross-executor view. See [external harness integration](state-external-harness.md).
