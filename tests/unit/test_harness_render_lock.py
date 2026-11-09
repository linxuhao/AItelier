"""The render routes serialise; the cheap headless routes do not.

Measured 2026-09-04: one unchanged tree swept 0 red alone and 6 red while a
second sweep ran, with disjoint red sets. Concurrency on the render path
turns the gate into a coin flip, so it is not allowed.

The second half of this file is about the one limit a caller may put on that
serialisation: `render_wait_timeout_sec` bounds the QUEUE WAIT. `0` (and a
negative value, which the request path clamps to 0) therefore means "may not
queue at all" -- not "may not render", which is what the code measured when it
charged the time spent AFTER ownership to the same limit.

The third section is the same limit from the other side: the queue wait it
bounds is the time a request spends waiting for SOMEONE ELSE. A request's own
owner-table admission transaction (connect, `CREATE TABLE IF NOT EXISTS`,
`BEGIN IMMEDIATE`, commit) is that request's own cost -- cheap on a quiet box,
slow enough to matter on a loaded one -- and charging it refused a request
whose render lock was FREE.
"""
import ast
import pathlib
import sys
import threading
import time

import pytest

SRC = pathlib.Path(__file__).resolve().parents[2] / "docker/godot/godot_harness.py"

# The ONE rig in this suite: a real ThreadingHTTPServer running the real
# `_Handler.do_POST` over a temp owner table, with only the render bodies
# replaced, from `test_harness_render_queue.py`. Imported rather than rebuilt --
# a second rig would witness a second harness and could agree with a bug the
# real request path does not have. Importing a fixture across test modules is
# the repo's established pattern (`tests/integration/
# test_retained_candidate_source_binding_race.py` does it with `fixture`).
from tests.unit.test_harness_render_queue import rig  # noqa: E402,F401


def _handler_class():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and any(
                isinstance(b, ast.Name) and "Handler" in b.id or
                isinstance(b, ast.Attribute) and "Handler" in b.attr
                for b in node.bases):
            return node
    raise AssertionError("no request-handler class in godot_harness.py")


def test_the_render_routes_are_named_and_include_playtest():
    cls = _handler_class()
    routes = None
    for node in cls.body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "_RENDER_ROUTES" for t in node.targets):
            routes = {ast.literal_eval(e) for e in node.value.elts}
    assert routes is not None, "_RENDER_ROUTES must list the routes that render"
    assert "/playtest" in routes, "the play-test sweep is the gate that flaked"
    assert "/compile" not in routes, "compile is headless and cheap; keep it concurrent"


def test_the_lock_is_class_level_so_all_threads_share_it():
    cls = _handler_class()
    assert any(isinstance(n, ast.Assign)
               and any(getattr(t, "id", "") == "_RENDER_LOCK" for t in n.targets)
               for n in cls.body), (
        "_RENDER_LOCK must be a CLASS attribute — ThreadingHTTPServer builds a new "
        "handler INSTANCE per request, so a per-instance lock serialises nothing")


def test_the_lock_is_released_on_every_path():
    src = SRC.read_text(encoding="utf-8")
    i = src.index("held = self.path in self._RENDER_ROUTES")
    body = src[i:i + 2000]
    assert "finally:" in body and "_RENDER_LOCK.release()" in body, (
        "release must be in a finally — a route that raises would otherwise wedge "
        "every later sweep behind a lock nobody holds any more")


# ── a-non-positive-render-wait-limit-means-no-queue-wait ────────────────────
# The defect (independent render-lock r3 review, point P7; iss-c2da59d15e45440c,
# iss-b859d7478c524da1 item 3): with `render_wait_timeout_sec: 0` against a FREE
# render lock the acquire succeeded without queueing, a few milliseconds passed,
# `elapsed > 0` was true, and the request was refused with
# `_wait_timed_out(..., owned=True)` -- "the request's own render_wait_timeout_sec
# ran out ... it took ownership only after the limit" -- although nothing was
# ever queued and the request would have rendered.
#
# The limit decides how long a request may QUEUE, so 0 (and a negative value,
# which the request path clamps to 0) means "may not queue at all": a free lock
# renders, a held lock is refused at once with a body that says so instead of
# borrowing the wording of a wait that ran out. An absent (or explicit null)
# limit keeps the ordinary bounded queue.
#
# Every test below drives the real request path -- `rig.post`/`post_in_thread`
# POST to the served `_Handler.do_POST`; only the render bodies are stubs.


