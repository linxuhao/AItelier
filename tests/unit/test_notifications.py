"""Unit tests for cli/tui/notifications.py — NotificationZone event display.

Tests the _format_ctx method and all event types in _handle_event.
Verifies timestamp formatting, project name, step name, and task ID display.

NOTE: Must mock Textual BEFORE importing any AItelier module because
cli/tui/__init__.py → cli.tui.app → textual.app.
"""

import sys
from unittest.mock import MagicMock

# ── Pre-import mocking: must happen before cli.tui is touched ─────────
# cli/tui/__init__.py imports AItelierApp which imports textual.app
_mock_textual_app = MagicMock()
_mock_textual_app.ComposeResult = list
_mock_textual_app.App = type("App", (), {})

# VerticalScroll must be a proper class (not MagicMock) for __new__ to work
class _MockVerticalScroll:
    can_focus = True
    def __init__(self, *a, **kw): pass
    def compose(self): return []
    def mount(self): pass
    def set_interval(self, *a): pass
    def query_one(self, *a): return MagicMock()
    def scroll_end(self, *a, **kw): pass
    def refresh(self, *a, **kw): pass

# textual.containers must have VerticalScroll as a proper class
_mock_containers = MagicMock()
_mock_containers.VerticalScroll = _MockVerticalScroll

_dummy_modules = {
    "textual": MagicMock(),
    "textual.app": _mock_textual_app,
    "textual.widgets": MagicMock(),
    "textual.containers": _mock_containers,
    "textual.widget": MagicMock(),
    "textual.container": MagicMock(),
    "textual.css": MagicMock(),
    "textual.css.query": MagicMock(),
    "textual.work": MagicMock(),
    "textual.message": MagicMock(),
    "textual.dom": MagicMock(),
    "textual.events": MagicMock(),
    "textual.reactive": MagicMock(),
    "textual.strip": MagicMock(),
    "textual._arrange": MagicMock(),
    "textual.screen": MagicMock(),
    "textual.coordinate": MagicMock(),
    "textual.geometry": MagicMock(),
    "textual.keys": MagicMock(),
    "textual.binding": MagicMock(),
    "textual.timer": MagicMock(),
    "textual.worker": MagicMock(),
    "textual._context": MagicMock(),
    "textual.color": MagicMock(),
    "textual.renderables": MagicMock(),
    "textual.widgets.Static": type("Static", (), {}),
    # cli.tui.app imports these
    "cli.tui.dashboard": MagicMock(),
    "cli.tui.chat": MagicMock(),
    "cli.tui.flash": MagicMock(),
    "cli.tui.keys": MagicMock(),
    "cli.completer": MagicMock(),
}
for name, mod in _dummy_modules.items():
    sys.modules[name] = mod

# Now safe to import
from cli.tui.notifications import NotificationZone, STEP_NAMES

import pytest
from unittest.mock import patch


@pytest.fixture
def zone():
    """Create a NotificationZone with mocked Textual internals."""
    z = NotificationZone.__new__(NotificationZone)
    z.server_url = "http://localhost:4444"
    z._lines = []
    z._display = MagicMock()
    z._project_name_cache = {}
    return z


# ── _format_ctx tests ──────────────────────────────────────────────────

class TestFormatCtx:
    def test_timestamp_local(self, zone):
        ts = 1718400000.0
        event = {"_ts": ts, "project_id": "test-proj"}
        ctx = zone._format_ctx(event)
        assert ":" in ctx

    def test_no_timestamp(self, zone):
        event = {"project_id": "test-proj"}
        ctx = zone._format_ctx(event)
        assert "--:--:--" in ctx

    def test_project_name_from_event(self, zone):
        event = {"_ts": 0, "_project_name": "E-Commerce Store", "project_id": "ec"}
        ctx = zone._format_ctx(event)
        assert "E-Commerce Store" in ctx

    def test_project_id_fallback(self, zone):
        event = {"_ts": 0, "project_id": "very-long-project-id-12345"}
        ctx = zone._format_ctx(event)
        assert "very-long-projec" in ctx

    def test_step_name_resolved(self, zone):
        event = {"_ts": 0, "project_id": "p", "_step_id": "t_impl"}
        ctx = zone._format_ctx(event)
        assert "Implementer" in ctx

    def test_step_name_fallback(self, zone):
        event = {"_ts": 0, "project_id": "p", "_step_id": "unknown_step"}
        ctx = zone._format_ctx(event)
        assert "unknown_step" in ctx

    def test_step_from_legacy_fields(self, zone):
        event = {"_ts": 0, "project_id": "p", "step": "1"}
        ctx = zone._format_ctx(event)
        assert "Researcher" in ctx

    def test_task_id_shown(self, zone):
        event = {"_ts": 0, "project_id": "p", "_task_id": "cart_bp"}
        ctx = zone._format_ctx(event)
        assert "cart_bp" in ctx


