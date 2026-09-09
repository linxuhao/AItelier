# Lightweight design search, direct relations and review impact

This extension adds **two read queries and one relation type**, using the existing
State design revision/baseline system. No graph database, embeddings, FTS migration,
solver, new approval framework, automatic LLM call or background job is introduced.
The small query module scans the selected records and traverses recorded relations.
The driver reads only the candidate text it needs.

## Driver loop

1. Search likely terms; inspect candidate IDs/revisions and why they matched.
2. Fetch exact candidate bodies with `get_design_revision`; search again if the
   definitions or relevant scope are still missing.
3. Propose a complete revision with direct relations and concrete reasons. Use
   the existing draft/review/approval path, not a new autonomous approver.
4. Query the old revision's impact and the proposed revision's direct context.
   Review affected nodes; do not confuse a hint with a failed acceptance.
5. Select a new baseline and rebind nodes only through the existing explicit
   commands. Queries never change a worker's frozen context or execution state.

These steps work with the existing MCP `state_graph_read/write`, authenticated
REST, internal director tools or a custom harness using the headless State-only
service. Neither query composes SkillFlow or requires a workflow.

## `search_design_items`

MCP example (replace the project/baseline with actual IDs):

```json
{"action":"search_design_items","arguments":{
  "project_id":"my-project","query":"普通移动 Ready","limit":20
}}
```

HTTP uses `POST /api/state/query/search_design_items` with the arguments object
alone as the body. The exact current request schema is in `state_graph_help`.

| Argument | Meaning |
|---|---|
| `project_id`, `query` | Required; query is nonempty, at most 200 characters and 8 whitespace-separated terms |
| `baseline_id` | Optional; an explicit baseline restricts search to its exact selected revisions |
| `scope` | Optional exact dimension/value filter; all supplied labels must match |
| `limit`, `offset` | Default 20/0; limit 1–100, offset 0–1,000,000 |

Without `baseline_id`, candidates are the **union of current-baseline revisions
and each item's newest saved revision**, deduplicated by ID/revision. A new draft
must not hide an older approved rule still in use. With no baseline, only newest
saved revisions are candidates. An explicitly missing baseline is an error,
never a fallback to a different selection. Fetch other historical versions using
their exact revision or historical baseline.

All terms must occur, case-insensitively, somewhere in the item's ID, title,
statement or rationale. Matching is literal substring matching, including Chinese
and mixed-language terms. There is no SQL/FTS expression language or automatic
synonym expansion. Quoted text, `%`, `*` and punctuation are just characters.
Too-specific queries can be broadened by the driver instead of silently dropping
terms or inventing semantic matches.

Scoring uses the strongest matching field per term: ID 8, title 4, statement 2,
rationale 1. Repeated body words do not inflate the score. Ties sort by adopted
first, ID ascending, revision descending. Scope is **not** an automatic filter:
different labels do not prove disjoint meaning. Even `all` is an opaque label,
not a wildcard. Only an explicit `scope` argument excludes differing labels.

Results contain `items`, exact baseline ID/hash, `selection_hash`, candidate count,
`total`, `offset`, `limit`, `next_offset` and `truncated`. Each item includes its
exact ID/revision/hash, title, lifecycle, scope, `adopted`, `is_latest`, a statement
snippet of at most 240 characters, and matched-field/term reasons. Full statement,
rationale, open questions and relations remain available by exact revision.

Each query uses one SQLite read snapshot. Concurrent edits cannot mix two
baselines inside one response. Paging an explicit immutable baseline is the
most stable choice. The default candidate pool and `is_latest` labels can change
between calls; compare `selection_hash` and restart paging when necessary rather
than claiming a multi-call snapshot. No server-side snapshot/cursor service is
added for this MVP.

No search hit, high ranking, or empty result proves either a relation or the
absence of conflicts. The API is a candidate finder, not a design interpreter.

## `conflicts_with`: a recorded assertion, not a required dependency

Continue to use `create_design_revision`, including the complete item and its
relation list. Only the new relation type is added; the object shape is unchanged:

```json
{"type":"conflicts_with",
 "target":{"design_id":"movement.requires-ready","revision":2},
 "rationale":"For ordinary overworld movement, one rule requires all-player Ready and the other forbids it. Review this shared scope."}
```

The target must already exist in the same project at that exact revision; a
nonempty rationale is required. Duplicate relations and references to the new
revision itself are rejected. No forward reference or physical delete API is
introduced. Relationships change only by appending another complete revision;
a new revision does not inherit an old edge unless it explicitly includes it.

`conflicts_with` can target an alternative **not selected in the new baseline**.
Recording a conflict must not force adoption of the conflicting rule. If both
endpoints are selected, `design_impact.declared_conflicts` reports the assertion
and marks `both_selected=true`; this is a review warning, not automatic rejection
of the baseline. The source revision's lifecycle is displayed, so a draft
assertion is not disguised as an approved requirement. Review conditions/scope
in the original rationale; this version does not compute scope intersections.

