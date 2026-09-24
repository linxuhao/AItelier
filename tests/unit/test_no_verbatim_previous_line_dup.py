from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent


def _python_sources():
    for path in sorted(_ROOT.glob("**/*.py")):
        if set(path.parts) & {"evidence", ".git", "__pycache__", "node_modules"}:
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
    ("tests/unit/test_godot_harness.py", 'gh._playtest_spec(tmp_path / "proj", _spec(["a"]), 60, 120)', 1),
    ("tests/unit/test_godot_harness.py", "gh.run_script(str(tmp_path), [], timeout=30)", 1),
    ("tests/unit/test_knowledge_sync.py", '_call(repo, ws, "proj1")', 1),
    ("tests/unit/test_postcompact_driver_hook.py", '"state_graph_read", "state_graph_read", "state_graph_help", "state_graph_read",', 1),
    ("tests/unit/test_public_read_hardening.py", '_read_cache.cached(("k2",), lambda: calls.append(1) or "v", ttl=-1)', 1),
    ("tests/unit/test_quota_hold.py", 'sched.tick_log("p", outcome)', 1),
    ("tests/unit/test_route_rotation.py", 'assert table.resolve("plain", rotate=True) == ["a/m1", "payg/m9"]', 1),
    ("tests/unit/test_route_rotation.py", 'assert table.resolve("solo", rotate=True) == ["a/m1", "payg/m9"]', 1),
    ("tests/unit/test_run_isolation_lifecycle.py", "await sc.poll_and_execute()", 2),
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
    assert len(TEST_INTENTIONAL_REPEATS) == 13




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
