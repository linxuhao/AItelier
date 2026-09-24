# Guarantee ledger — every sentence, its test, its mutation

Card `transport.the-guard-must-not-depend-on-the-shape-of-its-dependency`,
round 12 (State DAG revision 10). Date: 2026-09-24.
Rule: a guarantee sentence that no test can falsify is deleted or made true.
Each row names the test that can fire on it and the source mutation that shows
the test has teeth. Every number in a row was produced on this tree by the
command recorded in the log the row names; the logs (command, tree sha, UTC,
bare exit code) are committed beside the delivery note, in `logs/` at the root
of this tree, and `final/delivery-note.md` indexes them. Mutations ran in a
separate worktree (`gs10-mut`), never in the shared checkout; the ignition count
is how many times the mutated line EXECUTED during that run.

| # | Sentence | Falsifying test | Mutation / ignition |
|---|---|---|---|
| 0 | (control) No mutation. | the same 19-file set every mutation below runs (`logs/mut/mutrun_gs10.sh.txt`) | `empty`: bare RC 0, 504 passed, ignition 0 (`logs/mut/mut_empty.txt`) |
| 1 | "An anonymous caller that executes a non-public read on an opened project is refused at `execute`, with valid arguments." | `tests/unit/test_private_read_verdict_at_execution.py` - the actions are derived from `READ_REQUESTS` x `read_visibility`, a trusted caller answers with the same arguments, and `TestTheExecutePointVerdictItselfHasTeeth` replaces every handler with one that returns a secret | `x_exec` (the `refuse_private_read` call in `execute` removed): bare RC 1, ignition 429, 1 failed - `test_execute_refuses_before_any_handler_runs`, naming `events`, `list_director_messages`, `project_visibility`, `wait_for_state_change` (`logs/mut/mut_x_exec.txt`) |
| 2 | "A route that calls the service method directly, never `execute`, is refused by the method's own decoration and, beneath it, by the connection." | same file - `test_the_method_called_directly`, `test_a_route_that_bypasses_execute_is_refused[fresh]` / `[main]` | `x_deco` alone: bare RC 1, ignition 359, 3 failed, none of them these tests - the connection still refuses (`logs/mut/mut_x_deco.txt`); `x_deco_row_off` (decoration and row verdict both removed): bare RC 1, ignition 1121, 21 failed, including all three (`logs/mut/mut_x_deco_row_off.txt`) |
| 3 | "A reader written after this round - registered nowhere, decorated by nobody, named in no list - that holds only what an anonymous request holds is refused by the connection it reads through." | `tests/unit/test_every_private_table_reader_is_judged_at_the_read.py` - 21 in-scope shapes x 9 private tables x 1 anonymous identity x 1 opened project = 189 cells, in process and over HTTP on a fresh app and on `api.main.app`; each run: 140 REFUSED, 43 ABSENT (the attribute the shape needs does not exist on this tree), 6 CLEAN, 0 LEAKED (`logs/criteria_candidate.txt`) | `row_off` (connections never armed): bare RC 1, ignition 748, 14 failed; the matrix failure names S01 on all 9 private tables (`logs/mut/mut_row_off.txt`) |
| 4 | "`judged` means an authorization dependency executed; a public read cleared by its declaration reads `cleared`, never `judged`." | `tests/integration/test_coverage_measures_judged_not_reached.py` | `c2_arrival` (arrival counted as a judgment): bare RC 1, ignition 854, 4 failed, including `test_restoring_arrival_counting_re_lies_and_is_caught` (`logs/mut/mut_c2_arrival.txt`) |
| 5 | "The ban on lie-keeping phrases covers every file this card touched, scope taken from git against the card's first-round base `9c79f11f`, with no line-prefix exclusion." | `tests/integration/test_no_unfalsifiable_guarantees.py` | `ban_17` (a banned phrase appended to `api/state_graph_routers.py`, a file `git diff --name-only 98bffeac 3173d9c2` does not list, so the old base left it out at `3173d9c2`): bare RC 1, 1 failed - `test_a_round_file_carries_no_lie_keeping_phrase[api/state_graph_routers.py]`; a comment executes nothing, so ignition is 0 by construction (`logs/mut/mut_ban_17.txt`) |
| 6 | "The guard runs a verdict dependency on FastAPI's own machinery for six shapes." | `tests/integration/test_verdict_runs_on_fastapi_machinery.py` | `c6_handcall` (the guard hand-calls `dependency(request)`): bare RC 1, ignition 120, 4 failed (`logs/mut/mut_c6_handcall.txt`) |
| 7 | "The private-delivery check runs before any early `ok=True`." | `tests/integration/test_private_delivery_before_early_ok.py` | `c5_order` (the dispatch approval returns before the private check): bare RC 1, ignition 332, 6 failed (`logs/mut/mut_c5_order.txt`) |
| 8 | "A declaration whose handler source cannot be read, or whose delivery the reader cannot resolve, is REFUSED." | `tests/integration/test_state_verdict_is_effective.py::TestTheRegressionsTheEarlierRoundsBought::test_an_unreadable_declaration_refuses_it_does_not_approve`; `tests/integration/test_unreadable_delivery_is_refused.py` - five hiding shapes at the binding and at the guard | `m1_unreadable`: bare RC 1, ignition 2, 2 failed (`logs/mut/mut_m1_unreadable.txt`); `c4_no_failclosed` (the fail-closed `opaque` branch removed): bare RC 1, ignition 11, 2 failed - `[public_beside_unresolvable]` at the binding and at the guard (`logs/mut/mut_c4_no_failclosed.txt`); `c4_both`: bare RC 1, ignition 350, 4 failed (`logs/mut/mut_c4_both.txt`) |
| 9 | "An opened project's six note reads are public; no in-scope reader gets an unopened project's notebook rows." | `tests/integration/test_owner_ruled_notes.py` | `notes_allow` (notebook tables readable table-wide): bare RC 1, ignition 2037, 6 failed, including item 3 in process, `[fresh]` and `[main]` (`logs/mut/mut_notes_allow.txt`); `notes_deny` (notebook tables never readable): bare RC 1, ignition 124, 3 failed, including item 1 `[fresh]` and `[main]` (`logs/mut/mut_notes_deny.txt`) |
| 10 | "An MCP call with no request object reads as anonymous, and an untrusted store lists no unopened project." | `tests/unit/test_undeclared_reader_is_untrusted.py` | `mcp_else_true`: bare RC 1, ignition 2, 3 failed (`logs/mut/mut_mcp_else_true.txt`); `mcp_reqfrom_trusted`: bare RC 1, ignition 1, 1 failed (`logs/mut/mut_mcp_reqfrom_trusted.txt`); `lp_default`: bare RC 1, ignition 1, 1 failed (`logs/mut/mut_lp_default.txt`) |

