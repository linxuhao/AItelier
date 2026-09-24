# Delivery note: guard-shape round 12 (State DAG revision 10)

Node `transport.the-guard-must-not-depend-on-the-shape-of-its-dependency`, project
`aitelier`, external attempt `attempt-8a8c32d3483846a89dda3b5dca37bb11`.
Branch `director/guardshape-r10-20260924`. Written 2026-09-24.

**Trees.** `5ece896f` is the merge commit: main `b72339cc` is the first parent,
the r11 candidate `3173d9c2` the second. `6346a95d` is the code commit, and every
test and mutation below ran on it. The candidate commit is its child. That child
changes only `final/`, `logs/` and `docs/state-guard-guarantee-ledger.md`, so it
carries the same code.

**Logs.** Every log is under `logs/`. Each `.txt` log starts with the command,
cwd, `TREE_HEAD`, `TREE_DIRTY`, the container line and `START_UTC`, and ends with
`END_UTC` and `BARE_RC`. The exit code is captured straight from `docker run`,
never through a pipe. The runner is `logs/run.sh.txt`, and every run used a
throwaway `docker run --rm --init -m 3g`.

The runner mounts `~/AItelier/.git` read-only at its host path so that git works
inside the container. Without that mount, one test on main fails
(`logs/suite_main_b72339cc.txt`).

Mutations ran in a separate worktree, `gs10-mut`, detached at `6346a95d`, using
`logs/mut/mutrun_gs10.sh.txt` and `logs/mut/mutate_gs10.py.txt`. Each mutation
left these files:

- its diff, in `logs/mut/mut_<name>.diff`;
- its ignition, in `logs/mut/mut_<name>.ignition.txt` (how many times the mutated
  line executed);
- its run, in `logs/mut/mut_<name>.txt`.

The tree was restored after each mutation, and every run log ends with
`RESTORED porcelain_lines=0`.

## 1. The merge and its conflicts

A merge of main `b72339cc` into `3173d9c2` conflicted in two places.

1. **`api/state_graph_tools.py`, the MCP service's trust.**
   - Main (`0b63311a`) had `project_read_trusted=_mcp_may_read_private(request)
     if request is not None else True`.
   - The card had `mcp_read_trust(request)`.
   - Merged line, `api/state_graph_tools.py:47` (`logs/mcp_merged_line.txt`):
     `project_read_trusted=mcp_read_trust(request))`.
   - Its function, at `:12`:
     - returns `False` for `request is None`;
     - otherwise returns main's `api.mcp_router._mcp_may_read_private(request)`.
       That function keeps main's external-token branch: on the tunnel with
       `_EXTERNAL_TOKEN` set, only that token decides, and elsewhere
       `api.authz.may_read_private` decides.
   - `api/mcp_router.py:_request_from` returns `None` when the request cannot be
     acquired, so "no request" reaches that `False`.
   - So `else True` is gone, and the external-token check remains.
2. **`core/state_commands.py`, the read classification.**
   - Main put the six note reads in `PUBLIC_READS`, following the owner's ruling
     `note://aitelier/546f3b521eca`.
   - The card had put `get_driver_guide_section` in `PUBLIC_READS` and the notes
     in `WRITER_ONLY_READS`.
   - The merge keeps the guide public and makes the six note reads public. Each
     note read is still ANDed with project openness at `execute` and, at the
     row level, by the notebook projection (§2).
   - `WRITER_ONLY_READS` is `list_director_messages`, `events`,
     `wait_for_state_change` and `project_visibility`.

Once the merge landed, several things had to be fixed. Each fix is in `6346a95d`:

- **Tests whose private canary was the driver note.** The note became public on
  an opened project, so these tests now use the director mailbox, which the
  ruling left private:
  - `tests/support/state_author_surface.py` gains `PRIVATE_ACTION`, which asserts
    `read_visibility("list_director_messages") == "private"`, and
    `seed_private_mail`;
  - converted files: `test_author_surface_generator.py`,
    `test_coverage_measures_judged_not_reached.py`,
    `test_private_delivery_before_early_ok.py`,
    `test_state_verdict_is_effective.py`,
    `test_verdict_runs_on_fastapi_machinery.py`,
    `test_state_declaration_binding.py`, `test_project_privacy_rev3.py`,
    `test_state_read_visibility.py`, `test_state_portfolio.py`,
    `test_unreadable_delivery_is_refused.py`.