@pytest.mark.parametrize("limit", [0, 0.0, -3])
def test_a_non_positive_limit_renders_on_a_free_render_lock(rig, limit):
    """`0` is "do not queue", never "do not render": the free lock runs it."""
    thread, out = rig.post_in_thread(
        "/script", rig.payload("op-zero", render_wait_timeout_sec=limit))
    thread.join(15)
    assert out.get("code") == 200, (
        f"render_wait_timeout_sec={limit!r} against a FREE render lock was not "
        f"admitted: {out!r}")
    body = out["body"]
    assert body["route"] == "/script" and body["passed"] is True, body
    assert rig.rendered == [("/script", "op-zero")], (
        "the render body never ran for a request whose render lock was free",
        rig.rendered)
    # Nothing was queued, and the answer may not pretend otherwise. (The wait's
    # own seconds are NOT asserted from below: on a loaded box the one sqlite
    # admission attempt itself can take a second, and this test is about what
    # the request was allowed to do, not about wall time.)
    assert body["render_owner_waited_for_owner_ids"] == [], body
    assert [(r["operation_id"], r["status"], r["reason"] or "")
            for r in rig.settled_snapshot()] == [("op-zero", "released", "completed")]


def test_a_non_positive_limit_is_refused_at_once_and_says_it_may_not_queue(
        rig, monkeypatch):
    """Held lock + `0`: refused in one admission attempt, body says why."""
    holder_id = rig.start_holder()
    gh = rig.gh
    # The production poll interval: burning even one of them would be visible.
    gh.RENDER_OWNER_WAIT_POLL_SEC = 0.25
    attempts: list[str] = []
    real_acquire = gh.acquire_render_owner

    def counted(project_id, run_id, operation_id, **kwargs):
        attempts.append(operation_id)
        return real_acquire(project_id, run_id, operation_id, **kwargs)

    monkeypatch.setattr(gh, "acquire_render_owner", counted)
    started = time.monotonic()
    thread, out = rig.post_in_thread(
        "/script", rig.payload("op-zero", render_wait_timeout_sec=0))
    thread.join(10)
    elapsed = time.monotonic() - started
    rig.release.set()
    rig.holder[0].join(30)
    assert out.get("code") == 409, out
    body = out["body"]
    # The body states that THIS request may not queue because of its own limit,
    # not that a queue wait ran out.
    assert body["error"] == "render owner exists", body
    assert body["queue_allowed"] is False, body
    assert body["render_wait_timeout_sec"] == 0.0, body
    assert "may not queue" in body["detail"], body
    assert "took ownership only after the limit" not in body["detail"], body
    # The holder is named as the one IN THE WAY, never as someone queued behind,
    # and no queue vocabulary is borrowed.
    assert body["owner_id"] == holder_id, body
    assert body["owner_kind"] == "active", body
    assert body["blocking_owner_ids"] == [holder_id], body
    assert body["queued_for_owner_ids"] == [], body
    assert "waited_for_owner_ids" not in body and "waited_sec" not in body, body
    assert body["needs_reconciliation"] is False, body
    assert body["deployment_fence_held"] is False, body
    assert holder_id in body["detail"], body
    # Refused at once: ONE admission attempt, so no poll interval was burned.
    # The poll loop is what a queuing request runs, and with the holder held for
    # this whole test such a request could not have answered at all -- so the
    # attempt count is the deterministic witness and the wall clock only has to
    # stay below "it clearly answered while the holder still rendered".
    assert attempts == ["op-zero"], (
        "the acquire was attempted again, so a queue poll was burned", attempts)
    assert elapsed < 5.0, (
        f"the refusal took {elapsed:.3f}s while the holder was still rendering")
    assert "op-zero" not in rig.ops_with_rows(), (
        "an owner row was made for a request refused before ownership")
    assert [op for _, op in rig.rendered] == ["op-holder"], rig.rendered


