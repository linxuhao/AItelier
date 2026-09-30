# Static scope plans and retained coverage

This is a partial platform candidate. It does not enable subset execution or
publication. A separate private product candidate adopts the protocol in its
producer and publication consumers; both candidates require review together
before integration/deployment.

`tools/select_gate_scope.py REPO BASE HEAD FULL_SPEC --out PLAN` reads immutable
Git snapshots and `git diff --name-only --no-renames BASE HEAD`. It records the
changed files and declared symbols at both endpoints. Selection follows literal
resource references and registered GDScript class references from each authored
scene, with autoload dependencies included in every scene. A script change uses
the entire reverse file dependency, never a guess that only one function matters.
No directory or filename prefix maps a change to a scenario.

Missing resources, unsupported UID/relative references, dynamic resource loads
in reachable scripts, duplicate class identities, changes outside the proven
graph, selector exceptions, and incomplete current detector evidence produce an
explicit full plan with a reason. A missing/duplicate scenario inventory raises
an error: there is no safe full plan without its inventory. Empty selections are
never successful. This deliberately limited reader may select the full contract
for ordinary changes; no current product performance benefit has been measured.

The optional measurement envelope contains `head_sha`, `spec_sha256`, the native
`detector_result` from the existing `docker/godot/clock_sensitivity.py`, and the
separately retained `batch_context_dependent_scenarios` inventory. The reader
reuses the detector's assertion comparison. All four cells must cover every
current scenario and its authored assertion identities. An unflagged scenario
needs a load observation proving the injected cost reached it; untouched
negatives do not clear anything. Flagged scenarios and the known batch context
inventory are always included, with any identical opted-in replays. This detector
tests clock/load sensitivity; it does not establish independence from every
possible batch ordering or inherited-state effect.

The plan's `gate_coverage` payload must be copied intact into the attributed gate
`manifest.json`. It contains `coverage` (`full`/`subset`), `all_scenarios`,
`selected_scenarios`, `unselected_scenarios`, `fallback_full`, and structured
`selection_basis`, bound to the current head and full contract digest. Selection
preserves authored execution order. The producer should expand its existing
repeatability runs before planning, then reuse its existing contract-selection
helper with the selected names. Python, compile and script stages remain full;
this candidate changes only the static playtest scope plan and its transport.

`run_tests` reads that object from the attributed manifest, retains it in
`repo_gate`, at report top level, and in the tool return. It never extracts it
from a bounded log tail. Missing/invalid coverage makes an otherwise green gate
unmeasured; retained reds still outrank the absence. A subset cannot prune old
gate failure identities because the existing baseline has gate-level execution
authority rather than scenario-level authority.

`gate_evidence` retains coverage in its audit entries and refuses to turn a
subset or missing scope into clean release evidence. Existing red/skipped
outcomes remain red/skipped. This provides the goal-loop report/audit carrier,
but is not proof that a State DAG evidence receipt has adopted it. State evidence
must retain an immutable report reference and digest containing this payload;
the operator must record its acceptance separately.

`full_coverage` emits the default full payload from the producer's actual
expanded contract. `source_scope_refusal` checks its head, canonical contract
digest, and ordered inventory against an independently read source contract.
The separate private candidate reads the queue-attributed manifest in both
publication consumers, pins a clean source tree at gate start and finish, and
revalidates all four retained stage reports with its existing verifiers. A
subset is explicitly refused; missing or stale metadata is refused. These are
additional conjuncts: existing mainline-tip, head, exit-code, producer and
release requirements still apply. Focused tests exercise the actual producer
and authorization functions with real source commits and synthetic stage
responses. They do not establish native gate or publication acceptance.

Existing gate producers without this payload are intentionally incompatible
with clean coverage evidence. Integrate the producer, private publication
consumer and platform changes in one reviewed batch; no service is changed by
this candidate. Current tree context measurements and runtime timing pairs are
still required before any useful subset or speed claim.


## Provisional coding feedback

The opt-in `godot_playtest` purpose `provisional_round_feedback` reads the same
strict source contract and includes repeatability executions. Supply an exact
`base_sha`, project historical `sentinel_scenarios`, and `mandatory_scenarios`.
It prioritizes actual changed authored scenarios and their scenes' direct
literal resource consumers, retaining diff files/symbols and opaque dependency
reasons. Shared runtime changes recommend FULL; no consumers yields unavailable
rather than an empty success. An explicit `feedback_scenarios` diagnostic request
can run alongside a FULL recommendation, without claiming complete impact.

The machine report retains the complete ordered partition, source/spec and
normalizer source hashes, owner identity, request, first raw response and its
hash. `selected_pass` means only the exact selected assertion multiset ran and
passed. Omitted scenarios remain UNMEASURED with order/save/context UNKNOWN.
Even when every scenario was selected, provisional reports have `passed=false`,
`full_test_passed=false`, partial evidence and unresolved release disposition.
They cannot prune previous full-gate reds or authorize publication. A fresh
output directory preserves the first attempt rather than overwriting it.
The HTTP timeout includes queue wait; a timeout does not prove the engine owner
settled. The controller must independently observe settlement and quiescence.

Default calls retain their existing full acceptance behavior. The conservative
safety selector is unchanged and still selects FULL without its required proof.
The project CLI may provide its private sentinel/mandatory policy and its
existing mandatory play-test verifier. The private stable candidate must still
run one complete Python, compile, authored play-test and script gate before
acceptance or release. This path introduces no clock/context independence claim.