- **Tests that relied on an ambient gate.** The r11 review ran the suite through
  `docker exec aitelier`, whose environment has the gate on. In a throwaway
  container the gate is off, and `may_read_private` is True whenever the gate is
  off. `logs/base_3173d9c2_verdict_files_gate_unarmed.txt` shows base `3173d9c2`
  with 4 failures of `TestTheRegressionsTheEarlierRoundsBought` for exactly this
  reason, bare RC 1. `test_state_verdict_is_effective.py` now arms the gate
  itself. So do all the new test files, through `tests/support/state_canaries.py:arm`.
- **Words the ban scan rejects, in files main changed.** The merge put these
  files inside the `9c79f11f` scan. The words were changed and nothing else:
  - `core/ai_router.py:661`, `core/dpe_pipeline.py:834`,
    `docs/state-graph.md:489`: "deliberately" instead of the banned adverb;
  - `core/dpe_pipeline.py:2240`: "specified";
  - `docs/install-route.md:60`: "stated";
  - `docs/state-graph.md:6` and `:237`: "described";
  - `tests/unit/test_run_isolation_lifecycle.py:373`: "stated".
- **The WeakSet / stand-down AST scan in
  `test_state_declaration_binding.py`.** It is limited to `api/*.py` plus
  `core/state_*` and `core/director_*`, because main's `core/scheduler.py` uses
  `WeakSet` for its own purposes.
- **`tests/integration/test_no_unfalsifiable_guarantees.py`.** It now names a
  banned phrase by its index, so the log of a red run does not carry the phrase
  into this tree.

## 2. Where the verdict sits, and why every private read passes it

Three verdicts judge an anonymous read.

1. **The action.**
   - `core.state_privacy.refuse_private_read`, reached from
     `core.state_commands.execute` and from the `@writer_only_read` decoration on
     each service method, refuses every read that `read_visibility` does not call
     public.
   - The action table decides that, and it fails closed.
2. **The project.**
   - `execute` refuses any project an anonymous caller may not read. An unopened
     project and a missing one get the same refusal.
   - The trust behind this decision is taken once per request from the raw
     credential: `api/state_graph_routers.py:29` calls
     `may_read_private(request)`.
3. **The rows.** This round places it, and it covers the gap the review found.
   - An untrusted `StateService` / `StateGraphStore` holds only a
     `core.state_privacy.UntrustedDatabase`. That object has a path, a
     `get_connection`, and nothing else: it has no `decision_connection`, no
     `unrestricted` and no `_decision`, and there is no raw handle on `svc.db`.
   - Each connection it opens copies, before anyone sees it:
     - the public part of the private tables: opened projects' `visibility`, and
       their event watermark (`MAX(seq)`, the same number the public
       `project_overview` returns as `event_seq`);
     - their `run_isolation` rows;
     - when a notebook read names one project, that project's notebook rows, and
       only if that project is opened.
   - Then it installs `ReadVerdict`, a default-deny authorizer. It allows public
     `state_*` tables, the temp copies (unqualified or `temp.`), the column subsets
     of the projections, the schema tables, `json_each`/`json_tree`, and two
     pragmas. Everything else is denied: every other table, `main.` on a private
     table, writes, DDL, ATTACH, and `sqlite_dbpage`.
   - A denied read leaves as the same `ProjectPrivate` the other two verdicts
     raise.
   - An untrusted store cannot open a write transaction. Its schema is created on
     the handle it was given, before it drops that handle
     (`core/state_graph.py:initialize_state_schema`).

**Why every private read passes it.** An anonymous request's object graph holds
no other handle: every reader that anonymous code can reach sees only the armed
connection. The measurement is in §3, criterion
`every-reader-of-a-private-table-is-judged-at-the-read`.

**What does not pass it:**

- **Trusted construction points (§4).** They hold a real handle because their
  credential allows it.
