# harness.a-busy-render-lock-queues-instead-of-refusing (rev 1): delivery note

- Worker attempt: `attempt-8abf4d6f8d1e41a7b50077aaab3c879a`. This is a worker, not a pipeline round.
- Base: AItelier main `8b084c208dfaf688d862f806ba0a82d96670501d`.
- Branch: `director/lockq-r2-20260924`.
- Code commit: `98e89e21d6358d411345fc2017e6c0473e7e71e7`. The test runs below were taken on this commit.
- The commit after it adds only this note and `logs/`.
- Note written 2026-09-24T09:42:05Z (`date -u`).

Every test run below ran in a throwaway `aitelier:latest` container (`docker run --rm --init -m 3g`).
The repository was mounted at both paths, and every log header shows `gitstatus_rc=0`.
The live `aitelier-godot` container was not contacted. Every harness test serves `godot_harness.py` in-process
over a temp sqlite owner table.

## What changed

`docker/godot/godot_harness.py`

- A request that finds a live render owner now waits for it and then takes ownership. A live owner is `active`,
  with `heartbeat_at` younger than `RENDER_OWNER_HEARTBEAT_STALE_SEC` (120.0).
  - This is `acquire_render_owner_waiting`. It polls the durable table every `RENDER_OWNER_WAIT_POLL_SEC`.
  - The response of `/playtest`, `/script` and `/x11_input_smoke` carries two new fields:
    `render_owner_wait_sec`, and `render_owner_waited_for_owner_ids` (the owner_ids it waited behind).
  - `/playtest` also carries both fields in `timing`.
- A render owner now refreshes its `heartbeat_at` every `RENDER_OWNER_HEARTBEAT_INTERVAL_SEC` (20.0) while its
  render runs (`_start_owner_heartbeat`).
  - Before this, nothing ever called `heartbeat_render_owner`.
  - Without the heartbeat, any render longer than the threshold would read as stale.
- Some holders still block, and the request gets a 409 at once. The 409 body is built by `_render_owner_conflict`.
  - Which holders: an `owner_lost` holder, or an `active` holder whose heartbeat is older than the threshold (`active_stale`).
  - What the body carries: `owner_kind`, `owner_id`, `needs_reconciliation: true`, and a `detail`. The `detail`
    says why reconciliation is due, gives the recorded reason or heartbeat age, and names the reconcile route.
  - The crash-safety of the durable table is unchanged. The SELECT still returns `active` and `owner_lost` rows,
    and a row left behind by a dead process blocks until someone reconciles it.
- The wait has no harness timeout. It ends in one of two ways:
  - The caller disconnects (`_Handler._render_client_abandoned`: the socket reads EOF). The handler logs this and
    returns without rendering.
  - The request's own `render_wait_timeout_sec` runs out. The response is a 409 `render owner wait timed out`,
    which names the owner it waited behind.
  - Neither way creates an owner row.
- The comment above the render admission said "Queue behind any render in flight". It is now true, and I rewrote
  it to describe the three outcomes.

`aitelier/tools/godot_playtest/impl.py`

- A 409 whose body is a render-owner refusal is no longer logged as `godot-builder unreachable`.
- `post_playtest` now returns `gate_skipped: true`, `skipped_because: render_owner_<owner_kind>` (or
  `render_owner_wait_timed_out`), and `render_owner_conflict` with the body. The summary names the HTTP code,
  the kind and the owner_id.

`tests/unit/test_harness_render_queue.py`: 12 new tests, 12 passed (`logs/pytest_harness_render_queue_candidate.txt`, bare_rc=0).

## Conflict with the gate-accounting card, and my resolution

- The draft (`074ce24b`) turned a 409 refusal in `post_playtest` into `passed: false` with no `gate_skipped`. That
  charges a gate that did not run to the implementer.
- Main's gate-accounting card rules the opposite way: a gate that did not run is an absence, not a failure.
- I kept the gate-accounting contract. The refusal keeps this tool's existing absence shape (`gate_skipped: true`).
  - `aitelier/gate_evidence.report_state` reads that shape as `skipped`, and `release_disposition` reads it as
    `unresolved`.
  - The refusal is named by `skipped_because` and `render_owner_conflict`, and the gate-skip log gets the
    refusal's own reason. It no longer says `godot-builder unreachable`.
- `godot_playtest_scenario` returns `gate_skipped` reports as `{"error": summary}`. With this change, that error
  text names the holder.
- I did not port the draft's `render_wait_timeout_sec = 0.5 * timeout` in `post_playtest`. The caller's own bound
  already reaches the harness: its socket timeout closes the connection, and a closed connection ends the wait.
- I did not port the draft's harness default wait cap (1800 s) either. The criterion bounds the wait by the
  caller's timeout, not the harness's.

## Criteria

