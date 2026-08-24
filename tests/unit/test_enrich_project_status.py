"""`enrich_project_status` must not throw away the scheduler's enriched status.

The scheduler writes `failed:<why>` into `runs.status` (scheduler.py `_failure_reason`),
but skillflow's own run row only ever says `failed`. The read path used to overwrite
the DB value with the raw one for every status except `running`, so the user saw a
bare "failed" and had to go read container logs to learn why — which is exactly the
diagnosability problem the enrichment was added to solve.

The rule: the DB value wins when it is a REFINEMENT of the raw status (same prefix
before the colon); a prefix mismatch means the DB is stale and skillflow wins.
"""
from __future__ import annotations

import pytest

from api import dependencies as deps


class _FakeSF:
    def __init__(self, run):
        self._run = run

    def get_run_by_project(self, pid):
        return self._run

    def list_runs(self, pid):
        return [self._run] if self._run else []

    def get_steps(self, run_id):
        return []


@pytest.fixture
def patched(monkeypatch):
    def _apply(run):
        monkeypatch.setattr(deps, "get_skillflow", lambda: _FakeSF(run))
        monkeypatch.setattr(deps, "get_config_registry", lambda: None)
    return _apply


def _run(status, node=None):
    return {"id": "r1", "status": status, "current_node": node,
            "graph_name": "pipeline_forge"}


def test_enriched_failure_reason_survives(patched):
    patched(_run("failed"))
    p = {"project_id": "p1",
         "status": "failed:Cycle limit exceeded — v_smoke: no transition matched"}
    out = deps.enrich_project_status(p)
    assert out["status"] == (
        "failed:Cycle limit exceeded — v_smoke: no transition matched")
    # the block really ran (enrich swallows exceptions, which would leave the
    # status untouched and make this assertion pass for the wrong reason)
    assert "completed_project_steps" in out


def test_bare_failed_is_adopted_when_db_has_nothing_richer(patched):
    patched(_run("failed"))
    p = {"project_id": "p1", "status": "running:emit_graph"}
    assert deps.enrich_project_status(p)["status"] == "failed"


def test_stale_enriched_status_loses_to_a_reactivated_run(patched):
    """DB says failed:<old>, skillflow says the run is going again → skillflow wins."""
    patched(_run("running", node="emit_graph"))
    p = {"project_id": "p1", "status": "failed:something old"}
    assert deps.enrich_project_status(p)["status"] == "running:emit_graph"


def test_running_without_a_current_node_falls_back_to_raw(patched):
    patched(_run("running", node=None))
    p = {"project_id": "p1", "status": "planning"}
    assert deps.enrich_project_status(p)["status"] == "running"


def test_completed_is_not_shadowed_by_a_stale_running_status(patched):
    patched(_run("completed"))
    p = {"project_id": "p1", "status": "running:5"}
    assert deps.enrich_project_status(p)["status"] == "completed"


def test_enriched_running_status_from_the_db_is_replaced_by_the_live_node(patched):
    """The live node is fresher than the DB's cached one — AT-15's original case."""
    patched(_run("running", node="7"))
    p = {"project_id": "p1", "status": "running:3"}
    assert deps.enrich_project_status(p)["status"] == "running:7"


