# RSI optimizer round contract r2 — STATEMENT

This package states `rsi.optimizer.contract` r2. It supplies typed wire inputs,
exact lifecycle/error/ownership semantics, literal synthetic fixtures, a loud
standalone semantic fake, shared round scenarios and an actual SkillFlow graph.
It supplies **no native optimizer binding**. Independent acceptance-only readback
and genuine graph/provider conformance are separate later attempts. Statement
checks never establish native tool grants, owner authority, spend or adoption.
The graph is deliberately under `contracts/`, outside the host boot config scan.

## Frozen dependencies and scope

Normative design is approved **a23c04fa9409e991f1ce4dc3c55a2bcb1cdb17022c6e084b69f1eee33cd9ba94**,
including the independent design review and its receipt
6a3736a2d08b40f7a281b4803f6b635c. Source baseline is accepted
**1ec72c7d1a5e125b222dc2210511848374056526**. Consume these accepted interfaces:

- `contracts.rsi.pipeline_versioning`: `capture/list_versions/read/current/restore`,
  logical v2 codec, distinct native/seal/unit/SDK identities, mandatory whole-unit
  CAS. No run/pin/finish interface is added.
- `contracts.rsi.evaluator`: `EvaluationRequest/Result/Refusal`, `Selection`,
  `Benchmark`, `Runtime`, `Budget`, `TrainProjection`, `validate_result`,
  `scorer_receipt`; genuine `core.rsi_evaluator.NativeEvaluator` is accepted by
  receipt b0f9750197d344e688c0cdbd1bdd63b2. The fake invokes only its separate
  semantic fake, never that native implementation.
- `core.pipeline_benchmark.BenchmarkStore`: unchanged exact append-only ROW and
  full-document aggregate. Train-only 72 rows remain incomplete there; never
  redefine its 120-cell, all-20-case aggregate to get a train green.
- `core.pipeline_optimization.evaluate_matrix`: the **only** numeric comparison.
  Its inputs are trusted observed receipts adapted by accepted `scorer_receipt`.
  It proves consistency and arithmetic, never the truth of executions or authority.

First slice is one **manually supplied** candidate. Autonomous forge/proposal
sampling is not part of this contract mode. Numeric ceilings do not authorize it.
A later producer can supply the same bounded edit only under separate authority;
it cannot inherit generic Meta, shell, trace/config readers or tool creation.
No change is made to accepted components or normal host defaults.

## Exact wire inventory

`contract.py` Pydantic schemas (`RoundRequest.model_json_schema()` and the named
port types) are normative. `Frozen` is the accepted strict/frozen/extra-forbid
base. JSON unknown fields, bool-as-number, coercions and unknown enums refuse.
Untrusted JSON cannot provide actor context, provider objects, paths/roots,
checker code, permission flags, SDK IDs or observed settlement.

`RoundRequest` has schema `rsi.optimizer.round.v2`, campaign/round IDs, trusted
campaign ordinal 1 or 2, the exact design hash, default/only mode
`manual_candidate`, `Target`, reference **Selection**, **Benchmark**, **Runtime**
and **Budget**. These imported types retain their accepted meanings. Full
Benchmark/private identities are coordinator-private; the proposer never receives
this request. Reference Selection must be the reference arm and contain verified
logical version, native bundle/seal, physical config, actual SDK graph version and
all resolved-provider hashes. Reference shadow equivalence must already be proved
by the trusted binding before an evaluator request can be formed.

`Target` is either an owned generated name admitted by the accepted versioning
validator, with its raw 64-hex native `expected_current` unit CAS, or the builtin
`investigate`, with null CAS and an exact Source commit. A logical version digest
is not that CAS. Builtin adoption cannot use a generated-target restore API.

