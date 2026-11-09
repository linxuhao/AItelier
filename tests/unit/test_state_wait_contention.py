"""Real SQLite/SkillFlow waits with a deterministic overlap in observation."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
import sys
import threading
import time

import pytest
from skillflow.core import SkillFlow, StepResult
from skillflow.graph import PipelineGraph, StepNode

from core import state_changes
from core.state_database import StateDatabase
from core.db_manager import DBManager
from core.state_service import StateService
from core.workspace_manager import WorkspaceManager


# One generous bound for every fixture handshake. These tests wait on real
# database work -- a correction page reconciles up to ten attempts -- so a
# correct page that is merely slow (a loaded host) must never decide a verdict.
# A handshake that never completes still fails, well inside this bound and with
# a message saying which wait it was. The same bound covers the must-complete
# watchdogs below: they await the very same work, so a smaller number would let
# a slow-but-correct page, not the claim under test, decide the verdict.
#
# Every must-complete site names itself and the bound it read when that bound
# expires (a bare asyncio.TimeoutError names neither, so a reader could not tell
# a product regression from a slow host). The bound stays a direct name at both
# the await and the message, never copied into another variable. The witnesses
# further down put HANDSHAKE_SECONDS in force at each of SITE_WITNESS_BOUNDS and
# check that the message reports the value in force. A message that hardcoded one
# literal passes only the arm carrying that literal and fails the other arm.
HANDSHAKE_SECONDS = 20.0


def handshake(event, name, purpose):
    """Block the calling thread until *event* is set, or fail naming the wait.

    Blocks, so call it from a worker thread (or through awaited_handshake()
    from async code, as the original waits did). Returns the seconds waited.
    """
    started = time.monotonic()
    observed = event.wait(HANDSHAKE_SECONDS)
    waited = time.monotonic() - started
    assert observed, (
        f"fixture handshake {name!r} never completed: waited {waited:.1f}s of a "
        f"{HANDSHAKE_SECONDS:.1f}s bound while awaiting {purpose}")
    return waited


async def awaited_handshake(event, name, purpose):
    """handshake() for an async caller: block in a worker thread, as before."""
    return await asyncio.to_thread(handshake, event, name, purpose)


def node(name):
    return {"key": name, "goal": name, "acceptance": [
        {"id": "check", "kind": "test", "description": "Synthetic check"}]}


@pytest.fixture
def system(tmp_path):
    sf = SkillFlow(str(tmp_path / "engine.sqlite"))
    sf.register_graph(PipelineGraph(name="work", begin="implement",
                                   steps=[StepNode(id="implement")]))
    service = StateService(DBManager(str(tmp_path / "state.sqlite")),
        WorkspaceManager(str(tmp_path / "workspaces")), sf, {}, project_read_trusted=True)
    for project in ("owner", "follow"):
        service.create_project(project, project)
        service.store.add_nodes(project, [node("a"), node("b")])
    owner, rid = workflow(service, "owner", "a")
    sf.pause_run(rid)  # Cached running, actual paused: recovery must consult SkillFlow.
    yield service, owner, rid
    assert str(service.db.db_path) not in state_changes._recovering
    assert str(service.db.db_path) not in state_changes._waiters
    sf._conn.close()


def workflow(service, project, key, state="running"):
    attempt = service.attempts.reserve(project, key, 1, "work", key)
    if state == "reserved":
        return attempt, None
    sf = service.sf
    rid = sf.create_run("work", project_id=attempt["execution_project_id"])
    sf.start_run(rid)
    service.attempts.bind_run(attempt["attempt_id"], rid, sf)
    if state in ("paused", "resumed", "candidate"):
        sf.pause_run(rid)
        service.attempts.reconcile(attempt["attempt_id"], sf)
        if state != "paused":
            sf.resume_run(rid)
    if state == "candidate":
        sf.advance_run(rid)
        step = sf.claim_next_step(rid)
        assert step is not None
        sf.confirm_step(step.token, StepResult(outputs={"implement": "synthetic"}))
        sf.advance_run(rid)
        service.attempts.reconcile(attempt["attempt_id"], sf)
    return service.attempts.get(attempt["attempt_id"]), rid


def cursor(service, project):
    return service.store.events(project)[-1]["seq"]


def other_service(service):
    return StateService(StateDatabase(service.db.db_path), service.ws, service.sf, {},
                        project_read_trusted=True)


def observe(service, attempt, status):
    body = json.dumps({"status": status, "settled": True, "usable": True}).encode()
    report = Path(service.db.db_path).parent / (attempt["attempt_id"] + ".json")
    report.write_bytes(body)
    service.external.observe(attempt["attempt_id"], "observed", 0, attempt["context_hash"],
        status, str(report), hashlib.sha256(body).hexdigest(), quiescent=status == "failed")


@asynccontextmanager
async def held_recovery(system, monkeypatch):
    service, owner, rid = system
    entered, release = threading.Event(), threading.Event()
    get_run = service.sf.get_run

    def held_get_run(run_id):
        if run_id == rid:
            entered.set()
            handshake(release, "release", "fixture must release the actual observer thread")
        return get_run(run_id)

    monkeypatch.setattr(service.sf, "get_run", held_get_run)
    owner_wait = asyncio.create_task(service.wait_for_state_change("owner",
        after=cursor(service, "owner"), attempt_ids=[owner["attempt_id"]],
        return_when_idle=True, timeout_seconds=3))
    try:
        await awaited_handshake(entered, "entered", "owner must hold real recovery")
        yield release
    finally:
        release.set()
        # Must complete: the owner wait is the work under test, and a page that
        # is correct but slow must not decide this verdict.
        try:
            result = await asyncio.wait_for(owner_wait, HANDSHAKE_SECONDS)
        except asyncio.TimeoutError as expired:
            raise AssertionError(
                f"must-complete wait 'held_recovery_owner_page' never completed within "
                f"{HANDSHAKE_SECONDS}s: the owner wait under test did not reach its "
                f"paused status after the fixture released the observer") from expired
        assert result["events"][-1]["payload"]["status"] == "paused"


@pytest.mark.asyncio
@pytest.mark.parametrize("project", ["owner", "follow"])
async def test_external_only_wait_survives_other_recovery_and_replays_own_event(system, monkeypatch, project):
    service = system[0]
    external = service.external.register(project, "b", 1, "synthetic", "worker", "external")
    after = cursor(service, project)
    async with held_recovery(system, monkeypatch):
        waiter = asyncio.create_task(other_service(service).wait_for_state_change(project,
            after=after, attempt_ids=[external["attempt_id"]], return_when_idle=True,
            timeout_seconds=2))
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(waiter), .12)
            observe(service, external, "failed")
            # Must complete: this waiter owns the observed event, and the page
            # behind it being slow is not a verdict on the replay.
            try:
                result = await asyncio.wait_for(waiter, HANDSHAKE_SECONDS)
            except asyncio.TimeoutError as expired:
                raise AssertionError(
                    f"must-complete wait 'external_only_replay' never completed within "
                    f"{HANDSHAKE_SECONDS}s: the external-only waiter did not replay its "
                    f"own observed event") from expired
            assert result["events"][-1]["payload"]["attempt_id"] == external["attempt_id"]
            assert result["events"][-1]["payload"]["status"] == "failed"
            assert result["next_after"] > after and "reason" not in result
            assert all(event["project_id"] == project for event in result["events"])
        finally:
            if not waiter.done():
                waiter.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiter


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["reserved", "resumed", "paused", "candidate", "empty"])
async def test_busy_timeout_cannot_authorize_cached_workflow_or_idle(system, monkeypatch, state):
    service = system[0]
    target = None if state == "empty" else workflow(service, "follow", "a", state)[0]
    scope = [target["attempt_id"]] if target else ["no-such-attempt"]
    before = service.attempts.get(target["attempt_id"]) if target else None
    after = cursor(service, "follow")
    async with held_recovery(system, monkeypatch):
        result = await other_service(service).wait_for_state_change("follow", after=after,
            attempt_ids=scope, return_when_idle=True, timeout_seconds=.12)
        assert result == {"events": [], "next_after": after, "timed_out": True}
        if target:
            assert service.attempts.get(target["attempt_id"]) == before


@pytest.mark.asyncio
async def test_busy_wait_cancellation_leaves_owner_recovery_intact(system, monkeypatch):
    service = system[0]
    called = threading.Event()
    recover = state_changes.recover_page

    def observed(*args):
        result = recover(*args)
        if args[1] == "follow":
            called.set()
        return result

    monkeypatch.setattr(state_changes, "recover_page", observed)
    async with held_recovery(system, monkeypatch):
        waiter = asyncio.create_task(other_service(service).wait_for_state_change("follow",
            after=cursor(service, "follow"), return_when_idle=True, timeout_seconds=2))
        await awaited_handshake(called, "called", "recovery must reach the follow project")
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert str(service.db.db_path) in state_changes._recovering
        assert len(state_changes._waiters[str(service.db.db_path)]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["paused", "candidate", "empty"])
async def test_quiet_recovery_release_allows_fresh_disposition(system, monkeypatch, state):
    service = system[0]
    target = None if state == "empty" else workflow(service, "follow", "a", state)[0]
    scope = [target["attempt_id"]] if target else ["no-such-attempt"]
    async with held_recovery(system, monkeypatch) as release:
        waiter = asyncio.create_task(other_service(service).wait_for_state_change("follow",
            after=cursor(service, "follow"), attempt_ids=scope, return_when_idle=True,
            timeout_seconds=3))
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(waiter), .12)
            release.set()
            # Must complete: the released waiter must reach its disposition.
            try:
                result = await asyncio.wait_for(waiter, HANDSHAKE_SECONDS)
            except asyncio.TimeoutError as expired:
                raise AssertionError(
                    f"must-complete wait 'quiet_recovery_release' never completed within "
                    f"{HANDSHAKE_SECONDS}s: the released waiter did not reach its "
                    f"disposition") from expired
            assert not result["timed_out"] and not result["events"]
            assert result["reason"] == ("nothing_to_wait" if state == "empty" else "action_required")
            if target:
                assert result["attempts"][0]["attempt_id"] == target["attempt_id"]
        finally:
            if not waiter.done():
                waiter.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiter


@pytest.mark.asyncio
async def test_pending_recovery_with_exhausted_budget_cannot_authorize_cached_idle(system, monkeypatch):
    # The first observation cycle is unconditional, but a page that returns the
    # pending marker (another observer holds this database) observed nothing:
    # recovery_ok stays None, so no cached status may authorize an idle
    # decision -- the caller's exhausted .04 s budget is still a timeout.
    service = system[0]
    scan = state_changes.scan

    def slow_scan(*args):
        result = scan(*args)
        threading.Event().wait(.08)  # The caller's budget is gone by now.
        return result

    monkeypatch.setattr(state_changes, "scan", slow_scan)
    after = cursor(service, "follow")
    async with held_recovery(system, monkeypatch):
        result = await other_service(service).wait_for_state_change("follow",
            after=after, return_when_idle=True, timeout_seconds=.04)
        assert result == {"events": [], "next_after": after, "timed_out": True}


@pytest.mark.asyncio
async def test_observed_page_with_exhausted_budget_returns_nothing_to_wait(system, monkeypatch):
    # A page that really was observed may authorize an immediate idle decision:
    # with no nonterminal attempt in scope, reason=nothing_to_wait returns at
    # once (timed_out=false). The slowed scan outlives the .04 s budget, so
    # only the unconditional first observation keeps the disposition honest.
    service = system[0]
    scan, recover = state_changes.scan, state_changes.recover_page
    pages = []

    def slow_scan(*args):
        result = scan(*args)
        threading.Event().wait(.08)
        return result

    def observed_page(*args):
        page_after, ok = recover(*args)
        if args[1] == "follow":
            pages.append(ok)
        return page_after, ok

    monkeypatch.setattr(state_changes, "scan", slow_scan)
    monkeypatch.setattr(state_changes, "recover_page", observed_page)
    after = cursor(service, "follow")
    result = await service.wait_for_state_change("follow", after=after,
        return_when_idle=True, timeout_seconds=.04)
    assert pages and all(ok is True for ok in pages), "the page must be observed, not skipped"
    assert result == {"events": [], "next_after": after, "timed_out": False,
                      "reason": "nothing_to_wait"}


@pytest.mark.asyncio
async def test_busy_nonzero_recovery_cursor_waits_without_hotspin_and_reaches_last_page(system, monkeypatch):
    service = system[0]
    service.store.add_nodes("follow", [node("n" + str(i)) for i in range(11)])
    runs = [workflow(service, "follow", "n" + str(i)) for i in range(11)]
    service.sf.pause_run(runs[-1][1])  # Actual last-page change, not a cached projection.
    after = cursor(service, "follow")
    page_done, return_page = threading.Event(), threading.Event()
    calls = []
    recover = state_changes.recover_page

    def observed(*args):
        result = recover(*args)
        if args[1] == "follow":
            calls.append(args[2])
            if len(calls) == 1:
                page_done.set()
                handshake(return_page, "return_page", "the first recovery page must be released")
        return result

    monkeypatch.setattr(state_changes, "recover_page", observed)
    waiter = asyncio.create_task(other_service(service).wait_for_state_change("follow",
        after=after, return_when_idle=True, timeout_seconds=3))
    try:
        await awaited_handshake(page_done, "page_done", "the first recovery page must complete")
        async with held_recovery(system, monkeypatch) as release:
            return_page.set()
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(waiter), .15)
            assert 2 <= len(calls) <= 4, "busy page must sleep rather than spin"
            assert calls[1] > 0, "fixture must exercise an existing page cursor"
            release.set()
        # Must complete: the released waiter has to reach the last page, however
        # long the pages it walked to get there took.
        try:
            result = await asyncio.wait_for(waiter, HANDSHAKE_SECONDS)
        except asyncio.TimeoutError as expired:
            raise AssertionError(
                f"must-complete wait 'slow_page_walk_last_page' never completed within "
                f"{HANDSHAKE_SECONDS}s: the released waiter did not reach the last of "
                f"the {len(calls)} recovery pages it walked") from expired
        assert result["events"][-1]["payload"]["attempt_id"] == runs[-1][0]["attempt_id"]
        assert result["events"][-1]["payload"]["status"] == "paused"
        assert "reason" not in result
    finally:
        return_page.set()
        if not waiter.done():
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter


@pytest.mark.asyncio
async def test_reentrant_observation_does_not_deadlock_or_lose_outer_result(system, monkeypatch):
    service, owner, rid = system
    get_run = service.sf.get_run
    calls = []

    def reentrant(run_id):
        if run_id == rid:
            calls.append(state_changes.recover_page(service, "follow", 0, None, None))
        return get_run(run_id)

    monkeypatch.setattr(service.sf, "get_run", reentrant)
    # Must complete: this is the reentrant product call itself. A correct page
    # that reconciles for a couple of seconds is not a deadlock, so it gets the
    # same bound as every other must-complete wait in this file.
    try:
        result = await asyncio.wait_for(service.wait_for_state_change("owner",
            after=cursor(service, "owner"), attempt_ids=[owner["attempt_id"]],
            return_when_idle=True, timeout_seconds=1), HANDSHAKE_SECONDS)
    except asyncio.TimeoutError as expired:
        raise AssertionError(
            f"must-complete wait 'reentrant_observation' never completed within "
            f"{HANDSHAKE_SECONDS}s: the reentrant product call did not return the "
            f"paused result it had already observed") from expired
    assert calls
    assert result["events"][-1]["payload"]["status"] == "paused"


@pytest.mark.asyncio
async def test_a_slow_but_correct_page_does_not_decide_the_wait_verdict(system, monkeypatch):
    # Every follow-project recovery page is correct but takes `delay` seconds.
    # The verdict must still be the product's own: the paused run found by
    # walking the pages, not a timeout and not a cached idle decision. The product
    # budget is far larger than the slow pages, so only the slowness is waited on;
    # the handshake bound is what fails a wait that never ends.
    service = system[0]
    delay, budget = 1.5, 30.0
    service.store.add_nodes("follow", [node("n" + str(i)) for i in range(11)])
    runs = [workflow(service, "follow", "n" + str(i)) for i in range(11)]
    service.sf.pause_run(runs[-1][1])  # Actual last-page change, not a cached projection.
    after = cursor(service, "follow")
    recover = state_changes.recover_page
    served = []

    def slow(*args):
        if args[1] != "follow":
            return recover(*args)
        started = time.monotonic()
        time.sleep(delay)
        result = recover(*args)  # The real page, served late.
        served.append(time.monotonic() - started)
        return result

    monkeypatch.setattr(state_changes, "recover_page", slow)
    waiter = asyncio.create_task(other_service(service).wait_for_state_change("follow",
        after=after, return_when_idle=True, timeout_seconds=budget))
    try:
        done, _ = await asyncio.wait({waiter}, timeout=HANDSHAKE_SECONDS)
        if not done:
            # A slow page is in flight and holds the recovery guard. Let it finish
            # within the wait's own budget, so no guard outlives this test.
            await asyncio.wait({waiter}, timeout=budget)
            pytest.fail(
                f"must-complete wait 'slow_pages' never completed within "
                f"{HANDSHAKE_SECONDS}s: the follow wait did not finish walking slow but "
                f"correct recovery pages ({len(served)} served, {delay:.1f}s each; "
                f"wait finished={waiter.done()})")
        result = waiter.result()
    finally:
        if not waiter.done():
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
    assert len(served) >= 2, "the wait must walk more than one slow page"
    assert all(seconds >= delay for seconds in served), served
    assert not result["timed_out"] and "reason" not in result
    assert result["events"][-1]["payload"]["attempt_id"] == runs[-1][0]["attempt_id"]
    assert result["events"][-1]["payload"]["status"] == "paused"


@pytest.mark.asyncio
async def test_an_expired_must_complete_wait_names_its_site_and_seconds(system, monkeypatch):
    # Witness for the messages the must-complete sites raise: shorten the bound in
    # force (the module constant), make the waiter the awaited site owns unable to
    # finish, and expire the REAL site instead of formatting the message somewhere
    # else. The site driven here is test_reentrant_observation_does_not_deadlock_or_
    # lose_outer_result's own must-complete wait -- called, not re-implemented, so
    # the try/except under test is the one this file ships. Neither that call nor
    # this test adds an asyncio.wait/wait_for site: the shape the bounds audit
    # counts is exactly the ten sites the module already had.
    module = sys.modules[__name__]
    scan, release = state_changes.scan, threading.Event()

    def stalled_scan(*args):
        # Every observation cycle a waiter needs begins with this scan; this one
        # does not return within the shortened bound, so the wait cannot either.
        release.wait(.5)
        return scan(*args)

    monkeypatch.setattr(state_changes, "scan", stalled_scan)
    monkeypatch.setattr(module, "HANDSHAKE_SECONDS", .05)
    try:
        with pytest.raises(AssertionError) as expired:
            await test_reentrant_observation_does_not_deadlock_or_lose_outer_result(
                system, monkeypatch)
        message = str(expired.value)
        assert "reentrant_observation" in message, message
        assert "0.05" in message, message
        assert isinstance(expired.value.__cause__, asyncio.TimeoutError), (
            f"the failure must come from the expired bound, got {expired.value!r}")
    finally:
        # The stalled scan finishes off the event loop; let it return now instead
        # of when its own wait elapses.
        release.set()


# --- one witness per must-complete site -------------------------------------
#
# A must-complete site owes the reader a message naming itself and the bound it
# read, and a message no case ever renders is not a diagnostic. Each case below
# drives ONE shipped site into that site's own handler and reads what the handler
# rendered: its own label, and the bound in force at that moment. None of them
# restates the message text -- each forces the site's own awaited work to expire
# (a recovery page that cannot return in time, a scan that cannot finish) and
# sets the module's HANDSHAKE_SECONDS to each bound in SITE_WITNESS_BOUNDS in turn.
# Two distinct bounds are what make the seconds in the message a reading rather
# than a coincidence: a literal written into one site's message matches the arm
# that carries its value and fails the other.
#
# Driving, never re-implementing: a witness calls the shipped test (or the shipped
# context manager) that owns its site, so the try/except under test is the one this
# file ships. A witness adds no asyncio.wait/wait_for site of its own -- every wait
# and every expired bound it exercises belongs to the shipped code, which is what
# tests/unit/test_state_wait_bounds.py audits.

# The shortened bounds the witnesses put in force, one arm each. They are not wait
# sites: each only replaces the module constant for the duration of one case. Two
# distinct values make the check a comparison: a site whose message hardcoded
# either literal would still pass its own arm but fail the other.
SITE_WITNESS_BOUNDS = (.5, .75)

# Safety valve for the worker threads a stall holds: long enough that no case can
# ever finish on it, short enough that a thread cannot outlive the run even if a
# witness forgets to release it. It bounds nothing under test -- the site's own
# HANDSHAKE_SECONDS is what expires -- and it is a threading wait, not an asyncio
# wait site, so the bounds audit neither sees nor classifies it.
WITNESS_STALL_SECONDS = 30


def expire_after_real_page(monkeypatch, project_id):
    """Make *project_id*'s recovery page raise TimeoutError after doing its work.

    The real page still runs, so every precondition the shipped test handshakes
    on still happens; only that page's return becomes the expiration the owning
    site's handler is written to catch -- the wait's own awaited work failing,
    not a shortened timer firing first.
    """
    page = state_changes.recover_page

    def expiring(*args, **kwargs):
        result = page(*args, **kwargs)
        if args[1] == project_id:
            raise asyncio.TimeoutError(
                f"witness: the {project_id} recovery page's bound expired")
        return result

    monkeypatch.setattr(state_changes, "recover_page", expiring)


async def witness_held_recovery_owner_page(system, monkeypatch):
    """held_recovery's own site: the owner wait it releases must complete."""
    expire_after_real_page(monkeypatch, "owner")
    async with held_recovery(system, monkeypatch):
        pass