class TestTheRunsOwnReasonIsNotDiscarded:
    """The reconciliation above picks a STATUS between two producers. The run's
    `error_reason` is a third thing — the authoritative cause, sitting in the same
    row skillflow just returned — and it reached no client at all. When the DB's
    enriched status is stale its prefix disagrees, skillflow's bare "failed" wins,
    and the user is left with a status and no cause."""

    def test_error_reason_is_attached(self, patched):
        run = _run("failed")
        run["error_reason"] = "Node 'give_up_gate' reached"
        patched(run)
        out = deps.enrich_project_status({"project_id": "p1", "status": "failed"})
        assert out["error_reason"] == "Node 'give_up_gate' reached"

    def test_it_survives_the_branch_that_discards_the_db_status(self, patched):
        """Prefix mismatch → skillflow wins → previously the only surviving text
        was the bare word "failed"."""
        run = _run("failed")
        run["error_reason"] = "Output validation failed: Nothing matching '*' was written"
        patched(run)
        out = deps.enrich_project_status(
            {"project_id": "p1", "status": "running:C1"})   # stale DB value
        assert out["status"] == "failed"
        assert "Nothing matching" in out["error_reason"]

    def test_absent_reason_is_an_empty_string_not_a_missing_key(self, patched):
        patched(_run("completed"))
        out = deps.enrich_project_status({"project_id": "p1", "status": "completed"})
        assert out["error_reason"] == ""


def test_the_api_response_model_declares_error_reason():
    """A response_model is a FILTER. `enrich_project_status` attached
    `error_reason` and FastAPI dropped it on the way out, so the reason was
    produced and discarded one layer further along than the defect it was added
    to fix — caught only by reading a live response instead of the source. A
    field a client is meant to read must be declared, or it does not exist."""
    from models.schemas import ProjectWithStats
    assert "error_reason" in ProjectWithStats.model_fields


class TestTheReasonIsEnrichedNotTheFrameworkArtifact:
    """skillflow's `error_reason` often names the EDGE, not the cause. A novel
    chapter died with "Cycle limit exceeded" while the tool that rejected it had
    said "continuity_check 未通过: 字数超限 5662 字（上限 4500）". The scheduler
    already resolves that; serving the raw column would hide a tool result the
    host had already dug out."""

    def test_a_vague_reason_is_replaced_by_the_resolved_one(self, patched, monkeypatch):
        import api.dependencies as deps
        run = _run("failed")
        run["error_reason"] = "Cycle limit exceeded"
        patched(run)
        monkeypatch.setattr(
            "core.scheduler._failure_reason",
            lambda r: "Cycle limit exceeded — continuity: 字数超限 5662 字（上限 4500）")
        out = deps.enrich_project_status({"project_id": "p1", "status": "failed"})
        assert "字数超限" in out["error_reason"]

    def test_skillflows_own_enrichment_does_not_shut_the_resolver_out(self, monkeypatch):
        """skillflow >=1.5.30 names the routing file itself; the trace still speaks.

        The reason above stopped arriving verbatim — it now reads "Cycle limit
        exceeded — continuity_report.json summary: … (edges: …)" — and the
        scheduler's vagueness test was an exact string match, so the enriched base
        counted as specific and the tool feedback was dropped. Both details belong
        on the page. Nothing is monkeypatched between the two layers here: the whole
        path from the run row to `error_reason` runs.
        """
        from core import scheduler

        scheduler._failure_reason_cache.clear()
        run = _run("failed")
        run["error_reason"] = ("Cycle limit exceeded — continuity_report.json summary: "
                               "continuity check finished (edges: All transitions from "
                               "'continuity_check' are exhausted)")

        class _SFWithTrace(_FakeSF):
            def trace_query(self, run_id, sql, params):
                return [{"step_id": "continuity_check",
                         "payload_json":
                             '{"passed": false, "feedback": "字数超限: 5662 字（上限 4500）"}'}]

        monkeypatch.setattr(deps, "get_skillflow", lambda: _SFWithTrace(run))
        monkeypatch.setattr(deps, "get_config_registry", lambda: None)

        out = deps.enrich_project_status({"project_id": "p1", "status": "failed"})
        assert "字数超限" in out["error_reason"]
        assert out["error_reason"].startswith("Cycle limit exceeded — continuity_report.json")
        scheduler._failure_reason_cache.clear()

    def test_a_non_failed_run_keeps_the_plain_column(self, patched):
        import api.dependencies as deps
        run = _run("running", "C1")
        run["error_reason"] = None
        patched(run)
        out = deps.enrich_project_status({"project_id": "p1", "status": "running"})
        assert out["error_reason"] == ""

    def test_a_resolver_failure_falls_back_instead_of_raising(self, patched, monkeypatch):
        """This runs on every project list; it must never break the page."""
        import api.dependencies as deps
        run = _run("failed")
        run["error_reason"] = "Cycle limit exceeded"
        patched(run)

        def _boom(_r):
            raise RuntimeError("trace db gone")
        monkeypatch.setattr("core.scheduler._failure_reason", _boom)
        out = deps.enrich_project_status({"project_id": "p1", "status": "failed"})
        assert out["error_reason"] == "Cycle limit exceeded"