The request bounds cells to 8 physical requests/128k input/16k output/5min,
round to 1000 requests/16M input/2M output/4h/$25, and campaign to at most three
such envelopes/$75 (two train proposals plus separate final confirmation).
Actual preflight must resolve conservative price bounds, feasibility, deadlines,
existing host grant and consumed/reserved usage. The accepted NativeEvaluator
creates a fresh PhysicalBudget and time origin per
EvaluationRequest (`core/rsi_evaluator.py::_execute`); it does not by itself share
a scope across reference-train, candidate-train and final holdout requests. The
NEW trusted optimizer coordinator serializes those calls under existing campaign
ownership, subtracts ALL prior settled usage and unresolved reservations from
the original round/campaign envelopes, and passes the remaining amounts and
ORIGINAL remaining deadlines to each call, retaining the same authorization/scope
identity. It never resets clocks or mints a fresh full budget for each evaluation.
The immutable accepted evaluation receipts are the accounting evidence; this
requires no new budget service/operation database. A lost live scope or unknown
settlement blocks the next call/restart rather than reconstructing zero usage. These are **ceilings, not a spend grant**. Caller ordinal 1 is not proof of unused budget. Trusted campaign state
must count invalid/refused attempts as the design requires; renaming a round or
restarting a process cannot reset its consumption. The graph executes one round;
a host-controlled next round needs remaining campaign authority and an unconsumed
holdout, and may never exceed two train proposals. There is no autonomous loop.

| New port (`rsi_opt_` prefix) | Exact payload | Actor context / result effect |
|---|---|---|
| freeze | RoundRequest | coordinator verifies all frozen providers/current/CAS/authority before any evaluator work |
| train_reference | Empty | coordinator evaluates exactly 12×3 reference train cells; only allowed TrainProjection delivered |
| diagnose | Diagnosis(observation, train_case_ids) | proposer cites public train cells only |
| hypothesis | Hypothesis(id, change, falsifiable_prediction, metric=`mean_normalized`, train_case_ids) | one falsifiable hypothesis, then engine checkpoint |
| candidate | Edit(prompts={}, graph=null) | proposer supplies one parsed bounded edit; no edit of original/current |
| admissibility | Empty | coordinator validates full native unit and seals the private candidate |
| train_candidate | Empty | coordinator evaluates 12×3 candidate cells under explicit existing host budget grant |
| train_compare | Empty | accepted numeric train stage; stop/restore if invalid or no improvement; otherwise engine checkpoint before one holdout selection |
| holdout_seal | HoldoutAuthorization(selection) | owner context resolves an existing trusted evaluator holdout-selection grant, bound to campaign/round/reference/candidate/benchmark/runtime/budget/train receipts |
| holdout | Empty | coordinator consumes the selection once and evaluates paired 8×3×2 cells in accepted interleaving |
| compare | Empty | accepted numeric holdout stage, trusted-private result |
| decision | Decision(action=`candidate`, review_ref=null, owner_decision_ref=null) | owner selects manual candidate/retain by default; numeric success is never an adoption grant |
| adoption_review | Empty | owner binds the already verified review/decision to an engine checkpoint before adopt/Source patch |
| adopt | Empty | owner rechecks reviewed decision, current CAS/providers under native exclusion; generated target only |
| restore | Empty | coordinator verifies/restores exact reference without overwriting concurrent work; builtin verifies original Source unchanged |
| source_patch | Empty | owner emits a private reviewed Source-patch candidate for builtin, never runtime builtin override/push |
| stop | Stop(reason) | coordinator stops dispatch; retains unresolved work/reservations; no invented quiet |

These tool names are **new integration ports**, not tools claimed to exist in
MCP/ToolLoader. `RoundBinding.invoke(port,payload)->RoundView` is the required
binding surface. Every port is bound to its exact graph step, owned round and
host-supplied actor, with the exact type in PORTS/ROLES. No agent gets a generic
invoke/read/write/restore/evaluate tool. A direct, wrong-step, unknown-run/port,
ungranted actor or malformed invocation refuses before effects. Trusted fixture
controls may select caller contexts; they are not JSON authority fields.

`RoundView` returns finite phase, reference/candidate/current logical versions,
optional **accepted public TrainProjection**, stopped/reason, exact accepted
Amount totals for known usage and retained unknown reservations, quiescent and selected action; `grants_authority` is always false.
Private checker material, full receipts, holdout prompts/results and checker
traces are never included. Fixed numeric/status train feedback intentionally
reveals performance information; no semantic noninference guarantee is claimed.
This finite slice uses the accepted allowlisted train projection, not an invented
raw trace reader. Raw agent/checker traces remain outside proposer tool grants.
Trusted owner diagnostics may inspect retained immutable full receipts separately.