### a-busy-lock-queues-the-request

- Test: `test_a_busy_lock_queues_the_request[/playtest|/script|/x11_input_smoke]`. It covers 3 routes x 1 holder
  route (`/script`).
  - A holder request owns the render and blocks inside it.
  - A second request on the route is sent over HTTP. The test waits until the second request has met the holder,
    then asserts that it has not answered after 0.3 s.
  - The test then releases the holder and asserts:
    - The second request returns 200.
    - `render_owner_waited_for_owner_ids == [holder owner_id]` and `render_owner_wait_sec >= 0.3`.
    - The second request rendered as the one active owner.
    - The table ends as `op-holder` gen 1 released and `op-next` gen 2 released.
- `test_a_live_render_keeps_its_heartbeat_fresh` covers the heartbeat that keeps a long render queueable.
- Green: 3/3 route cases passed, together with the other 9 tests (`logs/pytest_harness_render_queue_candidate.txt`, bare_rc=0).
  The same file passed in 20 of 20 runs (`logs/pytest_harness_render_queue_x20_candidate.txt`, runs=20 nonzero=0, bare_rc=0).
- Red, M1 (the queue changed back to an immediate raise: every conflict re-raised):
  - bare_rc=1, ignitions=11, 6 failed (`logs/mutants/mutant_M1.txt`).
  - Failed tests: `test_a_busy_lock_queues_the_request` [/playtest], [/script] and [/x11_input_smoke];
    `test_a_stale_active_owner_blocks_and_a_fresh_one_is_queued`;
    `test_a_caller_that_disconnects_while_queued_never_gets_the_render`;
    `test_the_requests_own_wait_limit_ends_the_wait_without_a_render`.
  - The message: "/playtest answered while a live holder still rendered: {'code': 409, ...}".
- Red, M7 (no heartbeat thread): bare_rc=1, ignitions=9, 1 failed: `test_a_live_render_keeps_its_heartbeat_fresh`
  (`logs/mutants/mutant_M7.txt`).

### a-lost-owner-still-blocks-and-says-so

- `test_a_lost_owner_still_blocks_and_says_so[/playtest|/script|/x11_input_smoke]` (3 routes x the `owner_lost` holder).
  - It asserts that the response is a 409 within 5 s, that no render ran, and that no new owner row exists.
  - It also asserts that the body carries `owner_kind == "owner_lost"`, the holder's `owner_id` and
    `needs_reconciliation is True`, and that `detail` names owner_lost, reconciliation and the recorded reason.
- `test_a_stale_active_owner_blocks_and_a_fresh_one_is_queued` sets the heartbeat age from the constant
  `RENDER_OWNER_HEARTBEAT_STALE_SEC`.
  - At threshold + 5 s, the request gets a 409 with `owner_kind == "active_stale"`, the threshold in `detail`,
    and no render.
  - At threshold - 5 s, the same holder is queued behind: the response is `render owner wait timed out`, not a stop.
- `test_the_playtest_tool_does_not_read_a_refusal_as_an_unreachable_builder` points `post_playtest` at the served
  harness, then at a closed port.
  - Against the harness, the refusal is logged as `render_owner_owner_lost` and carries `skipped_because` and
    `render_owner_conflict`.
  - Against the closed port, the log says `godot-builder unreachable` and the report has neither field.
- Green: all of the above passed (`logs/pytest_harness_render_queue_candidate.txt`, bare_rc=0).
- Red, M2 (`owner_lost` removed from the blocking SELECT):
  - bare_rc=1, ignitions=103, 5 failed (`logs/mutants/mutant_M2.txt`).
  - Failed tests: `test_a_lost_owner_still_blocks_and_says_so` x3; the playtest-tool test; and the existing
    `test_godot_lifecycle.py::test_reconciliation_refuses_while_real_render_lock_is_held`.
  - The message: `assert 200 == 409`.
- Red, M3 (a refusal sent as a bare `{"error": "render owner exists"}` 409):
  - bare_rc=1, ignitions=6, 6 failed (`logs/mutants/mutant_M3.txt`).
  - Failed tests: `test_a_lost_owner_still_blocks_and_says_so` x3; the stale test; the playtest-tool test; the
    own-wait-limit test.
  - The message: `assert None == 'owner_lost'`.
- Red, M4 (the stale-heartbeat branch disabled): bare_rc=1, ignitions=303, 1 failed: the stale test
  (`assert 'active' == 'active_stale'`, `logs/mutants/mutant_M4.txt`).
- Red, M8 (the refusal branch in `post_playtest` disabled): bare_rc=1, ignitions=1, 1 failed: the playtest-tool test
  (`KeyError: 'skipped_because'`, `logs/mutants/mutant_M8.txt`).

### the-wait-is-bounded-by-the-callers-own-timeout

