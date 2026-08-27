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
        lines = text.splitlines()
        for i, line in enumerate(lines, 1):
            # Per STATEMENT, not per file. A whole-file
            # `if "RotatingFileHandler" in text: continue` exempted the only two
            # files in core/ that contain the string — i.e. exactly the two this
            # test was written to protect, forever.
            appending = "open(" in line and ('"a"' in line or "'a'" in line)
            if not appending:
                continue
            # A LOG, not per-project content: `meta/conversation.md` is the
            # transcript a step reads and is bounded by the conversation, while
            # anything named *.log or reached through a *_log_path() helper
            # grows for as long as the process runs. Look at the statement and
            # its neighbours, since the filename is often on the line above.
            window = " ".join(lines[max(0, i - 3):i + 2])
            if ".log" in window or "_log_path" in window:
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


def test_a_skipped_tick_reaches_the_tick_log_with_its_timestamp():
    """A max-instances skip means NO project was picked, so the tick log's own
    premise (one line per tick) fails silently — there is no tick to write the
    line from. It was visible only in the orphan sink and `docker logs`.

    Asserted on BEHAVIOUR with a real APScheduler event: the first version of
    this test read the source text, and could not see that the code read
    `scheduled_run_time` (singular, an EXECUTION-event field) off a SUBMISSION
    event that carries `scheduled_run_times` — so the timestamp was silently
    absent on exactly the outcome the logging was added for.
    """
    import datetime as dt
    from apscheduler.events import EVENT_JOB_MAX_INSTANCES, JobSubmissionEvent
    from core import scheduler as sc

    written = []

    class _L:
        def info(self, fmt, *args):
            written.append(fmt % args)

    old_logger, old_skip = sc._tick_logger, sc._tick_last_skip
    sc._tick_logger, sc._tick_last_skip = _L(), 0.0
    try:
        when = dt.datetime(2026, 8, 27, 1, 5, 42, tzinfo=dt.timezone.utc)
        ev = JobSubmissionEvent(EVENT_JOB_MAX_INSTANCES, "job-1", None, [when])
        sc.log_job_event(ev)
        assert written, "a max-instances skip wrote nothing to the tick log"
        line = written[0]
        assert "outcome=tick_skipped" in line
        assert "2026-08-27" in line, f"the scheduled time is missing: {line}"
        assert "\n" not in line, "one line per tick is the log's contract"
    finally:
        sc._tick_logger, sc._tick_last_skip = old_logger, old_skip


def test_a_repeating_skip_is_coalesced():
    """A tool step owning the interval job makes APScheduler fire MAX_INSTANCES
    on every tick for the step's whole duration — measured 16,133 events in 5.2
    days, which would become the log's second-largest outcome and evict ~25% of
    the history it exists to keep."""
    from core import scheduler as sc
    n = []

    class _L:
        def info(self, *a):
            n.append(1)

    old_logger, old_skip = sc._tick_logger, sc._tick_last_skip
    sc._tick_logger, sc._tick_last_skip = _L(), 0.0
    try:
        for _ in range(120):
            sc.tick_log("(scheduler)", "tick_skipped", job="x")
        assert len(n) == 1, f"{len(n)} lines for 120 identical skips"
    finally:
        sc._tick_logger, sc._tick_last_skip = old_logger, old_skip