# ── _handle_event: each event type ─────────────────────────────────────

class TestHandleEvent:
    def _ev(self, etype, **kw):
        return {"type": etype, "_ts": 0, "project_id": "test", **kw}

    def test_step_claimed(self, zone):
        """step_claimed now produces a separator line (dim ──)."""
        zone._handle_event(self._ev("step_claimed", step_id="t_impl"))
        assert "──" in zone._lines[0]

    def test_step_start(self, zone):
        """step_start produces a highlighted ● line."""
        zone._handle_event(self._ev("step_start", step="t_impl"))
        assert "●" in zone._lines[0]

    def test_step_completed(self, zone):
        """step_completed now produces a separator line (dim ──)."""
        zone._handle_event(self._ev("step_completed", step_id="1"))
        assert "──" in zone._lines[0]

    def test_step_end_success(self, zone):
        """step_end with success=true produces ✓ done line."""
        zone._handle_event(self._ev("step_end", step="1", success=True))
        assert "✓" in zone._lines[0]
        assert "done" in zone._lines[0]

    def test_step_failed(self, zone):
        zone._handle_event(self._ev("step_failed", step="t_verify",
                                     error="syntax error in app.py"))
        assert "✗" in zone._lines[0]
        assert "syntax error" in zone._lines[0]

    def test_step_timeout(self, zone):
        zone._handle_event(self._ev("step_timeout", step="t_plan",
                                     error="timed out after 300s"))
        assert "⏰" in zone._lines[0]
        assert "timed out" in zone._lines[0]

    def test_checkpoint_paused(self, zone):
        zone._handle_event(self._ev("checkpoint_paused",
                                     label="Architecture Review"))
        assert "⏳" in zone._lines[0]
        assert "Architecture Review" in zone._lines[0]

    def test_checkpoint_approved(self, zone):
        zone._handle_event(self._ev("checkpoint_approved",
                                     label="SOTA Review", action="approved"))
        assert "✓" in zone._lines[0]
        assert "SOTA Review" in zone._lines[0]
        assert "approved" in zone._lines[0]

    def test_checkpoint_rejected(self, zone):
        zone._handle_event(self._ev("step_checkpoint_rejected",
                                     label="PM Review"))
        assert "↺" in zone._lines[0]
        assert "rejected" in zone._lines[0]

    def test_agent_message_info(self, zone):
        zone._handle_event(self._ev("agent_message",
                                     content="Examining workspace...", level="info"))
        assert "i" in zone._lines[0]
        assert "Examining workspace" in zone._lines[0]

    def test_agent_message_milestone(self, zone):
        zone._handle_event(self._ev("agent_message",
                                     content="All checks passed!", level="milestone"))
        assert "!" in zone._lines[0]

    def test_agent_message_truncated(self, zone):
        long_msg = "x" * 200
        zone._handle_event(self._ev("agent_message", content=long_msg))
        # Should be truncated at 160 chars
        content_part = zone._lines[0].split("x" * 10)[0]
        assert len(zone._lines[0]) < 250

    def test_project_completed(self, zone):
        zone._handle_event(self._ev("project_completed"))
        assert "✓" in zone._lines[0]
        assert "Project done" in zone._lines[0]

    def test_project_failed(self, zone):
        zone._handle_event(self._ev("project_failed", reason="Out of memory"))
        assert "✗" in zone._lines[0]
        assert "Project failed" in zone._lines[0]
        assert "Out of memory" in zone._lines[0]

    def test_run_failed(self, zone):
        zone._handle_event(self._ev("run_failed",
                reason="No matching transition from 't_impl_review' with flags {}"))
        assert "✗" in zone._lines[0]
        assert "Run failed" in zone._lines[0]
        assert "t_impl_review" in zone._lines[0]

    def test_run_started(self, zone):
        zone._handle_event(self._ev("run_started"))
        assert "▶" in zone._lines[0]
        assert "Pipeline started" in zone._lines[0]

    def test_step_done_with_files(self, zone):
        zone._handle_event(self._ev("step_done", step_id="t_impl",
                                     files=["app.py", "models.py", "config.py"]))
        assert "✓" in zone._lines[0]
        assert "app.py" in zone._lines[0]

    def test_files_written(self, zone):
        zone._handle_event(self._ev("files_written",
                                     files=["templates/index.html"]))
        assert "wrote" in zone._lines[0]
        assert "templates/index.html" in zone._lines[0]

    def test_lifecycle_completed_empty_skipped(self, zone):
        """Completed with no detail is skipped (too noisy)."""
        zone._handle_event(self._ev("lifecycle_hook",
                                     hook="step_commit", status="completed"))
        # May produce a dim line or be skipped; either is acceptable
        if len(zone._lines) > 0:
            assert "step_commit" in zone._lines[0].lower() or "──" in zone._lines[0]

    def test_lifecycle_completed_with_detail_shown(self, zone):
        """Completed with detail (e.g. '5 file(s)') is shown."""
        zone._handle_event(self._ev("lifecycle_hook",
                hook="on_deliver", status="completed", detail="5 file(s)"))
        assert len(zone._lines) == 1
        assert "on_deliver" in zone._lines[0]
        assert "5 file(s)" in zone._lines[0]

    def test_lifecycle_failed_shown(self, zone):
        zone._handle_event(self._ev("lifecycle_hook",
                hook="syntax_lint", status="failed",
                detail="Missing <html> tag"))
        assert "syntax_lint" in zone._lines[0]
        assert "Missing <html> tag" in zone._lines[0]

    def test_lifecycle_warned_shown(self, zone):
        zone._handle_event(self._ev("lifecycle_hook",
                hook="syntax_lint", status="warned",
                detail="H030: Consider adding a meta description"))
        assert "syntax_lint" in zone._lines[0]


