"""The confidentiality verdict runs where the private read EXECUTES.

Criterion ``no-author-written-datum-decides-whether-to-judge``: an anonymous
caller that executes ANY non-public read (``read_visibility(action) !=
'public'``) on an OPENED project is refused, whatever route reaches it and
however the handler is written.

The actions under test are DERIVED from the action table
(``READ_REQUESTS`` filtered by ``read_visibility``), never from
``core.state_commands._handlers``. Each is driven with VALID arguments - a
trusted service runs the same arguments first and must answer, so an argument
error can never pass for a refusal - and each is reported as ``refused``,
``leaked`` or ``invalid``; ``invalid`` is a failure.

Three paths per action: ``execute``; the service method called directly (the
method is found by walking the anonymous service's own objects for the
execution-point decoration, not by reading the dispatch table); and HTTP
routes on a fresh app and on ``api.main.app`` that call the method without
``execute``.
"""
from __future__ import annotations

import asyncio
import inspect
import json

import pytest
from fastapi import Depends

import core.state_commands as state_commands
from core.state_commands import (READ_REQUESTS, REQUESTS, ProjectPrivate, execute,
                                 read_visibility)
from core.state_service import StateService
from tests.support import state_canaries as C

SECRET = "EXECUTION-POINT-SECRET-3f2a"

# Every non-public read, derived from the action table. Unknown actions never
# reach a handler (`execute` rejects them first), so this is the whole set.
NON_PUBLIC_READS = sorted(a for a in READ_REQUESTS if read_visibility(a) != "public")

# VALID arguments per non-public read. The keys are asserted against the derived
# set, so a non-public read added later cannot slip past this module without
# someone choosing its arguments - and `test_the_arguments_are_valid` proves a
# trusted caller gets an answer with them.
_ARGS = {
    "events": {"project_id": C.OPEN},
    "list_director_messages": {"project_id": C.OPEN},
    "project_visibility": {"project_id": C.OPEN},
    "wait_for_state_change": {"project_id": C.OPEN, "timeout_seconds": 0,
                              "actionable_only": False},
}


def _args(action):
    return dict(_ARGS[action])


def _settle(value):
    return asyncio.run(value) if inspect.isawaitable(value) else value


def _services(tmp_path):
    db = C.new_database(tmp_path, "execpoint.sqlite")
    return (StateService(db, actor="seeder", project_read_trusted=True),
            StateService(db, actor="anonymous", project_read_trusted=False))


def _classify(call):
    """``refused`` / ``leaked`` / ``invalid`` for one anonymous call."""
    try:
        result = _settle(call())
    except ProjectPrivate:
        return "refused", ""
    except Exception as exc:
        return "invalid", f"{type(exc).__name__}: {str(exc)[:120]}"
    if isinstance(result, dict) and "error" in result and len(result) <= 3:
        return "invalid", json.dumps(result)[:120]
    return "leaked", json.dumps(result, default=str)[:120]


def _report(label, outcomes):
    for action, (result, detail) in sorted(outcomes.items()):
        print(f"ACTION path={label} action={action} outcome={result} {detail}".rstrip())
    tally = {}
    for result, _ in outcomes.values():
        tally[result] = tally.get(result, 0) + 1
    print(f"TALLY path={label}: {len(outcomes)} non-public reads x 1 anonymous identity x "
          f"1 opened project = {len(outcomes)} calls; {sorted(tally.items())}")


def _decorated_targets(service):
    """action -> bound method, found by walking the service's own objects for
    the execution-point decoration (``__state_read_action__``)."""
    owners = [service] + [value for value in vars(service).values()
                          if hasattr(value, "__dict__") and not inspect.isroutine(value)]
    found = {}
    for owner in owners:
        for name in dir(type(owner)):
            member = getattr(type(owner), name, None)
            action = getattr(member, "__state_read_action__", None)
            if action is not None:
                found.setdefault(action, getattr(owner, name))
    return found


def _direct_kwargs(action):
    return REQUESTS[action].model_validate(_args(action)).model_dump()


class TestEveryNonPublicReadIsJudgedAtExecution:
    def test_the_argument_table_covers_every_non_public_read(self):
        print("NON_PUBLIC_READS =", NON_PUBLIC_READS)
        assert NON_PUBLIC_READS, "the derivation found no non-public read"
        assert sorted(_ARGS) == NON_PUBLIC_READS

    def test_the_arguments_are_valid(self, tmp_path):
        """Control: a TRUSTED caller gets an answer with the same arguments."""
        trusted, _ = _services(tmp_path)
        invalid = {}
        for action in NON_PUBLIC_READS:
            result, detail = _classify(lambda: execute(trusted, action, _args(action)))
            print(f"TRUSTED action={action} outcome={'answered' if result == 'leaked' else result}")
            if result != "leaked":
                invalid[action] = (result, detail)
        assert not invalid, f"these arguments do not produce an answer for a trusted caller: {invalid}"

    def test_through_execute(self, tmp_path):
        _, anonymous = _services(tmp_path)
        outcomes = {a: _classify(lambda a=a: execute(anonymous, a, _args(a)))
                    for a in NON_PUBLIC_READS}
        _report("execute", outcomes)
        assert all(r == "refused" for r, _ in outcomes.values()), {
            a: o for a, o in outcomes.items() if o[0] != "refused"}

    def test_the_method_called_directly(self, tmp_path):
        _, anonymous = _services(tmp_path)
        targets = _decorated_targets(anonymous)
        missing = sorted(set(NON_PUBLIC_READS) - set(targets))
        assert not missing, f"no decorated method found for: {missing}"
        outcomes = {a: _classify(lambda a=a: targets[a](**_direct_kwargs(a)))
                    for a in NON_PUBLIC_READS}
        _report("direct", outcomes)
        assert all(r == "refused" for r, _ in outcomes.values()), {
            a: o for a, o in outcomes.items() if o[0] != "refused"}

    def test_a_public_read_is_unaffected(self, tmp_path):
        _, anonymous = _services(tmp_path)
        assert "nodes" in execute(anonymous, "get_graph", {"project_id": C.OPEN})


