# harness.a-busy-render-lock-queues-instead-of-refusing (rev 2): delivery note

- Worker attempt: `attempt-2cd4b2ece4dd411aafb99c8365ba5b3c`. This is a worker, not a pipeline round.
- Base: `bf94f7247a2468726533b16e5204871e27bf79a9` (the r2 candidate, branch `director/lockq-r2-20260924`).
- Branch: `director/lockq-r3-20260924`.
- Code commits, in order:
  - `43c2175414c1c5e30888f3a2086b1c7ded83ad76`: harness, tool and tests.
  - `399e9fa702ef74743056beffc7c5881d4ec4047a`: the P2d test also queues a request while the table is locked.
  - `c389f53014adae76d09e5cfa7603478b0f23af69`: the reader test also covers `godot_compile` without an evidence cycle.
- Every test run below was taken on `c389f530`, unless its log header names another head.
  - The commit after `c389f530` adds only this note and `logs/`.
- Note written 2026-09-24T12:35:32Z (`date -u`).
- The review this round answers: `~/.AItelier/director/reports/lockq-r2-review-20260924/review.md`.
  - Its probes are the source of the new tests: `probes/probe_wait_heartbeat_test.py`,
    `probes/probe_queue_timeout_test.py`, `probes/probe_readers_test.py`.
  - I did not copy the probes. Each suite test cites the probe it came from.

Every test run below ran in a throwaway `aitelier:latest` container (`docker run --rm --init -m 3g`).
The repository was mounted at both paths. Every log written by `run.sh` or `mut3.sh` shows `gitstatus_rc=0`.
The x20 loop log has no such line, because its runner does not print it.
The live `aitelier-godot` container was not contacted. Every harness test serves `godot_harness.py` in-process
over a temp sqlite owner table, and the tool tests point the real tools at that served harness.

## What changed

`docker/godot/godot_harness.py`