`graph_result(view)` returns the exact `GraphResult` wire: flat `continue`,
`action`, `view`. Those first two fields are SDK tool-result flags derived from
trusted state, not candidate prose. No tool port may let a proposer set them.

`OptimizerError(code, detail)` is the loud pre-effect exception. Exact codes are
`UNKNOWN_PORT` (unbound tool), `UNKNOWN_INPUT` (unknown fixture fault), `AUTHORITY`
(caller/spend grant), `STOPPED`, `CHECKPOINT`, `ORDER`, `MALFORMED_INPUT` (strict
payload), `INPUT_MISMATCH` (request differs from trusted frozen inputs),
`UNSAFE_RESUME`, `PROVIDER_CHANGED`, `CONTAMINATION`, `FORBIDDEN_EDIT`,
`NO_ADMISSIBLE_EDIT`, `HOLDOUT_AUTHORITY`, `CONFLICT` (original whole-unit CAS),
`NOT_ELIGIBLE`, `ADOPTION_AUTHORITY`, and `POLICY_REFUSED` (target/action mismatch).
Every such refusal preserves the complete owned pre-call snapshot. Evaluator
post-launch invalid/unknown/budget outcomes instead retain their accepted results,
usage/reservations and a stopped RoundView; they are not rolled back into a
no-effect exception. A normal tool adapter maps an entry exception to the graph's
`tool_error -> stop` branch without claiming successful settlement.

## Existing edit path; current versus private shadow

Freeze the original complete unit first. Both reference and candidate use the
same private generated conversion, exact model/settings/providers and embedded
frozen prompts. For builtin investigate, export to a private generated reference
shadow; builtin bytes/registration remain unchanged. Refuse if its shape/runtime
cannot be preserved; do not silently substitute host/default models.

The accepted native `derive_candidate(bundle, {prompts,graph})` is the parsed
whitelist, followed by `compile_bundle/seal` and accepted version catalog paths.
`Edit` keys are exactly those native keys; prompt IDs come from the captured
native prompt map, e.g. `inline:gen_eval_fixture__investigator`, not arbitrary
role/model selectors. Allow only maker prompt bytes, graph/step descriptions and
ordering of already allowed optional public cross-config contexts. Required
sources, schemas/outputs, topology/steps, checker roles, tools/capabilities,
model/route/effort/temperature/limits, benchmark/checker/scoring inputs and global
settings stay exact. Unknown files/keys/references refuse. Exact frozen case IDs
and case literals are forbidden in proposals; literals are complete declared
benchmark strings, not a blacklist of ordinary words like “report” or “task”.
Lexical exclusion is finite and does not promise arbitrary semantic non-memorization.

The NEW trusted candidate adapter validates before any registration/seal effect,
then reuses the existing whole-unit path on an optimizer-owned **private draft**
only. If it calls `PipelineVersions.edit`/config_edit, their automatic adoption
changes that draft, **never the original user-serving target**. It captures and
re-verifies the resulting immutable catalog seal for evaluator Selection. It
must not use raw generic config_edit authority over the original. Record candidate
and original separately; a private registered candidate is not “current”.

On rejection/no improvement, existing `restore(original, reference_version,
expected_current=original_CAS)` is normally a verified no-op because original
never moved. Preserve provider-before-noop behavior. If a concurrent writer
changed CAS, refuse with no effects; never force old bytes over it. Failed or
unknown private publication stays pending under existing native descriptor/gate
until compatible reconciliation; candidate failures never warrant restoring over
unknown writes. Pinned old runs/roles/history remain governed by the accepted
native path; no repin or shadow run/pin/finish ledger is added.