class TestTheExecutePointVerdictItselfHasTeeth:
    def test_execute_refuses_before_any_handler_runs(self, tmp_path, monkeypatch):
        """Every handler replaced by one that returns a secret: only the verdict
        AT ``execute`` stands between the anonymous caller and that secret, so
        this assertion names each action whose execute-point verdict is gone.
        The public control proves the replaced handlers are the ones reached."""
        _, anonymous = _services(tmp_path)
        real = state_commands._handlers
        monkeypatch.setattr(state_commands, "_handlers", lambda service: {
            action: (lambda **kwargs: {"secret": SECRET}) for action in real(service)})
        control = execute(anonymous, "get_graph", {"project_id": C.OPEN})
        assert control == {"secret": SECRET}, "the replaced handler was not reached"
        leaked = []
        for action in NON_PUBLIC_READS:
            try:
                result = _settle(execute(anonymous, action, _args(action)))
            except ProjectPrivate:
                continue
            if SECRET in json.dumps(result, default=str):
                leaked.append(action)
        print("EXECUTE_POINT LEAKED =", leaked)
        assert not leaked, f"execute delivered these non-public reads to an anonymous caller: {leaked}"


# ---------------------------------------------------------------- over HTTP
@pytest.mark.parametrize("mode", ["fresh", "main"])
def test_a_route_that_bypasses_execute_is_refused(tmp_path, monkeypatch, mode):
    """A handler on the product ``get_service`` that calls the decorated method
    directly, never ``execute``, unguarded, on both apps."""
    from api.state_graph_routers import get_service
    C.arm(monkeypatch)
    db = C.new_database(tmp_path, f"execpoint-{mode}.sqlite")
    app = C.app_for(mode, db)
    client = C.client_for(app, mode)

    def bypass(action: str, svc=Depends(get_service)):
        return _settle(_decorated_targets(svc)[action](**_direct_kwargs(action)))

    outcomes = {}
    try:
        assert C.request_is_anonymous(app, client), "the per-request service is not anonymous"
        C.mount(app, "/execpoint-bypass/{action}", bypass, guarded=False)
        for action in NON_PUBLIC_READS:
            try:
                response = client.get(f"/execpoint-bypass/{action}")
                status, text = response.status_code, response.text
            except Exception as exc:
                status, text = "EXC", f"{type(exc).__name__}: {str(exc)[:120]}"
            outcomes[action] = ("refused" if status == 403 else
                                "leaked" if status == 200 else "invalid", f"{status} {text[:100]}")
    finally:
        C.unmount(app, bypass)
        C.release(app)
    _report(f"http-{mode}", outcomes)
    assert all(r == "refused" for r, _ in outcomes.values()), {
        a: o for a, o in outcomes.items() if o[0] != "refused"}


@pytest.mark.parametrize("mode", ["fresh", "main"])
def test_confidentiality_does_not_depend_on_the_route_reader(tmp_path, monkeypatch, mode):
    """The route-layer reader is neutered (``binding_for`` approves anything),
    so nothing at the route layer stops the handler; it bypasses ``execute``.
    A 403 can only come from the read itself."""
    import api.state_http as state_http
    from api.state_graph_routers import get_service
    from api.state_verdict import Binding
    C.arm(monkeypatch)
    monkeypatch.setattr(state_http, "binding_for", lambda *a, **k: Binding(True, "", frozenset()))
    db = C.new_database(tmp_path, f"neutered-{mode}.sqlite")
    app = C.app_for(mode, db)
    client = C.client_for(app, mode)

    def bypass(pid: str, svc=Depends(get_service)):
        return svc.director_messages.list_director_messages(pid)

    bypass._state_route = ("read", "get_graph")
    try:
        C.mount(app, "/api/state/reader-neutered/{pid}", bypass, guarded=True)
        response = client.get(f"/api/state/reader-neutered/{C.OPEN}")
    finally:
        C.unmount(app, bypass)
        C.release(app)
    print(f"NEUTERED app={mode} status={response.status_code} body={response.text[:100]!r}")
    assert response.status_code == 403, (response.status_code, response.text[:200])
    assert not C.leaked_tables(response.text)
