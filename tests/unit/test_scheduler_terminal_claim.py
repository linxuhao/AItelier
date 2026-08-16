"""Regression test: a missing required context source must FAIL the run.

`claim_next_step` raising RequiredContextMissing is not a transient error — the
same node resolves the same (absent) file every tick, and nothing else in the
system will ever write it. Live, a dpe_default run started without its
meta_conversation predecessor re-claimed on
"Required context source resolved to no content: finalize" every tick for 47
minutes while the project reported running:1.
"""

from unittest.mock import MagicMock

import pytest
from skillflow.exceptions import RequiredContextMissing

from core import scheduler


@pytest.fixture
def sf(monkeypatch):
    """A skillflow stub wired past everything the tick does before Phase B."""
    sf = MagicMock()
    sf.trace_query.return_value = [[0]]          # runaway-loop guard: 0 claims
    sf._get_resolver_for_run.return_value.is_tool.return_value = False
    monkeypatch.setattr(scheduler, "get_skillflow", lambda: sf)
    monkeypatch.setattr(scheduler, "_get_or_create_skillflow_run", lambda pid: "run1")
    monkeypatch.setattr(scheduler, "_has_active_claim", lambda *a: False)
    monkeypatch.setattr(scheduler, "_advance_recording_crashes", lambda *a: "1")
    monkeypatch.setattr(scheduler, "_sync_project_status_to_db", lambda pid: None)
    return sf


async def test_missing_required_context_fails_the_run(sf):
    sf.claim_next_step.side_effect = RequiredContextMissing(
        "Required context source resolved to no content: finalize. "
        "The step cannot run without it.")

    await scheduler._run_skillflow_tick("p1", None)

    sf.fail_run.assert_called_once()
    run_id, reason = sf.fail_run.call_args[0]
    assert run_id == "run1"
    assert "finalize" in reason


async def test_other_claim_errors_still_retry(sf):
    """Only the terminal case is terminal — a transient claim error must not
    fail the run (e.g. a stale-claim version conflict resolves on its own)."""
    sf.claim_next_step.side_effect = RuntimeError("database is locked")

    await scheduler._run_skillflow_tick("p1", None)

    sf.fail_run.assert_not_called()