- **The out-of-scope readers the director ruled 明写不保证.** These are:
  - the connection's own `set_authorizer(None)` (S07), `backup` (S08) and
    `blobopen` (S15);
  - `sqlite3.connect(path)` (S09).

  They leak on all 9 tables (36 cells, `logs/criteria_candidate.txt`). Closing
  them needs a process or file boundary. Two other shapes are refused on all 9
  tables: S10 (reopening by path through `type(svc.db)`) and S20 (a forged trust
  declaration). The ledger quotes the ruling word for word.

## 3. Criteria

### `an-undeclared-reader-is-untrusted`
- **Construction points derived from code.**
  - Command and output: `logs/trusted_construction_points.txt`, which runs
    `git grep -n "project_read_trusted=" -- core api aitelier scripts examples cli mutation_gate.py`
    (bare RC 0).
  - The AST derivation is
    `test_the_trusted_construction_points_are_derived_from_code`. It prints each
    `SITE`, each `TRUST_FUNCTION` with its `return`s and `except` branches, and
    `LITERAL_TRUE_SITES` (`logs/criteria_candidate.txt`, bare RC 0).
  - The list with credentials is in §4.
- **The two MCP mutations and `lp_default`.**
  - `mcp_else_true`: bare RC 1, ignition 2, 3 failed, including
    `test_no_request_object_is_no_credential` and
    `test_an_mcp_call_without_a_request_reads_as_anonymous`
    (`logs/mut/mut_mcp_else_true.txt`).
  - `mcp_reqfrom_trusted`: bare RC 1, ignition 1, 1 failed,
    `test_an_mcp_call_without_a_request_reads_as_anonymous`
    (`logs/mut/mut_mcp_reqfrom_trusted.txt`).
  - `lp_default`: bare RC 1, ignition 1, 1 failed,
    `test_an_untrusted_store_lists_no_unopened_project_by_default`
    (`logs/mut/mut_lp_default.txt`). The property it changes: an untrusted store
    that is asked with the default arguments would name unopened projects.
- The merged MCP line is quoted in §1.

### `an-unreadable-declaration-refuses-it-does-not-approve`
- Both poles.
  - Honest: `test_state_verdict_is_effective.py`, `test_author_surface_generator.py`
    and `test_state_declaration_binding.py` pass within the 504-test control run,
    bare RC 0 (`logs/mut/mut_empty.txt`).
  - Mutated: `m1_unreadable` gives bare RC 1, ignition 2, and 2 named failures,
    `test_no_generated_shape_leaks_and_only_agreeing_shapes_serve` and
    `test_an_unreadable_declaration_refuses_it_does_not_approve`
    (`logs/mut/mut_m1_unreadable.txt`).
- The route-layer mechanism (`binding_for`) was neither removed nor rewritten.
  Its corpus still comes from the action table.

### `coverage-measures-judged-not-merely-reached`
- The grep for `with_guard|and False` in
  `tests/integration/test_coverage_measures_judged_not_reached.py` has 0 hits,
  bare RC 1 (`logs/grep_coverage_selfevidence.txt`).
- `c2_arrival`: bare RC 1, ignition 854, 4 failed
  (`logs/mut/mut_c2_arrival.txt`).

### `every-guarantee-sentence-has-a-test-that-can-falsify-it`
- `docs/state-guard-guarantee-ledger.md` was rewritten for this round, dated
  2026-09-24. Each row names its log.
  - Old row 7 is now row 8 and is true. A fifth hiding shape,
    `public_beside_unresolvable` (in `tests/support/state_author_surface.py`), is
    a public literal delivered beside a `functools.partial` that the reader
    cannot follow. It kills `c4_no_failclosed`: bare RC 1, ignition 11, 2 failed
    (`logs/mut/mut_c4_no_failclosed.txt`). At `3173d9c2` this mutation lived.
  - New row 3: later readers are judged. `row_off` gives bare RC 1, ignition 748,
    14 failed (`logs/mut/mut_row_off.txt`).
  - New section: the 明写不保证 readers, quoted word for word.
- The header's promise about raw logs is now true: `logs/` is in this tree.
- Sentence (b) (`api/state_verdict.py:27-28` at `3173d9c2`) and sentence (c)
  (`api/state_http.py:198` at `3173d9c2`) are deleted. The grep has 0 hits, bare
  RC 1 (`logs/grep_sentences_b_c.txt`).
