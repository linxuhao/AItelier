from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent


def _python_sources():
    for path in sorted(_ROOT.glob("**/*.py")):
        # .cache and output hold DOWNLOADED third-party trees (a vendored CPython
        # under .cache/apply-patch-dev) and old capture copies. They are not this
        # repository's source, and walking them made this guard fail or pass
        # depending on which working copy it ran in - it only looked stable
        # because a fresh worktree has neither directory.
        if set(path.parts) & {"evidence", ".git", "__pycache__", "node_modules",
                              ".cache", "output", ".wt", ".venv", "site-packages",
                              ".claude"}:
            continue
        yield path


def _verbatim_previous_line_sites(source):
    """Every non-blank line identical to the previous non-blank line.

    Comment lines count: round 9's splice wrote one comment line twice in THIS
    file, and a reader that skipped comments stayed green over it."""
    sites = []
    previous = None
    for number, raw in enumerate(source.splitlines(), start=1):
        text = raw.rstrip("\n")
        if text.strip() == "":
            continue
        if previous is not None and text == previous:
            sites.append((number, text.strip()))
        previous = text
    return sites


# Deliberate, named repeats in the test tree: two calls or two asserts in a
# row that MEAN two invocations (a re-poll, a re-read, a two-item rotate).
# Each entry is (file, statement, consecutive_count): the statement text and
# how many times it appears as a consecutive duplicate in that file. A
# THIRD occurrence at a listed site changes the count and must be written
# down here before the suite goes green.
TEST_INTENTIONAL_REPEATS = [
    ("tests/integration/test_state_graph_entrypoints.py", 'assert module._start_driver("run1", scheduler_owned=True, auto_approve=False)', 1),
    ("tests/integration/test_tool_operation_owner_recovery.py", 'sf.reconcile_active_operations(run_id, trigger="retryable_trace")', 1),
    ("tests/unit/test_ai_router.py", 'litellm.exceptions.RateLimitError("Limit reached", model="zai", llm_provider="zai"),', 1),
    ("tests/unit/test_claim_precondition_bounded.py", 'await scheduler._run_skillflow_tick("p1", None)', 1),
    ("tests/unit/test_cli_client.py", 'client.approve_checkpoint("project", observed)', 1),
    ("tests/unit/test_debugctl_await_auth_headers.py", '"http://localhost:4444/api/events/stream",', 2),
    # One recent instant written to two columns (last_heartbeat_at, updated_at)
    # of the same attempt, in the live and the heartbeat arms of the external-
    # owner liveness window; both columns must sit inside the window, so the
    # repeat is the point, not a splice.
    ("tests/unit/test_deployment_quiescence.py", "(reference - timedelta(minutes=1)).isoformat(),", 2),
    ("tests/unit/test_godot_harness.py", 'gh._playtest_spec(tmp_path / "proj", _spec(["a"]), 60, 120)', 1),
    ("tests/unit/test_godot_harness.py", "gh.run_script(str(tmp_path), [], timeout=30)", 1),
    ("tests/unit/test_knowledge_sync.py", '_call(repo, ws, "proj1")', 1),
    ("tests/unit/test_native_empty_response.py", 'assert g.generate_native(MSGS).text == "visible answer"', 1),
    ("tests/unit/test_postcompact_driver_hook.py", '"driver_whoami", "state_graph_read", "state_graph_read", "state_graph_help", "state_graph_read",', 1),
    # Two empty dicts ARE two separate parameters of this param case (a gate
    # report and the flags beside it); the repeat is the point, not a splice.
    ("tests/unit/test_provisional_round_cannot_conclude_success.py", '{},', 1),
    ("tests/unit/test_public_read_hardening.py", '_read_cache.cached(("k2",), lambda: calls.append(1) or "v", ttl=-1)', 1),
    ("tests/unit/test_quota_hold.py", 'sched.tick_log("p", outcome)', 1),
    ("tests/unit/test_route_rotation.py", 'assert table.resolve("plain", rotate=True) == ["a/m1", "payg/m9"]', 1),
    ("tests/unit/test_route_rotation.py", 'assert table.resolve("solo", rotate=True) == ["a/m1", "payg/m9"]', 1),
    ("tests/unit/test_run_isolation_lifecycle.py", "await sc.poll_and_execute()", 2),
    # Two refusal CASES with two different argument shapes (`node_key`, then
    # `duplicate_of`) that the resolver answers with the same sentence; the
    # expected-messages list quotes one line per case, so the repeat is the
    # point, not a splice.
    ("tests/unit/test_state_issue_moot_resolution.py", '"moot takes exactly: artifact",', 1),
    # The blank line inside a parametrized SOURCE STRING. Every snippet in
    # tests/unit/test_state_wait_bounds.py keeps one source line per literal so
    # that the line number it expects to be refused at stays 5 (or 7), which
    # means two consecutive '"\\n"' lines per case are input data, not a splice.
    # Collapsing them into one literal would move the refused call's line and
    # break that expectation, so the repeat is named here instead.
    ("tests/unit/test_state_wait_bounds.py", '"\\n"', 52),
    ("tests/unit/test_tick_log.py", 'await scheduler._run_skillflow_tick("p1", None)', 1),
    ("tests/unit/test_waiting_tick_log_coalescing.py", 'await tick("p1")', 1),
    ("tests/writing_bench/test_early_semantic_refusal.py", 'adapter.publish_review_materials(bench, "run1", "literary", out)', 1),
]


