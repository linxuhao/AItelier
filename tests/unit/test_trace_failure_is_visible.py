"""A trace that cannot be written, and a replay that cannot find an instance,
must both SAY SO.

Both halves are the same defect class: the durable trace was reported dead for
a whole afternoon while it was in fact writing 165 rows per run — because
`_trace` swallowed every write error with a bare `pass`, and `replay` answered a
guessed `--instance 1` with "no prompt_delta events", which reads as "the trace
is empty" rather than "you asked for an id that isn't in it".
"""
import logging
import sqlite3
import subprocess
import sys
from pathlib import Path

from core.dpe_pipeline import PipelineEngine

REPO = Path(__file__).resolve().parents[2]


class _Broken:
    """Minimal host whose trace callback always fails."""
    def __init__(self):
        self.calls = 0

    def _trace_cb(self, *a, **kw):
        self.calls += 1
        raise sqlite3.OperationalError("attempt to write a readonly database")

    _trace = PipelineEngine._trace


def test_failed_trace_write_is_logged_once_and_never_raises(caplog):
    h = _Broken()
    with caplog.at_level(logging.WARNING, logger="aitelier.dpe"):
        for _ in range(50):
            h._trace("prompt", "prompt_delta", {"turn": 1})
    assert h.calls == 50                      # tracing still attempted every time
    warnings = [r for r in caplog.records if r.name == "aitelier.dpe"]
    assert len(warnings) == 1                 # reported, but does not flood
    msg = warnings[0].getMessage()
    assert "trace write FAILED" in msg
    assert "OperationalError" in msg and "readonly" in msg
    assert "prompt/prompt_delta" in msg


def test_successful_trace_write_logs_nothing(caplog):
    class _Ok:
        def _trace_cb(self, *a, **kw):
            pass
        _trace = PipelineEngine._trace

    with caplog.at_level(logging.WARNING, logger="aitelier.dpe"):
        _Ok()._trace("prompt", "prompt_delta", {})
    assert [r for r in caplog.records if r.name == "aitelier.dpe"] == []


# ── replay: a wrong instance id must not look like an empty trace ──────────

def _trace_db(tmp_path, project_id, rows):
    d = tmp_path / "workspaces" / project_id
    d.mkdir(parents=True)
    conn = sqlite3.connect(d / "trace.db")
    conn.execute("""CREATE TABLE skillflow_trace (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
        step_id TEXT, step_instance_id INTEGER, seq INTEGER NOT NULL,
        category TEXT NOT NULL, event TEXT NOT NULL,
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT (datetime('now')))""")
    conn.executemany(
        "INSERT INTO skillflow_trace (run_id, step_id, step_instance_id, seq, "
        "category, event, payload_json) VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return tmp_path


def _replay(datadir, project_id, *extra):
    return subprocess.run(
        [sys.executable, str(REPO / "debugctl.py"), "replay", project_id, *extra],
        capture_output=True, text=True, cwd=str(REPO),
        env={"PATH": "/usr/bin:/bin", "AITELIER_HOME": str(datadir),
             "HOME": str(datadir.parent), "PYTHONPATH": str(REPO)},
    ).stdout


def _rows(instance, step, n, start_seq=1):
    return [("r1", step, instance, start_seq + i, "prompt",
             "prompt_delta", '{"turn": 1, "role": "user", "content": "m%d"}' % i)
            for i in range(n)]


def test_wrong_instance_reports_the_instances_that_do_exist(tmp_path):
    dd = _trace_db(tmp_path / "ae", "p1", _rows(4817, "investigate", 3))
    out = _replay(dd, "p1", "--instance", "1")
    assert "no prompt_delta events" in out
    # the point: it must NOT stop at "nothing here"
    assert "3 prompt_delta events under other instances" in out
    assert "--instance 4817" in out and "step=investigate" in out


def test_instance_defaults_to_the_latest_traced_one(tmp_path):
    dd = _trace_db(tmp_path / "ae", "p1",
                   _rows(10, "t_plan", 2) + _rows(4817, "t_impl", 3, start_seq=3))
    out = _replay(dd, "p1")
    assert "# instance 4817 — 3 messages" in out


def test_genuinely_empty_trace_still_says_so(tmp_path):
    dd = _trace_db(tmp_path / "ae", "p1", [])
    out = _replay(dd, "p1")
    assert "no prompt_delta events anywhere" in out