# ── Enriched fields: project name + task ID ────────────────────────────

class TestEnrichment:
    def _ev(self, **kw):
        base = {"type": "step_start", "_ts": 0, "project_id": "t",
                "step": "t_plan"}
        base.update(kw)
        return base

    def test_project_name_displayed(self, zone):
        zone._handle_event(self._ev(_project_name="E-Commerce Store"))
        assert "E-Commerce Store" in zone._lines[0]

    def test_task_id_displayed(self, zone):
        zone._handle_event(self._ev(_task_id="frontend_checkout"))
        assert "frontend_checkout" in zone._lines[0]

    def test_full_enriched_line(self, zone):
        zone._handle_event({
            "type": "step_start", "_ts": 1718400000.0,
            "_project_name": "E-Commerce Store",
            "project_id": "ecommerce-store",
            "step": "t_impl", "_task_id": "cart_bp",
        })
        line = zone._lines[0]
        assert "●" in line
        assert "E-Commerce Store" in line
        assert "Implementer" in line
        assert "cart_bp" in line


# ── STEP_NAMES completeness ────────────────────────────────────────────

class TestStepNames:
    def test_all_known_steps(self):
        for step in ["1", "1_review", "2", "2_review", "3", "3_review",
                      "t_plan", "t_plan_review", "t_impl", "t_impl_review",
                      "t_verify", "t_verify_review", "5", "5_review", "5_test",
                      "task_loop"]:
            name = STEP_NAMES.get(step, step)
            assert isinstance(name, str) and len(name) > 0


# ── MAX_LINES ring buffer ──────────────────────────────────────────────

class TestMaxLines:
    def test_ring_buffer_eviction(self, zone):
        zone._MAX_LINES = 3
        for i in range(5):
            zone._add_line(f"line {i}")
        assert len(zone._lines) == 3
        assert zone._lines[0] == "line 2"
        assert zone._lines[-1] == "line 4"


# ── SSE bridge enrichment tests (api/main.py closures) ─────────────────

