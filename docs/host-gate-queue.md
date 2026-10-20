# Host gate queue Source

`scripts/gate_queue.py` is the single-file host FIFO in front of the existing
launcher. No official package provides this host-owned disposition protocol.
Source review, installation, native execution and deployment are separate.
This change installs nothing and does not retrofit existing unbound tickets.

Ordinary `submit` requires a registration file and a purpose. One exact Source,
controller and ordered registration batch produce one physical ticket, including
when five State contexts share the same native proof. The complete registration
input is retained privately beside the ticket; only its immutable identity fields
form the binding. Source `context.base_sha` identifies the measured commit. A
State result artifact (for example a SHA256 runtime-proof bundle) is separate.

Owned commands use the existing AItelier CLI virtual environment and explicit
Source root. Plain host `python3` may lack `httpx`; that is a local configuration
refusal, never a held State observation. Invoke the interpreter by absolute path,
without activation or changing PATH/PYTHONPATH:

```sh
AROOT="$HOME/AItelier"
PY="$AROOT/.venv/bin/python"
Q="$HOME/.AItelier/bin/gate_queue.py"
# DRIVER is this controller's own configured driver ID; never a token value.
env AITELIER_DRIVER_ID="$DRIVER" GATE_QUEUE_AITELIER_ROOT="$AROOT" \
  "$PY" "$Q" submit --registrations "$R/registrations.json" \
  --purpose final-composed-normal --label "$LABEL" "$WORKTREE" "$SHA"
```

The root must contain the real `cli/client.py` and `core/driver_credentials.py`.
The queue lazily imports those modules and uses the existing APIClient, timeout,
URL and credential-selection policy. It requires its own driver header and
checks `/api/drivers/me`; admin credentials confer no bypass. It records client
module hashes and interpreter provenance, never headers or tokens. No dependency
installation, alternative runtime, automatic re-exec or credential search occurs.

The detached daemon uses that same interpreter, Source root and driver ID. Keep
the existing `GATE_RUN_SH` and `GATE_BINDING_FILE` assignments and their meanings.
The launcher receives the pre-client-import environment with the four recipe
identity variables removed; client `.env` additions do not reach the child.
Before installation the same recipe can name the reviewed checkout's
`scripts/gate_queue.py`. Copying reviewed bytes into the host bin is a separate
authorized operation, after the old daemon and gate have settled.

`status TICKET --json` reads the full ticket without importing the client.
The readback includes binding digest, ticket revision, immutable contexts,
active/excluded contexts, hold, admission/invocation identity, decisions,
physical verdict, provenance and State-delivery receipts.

Use `report-context` as the covered controller's external-observation boundary:

```sh
env AITELIER_DRIVER_ID="$DRIVER" GATE_QUEUE_AITELIER_ROOT="$AROOT" \
  "$PY" "$Q" report-context "$TICKET" --expected-sha "$SHA" \
  --binding-sha256 "$BINDING" --expected-revision "$REVISION" \
  --request-id "$REQUEST" --context "$ATTEMPT" \
  --observation "$R/observation.json" --failure-decision "$R/failure.json"
```

A failed observation requires an explicit governing failure decision. Its JSON
identifies `decision_id`, `irreversible_for_attempt:true`, controller, ticket,
Source, binding, original purpose, affected original context IDs, source project,
node/revision/criterion, optional source attempt/context/evidence ID, artifact
kind/ref, completed immutable report ref/hash, applicability reason and
`disposition:hold|cancel`. Nullable fields are named
`source_attempt_id_or_null`, `source_context_hash_or_null`, and
`source_evidence_id_or_null`. A completed report has `status:completed`,
`settled:true`, `usable:true`, the exact `criterion_id`, `verdict:fail`,
`artifact` and `source_sha`. The queue validates source association against
supported State queries and preserves the bytes. Worker prose, generic FAIL
text, timeouts and disconnects provide no irreversible-failure authority.

The local failure disposition commits before any covered failed State POST.
The default is `held/needs-disposition`, with no gate verdict, exit or report
for work never launched. State terminal failure can also hold admission without
inventing a native criterion failure. Held tickets are not repeatedly queried;
an unrelated eligible ticket can advance. `daemon --once` counts one actual
admission, scanning past held work and returning idle if none can run.

Resolve a held ticket with `decide` using the same exact SHA, binding, revision
and unique request ID. `--action cancel --reason TEXT` withdraws only that
physical ticket. `--action keep-reuse` additionally requires
`--reuse-purpose`, `--retain-contexts` (absolute JSON array file) and
`--ack-failures` (absolute JSON array of the exact recorded fingerprints).
`continue-contexts` retains a nonempty healthy original subset; failed/stale
members remain recorded and excluded. `baseline-evidence:REASON` retains no
acceptance contexts and requires `--baseline-authority ATTEMPT` naming the exact
failed current context whose matching node claim supplies authority. Both forms
are tied to the original binding/purpose/Source and are invalidated by a new
governing failure. `--action recheck` is for an authority hold with no failure or
keep decision. No operation clears State ownership or adopts a foreign owner.

Cancellation before admission means zero launches. Admission first permanently
records the invocation and means finish-and-exclude: no engine signals and no
claim that resources became free. A terminal candidate/failed State observation
is deferred while that physical invocation is live. Later use the same immutable
failure decision, a new wrapper request ID, the genuine completion observation
and `--physical-receipt` for that invocation. The receipt names `ticket`,
`source_sha`, `invocation_identity`, `outcome:owned-quiescent`,
`actual_ownership_refs_and_sha256:[{path,sha256},...]`,
`raw_gate_exit_ref_or_null`, and `report_scope:{attempt_ids:[...]}`. Ownership
refs are immutable local files. The queue also requires its exact child-exit
receipt and an exited invocation. This only establishes the covered physical
operation; the controller remains responsible for the full external-report scope.
An excluded context cannot receive candidate gate evidence from the shared run.

