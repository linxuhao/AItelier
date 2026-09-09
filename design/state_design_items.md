# Structured design items in State

State's SQLite database is the writable source for adopted design items. Markdown
is generated from immutable records. This feature does not migrate existing game
design, change execution dependencies, or publish content. Existing repositories
must label adopted versus unmigrated sections explicitly; no normative section
may have two writable sources. A source-document import workflow is not included.

## Author and select exact versions

Use the existing authenticated `/api/state/commands/{action}` and
`/api/state/query/{action}` endpoints, or `state_graph_write/read` MCP tools.
`state_graph_help` / `/api/state/schema` contains strict argument schemas. All
existing State transport authorization and server-derived actor rules apply;
these commands do not introduce a second permission or task framework.

1. `create_design_revision`: supply project_id, stable design_id,
   expected_revision (0 creates a new item), title, statement, rationale,
   open_questions, scope, lifecycle_status, kind and optional relations.
   A revision contains the complete item, including its relations, not a patch.
   Status defaults to draft; approved requires no open questions. Approval is an
   authorized writer's explicit declaration, not automatic design quality review.
   IDs are never reused; subsequent changes append revisions with an exact parent.
2. `create_design_baseline`: choose a new baseline_id and selected_revisions such
   as `[{"design_id":"cooldown","revision":1}]`. Supply expected_baseline_id
   (null only before the first baseline). The transaction selects a new current
   baseline with optimistic concurrency control. Existing baselines and revisions
   reject updates/deletes, including direct SQL updates. Lost-response recovery:
   read the exact requested baseline ID before issuing another selection.
3. `get_design_revision`, `get_design_baseline`, and `design_catalog` expose exact
   historical revisions, manifests and the current baseline ID. A baseline selects
   only one version of each ID. Draft/historical items may be included as context;
   a baseline does not turn them into approved requirements.

Scope is a nonempty object of explicit dimension/value identifiers, for example
`{"mode":"local_coop","platform":"desktop"}`. Values are opaque labels, not
predicates; `all` is also an opaque explicit label. The MVP requires exact scope
equality for full-item binding and supersession. It does not infer intersections,
subsets, wildcard priority or scope equivalence.

Relations have exactly `type`, `target` (`design_id`, integer `revision`), and
`rationale`. `depends_on` and `references` must resolve to exact versions selected
in the same baseline. `conflicts_with` names an existing exact alternative; its
target need not be selected. It is a direct, reviewable assertion, not a necessary
dependency or automatic baseline veto. Query either endpoint with `design_impact`
without storing a reverse edge; conflict relations never propagate. Approved items cannot depend_on non-approved items.
`supersedes` targets an existing historical exact revision with identical scope;
it cannot select the replaced revision alongside its replacement or competing
full-item replacements. Transitive replaced ancestors are checked as well. No
partial supersession field is supported; it is rejected instead of ignored.
References can only name already persisted versions; forward/mutually recursive
revision creation is not provided. Relations never alter State requires edges.

## Bind execution scope and preserve evidence

`bind_node_design` takes project_id, node_key, expected_revision, baseline_id,
reason and bindings:

```json
[{"design_id":"cooldown","revision":1,"purpose":"implements","coverage_scope":{"mode":"all"}}]
```

Purpose is `implements`, `verifies`, or `context`. The first two require approved
items. A binding must name a version selected by its explicit baseline and cover
the full item scope. Binding writes replace the node's complete binding set,
append an immutable binding snapshot, and use the existing State node revision
and invalidation transaction. Existing requires dependencies stay unchanged.
For this MVP even a context-only rebinding conservatively invalidates the node
and downstream acceptance; the director decides when an explicit rebind is needed.
There is no unbind operation; replace a binding set or create a new goal.

New SkillFlow and external attempts freeze the baseline manifest/hash, binding
hash, bound revision bodies and their transitive depends_on bodies in their
existing context JSON. Unrelated baseline bodies are excluded. No attempt
history is backfilled or rewritten; unbound nodes retain their old context shape.
Acceptance receipts for bound attempts include baseline/manifest/binding hashes
and reference the original attempt. Every verifier still must satisfy the node's
existing acceptance checks; trace links alone never prove a test passed.

Selecting a baseline does not change current node bindings or cancel attempts.
`get_design_bindings` (also `get_node.design`) reports baseline_review_required
when the binding's baseline differs from the current selection. This is a coarse
review hint, not a semantic incompatibility assertion. An old-baseline attempt
may finish and be accepted for its explicitly old-bound node until an explicit
rebind. It is not proof of current-baseline coverage. Rebinding increments the
node revision, so old candidate evidence cannot accept the new node revision.
Historical attempts/receipts remain queryable. There is no automatic evidence
carry-forward or release coverage gate.

## Generate and check Markdown

`export_design_markdown(project_id, baseline_id)` returns `markdown`, its byte
SHA-256 and the exact manifest hash. The view identifies stable IDs/revisions,
status, scope, statement, rationale, open questions and relations. Non-approved
content is labeled separately. Output order is stable by design ID and contains
no export-time timestamp. The same immutable baseline regenerates identical
UTF-8 bytes regardless of new revisions or baseline selections.

`check_design_markdown(project_id, baseline_id, markdown)` compares the supplied
text against exact regeneration and returns matches plus expected/actual hashes.
A repository CI can read its generated file, invoke this read query, and fail on
matches=false. No automatic CI installation, filesystem write or import endpoint
is supplied. Manual edits are detected when this check runs; they never write
back to State. Retain the underlying State database to reconstruct history.

## Candidate search and review impact

`search_design_items` returns bounded literal-search summaries, exact versions,
match reasons and baseline/selection hashes. By default it searches the union of
current-adopted and newest saved versions so drafts cannot hide adopted rules;
an explicit baseline searches only its pinned revisions. `design_impact` exposes
direct assertions, reverse depends_on paths and latest matching node bindings.
It does not change goals, baseline selection, bindings, attempts or acceptance.

See [the lightweight search/impact protocol](../docs/design-search-impact.md) for
query schemas, scope/paging/truncation semantics, conflicts_with, and a real
State-only HTTP example. These are candidate/review helpers, not semantic proof.

## Boundaries

No graph database, OSLC/ReqIF exchange, game migration, front-end editor,
LLM-derived dependencies, semantic diff or automatic impact adjudication, partial-scope override,
source import, automatic revalidation, scheduler changes or production deployment.
Relations describe design interpretation; only explicitly authored existing State
dependencies block execution. Commercial design records and generated documents
remain private under the existing access rules.