def _is_test(path):
    return "tests" in path.relative_to(_ROOT).parts


def _derived_sites():
    import collections
    non_test = []
    test = collections.Counter()
    for path in _python_sources():
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = str(path.relative_to(_ROOT))
        for _lineno, text in _verbatim_previous_line_sites(source):
            if _is_test(path):
                test[(rel, text)] += 1
            else:
                non_test.append((rel, text))
    return non_test, test


def test_no_verbatim_previous_line_dup_in_non_test_code():
    non_test, _test = _derived_sites()
    assert non_test == []


def test_named_test_repeats_are_pinned_and_current():
    """The tests/ repeats are a named LIST, not a hand-counted number: every
    listed entry must still be a real repeat, and there must be no unlisted
    repeat. The consecutive-count is pinned too — adding a THIRD duplicate at
    a listed site changes the count and must be written down here."""
    _non_test, test = _derived_sites()
    derived = dict(test)
    listed = {(f, s): c for f, s, c in TEST_INTENTIONAL_REPEATS}
    assert derived == listed, (
        f"tests/ repeats drifted; "
        f"only-in-tree={sorted(set(derived) - set(listed))} "
        f"only-in-list={sorted(set(listed) - set(derived))} "
        f"count-mismatch={[(k, derived[k], listed.get(k)) for k in derived if k in listed and derived[k] != listed[k]]}")
    # The length is pinned as well: a silently DROPPED entry is invisible to the
    # equality above (its site would simply stop being a repeat), so the list's
    # size is written down here too.
    assert len(TEST_INTENTIONAL_REPEATS) == 23




_SELF = "tests/unit/test_no_verbatim_previous_line_dup.py"


def test_the_checker_file_is_clean_under_its_own_rule():
    src = (_ROOT / _SELF).read_text(encoding="utf-8")
    assert _verbatim_previous_line_sites(src) == []


def test_a_duplicated_comment_line_is_red():
    src = "# one sentence of a comment\n# one sentence of a comment\nx = 1\n"
    assert _verbatim_previous_line_sites(src) == [
        (2, "# one sentence of a comment")]


def test_the_duplicated_statement_pole_is_red():
    src = 'import re\n_X = re.compile("x")\n\n_X = re.compile("x")\n'
    assert _verbatim_previous_line_sites(src)
