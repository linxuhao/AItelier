# iss-ab57b4061b314de3 — `test_an_engine_without_the_counter_costs_a_key_not_a_step` made order-independent

base_sha `277898038a9643fc551b39f00cde857b32795623`. One test body edited:
`tests/unit/test_a_round_reports_what_it_paid_twice_for.py`. No production file
changed, no assertion weakened, no skip/xfail, no collection-order change, no
pytest plugin.

## The leak, named

The degrade test asks: when the engine's `skillflow` wheel has no
`read_accounting`, does `_read_accounting()` degrade to `{}` instead of
raising? It detects "has no counter" by reaching `from skillflow import
read_accounting` and letting that fail.

* **Shared state:** the package attribute `skillflow.read_accounting`. Once it
  exists on the `skillflow` package object it is process-wide and does not
  disappear between tests.
* **Who WRITES it:** `core/dpe_pipeline.py:3202` — `from skillflow import
  read_accounting`. The skillflow installed on this host **ships**
  `read_accounting.py` (verified in `logs/env.txt`:
  `/usr/local/lib/python3.12/site-packages/skillflow/read_accounting.py`). The
  first successful execution of line 3202 against the real wheel imports the
  submodule, which binds `skillflow.read_accounting` on the package and puts
  the real module into `sys.modules` — both for the rest of the process. Any
  test collected earlier that reaches the real import path triggers this; the
  contract's new file `tests/unit/test_harness_render_queue.py` is exactly such
  an earlier test on the run that broke. (Reproduced here without that file by
  dropping a one-line importer module ahead of the target — `logs/02`.)
* **Who READS it (the consumer that went wrong):** the test's own setup
  pre-fix `tests/unit/test_a_round_reports_what_it_paid_twice_for.py:127`
  (now the `setitem` at line 139) tried to neutralize the counter by
  `monkeypatch.setitem(sys.modules, "skillflow.read_accounting", None)` and
  then `core/dpe_pipeline.py:3202` resolved the engine's counter from the
  **package attribute**, which `getattr` answers *before* the import machinery
  ever consults the nulled `sys.modules` mirror.
* **In which order it errors:** every order in which the permanent package
  attribute already exists when this test runs — i.e. a test collected earlier
  in `tests/unit` reached the real `read_accounting` import. With such a test
  ahead of it, `_read_accounting()` returned the real (all-zero) summary
  dict, so `pipeline._read_accounting() == {}` failed. Run alone, on a fresh
  interpreter the attribute is still absent at this test's turn, so the import
  fails the way the test assumes and it passes — which is the whole point of
  the reported symptom.

## The fix

To stand in for "the engine has no `read_accounting`" in a way the code can
actually observe, the test must make the **package attribute** missing, not
just the `sys.modules` mirror. Added, both monkeypatch-restored:

```
    import skillflow
    monkeypatch.delattr(skillflow, "read_accounting", raising=False)
    monkeypatch.setitem(sys.modules, "skillflow.read_accounting", None)   # already there
```

With the attribute gone and `sys.modules` nulled, `from skillflow import
read_accounting` raises (`getattr` misses; the submodule import sees `None` in
`sys.modules` and raises `ImportError`) and `core/dpe_pipeline.py:3204` returns
`{}` — now whether or not an earlier test imported the real submodule. The
two new lines are torn down by monkeypatch, so this test also leaves no
attribute/mirror behind for a later test.

## Assertion before / after (the two `assert`s are byte-identical)

Before:
```
    monkeypatch.setitem(sys.modules, "skillflow.read_accounting", None)
    pipeline = _Pipeline()
    assert pipeline._read_accounting() == {}
    pipeline._report_read_accounting("t_impl")
    assert pipeline.traced == []
```
After:
```
    import skillflow
    monkeypatch.delattr(skillflow, "read_accounting", raising=False)
    monkeypatch.setitem(sys.modules, "skillflow.read_accounting", None)
    pipeline = _Pipeline()
    assert pipeline._read_accounting() == {}          # UNCHANGED
    pipeline._report_read_accounting("t_impl")
    assert pipeline.traced == []                       # UNCHANGED
```
Only setup lines were added; the asserted property (a missing counter costs an
empty key, and silence in the trace, never an exception) is the same claim.

## Two poles (mutation, on the real files, bare exit code)

* Revert ⇒ red and names the test: with the two added lines removed (the
  pre-fix body) and an importer module collected first, the run is
  `1 failed, 1 passed`, FAILED `::test_an_engine_without_the_counter_costs_a_key_not_a_step`,
  bare RC **1** — `logs/02_repro_red_order_before_fix_rc1.txt`.
* Restore ⇒ green: same collection order, fixed body, `9 passed`, bare RC **0**
  — `logs/03_repro_green_order_after_fix_rc0.txt`.

## Every number with the file that produced it

| Ordering | Result | bare RC | log |
|---|---|---|---|
| (1) single test, alone | 1 passed | 0 | `logs/01_single_run_rc0.txt` |
| (3) the order that made it red (importer first), **pre-fix** | 1 failed, 1 passed | **1** | `logs/02_repro_red_order_before_fix_rc1.txt` |
| (3) the order that made it red (importer first), **post-fix** | 9 passed | 0 | `logs/03_repro_green_order_after_fix_rc0.txt` |
| target file standalone, post-fix | 8 passed | 0 | `logs/04_target_file_standalone_rc0.txt` |
| (2) whole `tests/` default order | 300 s probe cap, no failure line returned before stop | see log | `logs/05_full_suite_probes_TIMEOUT_300s.txt` |
| (4) `tests/unit` full | 300 s probe cap, timed out, no failure line before kill | see log | `logs/05_full_suite_probes_TIMEOUT_300s.txt` |

Orderings (2) and (4) are full-suite and exceed the implementation-time
focused_check's 300 s cap (these suites spawn servers, git, litellm/DPE
subagents); their authoritative green number is the `run_tests` gate, stated as
such in `logs/05`. This change touches exactly one test body in `tests/unit`;
the order-independence of that test does not depend on a full-suite timing — it
is established by `logs/02` (red with the incoming attribute present) vs
`logs/03` (green with the same attribute present) and by the monkeypatch
restore of both new lines (nothing leaks out).