- The scan base stays `9c79f11f`. `ban_17` gives bare RC 1 and 1 failed:
  `test_a_round_file_carries_no_lie_keeping_phrase[api/state_graph_routers.py]`
  (`logs/mut/mut_ban_17.txt`).

### `every-reader-of-a-private-table-is-judged-at-the-read`
Test file: `tests/unit/test_every_private_table_reader_is_judged_at_the_read.py`.
Canaries come from `tests/support/state_canaries.py`. All numbers here are from
`logs/criteria_candidate.txt` (bare RC 0) unless another log is named.

1. **Canaries.** Each of the 9 private tables gets its own canary, derived from
   `PRIVATE_STATE_TABLES`. A test asserts that every `state_*` table in the schema
   is classified, and prints `UNCLASSIFIED = []`. Each canary sits in the row its
   reader actually reads:
   - the notebook tables: the UNOPENED project's rows;
   - `state_project_access`: the opened row's private columns;
   - `state_events`: an opened-project event;
   - the director tables: rows aimed at the opened project;
   - `state_director_inbox_sequences`, which has no other text column: the canary
     is the key.

   Positive control: a raw connection reads all 9 canaries back first
   (`CANARY_PRESENT`).
2. **The in-scope matrix.**
   - Shapes: S01–S06, S11–S14 and S16–S19, plus X01–X07. X01–X07 are a CTE
     named like the table, the unopened project's notebook connection,
     `main.` on the opened notebook connection, an unqualified name on the opened
     notebook connection, `temp.`, and two stores rebuilt from `svc.db` or
     through `__self__`.
   - Size: 21 shapes × 9 private tables × 1 anonymous identity × 1 opened project
     = 189 cells.
   - Result: 140 REFUSED, 43 ABSENT, 6 CLEAN, 0 LEAKED. The run was repeated
     in process, over HTTP on a fresh app, and over HTTP on `api.main.app`, with
     the same tally each time.
   - **ABSENT** means the attribute does not exist on this tree, and the call's
     result is recorded:
     - `decision_connection`, `unrestricted` and `_decision` (S03, S04, S06, S12:
       36 cells);
     - `temp.<table>` where the armed connection has no copy (X05: 7 cells,
       "no such table").
   - **CLEAN** means the opened notebook was read, or the unopened notebook
     connection's empty copy (X02 and X04 on the 3 notebook tables).
   - A compile error or `OperationalError` counts as ERROR and fails the test.
3. **The reviewed paths.**
   - Unguarded over HTTP: 11 paths × 1 unguarded route × 1 anonymous identity ×
     1 opened project, on each app (`CROSS_PRODUCT` lines). Every path that must
     refuse is 403 with the one refusal. `wait_disposition` answers 200
     `"rescan"` with no canary; see §5.
   - Behind the product guard's dispatch route: 5 inline handlers per app. The
     `get_project_access` closure is 403 on both apps, and so are the `events`,
     `list_director_messages` and `project_visibility` closures. The control
     handler is 200.
   - Base side by side:
     - base `3173d9c2` (`logs/sidebyside_base_3173d9c2.txt`, bare RC 1): the
       `get_project_access` closure is 200 and leaks `state_project_access`
       through the guard on both apps, the review's finding. Its in-scope matrix
       is 72 LEAKED / 81 REFUSED / 36 ABSENT per run.
     - base `a19a33a6` (`logs/sidebyside_base_a19a33a6.txt`, bare RC 1): 117
       LEAKED / 72 ABSENT per run. Nine of the 11 unguarded reviewed paths
       answer 200 on each app, and 6 of those leak a canary.
4. **Removing the verdict layer.**
   - In-test disarm of `_arm`: `DISARMED IGNITION_COUNT = 9`, and `LEAKED` lists
     all 9 tables.
   - Source mutation `row_off`: bare RC 1, ignition 748, 14 failed. The matrix
     failure names S01 on each of the 9 tables (`logs/mut/mut_row_off.txt`).
5. The layers are explained in §2.

### `no-author-written-datum-decides-whether-to-judge`
Test file: `tests/unit/test_private_read_verdict_at_execution.py`.