- `test_a_caller_that_disconnects_while_queued_never_gets_the_render`:
  - A holder renders.
  - A raw-socket request queues and then closes its socket.
  - The test waits for that request's wait to end, then releases the holder.
  - It asserts that the wait ended with `RenderWaitAbandoned`, that no owner row with operation_id `op-ghost`
    exists, and that only `op-holder` rendered.
- `test_the_requests_own_wait_limit_ends_the_wait_without_a_render`: with `render_wait_timeout_sec: 0.3`, the
  response is a 409 `render owner wait timed out` with `waited_sec >= 0.3`. After the holder releases, there is
  no row for `op-capped`.
- `test_an_open_client_socket_is_not_read_as_a_disconnect` checks both polarities of the disconnect probe on one socketpair.
- Green: all passed (`logs/pytest_harness_render_queue_candidate.txt`, bare_rc=0).
- Red, M5 (`should_abort` not passed): bare_rc=1, ignitions=17, 1 failed: the disconnect test ("the queued request
  kept waiting after its caller disconnected", `logs/mutants/mutant_M5.txt`).
- Red, M6 (the request's own limit ignored): bare_rc=1, ignitions=761, 2 failed: the own-wait-limit test and the
  stale test (`logs/mutants/mutant_M6.txt`).

Each mutant ran in its own git worktree at `98e89e21`, with one mutation applied by exact match. The run covered
`tests/unit/test_harness_render_queue.py` and `tests/unit/test_godot_lifecycle.py`. Each mutant log starts with
the mutation's `git diff`. An ignition is one execution of the mutated line, counted by a write placed on that line.

### the-suite-stays-green-and-the-note-carries-every-number

- Whole suite, base `8b084c20`: 5345 passed, 10 skipped, 11 deselected, bare_rc=0 (`logs/pytest_suite_base_8b084c20.txt`).
- Whole suite, candidate `98e89e21`: 5360 passed, 10 skipped, 11 deselected, bare_rc=0 (`logs/pytest_suite_candidate_98e89e21.txt`).
- The +15 is the 12 new tests plus 3 new parametrized cases of
  `tests/integration/test_no_unfalsifiable_guarantees.py::test_a_round_file_carries_no_lie_keeping_phrase`,
  one for each file this commit changed.
  - Collection: 5355 on base, 5370 on the candidate.
  - `diff_rc=1` for the collected-id diff. Its only other difference is the 2 random-uuid ids of
    `test_fake_id_factory` (`logs/collect_diff_base_vs_candidate.txt`).
- Harness family, 15 existing files: base 431 passed, 8 skipped, bare_rc=0 (`logs/pytest_harness_family_base.txt`).
  The same 15 files plus the new file on the candidate: 443 passed, 8 skipped, bare_rc=0 (`logs/pytest_harness_family_candidate.txt`).
- The known flake `test_parallel_runs_progress_but_competing_controllers_admit_one_owner` did not go red in either
  suite run (both bare_rc=0). So I did not run the x20 rerun.
- The draft said `test_a_round_reports_what_it_paid_twice_for.py` goes red when it runs after `test_addon_registry.py`.
  On this base that is not reproduced:
  - The pair in that order gives 46 passed, bare_rc=0 on base (`logs/pytest_order_pair_base.txt`) and on the
    candidate (`logs/pytest_order_pair_candidate.txt`).
  - Both whole-suite runs are green.
- Read-back word-diffs against the base:
  - `logs/readback_godot_harness.txt`
  - `logs/readback_godot_playtest_impl.txt`
  - `logs/readback_test_harness_render_queue.txt`
  - This note's own read-back is in the next commit, as `logs/readback_delivery_note.txt`.

## Not done here

- Deployment needs an `aitelier-godot` restart. That is the director's step, with the owner's consent.
  - Until then, the live sidecar still refuses at once.
  - Holders created by the running (old) process never heartbeat. After the restart, such a row reads as
    `active_stale` once 120 s have passed and blocks until someone reconciles it (`/lifecycle/owner-lost`, then
    `/lifecycle/reconcile`).
- The game repository's `tools/godot_gate.py` still reads any 409 as "godot-builder unreachable". That file
  belongs to the game repository, not to this repository.
  - With this change, the harness answers 409 only for a holder that needs reconciliation, or for a request that
    set its own wait limit.
  - I read one copy of the gate: `~/p55r4/final5-87f242097fe9fed907435cea17387e1dfda372f3/source/tools/godot_gate.py`
    on linxuhaserver. It sends no `render_wait_timeout_sec`, and its socket timeouts come from its own
    arguments (for example 1800 s for `/script`, line 349).
  - So with that gate, a busy lock now makes the request wait, up to the gate's own socket timeout, instead of
    getting an immediate 409.
  - I did not check the game repository's current main for this.