# ── a-post-ownership-lock-wait-is-bounded (postlock-wait) ───────────────────
# Once a request OWNS the render row it still meets two waits: the process render
# lock, then the cross-process render-effect flock. Both used to be unbounded, so
# an owner could sit there past its caller's socket. Each bound is a module
# constant; the tests set it small and drive the real `_Handler.do_POST` through
# `rig`. `render_wait_timeout_sec` is deliberately absent here: that limit bounds
# only the owner-row QUEUE wait and must not be charged for these waits.


def _assert_post_ownership_refusal(rig, out, blocked_by):
    """The shared truth every post-ownership bound must answer with."""
    assert out.get("code") == 409, (
        f"a request that owned the render and timed out on {blocked_by} must "
        f"answer 409, not hang or render: {out!r}")
    body = out["body"]
    assert body["error"] == "render owner wait timed out", body
    assert body["blocked_by"] == blocked_by, body
    assert "nothing was rendered" in body["detail"], body
    assert body["needs_reconciliation"] is False, body
    # The owner row was made (the request owned the render) and then released
    # with a reason that says it was NOT rendered -- not left active, not
    # reported as completed.
    rows = rig.settled_snapshot()
    mine = [r for r in rows if r["operation_id"] == "op-bounded"]
    assert len(mine) == 1, rows
    assert mine[0]["status"] == "released", mine
    assert (mine[0]["reason"] or "").startswith("not rendered"), mine
    assert blocked_by in (mine[0]["reason"] or ""), mine[0]["reason"]
    # No render body ran for this request.
    assert all(op != "op-bounded" for _, op in rig.rendered), rig.rendered


def test_a_held_process_render_lock_is_bounded_and_answers_not_rendered(rig, monkeypatch):
    """Process lock held by another thread; the request owns the row, then gives up."""
    gh = rig.gh
    monkeypatch.setattr(gh, "RENDER_PROCESS_LOCK_WAIT_SEC", 0.3)
    # Hold the process render lock from another thread, with no owner row, so
    # the request's own owner acquisition succeeds and it reaches the
    # process-lock wait.
    gate = threading.Event()
    held = threading.Event()

    def hold():
        gh._Handler._RENDER_LOCK.acquire()
        held.set()
        gate.wait(30)
        gh._Handler._RENDER_LOCK.release()

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert held.wait(10), "the test could not take the process render lock"
    started = time.monotonic()
    try:
        thread, out = rig.post_in_thread(
            "/script", rig.payload("op-bounded"), timeout=20)
        thread.join(15)
        elapsed = time.monotonic() - started
    finally:
        gate.set()
        holder.join(10)
    assert not thread.is_alive(), (
        f"the request did not answer while the process render lock was held "
        f"({elapsed:.1f}s): this is the unbounded wait")
    assert elapsed < 10.0, f"the bound of 0.3s took {elapsed:.2f}s to answer"
    _assert_post_ownership_refusal(rig, out, "process_render_lock")
    assert "process render lock" in out["body"]["detail"], out["body"]
    assert rig.rendered == [], rig.rendered


def test_a_held_render_effect_lock_is_bounded_and_answers_not_rendered(rig, monkeypatch):
    """Effect flock held through a SEPARATE descriptor in this process."""
    import fcntl

    gh = rig.gh
    monkeypatch.setattr(gh, "RENDER_EFFECT_LOCK_WAIT_SEC", 0.3)
    # flock belongs to the open file description, so a second descriptor opened
    # in this same process conflicts with it: the bounded wait really sees a
    # held lock. No second harness process is needed.
    path = gh.RENDER_EFFECT_LOCK
    holder_fd = open(path, "a+b")
    fcntl.flock(holder_fd.fileno(), fcntl.LOCK_EX)
    started = time.monotonic()
    try:
        thread, out = rig.post_in_thread(
            "/script", rig.payload("op-bounded"), timeout=20)
        thread.join(15)
        elapsed = time.monotonic() - started
    finally:
        fcntl.flock(holder_fd.fileno(), fcntl.LOCK_UN)
        holder_fd.close()
    assert not thread.is_alive(), (
        f"the request did not answer while the render-effect lock was held "
        f"({elapsed:.1f}s): this is the unbounded flock wait")
    assert elapsed < 10.0, f"the bound of 0.3s took {elapsed:.2f}s to answer"
    _assert_post_ownership_refusal(rig, out, "render_effect_lock")
    assert "render-effect lock" in out["body"]["detail"], out["body"]
    assert rig.rendered == [], rig.rendered
    # The process lock this request took must have been released again, or the
    # next render would wedge behind a lock nobody holds.
    assert not gh._Handler._RENDER_LOCK.locked(), (
        "the process render lock was left held after the effect-lock refusal")