class TestRejectionRoundsCarryBothKeyNames:
    """Three consumers, two spellings: the web modal and the TUI chat pane read
    `user_feedback or reason`; `cli/app.py` reads only `reason`. Emitting one name
    fixes two surfaces out of three and prints "Last feedback: N/A" on the
    third."""

    def _rounds(self, tmp_path, text):
        import api.meta_routers as mr
        from unittest.mock import MagicMock, patch
        log = tmp_path / "_feedback" / "outline.md"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(text, encoding="utf-8")
        sf = MagicMock()
        sf._get_resolver.return_value.get_node.return_value = None
        sf._workspace.get_config_path.return_value = tmp_path
        with patch.object(mr, "get_skillflow", return_value=sf):
            return mr._read_rejection_rounds("p1", "outline", "novel_chapter")

    def test_both_keys_are_present(self, tmp_path):
        rounds = self._rounds(tmp_path, "## 反馈轮 #1 · ts\n\n王超哪来的问题？\n")
        assert len(rounds) == 1
        assert rounds[0]["user_feedback"] == "王超哪来的问题？"
        assert rounds[0]["reason"] == rounds[0]["user_feedback"]

    def test_multiple_rounds_are_split(self, tmp_path):
        rounds = self._rounds(
            tmp_path, "## 轮 #1 · a\n\nfirst\n\n## 轮 #2 · b\n\nsecond\n")
        assert [r["reason"] for r in rounds] == ["first", "second"]

    def test_the_users_own_markdown_headings_do_not_become_rounds(self, tmp_path):
        """One rejection stays ONE round even when its feedback is itself
        structured with `## ` sections.

        The banner says "this step has been revised N time(s)", and N used to be
        "how many level-2 headings are in the log" -- so a single rejection whose
        feedback had five `## ` sections reported six rounds (round header + the
        author's own five). A user who rejects once and is told the step was
        revised six times cannot tell a real loop from a display artifact, and a
        history banner that misreports is worse than no banner: it reads as
        evidence. Rounds are now anchored on skillflow's `#<N> ·` marker, the same
        literal its writer counts to number them.
        """
        log = ("## 反馈轮 #1 · 2026-08-24 10:55 UTC\n\n"
               "研究做得扎实,三处要改。\n\n"
               "## 一、场景可以自己指定 boot scene\n\n正文\n\n"
               "## 二、否决 headless 检测\n\n正文\n\n"
               "## 三、建议的形状\n\n正文\n")
        rounds = self._rounds(tmp_path, log)
        assert len(rounds) == 1
        assert "## 一、场景可以自己指定 boot scene" in rounds[0]["user_feedback"]

    def test_a_log_without_headings_is_still_one_round(self, tmp_path):
        rounds = self._rounds(tmp_path, "just some feedback text\n")
        assert rounds[0]["reason"] == "just some feedback text"

    def test_an_absent_log_is_none_not_an_empty_banner(self, tmp_path):
        import api.meta_routers as mr
        from unittest.mock import MagicMock, patch
        sf = MagicMock()
        sf._get_resolver.return_value.get_node.return_value = None
        sf._workspace.get_config_path.return_value = tmp_path
        with patch.object(mr, "get_skillflow", return_value=sf):
            assert mr._read_rejection_rounds("p1", "outline", "novel_chapter") is None