An eligible private result yields a **manual candidate** by default. Generated
adoption needs a separate actual nonauthor review and explicit existing owner
decision, rechecked at effects under native CAS/exclusion. The graph's checkpoint
or a JSON review_ref string alone cannot grant that authority. Builtin winners
produce Source-patch candidates for normal reviewed integration; no builtin alias
replacement is allowed. The fake's `reviewed`/`owner-approved` fixture labels model
host-owned records only; they are not production tokens.

## Frozen measurement, holdout and stops

Train is 12 cases, three repeats per arm. Reference train result is captured
before public diagnosis; candidate train adds the matching 36 cells. No subset,
extra favorable retry or previous single-run baseline substitutes. Select at most
one sealed train candidate before holdout. Holdout is eight cases, three repeats
and **both fresh arms** (48 cells), interleaved by the accepted evaluator plan.
The accepted `Selection` remains an arm identity, never a permission. The
`holdout_selection` digest identifies the existing trusted host selection record;
`HoldoutAuthorization.selection` references that same record. The optimizer may
compute the frozen selection identity but cannot authorize it by hashing inputs.
A trusted lookup must find the actual existing owner grant and exact frozen
reference/candidate/campaign scope; it must not mint fresh authority per call.
Only explicit host-owned one-use holdout authority allows launch. Its consumption
survives unknown results/restarts; no second selection for the same campaign or
feedback-driven holdout reuse. Unknown execution/accounting retains reservations,
blocks new dispatch and never fabricates quiet, retry or successful restoration.

Call accepted `validate_result` on every actual result, then `scorer_receipt` and
unchanged `evaluate_matrix` with its exact frozen manifest. Every expected cell
is required. Only genuinely settled task failure is fixed zero. Unknown/incomplete/
invalid/accounting/runtime/provider drift cannot be dropped or scored as zero.
The unrounded holdout rule is inclusive mean candidate−reference **>=5**, no
predeclared family regression, and one-sided paired eight-case sign-flip
**p<=1/20** over all256 assignments including zeros. Repeats are not independent
samples. Train completion never yields eligibility. Numeric eligibility remains
separate from review, owner decision and adoption.

Stop on target95, proposal/round limit, budget/deadline/feasibility, contamination,
no admissible edit, no train improvement, holdout rejection, invalid/unknown
execution/accounting, provider/runtime/Source/current drift or owner stop. A
complete round's engine path ends at stop; at most two proposal ordinals exist.
Unknown stop may have quiescent=false; original unknown receipts/reservations stay
retained until a separate supported reconciliation proves settlement. Process-loss
resume without reliable retained authority refuses UNSAFE_RESUME. There is no
new operation database/runtime, heartbeat or GO-token scheme.

## Graph, fake and shared proof obligations

`round.graph.json` is actual SDK1.5.94 `PipelineGraph` data with explicit tool
nodes, no agent/default reader/tool-creation grants, bounded retries=0, typed port
mapping and explicit branches to stop/restore. Hypothesis approval precedes
candidate creation; train comparison approval precedes holdout seal/dispatch;
adoption review approval precedes generated adoption or builtin Source-patch
candidate. Privileged continuation requires both the checkpoint flag and trusted
result flag. Manual/default decision goes to stop. Each new trusted tool binding
must enforce the exact owned graph-step/actor/provider scope too; a graph alone
does not authenticate callers or enforce physical budgets. This statement proves
schema/GraphResolver routing, not actual host checkpoint/tool-dispatch conformance.

`fake.py` is a standalone **simulation**, importing only pure accepted numeric/
codec and separate evaluator-fake/types. It has no SDK run creation, normal host
registration, provider send, production benchmark or native evaluator import.
Its simulated receipt IDs, current/history, scores, restore and counters are model
state only. No fake success can satisfy `native-shared-conformance`.