@pytest.mark.parametrize("limit", ["absent", None])
def test_an_absent_or_null_limit_keeps_the_ordinary_bounded_queue(rig, limit):
    """The default path, unchanged: queue behind a held owner, then render."""
    holder_id = rig.start_holder()
    payload = rig.payload("op-default")
    if limit != "absent":
        payload["render_wait_timeout_sec"] = limit
    thread, out = rig.post_in_thread("/script", payload)
    assert rig.queued.wait(10), "the default-path request never met the live holder"
    time.sleep(0.3)
    assert thread.is_alive() and "code" not in out, (
        f"a request with limit {limit!r} answered while its holder still "
        f"rendered: {out!r}")
    rig.release.set()
    thread.join(30)
    rig.holder[0].join(30)
    assert out.get("code") == 200, out
    body = out["body"]
    assert body["render_owner_waited_for_owner_ids"] == [holder_id], body
    assert body["render_owner_wait_sec"] >= 0.3, body
    assert rig.rendered == [("/script", "op-holder"), ("/script", "op-default")], (
        "the default path must still queue and then render", rig.rendered)


# ── a-the-wait-budget-bounds-only-waiting-for-someone-else ──────────────────
# The defect (found in the 2026-11-09 whole-suite gate row):
# `render_wait_timeout_sec` bounds the QUEUE WAIT -- the seconds a request
# spends waiting for SOMEONE ELSE -- but
# `_acquire_render_owner_waiting_inner` started its clock immediately before
# the FIRST `acquire_render_owner(...)` call, so the request's own owner-table
# transaction (connect + `CREATE TABLE IF NOT EXISTS` + `BEGIN IMMEDIATE` +
# commit) was charged to its caller's budget. do_POST then re-checked
# `monotonic() - admission_started` AFTER the request already owned the render,
# and refused 409 `_wait_timed_out(..., owned=True)` -- `owner_kind: active`,
# `owner_id: null`, `waited_for_owner_ids: []` -- for a request nothing was
# ever in the way of. The gate row then read that 409 as "godot-builder
# unreachable" (exit 2) and reported a red for a render that never ran.
#
# Every test below drives the real served `do_POST` through `rig`. The slowness
# lives in the PROBE (a wrapper around the module's `acquire_render_owner`), so
# the accounting under test stays in the product code. The same probe file run
# against base 732fe4b757 is the other arm: there the two c1 cases answer 409.


class _Tee:
    """Record what the harness prints while forwarding it to the real stdout.

    `print(..., flush=True)` is the harness's only channel, and it writes from
    the server thread: replacing `sys.stdout` for the test records it whether
    pytest captures the run or is told not to (`-s`), and the forwarded copy is
    what a retained log shows.
    """

    def __init__(self, stream):
        self.stream = stream
        self.written: list[str] = []

    def write(self, text):
        self.written.append(text)
        return self.stream.write(text)

    def flush(self):
        self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)

    @property
    def text(self) -> str:
        return "".join(self.written)


def _slow_own_admission(gh, monkeypatch, delay, *, call=1, before=None) -> list[str]:
    """Make THIS request's own owner-table admission take `delay` seconds.

    Wraps the module's `acquire_render_owner` -- the call whose connect,
    `CREATE TABLE IF NOT EXISTS`, `BEGIN IMMEDIATE` and commit used to be
    charged to the caller's budget -- and sleeps on attempt number `call`,
    before delegating. The defect stays in the product code; only the clock is
    under test. `before()` runs at that same point, for a probe that must let a
    real holder go while the attempt is still in flight.
    """
    real_acquire = gh.acquire_render_owner
    attempts: list[str] = []

    def slow(project_id, run_id, operation_id, **kwargs):
        attempts.append(operation_id)
        if len(attempts) == call:
            if before is not None:
                before()
            time.sleep(delay)
        return real_acquire(project_id, run_id, operation_id, **kwargs)

    monkeypatch.setattr(gh, "acquire_render_owner", slow)
    return attempts