async def witness_external_only_replay(system, monkeypatch):
    """test_external_only_wait_survives_...: the waiter owns an observed event."""
    expire_after_real_page(monkeypatch, "follow")
    await test_external_only_wait_survives_other_recovery_and_replays_own_event(
        system, monkeypatch, "follow")


async def witness_quiet_recovery_release(system, monkeypatch):
    """test_quiet_recovery_release_...: the released waiter reaches a disposition."""
    expire_after_real_page(monkeypatch, "follow")
    await test_quiet_recovery_release_allows_fresh_disposition(
        system, monkeypatch, "empty")


async def witness_slow_page_walk_last_page(system, monkeypatch):
    """test_busy_nonzero_recovery_cursor_...: the released waiter reaches its last page."""
    # That test asserts the waiter walked onto a second page before this site is
    # reached, so two pages are served for real and the scan after them stalls:
    # nothing bounds that scan, so the site's own bound is what expires.
    page, scan, release, served = (state_changes.recover_page, state_changes.scan,
                                   threading.Event(), [])

    def counting_page(*args, **kwargs):
        result = page(*args, **kwargs)
        if args[1] == "follow":
            served.append(args[2])
        return result

    def stalling_scan(*args, **kwargs):
        result = scan(*args, **kwargs)
        if args[1] == "follow" and len(served) >= 2:
            release.wait(WITNESS_STALL_SECONDS)
        return result

    monkeypatch.setattr(state_changes, "recover_page", counting_page)
    monkeypatch.setattr(state_changes, "scan", stalling_scan)
    try:
        await test_busy_nonzero_recovery_cursor_waits_without_hotspin_and_reaches_last_page(
            system, monkeypatch)
    finally:
        release.set()