`conformance.run_shared(factory)` uses the SAME literal scenarios for future fake
and genuine bindings, without provider-kind skips. Fixture controls prepare
synthetic public units/providers, select real caller context, authorize existing
owner records, approve genuine checkpoint CAS and alter actual corresponding
fault inputs/transport. A native fixture must observe actual SDK/provider/current/
benchmark/accounting artifacts; it must not patch asserted methods, fabricate
executions or return expected counters. Refusals compare full owned snapshots.
Literal cases include useful edit, manual/default versus approved adoption,
builtin Source-only selection, exact inclusive margin/noise, family rejection,
no improvement, grant/order/checkpoint denial, holdout one-use, unknown/budget/
runtime stop, residual cross-call budgets/original deadlines, concurrent edit
refusal before more evaluation/adoption/restore, exact case-id/literal/topology/route/model
refusal and ordinary common-token proposal success. All are synthetic.

The statement test exports wire schemas, fake transcript, exact imports and actual
SDK compile/routing evidence. Future acceptance-only readback and native proof
remain **SKIP** here. No benchmark/private gold/model spend, normal runtime
adoption, deployment or general security/sandbox guarantee follows.

## Native binding (PROOF)

`native_binding.NativeRound` is the trusted `RoundBinding`; `install(sf, loader)`
binds the 17 ports and this graph into the ONE host it is handed (never at import
or boot). A round is an ordinary engine run of `round.graph.json` with a
`_rsi_optimizer` run-context marker. `invoke(port, payload, actor=...)` checks,
before any effect: port, host-supplied actor against ROLES, the run's actual
terminal/paused state and current node, strict payload, port preconditions and
the original target CAS. Only then does it stage the invocation and call the
engine's `advance_run`, which claims and executes exactly that tool step; the
port tool resolves the live binding by the engine-injected `run_id` and refuses
(graph error branch, no effect) any dispatch without its owning invocation.
Flags returned to the engine come from the binding's trusted decision. Owner
checkpoint answers use the engine's `get_checkpoint`/`approve_checkpoint` CAS.

Effects use accepted providers only: `PipelineVersions` capture/read/current/
restore/whole-unit CAS; `derive_candidate` on the captured reference bundle,
then `compile_bundle`/`seal` through the versioning catalog and
`register_sealed` of the immutable physical candidate (no target is edited; the
original serving target changes only through `restore` of the cataloged
candidate after reviewed owner adoption); `NativeEvaluator.evaluate` for 36
reference train, 36 candidate train and 48 paired holdout cells; accepted
`validate_result`, `scorer_receipt` and unchanged `evaluate_matrix`. Each
request carries the frozen `Runtime` with the accepted compiled policy identity
for exactly its arms (that identity covers the selected arm set), and the
residual round/campaign `Budget`: original envelopes minus known usage and
retained reservations of every sealed evaluator result of this round/campaign,
and the original deadline minus host-clock time since freeze. An unresolved or
unknown evaluation, or an expired deadline, dispatches nothing. Holdout needs
the owner's host grant of the exact computed selection identity and refuses an
evaluator consumption record for it. A round run without its live binding
refuses `UNSAFE_RESUME`. Builtin targets use the private generated shadow as
the measured reference, verify the builtin Source files unchanged and end in a
private Source-patch CANDIDATE artifact.

Graph correction found by the first native dispatch (SDK 1.5.94): a checkpoint
TOOL step pauses only when no transition matches, and approval continues to the
FIRST `from: checkpoint` edge. The statement's unconditional fallbacks on
`hypothesis`/`train_compare`/`adoption_review` were therefore taken without any
pause. Fallbacks now match `continue: false` or `_tool_error: true`; the single
approved continuation of `adoption_review` is `adopt`, which performs no runtime
adoption for a builtin target and routes `action: source_patch` on to
`source_patch`. Nodes, tools, checkpoints, ports and resolver checks are
unchanged.

`native_fixture` builds a fresh accepted evaluator world per scenario with only
the reference target; the candidate is always derived from the proposer's edit.
The accepted deterministic transport answers under the real admission boundary;
its answer script is fixture data scored by the trusted checker (reference
value-only 6/10; candidate value+citation 10/10 on its first repetition, else
6/10; fault scripts map the fake's better/equal/worse cells onto those levels).
That checker's points are {0,4,6,10} of 10, so per-case holdout differences are
multiples of 20/3: the literal `inclusive_margin` vector (difference exactly 5
with all eight cases positive, p=1/256) cannot be produced natively.