def _queue_lines(printed: str) -> list[str]:
    """The harness's `waited Ns for render owner(s) [...]` lines, nothing else.

    The refusal line the harness also prints spells its part `queued for render
    owner(s)`, so it is excluded by name: only the queue's own success line is
    what c3 is about.
    """
    return [line for line in printed.splitlines()
            if " for render owner(s) " in line and "queued for" not in line]


def _wait_for_row(rig, op: str, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if op in rig.ops_with_rows():
            return
        time.sleep(0.02)
    raise AssertionError(f"{op} never took ownership")


@pytest.mark.parametrize("delay", [0.3, 1.2], ids=["admission-0.3s", "admission-1.2s"])
def test_an_own_admission_transaction_is_not_charged_to_the_wait_budget(
        rig, monkeypatch, delay):
    """Budget 0.05s, own admission slowed past it, NOTHING in the way: 200.

    The same probe on base 732fe4b757 answered 409 `render owner wait timed
    out` -- `owner_kind: active`, `owner_id: null`, `waited_for_owner_ids: []`
    -- for a render whose lock was free. The 1.2s arm is the one that also
    printed `waited 1s for render owner(s) []`, because the harness only logs a
    queue wait above one second.
    """
    budget = 0.05
    attempts = _slow_own_admission(rig.gh, monkeypatch, delay)
    tee = _Tee(sys.stdout)
    monkeypatch.setattr(sys, "stdout", tee)
    started = time.monotonic()
    thread, out = rig.post_in_thread(
        "/script", rig.payload("op-own-slow", render_wait_timeout_sec=budget))
    thread.join(30)
    elapsed = time.monotonic() - started
    printed = tee.text
    body = out.get("body") or {}
    print(f"[c1] admission_delay_sec={delay} render_wait_timeout_sec={budget} "
          f"code={out.get('code')} waited_sec={body.get('render_owner_wait_sec')!r} "
          f"waited_for={body.get('render_owner_waited_for_owner_ids')!r} "
          f"queue_line={_queue_lines(printed)!r} wall_sec={elapsed:.3f}", flush=True)
    assert attempts == ["op-own-slow"], (
        "the probe's request made more than the one admission attempt a free "
        "render lock needs", attempts)
    assert elapsed >= delay, (
        f"the probe never slowed the admission: the request answered in "
        f"{elapsed:.3f}s")
    assert out.get("code") == 200, (
        f"a {delay}s own admission against a FREE render lock and a {budget}s "
        f"queue budget was not admitted: {out!r}")
    # The only wait this request had was its own admission transaction, and that
    # is not a queue wait: nothing was waited for, and the body says so.
    assert body["render_owner_wait_sec"] == 0.0, body
    assert body["render_owner_waited_for_owner_ids"] == [], body
    # ... and the render really ran, under an owner row released as completed.
    assert rig.rendered == [("/script", "op-own-slow")], rig.rendered
    assert [(r["operation_id"], r["status"], r["reason"] or "")
            for r in rig.settled_snapshot()] == [
        ("op-own-slow", "released", "completed")]
    # c3, the no-contention pole: no `waited Ns for render owner(s) []` line for
    # a wait that was only its own admission transaction -- not even on the arm
    # whose admission alone passed the one-second print threshold.
    assert _queue_lines(printed) == [], printed


def test_a_request_that_queues_past_its_budget_and_then_acquires_is_still_refused(
        rig, monkeypatch):
    """Budget 0.05s behind a LIVE holder, and the poll that wins lands after it.

    The guard this pins: taking ownership late does not admit the request. Its
    refusal names the holder it queued behind -- never `owner_kind: active`
    with an empty owner list.
    """
    budget = 0.05
    holder_id = rig.start_holder()

    def release_the_holder():
        # The holder goes while the request's SECOND admission attempt is in
        # flight, so that attempt wins -- after the budget has run out.
        rig.release.set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not [r for r in rig.gh.render_owner_snapshot()
                    if r["status"] in ("active", "owner_lost")]:
                return
            time.sleep(0.01)
        raise AssertionError("the holder's owner row was never released")

    attempts = _slow_own_admission(rig.gh, monkeypatch, 0.1, call=2,
                                   before=release_the_holder)
    thread, out = rig.post_in_thread(
        "/script", rig.payload("op-late", render_wait_timeout_sec=budget))
    thread.join(30)
    rig.holder[0].join(30)
    body = out.get("body") or {}
    print(f"[c2-late-poll] holder={holder_id} render_wait_timeout_sec={budget} "
          f"code={out.get('code')} waited_sec={body.get('waited_sec')!r} "
          f"owner_kind={body.get('owner_kind')!r} owner_id={body.get('owner_id')!r} "
          f"waited_for={body.get('waited_for_owner_ids')!r} "
          f"attempts={len(attempts)}", flush=True)
    assert len(attempts) == 2, (
        "the request did not queue: it must fail once behind the live holder "
        "and only then take ownership late", attempts)
    assert out.get("code") == 409, (
        f"a request that queued past its {budget}s budget and only then took "
        f"ownership was admitted: {out!r}")
    assert body["error"] == "render owner wait timed out", body
    assert body["owner_kind"] == "active", body
    assert body["owner_id"] == holder_id, body
    assert body["waited_for_owner_ids"] == [holder_id], body
    assert holder_id in body["detail"], body
    assert body["waited_sec"] > budget, body
    assert body["wait_timeout_sec"] == budget, body
    # The LATE refusal, not the queue loop's: it took ownership first.
    assert "took ownership only after the limit" in body["detail"], body
    # Nothing rendered, and the owner row it did take was released as not
    # rendered rather than left active or reported as completed.
    assert all(op != "op-late" for _, op in rig.rendered), rig.rendered
    assert [(r["status"], r["reason"] or "") for r in rig.settled_snapshot()
            if r["operation_id"] == "op-late"] == [
        ("released", "not rendered: render_wait_timeout_sec ran out before "
                     "the render started")]


def test_a_queued_request_refused_at_its_budget_names_the_holder(rig):
    """The queue loop's own refusal: budget 0.05s behind a live holder."""
    budget = 0.05
    holder_id = rig.start_holder()
    thread, out = rig.post_in_thread(
        "/script", rig.payload("op-queued", render_wait_timeout_sec=budget))
    thread.join(30)
    rig.release.set()
    rig.holder[0].join(30)
    body = out.get("body") or {}
    print(f"[c2-queue] holder={holder_id} render_wait_timeout_sec={budget} "
          f"code={out.get('code')} waited_sec={body.get('waited_sec')!r} "
          f"owner_kind={body.get('owner_kind')!r} owner_id={body.get('owner_id')!r} "
          f"waited_for={body.get('waited_for_owner_ids')!r}", flush=True)
    assert out.get("code") == 409, out
    assert body["error"] == "render owner wait timed out", body
    assert body["owner_kind"] == "active", body
    assert body["owner_id"] == holder_id, body
    assert body["waited_for_owner_ids"] == [holder_id], body
    assert holder_id in body["detail"], body
    assert body["waited_sec"] >= budget, body
    assert "nothing was rendered and no owner row was made" in body["detail"], body
    assert "op-queued" not in rig.ops_with_rows(), rig.ops_with_rows()
    assert [op for _, op in rig.rendered] == ["op-holder"], rig.rendered


def test_the_printed_queue_line_counts_the_holders_real_hold_time(rig, monkeypatch):
    """c3, the contended pole: the line appears, and its number is the hold.

    No queue limit here, so the request queues behind the holder and then
    renders; the harness logs its queue wait because it is above one second.
    """
    hold = 2.0
    holder_id = rig.start_holder()
    tee = _Tee(sys.stdout)
    monkeypatch.setattr(sys, "stdout", tee)
    thread, out = rig.post_in_thread("/script", rig.payload("op-after-hold"))
    assert rig.queued.wait(10), "the request never met the live holder"
    time.sleep(hold)
    rig.release.set()
    thread.join(30)
    rig.holder[0].join(30)
    printed = tee.text
    body = out.get("body") or {}
    lines = _queue_lines(printed)
    print(f"[c3] holder={holder_id} hold_sec={hold} code={out.get('code')} "
          f"waited_sec={body.get('render_owner_wait_sec')!r} "
          f"waited_for={body.get('render_owner_waited_for_owner_ids')!r} "
          f"queue_lines={lines!r}", flush=True)
    assert out.get("code") == 200, out
    waited = body["render_owner_wait_sec"]
    assert waited >= hold, (
        f"the recorded queue wait {waited!r}s does not cover the {hold}s the "
        f"holder really held the render")
    assert body["render_owner_waited_for_owner_ids"] == [holder_id], body
    assert len(lines) == 1, printed
    assert holder_id in lines[0], lines
    printed_sec = int(lines[0].split("waited ", 1)[1].split("s ", 1)[0])
    assert abs(printed_sec - waited) <= 1.0, (
        f"the printed queue wait {printed_sec}s is not the recorded {waited!r}s "
        f"(the line rounds to whole seconds)", lines)


def test_a_request_held_by_the_deployment_fence_names_the_fence(rig):
    """No owner exists and the fence is held: the refusal names the fence.

    `owner_kind: active` with an empty `waited_for_owner_ids` used to be the
    only answer available here, which reads as "a live owner" that the payload
    cannot point at.
    """
    import fcntl

    budget = 0.05
    stream = open(rig.gh.DEPLOYMENT_LOCK, "a+b")
    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
    try:
        code, body = rig.post(
            "/script", rig.payload("op-fenced", render_wait_timeout_sec=budget),
            timeout=15)
    finally:
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()
    print(f"[c2-fence] render_wait_timeout_sec={budget} code={code} "
          f"owner_kind={body.get('owner_kind')!r} owner_id={body.get('owner_id')!r} "
          f"waited_for={body.get('waited_for_owner_ids')!r} "
          f"deployment_fence_held={body.get('deployment_fence_held')!r} "
          f"waited_sec={body.get('waited_sec')!r}", flush=True)
    assert code == 409, (code, body)
    assert body["error"] == "render owner wait timed out", body
    assert body["deployment_fence_held"] is True, body
    assert body["waited_for_owner_ids"] == [], body
    assert body["owner_id"] is None, body
    assert body["owner_kind"] == "deployment_fence", (
        "a fence-held refusal must name the fence, not a live owner it cannot "
        "point at", body)
    assert "deployment-admission fence" in body["detail"], body
    assert body["waited_sec"] >= budget, body
    assert "op-fenced" not in rig.ops_with_rows(), rig.ops_with_rows()
    assert rig.rendered == [], rig.rendered


def test_a_post_ownership_lock_wait_names_the_lock_class_not_an_owner(rig):
    """Budget 0.3s, no queue wait at all: the EFFECT lock is held for 0.6s.

    A request that really waited for another render past its budget is still
    refused after it took ownership, and the refusal names the class of blocker
    that held it -- `post_ownership_lock` -- instead of `active` with an empty
    owner list.
    """
    import fcntl

    budget = 0.3
    holder_fd = open(rig.gh.RENDER_EFFECT_LOCK, "a+b")
    fcntl.flock(holder_fd.fileno(), fcntl.LOCK_EX)
    try:
        thread, out = rig.post_in_thread(
            "/script", rig.payload("op-lock-late", render_wait_timeout_sec=budget))
        _wait_for_row(rig, "op-lock-late")
        time.sleep(0.6)
    finally:
        fcntl.flock(holder_fd.fileno(), fcntl.LOCK_UN)
        holder_fd.close()
    thread.join(20)
    body = out.get("body") or {}
    print(f"[c2-lock] render_wait_timeout_sec={budget} code={out.get('code')} "
          f"owner_kind={body.get('owner_kind')!r} waited_for="
          f"{body.get('waited_for_owner_ids')!r} waited_sec="
          f"{body.get('waited_sec')!r}", flush=True)
    assert out.get("code") == 409, out
    assert body["error"] == "render owner wait timed out", body
    assert body["waited_for_owner_ids"] == [], body
    assert body["owner_kind"] == "post_ownership_lock", body
    assert body["waited_sec"] > budget, body
    assert "took ownership only after the limit" in body["detail"], body
    assert "owned the render row" in body["detail"], body
    assert all(op != "op-lock-late" for _, op in rig.rendered), rig.rendered
    assert [(r["status"], r["reason"] or "") for r in rig.settled_snapshot()
            if r["operation_id"] == "op-lock-late"] == [
        ("released", "not rendered: render_wait_timeout_sec ran out before "
                     "the render started")]
