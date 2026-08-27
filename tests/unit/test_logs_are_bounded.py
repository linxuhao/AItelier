"""Every durable log this system writes must have a ceiling.

A diagnostic that outlives its investigation and grows without bound is a disk
leak, not a diagnostic — and this one lives on the MOUNTED data volume, so it
survives every container rebuild that would otherwise have wiped it.
"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


def _reset(mod):
    mod._logger = None
    for h in list(logging.getLogger("aitelier.orphan_dbg").handlers):
        logging.getLogger("aitelier.orphan_dbg").removeHandler(h)


def test_the_orphan_debug_sink_rotates(tmp_path, monkeypatch):
    """It was a bare `open(..., "a")`: every line ever written, kept forever.
    5.1 MB by the time anyone looked, with nothing to stop it."""
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path))
    from core import orphan_dbg
    _reset(orphan_dbg)
    try:
        handlers = orphan_dbg._sink().handlers
        rot = [h for h in handlers if isinstance(h, RotatingFileHandler)]
        assert rot, "the durable orphan log has no rotation"
        assert rot[0].maxBytes > 0 and rot[0].backupCount > 0
    finally:
        _reset(orphan_dbg)


def test_the_orphan_sink_actually_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path))
    from core import orphan_dbg, datadir
    _reset(orphan_dbg)
    try:
        orphan_dbg.odbg("hello")
        assert "hello" in datadir.orphan_log_path().read_text()
    finally:
        _reset(orphan_dbg)


def test_no_durable_log_is_opened_for_append_without_rotation():
    """Guardrail: the next durable log must not repeat the same mistake.

    An `open(..., "a")` on a path under the data root is unbounded by
    construction — route it through a RotatingFileHandler instead.
    """
    offenders = []
    for py in (ROOT / "core").rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="replace")
        if "RotatingFileHandler" in text:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            appending = '"a"' in line and "open(" in line
            # A LOG, not per-project content: `meta/conversation.md` is the
            # transcript a step reads and is bounded by the conversation, while
            # anything named *.log or reached through a *_log_path() helper
            # grows for as long as the process runs.
            is_log = ".log" in line or "_log_path()" in line
            if appending and is_log:
                offenders.append(f"{py.relative_to(ROOT)}:{i}: {line.strip()}")
    assert not offenders, (
        "durable append with no rotation — use a RotatingFileHandler:\n  "
        + "\n  ".join(offenders))


def test_the_tick_log_rotates():
    """The tick log is the file someone opens to answer 'why is nothing
    moving'; it is also written on every 5s tick."""
    from core import scheduler
    assert scheduler._TICK_LOG_MAX_BYTES > 0
    assert scheduler._TICK_LOG_BACKUPS > 0


def test_a_skipped_tick_reaches_the_tick_log():
    """A max-instances skip means NO project was picked, so the tick log's own
    premise (one line per tick) fails silently — there is no tick to write the
    line from. It was visible only in the orphan sink and `docker logs`."""
    src = (ROOT / "core" / "scheduler.py").read_text()
    assert "EVENT_JOB_MAX_INSTANCES: \"tick_skipped\"" in src
    i = src.index("def _log_job_event")
    assert "tick_log(" in src[i:i + 1400], "job events never reach the tick log"