- The waiter tries the deployment-admission fence without blocking.
  - `acquire_render_owner_waiting` calls `acquire_render_owner(..., blocking_fence=False)`, which takes `LOCK_SH | LOCK_NB`.
  - A held fence raises `BlockingIOError`. The waiter counts that poll as queued and polls again.
  - Before this, each poll blocked on the fence. The disconnect check and the limit check ran only between polls, so
    neither could fire while a deployment held the fence (the review's P3c and P3d).
  - Callers other than the waiter keep the blocking default.
- The waiter also keeps waiting when a poll loses sqlite's busy wait (`sqlite3.OperationalError` "database is locked").
  Any other `OperationalError` still raises.
- `do_POST` checks both conditions again once the request owns the render row and holds both render locks, just
  before the render starts.
  - If the caller has disconnected, the row is released with reason
    `not rendered: the caller disconnected before the render started`, and nothing renders.
  - If `render_wait_timeout_sec` has run out (time counted from admission start), the row is released with reason
    `not rendered: render_wait_timeout_sec ran out before the render started`. The response is the 409
    `render owner wait timed out`, and its `detail` says ownership came after the limit.
  - Before this, a request could take ownership after the fence lifted and render for a caller that had gone, or
    render past its own limit.
- The 409 `render owner wait timed out` body is now built by `_wait_timed_out`. It adds `deployment_fence_held`, and
  its `detail` names the fence when the fence was what the request waited behind.
- The heartbeat thread retries after an `sqlite3.OperationalError`, logging
  `render owner heartbeat failed, retrying: ...`.
  - Any other exception still ends the thread (`render owner heartbeat stopped: ...`).
  - A process that dies takes its heartbeat thread with it, so its row still goes stale and blocks.
- The comment above the render admission ("Queue behind any render in flight") now says:
  - Both checks run on every poll, and again after ownership with both locks held.
  - A request stopped while queued makes no row.
  - A request stopped after taking ownership releases its row with a "not rendered" reason.
  - The three tests under "the-wait-is-bounded-by-the-callers-own-timeout" below check each of those statements.

`aitelier/tools/godot_playtest/impl.py`

- New constant `RENDER_QUEUE_SHARE = 0.5`.
- `post_playtest(payload, timeout)` now sends `render_wait_timeout_sec = RENDER_QUEUE_SHARE * timeout`, unless the
  payload already sets it.
- The docstring now names the third shape (a render-owner refusal is a gate that did not run) and the queue share.

`tests/unit/test_harness_render_queue.py`: 11 new tests (23 in the file). One existing test gained an assertion.

## The regression, and how the queue share resolves it

- The regression, from the review's P4:
  - `urllib`'s socket timeout counts idle time.
  - The harness sends nothing while a request is queued.
  - So a queue wait longer than the caller's `timeout` ended as a socket timeout. `post_playtest` reported it as
    `gate_timeout` with `passed: false`.
  - `gate_evidence` reads that as `failed` / `known_failure`, and game_harness `5_compile` routed it to `5_vision`,
    the product-failure path.
- Raising the socket timeout is forbidden, and it also does not fit: `5_compile` has `timeout_seconds: 3900`, so the
  socket cannot outlast `post_playtest`'s 3600 s by much.
- The fix: the caller declares its own queue bound, below its socket timeout.
  - The bound is `render_wait_timeout_sec = 0.5 * timeout`: 1800 s for the default 3600 s.
  - A queue wait that reaches it ends as the harness's 409 `render owner wait timed out`, well before the socket's
    idle limit.
    - The harness answers within one poll of the limit: the 0.25 s sleep, plus the poll itself.
    - A poll against a locked owner table can take up to sqlite's 5 s busy timeout. The margin is
      `0.5 * timeout` (1800 s by default).
  - `post_playtest` already turned that 409 into the absence shape: `gate_skipped: true`,
    `skipped_because: render_owner_wait_timed_out`.
  - Every reader then sees a gate that did not run. `report_state` gives `skipped`, `release_disposition` gives
    `unresolved`, and `5_compile` routes to `5_release_wait`. This is the gate-accounting contract that has been live
    since 11:26Z today; this change adds no new reading of it.
- A timeout after the render started stays a measured `gate_timeout`, `failed` / `known_failure`, routed to `5_vision`.
- The cost: a render that starts after waiting w seconds has `timeout - w` seconds of socket left. That is at least
  half of `timeout`, since w is at most the queue share.
  - So a render that needs more than `timeout - w` now times out as a measured timeout.
  - Before r2 the same request got an immediate 409 and never rendered.

## Criteria

Mutant runs: each mutant ran in its own git worktree at `c389f530`, with one mutation applied by exact match.
- Mutants M1-M8 and R1-R10 were applied with the review's own `~/.AItelier/director/reports/lockq-r2-review-20260924/mutants.py`.
- Mutants N1-N9 are this round's, from `logs/mutants_r3/mutants3.py.txt`. The runner is `logs/mutants_r3/mut3.sh.txt`.
- Every run covered the same 10 files, with no probes:
  `test_harness_render_queue.py`, `test_godot_lifecycle.py`, `test_harness_render_lock.py`, `test_godot_harness.py`,
  `test_godot_playtest_spec.py`, `test_gate_skips_are_recorded.py`, `test_godot_playtest_scenario.py`,
  `test_blind_gate.py`, `test_playtest_blind_run.py`, `test_godot_compile.py`.
- Each mutant log starts with the mutation's `git diff`.
- An ignition is one execution of the mutated line, counted by a write placed on that line into `logs/mutants_r3/ignitions_<id>_c389f530.txt`.
- The unmutated control ran the same 10 files: 145 passed, 8 skipped, bare_rc=0, ignitions=0
  (`logs/mutants_r3/mutant_CONTROL_c389f530.txt`).
- Result: all 27 mutants (M1-M8, R1-R10, N1-N9) went red, each with ignitions > 0. The logs are
  `logs/mutants_r3/mutant_<id>_c389f530.txt`.

Green for every test named below:
- One run: 23 passed, bare_rc=0 (`logs/pytest_harness_render_queue_candidate_c389f530.txt`).
- 20 runs: runs=20 nonzero=0, bare_rc=0 (`logs/pytest_harness_render_queue_x20_candidate_c389f530.txt`).

### a-busy-lock-queues-the-request (kept)

- Tests: `test_a_busy_lock_queues_the_request[/playtest|/script|/x11_input_smoke]` (3 routes x 1 holder route,
  `/script`), and `test_a_live_render_keeps_its_heartbeat_fresh`.
- Red, R1 (the literal revert: take the owner once, no waiting): bare_rc=1, ignitions=9126, 11 failed. The 11 include
  all 3 route cases (`logs/mutants_r3/mutant_R1_c389f530.txt`).
- Red, M1 (every conflict re-raised): bare_rc=1, ignitions=8220, 9 failed. The 9 include all 3 route cases
  (`logs/mutants_r3/mutant_M1_c389f530.txt`).
- Red, R6 (the waited-for owner ids not recorded): bare_rc=1, ignitions=902, 5 failed. The 5 include all 3 route cases.
- Red, R8 (`/x11_input_smoke` drops the owner-wait fields): bare_rc=1, ignitions=1, 1 failed:
  `test_a_busy_lock_queues_the_request[/x11_input_smoke]`.
- Red, M7 (no heartbeat thread): bare_rc=1, ignitions=18, 4 failed. The 4 include
  `test_a_live_render_keeps_its_heartbeat_fresh`.

### a-lost-owner-still-blocks-and-says-so (kept)

- Tests:
  - `test_a_lost_owner_still_blocks_and_says_so[/playtest|/script|/x11_input_smoke]` (3 routes x the `owner_lost` holder).
  - `test_a_stale_active_owner_blocks_and_a_fresh_one_is_queued`.
  - `test_the_playtest_tool_does_not_read_a_refusal_as_an_unreachable_builder`.
- Red:
  - M2 (`owner_lost` dropped from the blocking SELECT): bare_rc=1, ignitions=953, 5 failed. The 5 are the 3 route
    cases, the tool test, and `test_godot_lifecycle.py::test_reconciliation_refuses_while_real_render_lock_is_held`.
  - M3 (a bare 409 body): bare_rc=1, ignitions=38, 11 failed.
  - R2 (`owner_lost` read as live): bare_rc=1, ignitions=2589, 4 failed (the 3 route cases and the tool test).
  - R3 (nothing needs reconciliation): bare_rc=1, ignitions=3059, 6 failed.
  - M4 (the stale branch disabled): bare_rc=1, ignitions=1370, 2 failed: the stale test and the process-death test.
  - R9 (the threshold enforced x10): bare_rc=1, ignitions=1381, 2 failed: the same 2 tests.
- Each log is `logs/mutants_r3/mutant_<id>_c389f530.txt`.

### a-queue-wait-is-never-charged-as-a-product-failure (new)

- `test_a_queue_wait_that_outlasts_the_callers_timeout_is_not_a_product_failure[live_holder|deployment_fence]`.
  It is built from the review's P4 and readers probes.
  - It uses the real harness served in-process and the real tools, with `post_playtest`'s timeout set to 2 s.
    That gives a queue share of 1 s.
  - One case has a live holder that renders until the test ends. The other has a deployment that holds the fence
    throughout. Either way, the wait outlasts the caller's timeout.
  - It runs each reader in the review's `logs/probe_readers_cand.txt` and asserts:
    - A `godot_playtest`: returns `passed: true`. Its report has `skipped_because: render_owner_wait_timed_out`
      and no `gate_timeout`.
    - B `gate_evidence`: `report_state` gives `skipped` and `release_disposition` gives `unresolved`. This is
      checked on the A, C and C2 reports.
    - C `godot_compile` as game_harness wires it (evidence cycle from `5_test`): `release_evidence: unresolved`.
      The route is read from `configs/addons/game_harness.yaml`'s `5_compile` transitions and is `5_release_wait`.
    - C2 `godot_compile` without an evidence cycle: returns `passed: true`, and its report reads as A's does.
    - D `audit_evidence` and `verify_evidence`: state `skipped`, `passed: false`, and the one skipped gate names
      `render_owner_wait_timed_out`.
    - E `godot_playtest_scenario`: its error names `render_owner_wait_timed_out`.
    - F `focused_check kind=godot_scenario`: `timed_out: false`, and its output names `render_owner_wait_timed_out`.
    - G `godot_vision` on C's report: `release_disposition` gives `unresolved`.
    - The gate-skip log: every entry is `render_owner_wait_timed_out`.
    - No queued request rendered.
  - Reader H (`post_playtest` directly) is the call under A, C, C2, E and F.
- Control: `test_a_timeout_after_the_render_started_stays_a_measured_timeout`.
  - Every render outlasts the tool timeout, so the render has started when the socket expires.
  - It asserts: one render ran, the report has `gate_timeout: true` and `passed: false`, the evidence reads
    `failed` / `known_failure`, and the route is `5_vision`.
- Red:
  - N7 (the polarity the criterion names: a queue that ended is reported as a timeout, with `gate_timeout: true` and
    `passed: false`): bare_rc=1, ignitions=10, 2 failed: both cases of the reader test.
  - N6 (the tool declares no queue wait, so the socket expires while queued): bare_rc=1, ignitions=30, 2 failed:
    both cases.
  - R5 (the tool does not recognise a wait-timed-out refusal): bare_rc=1, ignitions=11, 2 failed: both cases.
  - R10 (the refusal reported as `passed: false`): bare_rc=1, ignitions=11, 3 failed: both cases and the tool test.
  - M8 (the refusal branch disabled): bare_rc=1, ignitions=12, 3 failed: the same 3.
  - N1 (the waiter blocks on the fence): 3 failed, one of them the `deployment_fence` case (see the last criterion).
  - N9 (the forbidden direction: an in-render timeout made unmeasured): bare_rc=1, ignitions=1, 1 failed: the control
    `test_a_timeout_after_the_render_started_stays_a_measured_timeout`.
- The control stays green under every other mutant.
- Each log is `logs/mutants_r3/mutant_<id>_c389f530.txt`.

### one-failed-heartbeat-does-not-make-a-live-render-stale (new)

- `test_a_locked_owner_table_does_not_make_a_live_render_stale` reproduces P2d. It comes from the review's
  `probe_wait_heartbeat_test.py`.
  - Setup: heartbeat interval 0.2 s, staleness threshold 8.0 s. A holder renders.
  - A second connection holds `BEGIN EXCLUSIVE` on the owner table for 6.5 s. That is longer than the table's own
    busy timeout (5 s), so heartbeats fail.
  - From the unlock until threshold + 1 s later, it sends probe requests with a 0.5 s limit. Every one must get
    `owner_kind: active` (queued behind a live render), never a reconcile 409.
  - The heartbeat must be newer than the unlock, and the holder must still be rendering while the probes run.
  - The holder ends 200, and its row ends `released`.
  - A request sent during the lock with an 8 s limit must end as the 409 `render owner wait timed out`, not as an
    error. This pins the waiter's locked-table retry.
- Control: `test_a_holder_whose_process_died_goes_stale_and_blocks`.
  - A child process takes ownership through the harness module and heartbeats every 0.1 s. The threshold is 2.0 s.
  - While the child lives, a request past the threshold is queued (`owner_kind: active`).
  - After `kill` (a real process death, SIGKILL), a request past the threshold gets a 409 with
    `owner_kind: active_stale` and the child's `owner_id`, and `needs_reconciliation: true`.
  - Nothing rendered, and the dead holder's row stays `active` until someone reconciles it.
- Red:
  - N5 (the r2 thread: stop after one sqlite failure): bare_rc=1, ignitions=1, 1 failed: the P2d test.
  - N8 (the waiter re-raises a locked table): bare_rc=1, ignitions=1, 1 failed: the P2d test.
  - M7 (no heartbeat): 4 failed, the P2d test among them.
- Red on the control:
  - M4 (bare_rc=1, ignitions=1370) and R9 (bare_rc=1, ignitions=1381) each fail the process-death test.
  - R3 (bare_rc=1, ignitions=3059) and M3 (bare_rc=1, ignitions=38) fail it too.
- Each log is `logs/mutants_r3/mutant_<id>_c389f530.txt`.

### the-wait-is-bounded-by-the-callers-own-timeout

- Kept, the original construction:
  - `test_a_caller_that_disconnects_while_queued_never_gets_the_render`: the holder holds, the queued client
    disconnects, the holder releases. There is no owner row for `op-ghost`, and only `op-holder` rendered.
  - `test_the_requests_own_wait_limit_ends_the_wait_without_a_render`.
  - `test_an_open_client_socket_is_not_read_as_a_disconnect`.
- New, P3c: `test_a_caller_that_disconnects_while_the_fence_is_held_never_gets_the_render`.
  - A request queues behind a holder. A deployment takes the fence. The holder releases, and the caller disconnects
    while the fence is still held.
  - It asserts: the wait ends while the fence is still held, only `op-holder` rendered after the fence lifts, and any
    row for the request is `released` with a "not rendered" reason.
- New, P3d: `test_the_requests_own_wait_limit_holds_while_the_fence_is_held`.
  - The same setup, with a 0.5 s limit.
  - It asserts: the 409 `render owner wait timed out` arrives while the fence is still held, it carries
    `deployment_fence_held: true`, and nothing rendered.
- New: `test_a_caller_gone_after_ownership_releases_the_row_unrendered` and
  `test_a_limit_reached_after_ownership_releases_the_row_unrendered`.
  - Setup: the request owns its row and then blocks on the render-effect lock. While it is there, the caller
    disconnects, or its 0.3 s limit passes.
  - They assert nothing rendered and the row is `released` with the exact "not rendered" reason.
  - The limit case also asserts the 409 whose `detail` says ownership came after the limit.
- Red:
  - N1 (the waiter blocks on the fence): bare_rc=1, ignitions=948, 3 failed: P3c, P3d, and the reader test's
    `deployment_fence` case.
  - N2 (no disconnect re-check after ownership): bare_rc=1, ignitions=18, 1 failed: the caller-gone-after-ownership test.
  - N3 (no limit re-check after ownership): bare_rc=1, ignitions=17, 1 failed: the limit-after-ownership test.
  - N4 (N1, N2 and N3 together, the r2 admission path): bare_rc=1, ignitions=993, 5 failed. They are P3c, P3d, both
    after-ownership tests, and the `deployment_fence` reader case.
  - M5 (`should_abort` not passed): bare_rc=1, ignitions=59, 2 failed: the original disconnect test and P3c.
  - M6 (the waiter's limit ignored): bare_rc=1, ignitions=3873, 7 failed. The 7 include P3d and both reader cases.
  - R7 (the disconnect probe never sees EOF): bare_rc=1, ignitions=391, 4 failed. The 4 include the original
    disconnect test, P3c and the caller-gone-after-ownership test.
- Each log is `logs/mutants_r3/mutant_<id>_c389f530.txt`.

### the-suite-stays-green-and-the-note-carries-every-number

- Whole suite, base `bf94f724`: 5383 passed, 10 skipped, 11 deselected, bare_rc=0 (`logs/pytest_suite_base_bf94f724.txt`).
- Whole suite, candidate `c389f530`: 5394 passed, 10 skipped, 11 deselected, bare_rc=0 (`logs/pytest_suite_candidate_c389f530.txt`).
  - The +11 are the 11 new tests in `tests/unit/test_harness_render_queue.py` (12 tests on base, 23 on the candidate).
- The suite on the note commit is in the next commit, as `logs/pytest_suite_note_commit.txt`.
- The known flake `test_parallel_runs_progress_but_competing_controllers_admit_one_owner` did not go red in either
  run (both bare_rc=0), so I did not run the x20 rerun.
- Harness family, 16 files:
  - The files: the 15 files listed in the r2 note, plus `test_harness_render_queue.py`.
  - Base `bf94f724`: 443 passed, 8 skipped, bare_rc=0 (`logs/pytest_harness_family_base_bf94f724.txt`).
  - Candidate `c389f530`: 454 passed, 8 skipped, bare_rc=0 (`logs/pytest_harness_family_candidate_c389f530.txt`).
- The three survivors the review named, now red:
  - R4 (the heartbeat outlives the render): bare_rc=1, ignitions=18, 2 failed:
    `test_the_heartbeat_stops_when_the_render_ends[returns]` and `[raises]` (`logs/mutants_r3/mutant_R4_c389f530.txt`).
  - R5: bare_rc=1, ignitions=11, 2 failed: both cases of the reader test (`logs/mutants_r3/mutant_R5_c389f530.txt`).
  - R10: bare_rc=1, ignitions=11, 3 failed. The tool test now also compares `passed` and `gate_skipped` with the
    unreachable-builder report (`logs/mutants_r3/mutant_R10_c389f530.txt`).
- Read-back word-diffs against `bf94f724`:
  - `logs/readback_godot_harness_bf94f724_c389f530.txt`
  - `logs/readback_godot_playtest_impl_bf94f724_c389f530.txt`
  - `logs/readback_test_harness_render_queue_bf94f724_c389f530.txt`
  - This note's own read-back is in the next commit, as `logs/readback_delivery_note_r3.txt`.
- The r2 logs stay in `logs/` and `logs/mutants/` under their r2 names. This round's logs carry `bf94f724` or
  `c389f530` in their names, or sit in `logs/mutants_r3/`.

## Deployment steps

The deployment is the director's step, with the owner's consent. This change touches two processes: the AItelier
server (`impl.py`) and the `aitelier-godot` sidecar (`godot_harness.py`).

1. Deploy the AItelier side first, or together with the sidecar. The reason is what each order leaves running:
   - Old AItelier with the new sidecar: the old `post_playtest` declares no queue wait, so a long queue wait comes back
     as `gate_timeout` again (the regression above).
   - New AItelier with the old sidecar: the old sidecar ignores `render_wait_timeout_sec` and refuses a busy lock at
     once. `post_playtest` already reads that 409 as `gate_skipped`.
2. Drain the sidecar. Wait until no render is in flight: no `active` row, and the render lock is free.
3. Read the owner table. Every row with status `active` or `owner_lost` left from the old process needs attention:
   ```
   SELECT owner_id, generation, status, actor, started_at, heartbeat_at FROM render_owners
   WHERE status IN ('active','owner_lost');
   ```
   - The review's read-only snapshot at 10:41:33Z today showed none.
   - Take this read again at deployment time; that snapshot is hours old.
4. Restart `aitelier-godot` with the new harness.
5. Reconcile every `active` row that the old process left. Do this right after the restart.
   - Why: the old process never heartbeats, so such a row reads as `active_stale` once its `heartbeat_at` is 120 s
     old. From then on it blocks every render until reconciled.
   - For each row: `POST /lifecycle/owner-lost` with `{owner_id, generation, reason}`, then
     `POST /lifecycle/reconcile` with `{owner_id, generation, actor, reason}`.
   - Reconcile compares the row's `actor` pid (`godot-harness:<pid>`) with live processes in the new container. If a
     new process reuses the old pid, reconcile answers 409 `render owner process is still alive`, and this route
     cannot reconcile the row while that process lives. That case needs the director's decision.

## Not done here

- The game repository's `tools/godot_gate.py` is not in this repository.
  - The copy I read in r2 (`~/p55r4/final5-87f242097fe9fed907435cea17387e1dfda372f3/source/tools/godot_gate.py`) sends
    no `render_wait_timeout_sec`. So a queued gate request waits until the gate's own socket timeout, then
    disconnects, and the harness drops it unrendered.
  - How that gate reports its own socket timeout is the game repository's reading. I did not check the game
    repository's current main for it.
- The review's probes were not rerun against this candidate. Their scenarios are suite tests here (P2d, P3c, P3d,
  P4, and the readers), and each one cites its probe.