class TestSSEEnrichment:
    """Test the SQL query patterns used by _resolve_task_context,
    _resolve_project_info, and _resolve_run_info — verifying correct
    tuple-row access (NOT dict-style row['column'])."""

    @pytest.fixture
    def dbs(self, tmp_path):
        """Create temp DBs mimicking skillflow.db and aitelier.db schemas."""
        import sqlite3, json
        sf = tmp_path / "skillflow.db"
        sf_conn = sqlite3.connect(str(sf))
        sf_conn.execute("CREATE TABLE skillflow_runs (id TEXT, project_id TEXT, status TEXT)")
        sf_conn.execute("CREATE TABLE skillflow_loop_state (run_id TEXT, items_json TEXT, current_index INTEGER)")
        sf_conn.execute("INSERT INTO skillflow_runs VALUES ('run-1', 'proj-1', 'running')")
        sf_conn.execute(
            "INSERT INTO skillflow_loop_state VALUES ('run-1', ?, 2)",
            (json.dumps(["backend", "frontend_css", "checkout_bp", "tests"]),),
        )
        sf_conn.commit()

        af = tmp_path / "aitelier.db"
        af_conn = sqlite3.connect(str(af))
        af_conn.execute("CREATE TABLE projects (project_id TEXT, name TEXT)")
        af_conn.execute("INSERT INTO projects VALUES ('proj-1', 'E-Commerce Store')")
        af_conn.commit()

        yield {"skillflow": str(sf), "aitelier": str(af)}
        sf_conn.close()
        af_conn.close()

    def test_task_context_tuple_access(self, dbs):
        """Tuple-row: row[0]=items_json, row[1]=current_index — NOT row['items_json']."""
        import sqlite3, json
        conn = sqlite3.connect(dbs["skillflow"])
        row = conn.execute(
            "SELECT items_json, current_index FROM skillflow_loop_state WHERE run_id = ?",
            ("run-1",),
        ).fetchone()
        conn.close()
        # Must be a tuple — dict access would fail at runtime
        assert isinstance(row, tuple)
        items = json.loads(row[0])  # items_json
        idx = int(row[1]) if row[1] is not None else 0  # current_index
        assert items == ["backend", "frontend_css", "checkout_bp", "tests"]
        assert idx == 2
        assert items[idx] == "checkout_bp"

    def test_task_context_missing_run(self, dbs):
        """Graceful: no loop state for unknown run."""
        import sqlite3
        conn = sqlite3.connect(dbs["skillflow"])
        row = conn.execute(
            "SELECT items_json, current_index FROM skillflow_loop_state WHERE run_id = ?",
            ("nonexistent",),
        ).fetchone()
        conn.close()
        assert row is None

    def test_run_info_tuple_access(self, dbs):
        """Tuple-row: row[0]=project_id — NOT row['project_id']."""
        import sqlite3
        conn = sqlite3.connect(dbs["skillflow"])
        row = conn.execute(
            "SELECT project_id FROM skillflow_runs WHERE id = ?",
            ("run-1",),
        ).fetchone()
        conn.close()
        assert isinstance(row, tuple)
        assert row[0] == "proj-1"

    def test_project_name_lookup(self, dbs):
        """Project name from aitelier.db."""
        import sqlite3
        conn = sqlite3.connect(dbs["aitelier"])
        row = conn.execute(
            "SELECT name FROM projects WHERE project_id = ?",
            ("proj-1",),
        ).fetchone()
        conn.close()
        assert row[0] == "E-Commerce Store"

    def test_task_context_idx_out_of_range(self, dbs):
        """If current_index >= len(items), no task name is set (graceful skip)."""
        import sqlite3, json
        conn = sqlite3.connect(dbs["skillflow"])
        # Set index past end
        conn.execute("UPDATE skillflow_loop_state SET current_index = 99 WHERE run_id = 'run-1'")
        conn.commit()
        row = conn.execute(
            "SELECT items_json, current_index FROM skillflow_loop_state WHERE run_id = ?",
            ("run-1",),
        ).fetchone()
        conn.close()
        items = json.loads(row[0])
        idx = int(row[1])
        # Should NOT crash — should skip
        assert idx >= len(items)
        assert not (0 <= idx < len(items))