async def witness_reentrant_observation(system, monkeypatch):
    """test_reentrant_observation_...: the reentrant product call must return."""
    release = threading.Event()
    # Every observation cycle begins with this scan; stalling it before the real
    # page runs means the reentrant call cannot return inside the bound.
    scan = state_changes.scan

    def stalling(*args, **kwargs):
        result = scan(*args, **kwargs)
        release.wait(WITNESS_STALL_SECONDS)
        return result

    monkeypatch.setattr(state_changes, "scan", stalling)
    try:
        await test_reentrant_observation_does_not_deadlock_or_lose_outer_result(
            system, monkeypatch)
    finally:
        release.set()


async def witness_slow_pages(system, monkeypatch):
    """test_a_slow_but_correct_page_...: the follow wait must reach its verdict."""
    # Its own slow pages (1.5 s each) outlast the shortened bound, and its own
    # `budget` drain then lets the waiter finish, so the shipped site fails by
    # its own rule rather than by anything this witness adds.
    await test_a_slow_but_correct_page_does_not_decide_the_wait_verdict(
        system, monkeypatch)


# Site label -> the shipped code that renders it. The keys ARE the labels the
# messages must name, so a renaming cannot silently leave a site unwitnessed.
SITE_WITNESSES = {
    "held_recovery_owner_page": witness_held_recovery_owner_page,
    "external_only_replay": witness_external_only_replay,
    "quiet_recovery_release": witness_quiet_recovery_release,
    "slow_page_walk_last_page": witness_slow_page_walk_last_page,
    "reentrant_observation": witness_reentrant_observation,
    "slow_pages": witness_slow_pages,
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "site,bound",
    [pytest.param(site, bound, id=f"{site}-{bound}")
     for site in SITE_WITNESSES for bound in SITE_WITNESS_BOUNDS])
async def test_every_must_complete_site_renders_its_own_label_and_bound(
        system, monkeypatch, site, bound):
    monkeypatch.setattr(sys.modules[__name__], "HANDSHAKE_SECONDS", bound)
    with pytest.raises((AssertionError, pytest.fail.Exception)) as expired:
        await SITE_WITNESSES[site](system, monkeypatch)
    message = str(expired.value)
    assert site in message, (
        f"the {site} witness expired its own site, but the message it read names "
        f"no such site: {message!r}")
    assert f"{bound}s" in message, (
        f"the {site} witness put {bound} in force, but the message "
        f"it read does not report that bound in seconds: {message!r}")
    named = [label for label in SITE_WITNESSES if label != site and label in message]
    assert not named, (
        f"the {site} witness read a message naming {named} instead of its own site: "
        f"{message!r}")
    if isinstance(expired.value, AssertionError):
        assert isinstance(expired.value.__cause__, asyncio.TimeoutError), (
            f"the failure must come from the expired bound, got "
            f"{expired.value!r} caused by {expired.value.__cause__!r}")