- The actions are derived as `READ_REQUESTS` × `read_visibility` (not from
  `_handlers`). This yields `events`, `list_director_messages`,
  `project_visibility` and `wait_for_state_change`.
- Arguments are valid: the same arguments get a trusted caller an answer
  (`TRUSTED ... outcome=answered`).
- Each call is reported as refused, leaked or invalid, and invalid is a failure.
- Candidate (`logs/criteria_candidate.txt`, bare RC 0): 4 non-public reads × 1
  anonymous identity × 1 opened project = 4 calls on each of 4 paths
  (`execute`, direct call, HTTP fresh, HTTP main). All 4 are refused on every
  path.
- The neutered-reader route is 403 on both apps.
- Bases:
  - `3173d9c2`: 10 non-public reads × 1 × 1, of which 4 refused and 6 invalid.
    The six note reads were private there, and this file has no arguments for
    them (`logs/sidebyside_base_3173d9c2.txt`).
  - `a19a33a6`: 11 non-public reads, of which 4 refused and 7 invalid
    (`logs/sidebyside_base_a19a33a6.txt`).
- `x_exec`: bare RC 1, ignition 429, 1 failed,
  `test_execute_refuses_before_any_handler_runs`. Its assertion names `events`,
  `list_director_messages`, `project_visibility` and `wait_for_state_change`
  (`logs/mut/mut_x_exec.txt`).

### `the-delivery-note-carries-every-number`
- This file and `logs/*.txt` are in the tree. Every number above names its log.

### `the-owner-ruled-notes-stay-public-and-unopened-notes-stay-shut`
Test file: `tests/integration/test_owner_ruled_notes.py`
(`logs/criteria_candidate.txt`, bare RC 0).

1. On the opened project, 6 note reads × 1 project state × 2 apps = 12 requests.
   All return 200. Five carry the note text, and `check_driver_note_index`
   returns counts.
2. For the unopened and the missing project, 6 note reads × 2 project states ×
   2 apps = 24 requests. All are 403
   `{"detail":"This State DAG record is not available."}`, and the unopened and
   missing responses are byte-identical.
3. 21 in-scope shapes × 3 notebook tables × 1 anonymous identity = 63 cells,
   repeated in process, over HTTP on a fresh app and over HTTP on
   `api.main.app`. Each run gives 42 REFUSED, 15 ABSENT, 6 CLEAN, 0 LEAKED. This
   includes `SELECT * FROM` each notebook table with no `project_id` filter.
4. `list_director_messages` × `events` on the opened project × 2 apps: all 403
   with no canary.

Mutations:

- `notes_allow`: bare RC 1, ignition 2037, 6 failed, including item 3 in
  process, `[fresh]` and `[main]` (`logs/mut/mut_notes_allow.txt`).
- `notes_deny`: bare RC 1, ignition 124, 3 failed, including item 1 `[fresh]` and
  `[main]` (`logs/mut/mut_notes_deny.txt`).
- Base `3173d9c2`, where the notes were private: items 1–3 are red
  (`logs/sidebyside_base_3173d9c2.txt`).

### `the-private-delivery-check-runs-before-any-early-ok`
- `test_private_delivery_before_early_ok.py` passes in the control run
  (`logs/mut/mut_empty.txt`). It holds the private-dispatch pole, the product
  `/api/state/query/{action}` positive case, and the write/director-route spy.
- `c5_order`: bare RC 1, ignition 332, 6 failed (`logs/mut/mut_c5_order.txt`).
- `binding_for` was not changed.

### `the-verdict-runs-on-fastapis-own-machinery`
- The grep for the two teardown sentences has 0 hits, bare RC 1
  (`logs/grep_machinery_sentences.txt`).
- `c6_handcall`: bare RC 1, ignition 120, 4 failed
  (`logs/mut/mut_c6_handcall.txt`).

## 4. Trusted construction points (derived, `logs/trusted_construction_points.txt`)

Each site, and the credential its trust comes from:

- **`api/state_graph_routers.py:29`**, `may_read_private(request)`. Trusted means
  one of:
  - a Cloudflare Access JWT whose email is in `AITELIER_WRITERS`;
  - the CLI's `X-AItelier-Admin-Token`, off the tunnel;
  - test mode;
  - the gate is not configured.