Only one assertion is needed. Impact queries expose it from either end without
writing a reverse edge. The original source/target and direction remain visible.
An incoming edge from an unselected revision is not mixed into the selected
baseline's graph. Query that unselected revision itself to inspect its proposal.

There is **no transitive conflict inference**. D1 conflicts with D3 and D3 depends
on D2 does not imply D1 conflicts with D2. `references` only suggests reading.
`depends_on/references` still require the exact target in the same baseline;
approved rules still cannot depend on unapproved rules. Full-item, identical-scope
`supersedes` and its existing replacement checks remain unchanged. None of these
design edges writes a State goal's `requires` edges.

## `design_impact`

```json
{"action":"design_impact","arguments":{
  "project_id":"my-project","design_id":"movement.requires-ready",
  "revision":2,"baseline_id":"before-change","limit":50,"max_visits":1000
}}
```

HTTP: `POST /api/state/query/design_impact`. An exact subject revision is required.
Omitting `baseline_id` resolves the current baseline; if none exists, the query
fails explicitly. The subject itself may be an older version or a proposed item
outside that baseline, and is marked `selected=false` rather than substituted.

The response separates these sections:

| Section | What it means |
|---|---|
| `subject`, baseline ID/hash | The exact reviewed item and relationship-selection context |
| `direct_relations` | Subject's outgoing assertions plus incoming assertions from baseline-selected revisions |
| `declared_conflicts` | Direct conflict assertions from either endpoint, original rationale and endpoint-selection/lifecycle labels |
| `dependent_designs` | Designs reached by reverse `depends_on` only, with one deterministic shortest explanatory path |
| `affected_nodes` | Latest node-binding snapshots containing that exact item in a direct binding or captured design-dependency body |
| `traversal` | Visited count, budget, path limit and truncation |

`limit` applies separately to each list (default 50, maximum 100). Each list reports
its own total and truncation. `max_visits` is 1–1000 and includes the subject.
If traversal runs out of budget, dependent totals are explicitly not exact.
Paths over 32 entries return their first/last 16 with the original path length
and `path_truncated=true`. Missing middle hops are not asserted to be direct
edges. All totals describe recorded data in the declared scope, not semantic
completeness.

Affected nodes are found from **actual bound bodies and their captured dependency
closure**, not from every member of the baseline manifest stored in a snapshot.
Otherwise an unrelated goal would falsely appear affected just because it used
the same baseline. The query reads latest bindings in one SQL projection, not
one lookup/request per node. Results show current node revision/status/receipt,
binding revision/hash/baseline, match flags and `review_only=true`.

The latest binding can still refer to an older baseline, or predate a subsequent
node-contract edit. These cases are labelled. Replaced historical bindings are
not current impacts; they remain in immutable storage. **The design relationship
part is baseline-pinned, but the affected-node part is a current binding
snapshot**, even when querying an old baseline. Explicit rebinding can therefore
change affected-node results without altering historical design relations.

Queries do not cancel or create Runs, rewrite evidence, rebind nodes, select a
baseline, mark conflicts solved, or change VERIFIED. Review hints do not mean an
existing implementation suddenly failed its historical acceptance test.

## Editing or retiring a rule

For a modification, inspect the old exact version's impact and search the new
text; neither search alone nor the old neighborhood alone is sufficient. A
relationship-only edit still creates a revision under the existing protocol.
For retirement, first query the old baseline, then exclude/replace the item in a
new baseline using existing checks. Do not delete old rows or rewrite pinned
relations, attempts or reports. Existing exact references may need an explicitly
reviewed new revision before the target can be excluded.

The global search/impact workflow remains advisory. Unrecorded semantic relations,
three-way constraint contradictions, equivalent wording and subtle scope overlap
can still require human/LLM review. This MVP deliberately adds no generic logic
engine, automatic invalidation or mandatory whole-project review scheduler.

## Executable example and rollout

```sh
python examples/design_review_demo.py --report /tmp/design-review.json
pytest tests/unit/test_state_design_search.py tests/unit/test_state_design_impact.py \
       tests/integration/test_design_review_flow.py
```

The demo uses real authenticated HTTP and temporary State-only SQLite. A scripted
review fixture finds ordinary-movement Ready rules and a related monthly rule,
asserts only the direct ordinary-movement conflict, inspects impact, selects the
other baseline, checks the original report/Markdown and then explicitly rebinds.
No model inference, real game migration, workflow runtime, Attempt or acceptance
is created. Tests also exercise the actual MCP wire and reject importing the
workflow engine in a subprocess.

Implementation is a small `core/state_design_queries.py` module, thin existing
StateDesign facade methods, the relation enum/selection change and two strict
read-operation schemas. There is no schema or search-index migration, package
addition or frontend change. Deploy through the normal user-managed service
reload procedure; committed source is not proof the running process loaded it.
If new `conflicts_with` data is adopted, coordinate all writers on compatible
code: older baseline-selection code would treat any non-supersedes relation as
mandatory. Never roll old code back over newer design semantics without review.
