# Host gate queue Source

`scripts/gate_queue.py` is the single-file host FIFO in front of the existing
launcher. No official package provides this host-owned disposition protocol.
Source review, installation, native execution and deployment are separate:
installing the reviewed bytes is `tools/install_gate_queue.py` (below), and no
part of this protocol retrofits existing unbound tickets.

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
`scripts/gate_queue.py`.

### Installing the reviewed bytes is a tool, not a copy

`tools/install_gate_queue.py` is the only supported way to put reviewed bytes into
the host bin. It is stdlib-only and uses absolute paths:

```sh
REPO="$HOME/AItelier"          # any checkout holding the reviewed commit
env GATE_QUEUE_DIR="$HOME/.AItelier/gate-queue" "$PY" "$REPO/tools/install_gate_queue.py" \
  --commit 7e9f53a8c114c8f5d7cc7523f126d52aa8d76faf \
  --sha256 409f0779a8b32ca929f85bfbd0045c12df2f09955e5fbb0bca0fd993fc808e43
# drift report, never a write:
env GATE_QUEUE_DIR="$HOME/.AItelier/gate-queue" "$PY" "$REPO/tools/install_gate_queue.py" \
  --check --commit 7e9f53a8c114c8f5d7cc7523f126d52aa8d76faf \
  --sha256 409f0779a8b32ca929f85bfbd0045c12df2f09955e5fbb0bca0fd993fc808e43
```

It reads the bytes with `git -C <repo> show <commit>:scripts/gate_queue.py` --
never the working tree, so a dirty checkout cannot install itself -- and writes
only when those bytes hash to the explicitly supplied `--sha256`. The write is a
temp file in the target directory followed by `os.replace`, mode 0644, into
`<dest>/gate_queue.py` (`<dest>` defaults to `~/.AItelier/bin`, `--repo` to the
checkout the tool lives in). `--check` never writes: it exits 0 only when the
installed bytes equal the named reviewed bytes, and otherwise exits 1 after
printing both sha256 values and the commit it compared against. Exit codes: 0
installed/match, 1 `--check` drift, 2 refusal (usage, unreadable commit, sha256
pin, unsettled queue, filesystem). Every refusal names what it measured and says
nothing was written.

The two settle conditions are executable, not prose, and each refusal names the
blocker:

1. **No live daemon.** The probe uses the queue's own markers, not a guess: the
   daemon holds an exclusive flock on `<queue>/daemon.lock` for its whole
   lifetime and publishes its pid in `<queue>/daemon.pid` (`cmd_daemon`). A held
   lock, or a pid that is live, not a zombie, and whose `/proc/<pid>/cmdline` is
   this queue, is a live daemon. During the write the installer takes that same
   lifetime lock itself, so no daemon can start halfway through and mixed
   generations cannot meet in flight.
2. **Every ticket terminal.** The probe reads `<queue>/tickets/*/ticket.json`:
   `done`, `failed` and `cancelled` are terminal; `queued`, `running`, `held`
   (and a `ticket.json` it cannot parse) are not, and the refusal lists their
   ticket ids and states.

`--daemon-probe CMD` / `--ticket-probe CMD` replace one probe with a command that
receives the queue directory and exits 0 when its blocker is present; its first
stdout line is quoted in the refusal. `tests/unit/test_gate_queue_installer.py`
drives both the real markers (including a genuinely running
`scripts/gate_queue.py daemon`) and that seam, both ways.

**Measured on this host, 2026-11-08.** The installed copy was sha256
`95b1255073bd09bbb8f77f69ad3c8113e6e8ee8ed43cda58c1164343e9fae7e6` (70521 B,
installed from git `8a755972cc`, mtime 2026-10-20) -- **superseded**. The reviewed
`7e9f53a8c1:scripts/gate_queue.py` is sha256
`409f0779a8b32ca929f85bfbd0045c12df2f09955e5fbb0bca0fd993fc808e43` (73630 B).
Before installing, the settle measurement was: no live daemon (`daemon.lock`
unlocked, no `daemon.pid`, no `gate_queue.py daemon` process) and 196 tickets, all
terminal (150 `done`, 23 `failed`, 23 `cancelled`). The host-gate-queue suite run
in a disposable container with `GATE_QUEUE_SOURCE` pointed at the installed file
measured **3 failed / 61 passed** against the superseded bytes and **64 passed /
0 failed** against the reviewed ones; `--check` reports `match` afterwards. Those
two numbers, not this paragraph, are the claim.

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

Read `platform_unreviewed` with every verdict. Since the 2026-11-04 owner ruling
(wuxia `iss-5e51a56608c64f60`) a stale frozen binding never stops a gate: the
identity is observed and recorded instead, and when it is not the reviewed
binding `gate_run.sh` writes `platform_unreviewed ...` on the second line of
`<suffix>.gate.exit`. Finalization keeps the verdict exactly as the gate reported
it (a red stays `verdict:failed`, a green stays `verdict:passed`) and adds
`platform_unreviewed: true`, `platform_unreviewed_evidence`, and a daemon-log
line. `platform_unreviewed: true` with `verdict:passed` is a measurement, not
release evidence; with `verdict:failed` it is still evidence that the code is
broken. A single-line exit file -- every gate before that ruling -- is read the
same way, with `platform_unreviewed: false`.
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