Observation JSON uses the supported `report_external_attempt` arguments unchanged:
`attempt_id`, `observation_id`, `expected_version`, `context_hash`, `status`,
`report_ref`, `report_sha256`, `quiescent`, optional `artifact`/`artifact_kind`,
`detail`, and the **attempt** `fence`. Failed observations omit artifact fields.
A node claim's fence is distinct. HTTP queries use plain arguments and unwrapped
results: newest-first `get_node.attempts`, live matching node claims,
`get_attempt`, and an evidence list. No private State database or invented
terminal-report endpoint is used.

If State delivery refuses or becomes unknown, the local disposition stands.
Replay the same wrapper request ID and exact observation bytes; the queue reads
back the attempt and resends the same observation ID/body if necessary. A changed
body under the same request or decision ID refuses. Resolve an unknown delivery
before introducing a new observation. A deferred intent was never sent, so actual
completion is a new observation and wrapper ID referencing the old decision.
Cancelled tickets are final; deliberate retries require fresh submission/new IDs.

`mutation.lock` is the stable, short transition lock for submission, admission,
disposition, finalization and recovery. Client mutations take it non-blocking;
`daemon.lock` remains a single-daemon lifetime lock. Admission rereads the selected
ticket, takes one bounded State snapshot (10 seconds per request, 30 per batch),
then records a permanent admission marker before spawning the existing launcher.
Finalization rereads disk so concurrent exclusions survive. Exact pre-transition
bytes and hashes live in immutable sibling history files; ticket publication
fsyncs file/directory. Receipts/tickets are capped at 1 MiB, contexts and retained
requests/decisions at 32, IDs at 128 ASCII characters and reasons at 4000 characters.
Overflow refuses rather than truncates.

Bound recovery adopts only an exact PID/birth/nonce identity or an exact matching
exit receipt. Exact-live adoption records the observed child exit even when no
native verdict appears; its runner return code remains unknown (`null`). That
no-verdict outcome stays an infrastructure failure with no native exit/verdict,
while the retained matching exit receipt permits the controller's genuine final
physical receipt and deferred terminal State delivery. Failed status alone is
never quiescence evidence. Recovery never uses a global container-name prefix as
ownership. Unknown execution becomes `held/recovery_unknown` and blocks further admission where a
live consumer may remain. Known adopted work finishes once; no automatic retry
or duplicate launch. Gate exit 1 plus a real manifest remains a delivered native
red (`done`, `verdict:failed`), distinct from no execution and infrastructure loss.
State and filesystem snapshots are not a global transaction: the local lock orders
the covered report producer, while a separate State writer can race afterward.

Return codes: 0 local/read success (inspect delivery/outcome), 3 unknown ticket,
4 busy short lock, 5 refusal/bootstrap error, 6 admitted finish/deferred terminal
report, 7 State/delivery/recovery unknown. No result implies State verification.

Explicit unowned work uses `submit-unowned --unowned-reason TEXT`. That command,
read-only commands, v1 `cancel`, and a v1-only daemon remain stdlib-only under
plain `python3`. V1 cancellation retains the accepted exact fields/preimage and
assertions. V2 cancellation requires authenticated `decide`; legacy `cancel`
refuses v2 fields. Never restore old ticket bytes while a daemon can run.

The focused tests are **UNRUN by the Source author**. A different author must
review and execute the final exact commit in a detached owned throwaway container
with `--rm --init --network none --cpus 2 --memory 2g --memory-swap 2g`, at most four
such containers globally, never the production backend or the host. Mount the
existing CLI venv read-only at its identical absolute path and set
`GATE_QUEUE_TEST_VENV_PYTHON` to that interpreter. Set
`GATE_QUEUE_TEST_ORDINARY_PYTHON=/usr/bin/python3`. Use the container's own exact
checkout and explicit PYTHONPATH for pytest; the child recipe does not inherit it.

```sh
python3 -m pytest -c tests/host_gate_queue/pytest.ini \
  --confcutdir=tests/host_gate_queue -s tests/host_gate_queue
```

The owned suite exports only six required tracked files from exact `HEAD` into a
disposable root (queue, package initializers, client, credentials and datadir;
about 95 KiB, with no full-repository copy per test), uses the real
APIClient/httpx/credential modules and a loopback stdlib HTTP fixture. Missing
interpreter/modules or an escaped credential path is an error, never a skip.
It builds the accepted literal harmless Git objects and five registrations,
exercises real CLI transitions and a pipe-controlled harmless child, and prints
nonempty timelines and literal request transcripts without token bytes. All
owned children are released/reaped or observed exited before fixture quiescence.
B01–B03 and P01–P18 are the 21 owned vectors; O01 is the retained 15-case v1 suite
(with only its submit helper adapted). Run that retained suite under the ordinary
interpreter separately if the pytest runner uses a different interpreter.

C01–C06 retain actual first assertion failures for missing binding (exact 8a755
Source), reversed State/disposition order, stale dequeue, plain-python owned
bootstrap, post-import environment inheritance and an in-process transport stub.
Original missing-producer errors and prior withdrawal-baseline RED remain visible.
The original withdrawal baseline SHA256 is
`283b4bbf1c39c1dd899e34cad8b2dce77426ef32d5ae38da31fa5b7c76b44ee4`;
its prior evidence is not regenerated or replaced by these controls. Passing
Source tests does not prove installation, native engine behavior or deployment.