- **`api/state_graph_tools.py:47`**, `mcp_read_trust(request)`:
  - no request means untrusted;
  - on the tunnel with an external token configured, that token decides;
  - otherwise `may_read_private`.
- **`api/state_only.py:51`**: the State-only app. Every request first passes its
  bearer-token middleware (a token of at least 32 bytes, checked with
  `secrets.compare_digest`).
- **`core/state_meta.py:27`**: the butler's `state_graph_read`/`_write` tools.
  They run in the server's meta-agent for a session whose chat is a mutating
  request, and `write_gate` admits those only for a writer credential.
- **`core/state_changes.py:227`**: `reconcile_workflow_project`, called by the
  scheduler (`core/scheduler.py:2259`) inside the server process. It serves no
  request.
- **`core/state_migration.py:211`**: `dry_run` on a `TemporaryDirectory` shadow
  database.
- **`aitelier/writing_bench/adapter.py:31`**: the Writing Bench host, run by the
  operator.
- **`scripts/check_driver_note_index.py:32,34`** and
  **`examples/state_graph_demo.py:59,128`**: operator scripts on a local database
  path. The credential is access to that file.
- **`mutation_gate.py:40`**: a probe on a `TemporaryDirectory` database.
- **`core/state_service.py:70,81,96`** and **`core/director_messaging.py:144`**:
  these pass the caller's own level down to the leaves. They create no trust.

Test fixtures are legitimate construction points too. Nine fixture sites that
build a store and then write now declare `project_read_trusted=True`:

- `tests/contracts/director_messaging/test_sqlite_proof.py` (2)
- `tests/unit/test_deployment_quiescence.py` (3)
- `tests/unit/test_state_relay.py` (1)
- `tests/integration/test_composed_product_delivery_cycle.py` (2)
- `tests/integration/test_state_external_entrypoints.py` (1)

## 5. Whole suite, and what is not done

**Whole suite** (`python -m pytest -p no:cacheprovider -q -rf tests`, each in its
own `docker run --rm --init -m 3g`):

- Candidate code `6346a95d`: 5075 passed, 10 skipped, 11 deselected, bare RC 0
  (`logs/suite_candidate_6346a95d.txt`).
- Main `b72339cc`, run at the same time as the candidate: 1 failed, 4843 passed,
  10 skipped, bare RC 1 (`logs/suite_main_b72339cc_r2.txt`). The failure was
  `tests/unit/test_bash_cleanup_is_async.py::test_a_timed_out_command_keeps_the_loop_responsive`.
  Run alone 20 times, that test failed 0 of 20 on main and 0 of 20 on the
  candidate, bare RC 0 both times (`logs/bash_cleanup_x20_main_b72339cc.txt`,
  `logs/bash_cleanup_x20_candidate_6346a95d.txt`). The suite failure came while
  two suites and the mutation runs shared the host.
- Main's first run, before the runner mounted `.git`, failed
  `test_novel_scaffold_git_freeze.py::test_no_git_nested_root_and_invalid_root_fail_before_writes`:
  4843 passed, bare RC 1 (`logs/suite_main_b72339cc.txt`).
- The known flake
  `test_isolated_recoverable_execution.py::test_parallel_runs_progress_but_competing_controllers_admit_one_owner`
  did not go red in these runs.

**Not done, or not as the criterion words it:**

- **`wait_disposition` answers 200 `"rescan"` rather than 403.** It is a helper,
  not an action. It compares the caller's cursor against the opened project's
  event watermark, and that watermark is the same `event_seq` the public
  `project_overview` returns. No canary crosses it: the events canary is in a
  column the projection does not copy. On base `3173d9c2` it was 403, because
  that tree denied `state_events` outright.
- **The note's own commit.** The candidate commit adds this note, the logs and
  the ledger. Its whole-suite run is reported in the attempt's final message, not
  here, because the note cannot contain a run of the commit that contains it.
- **One raw log is left out of this tree.** The run of the failing file set at
  the merge commit `5ece896f`, before the fixes in §1, repeats a banned phrase
  from a failure message, so it is not here. No number in this note comes from
  it.