## The two interpreters, and how the suite supplies them

The owned vectors spawn real children under two interpreters:

| variable | role | supplied only when observed to |
|---|---|---|
| `GATE_QUEUE_TEST_VENV_PYTHON` | real owning CLI/client surface | run, and `httpx` resolves to an existing file |
| `GATE_QUEUE_TEST_ORDINARY_PYTHON` | stdlib-only surface | run, and that file does not resolve |

`tests/host_gate_queue/conftest.py` supplies both from the environment the suite
actually runs in, and both invocation shapes load it — the gate's pytest leg
(`pytest <repo> --rootdir <repo> …`, which collects this tree) and the review
shape below. A hook in `tests/conftest.py` would not serve the second one, which
is why the supply sits in the directory itself.

```sh
# gate shape: the gate's pytest leg runs this argv over the repo
python3 -m pytest tests/host_gate_queue --rootdir "$REPO" \
  --continue-on-collection-errors -p no:cacheprovider
# review shape
python3 -m pytest -c tests/host_gate_queue/pytest.ini \
  --confcutdir=tests/host_gate_queue -s tests/host_gate_queue
```

Nothing about either role is assumed from a path. Each candidate is executed
(`subprocess.run([candidate, "-c", PROBE], …)`, with `PYTHONPATH` removed
because the fixture strips it from every child it builds) and only that observed
result fills the variable; the running interpreter is the first candidate, and
when it lacks the client it serves the stdlib-only role instead. `PROBE`
(`tests/host_gate_queue/conftest.py`) exits 0 only when `httpx` resolves to an
existing file: a bare `import httpx` also succeeds on an empty directory named
`httpx` — a namespace package, whose `__file__` is None — and that is not a
usable client, so the name alone is not the observation.
`test_client_probe_rejects_a_namespace_package_named_httpx` pins both sides,
running the probe with `-S` so site-packages cannot shadow the directory under
test, and the positive control is a regular package in a second directory.
A pair that is already set is never probed. In the ordinary case this costs two
short subprocess probes, writes no file, installs nothing, re-execs nothing and
mounts no venv. Measured in the disposable container of this image
(2026-11-07): `/usr/local/bin/python` → `python3.12` (3.12.15, the binary the
image's `python3` resolves to) imports `httpx` 0.28.1, and `/usr/bin/python3`
(3.13.5) fails `import httpx`. With no `GATE_QUEUE_TEST_*` set the gate shape is
64 passed / 0 skipped and the review shape is 64 passed / 0 skipped; with
`GATE_QUEUE_TEST_VENV_PYTHON=` (present but empty) it is 18 passed / 46 skipped.
The supply itself was observed case by case in that same container: two probes
and both roles when nothing is set, zero probes and both values kept when the
caller set the pair, and neither variable set when a role could not be observed.
Those numbers are this image's, measured with the directory mounted read-write
and `.git` read-only; no capability is claimed for any other image, host or
mount, and none of the three measurements is a claim about the production
backend.

What the suite still refuses, deliberately:

- a variable the caller sets is used verbatim and never overwritten — including
  one that is present but empty, so `GATE_QUEUE_TEST_VENV_PYTHON=` keeps the
  owned suite skipping, naming
  `GATE_QUEUE_TEST_VENV_PYTHON must name the existing read-only CLI venv interpreter`,
  and running none of the owned vectors;
- an environment where a role cannot be observed — no candidate imports
  `httpx`, or no candidate is a working stdlib-only interpreter — fills
  *neither* variable and therefore skips, because a half-supplied pair would make
  the fixture raise instead of skipping;
- a value that names a missing interpreter or missing modules, or an escaped
  credential path, is still an error, never a skip;
- nothing is installed or downloaded, and no virtualenv is created or assumed to
  exist at any path.

Supplying the exact CLI venv by hand remains available for reproducing one
specific owned environment (mount it read-only at its identical absolute path
and set `GATE_QUEUE_TEST_VENV_PYTHON` to that interpreter), but it is no longer
required for the suite to run. Use the container's own exact checkout and
explicit PYTHONPATH for pytest; the child recipe does not inherit it.

The focused tests are **UNRUN by the Source author**. A different author must
review and execute the final exact commit in a detached owned throwaway container
with `--rm --init --network none --cpus 2 --memory 2g --memory-swap 2g`, at most four
such containers globally, never the production backend or the host.

The owned suite exports only six required tracked files from exact `HEAD` into a
disposable root (queue, package initializers, client, credentials and datadir;
about 95 KiB, with no full-repository copy per test), uses the real
APIClient/httpx/credential modules and a loopback stdlib HTTP fixture.
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
