"""The render routes serialise; the cheap headless routes do not.

Measured 2026-09-04: one unchanged tree swept 0 red alone and 6 red while a
second sweep ran, with disjoint red sets. Concurrency on the render path
turns the gate into a coin flip, so it is not allowed.

The second half of this file is about the one limit a caller may put on that
serialisation: `render_wait_timeout_sec` bounds the QUEUE WAIT. `0` (and a
negative value, which the request path clamps to 0) therefore means "may not
queue at all" -- not "may not render", which is what the code measured when it
charged the time spent AFTER ownership to the same limit.
"""
import ast
import pathlib
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
