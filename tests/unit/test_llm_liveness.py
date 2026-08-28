"""core/llm_liveness — the per-run stream-tick registry the hung-step warning
reads to tell "long generation" from "no generation"."""

import core.llm_liveness as ll


def _clear():
    with ll._lock:
        ll._last.clear()


def test_note_then_read_round_trips():
    _clear()
    ll.note_progress("run-1", "t_impl", 1234)
    rec = ll.last_progress("run-1")
    assert rec["step_id"] == "t_impl"
    assert rec["chars"] == 1234
    assert rec["at"] > 0


def test_newest_tick_wins():
    _clear()
    ll.note_progress("run-1", "t_impl", 10)
    ll.note_progress("run-1", "t_impl_review", 20)
    assert ll.last_progress("run-1")["step_id"] == "t_impl_review"


def test_unknown_run_is_none():
    _clear()
    assert ll.last_progress("nope") is None


def test_reader_gets_a_copy():
    _clear()
    ll.note_progress("run-1", "t_impl", 1)
    ll.last_progress("run-1")["chars"] = 999
    assert ll.last_progress("run-1")["chars"] == 1


def test_capped_by_evicting_the_oldest():
    _clear()
    for i in range(ll._CAP + 5):
        ll.note_progress(f"run-{i}", "s", i)
    with ll._lock:
        assert len(ll._last) == ll._CAP
    # the oldest ticks are the ones evicted
    assert ll.last_progress("run-0") is None
    assert ll.last_progress(f"run-{ll._CAP + 4}") is not None


def test_bad_input_never_raises():
    _clear()
    ll.note_progress("run-1", "s", None)   # chars=None → 0
    assert ll.last_progress("run-1")["chars"] == 0