## Written NOT guaranteed (明写不保证)

The director's scope ruling of 2026-09-23, quoted verbatim from the card's goal:

> **不在范围内**：调用 sqlite3 连接对象自己的管理方法关掉或绕开裁决 —— `set_authorizer(None)`、`backup(...)`、`blobopen(...)`（复查的 S07、S08、S15）；按路径重开数据库文件；伪造信任声明。
> 理由：同一个 Python 进程里，持有连接对象的代码总能调用它自己的管理方法；关掉这一类要进程或文件边界，本卡不买。

On this tree these readers are run and printed, and are not part of any pass
condition: `tests/unit/test_every_private_table_reader_is_judged_at_the_read.py::TestAnAfterTheFactReaderIsJudged::test_out_of_scope_shapes_are_measured_and_printed`
measures S07 `set_authorizer(None)`, S08 `backup`, S15 `blobopen`, S09/S10
reopening the file by its path and S20 a forged trust declaration, each x 9
private tables x 1 anonymous identity x 1 opened project = 54 cells:
S07, S08, S09 and S15 LEAKED on all 9 tables (36 cells), S10 and S20 REFUSED on
all 9 (18 cells) (`logs/criteria_candidate.txt`). The 36 LEAKED cells deliver
private rows. Closing them needs a process or file boundary.

PUBLIC state tables are judged by table, not by project: an armed connection
reads their rows for unopened projects too.

## Deleted this round

* The ledger's previous row 7 said a declaration that "delivers a private
  action" is refused by the route reader. The half about a delivery the reader
  cannot resolve had no test that could fire: the review's `c4_no_failclosed`
  mutation lived. It is now row 8, with a fifth hiding shape
  (`public_beside_unresolvable`) that only the fail-closed branch refuses.
* Sentence (b), `api/state_verdict.py:27-28` at `3173d9c2` ("the stricter action
  wins"), and sentence (c), `api/state_http.py:198` at `3173d9c2` (the guard
  docstring's claim that no route shape can decline the judgment), are deleted.
  The grep is in `logs/grep_sentences_b_c.txt`.
