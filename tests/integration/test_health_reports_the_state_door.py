"""`GET /health` must witness the State dispatch door, not only liveness.

On 2026-11-19 a defect in `api/state_verdict.py` made `binding_for` refuse EVERY
public action over `POST /api/state/query/{action}` for about 110 minutes (403
"state route declaration does not match its handler"). `/health` answered
`status: ok` the whole time: it reported liveness, a gate-export digest and a
source digest, but never asked the question the door asks. Nothing standing
noticed; an ad-hoc probe found it.

So `/health` gained one field, `state_door`, that asks that question WITHOUT
sending a request: per State route, the verdict the request-time guard
(`api/state_http._router_guard`) would reach for it. These tests execute the
property instead of describing it:

* the field's rows are derived from the SAME `binding_for(endpoint, action,
  path)` calls the guard makes - the expectation here is computed, never
  hand-written (`test_the_dispatch_rows_equal_the_guard_s_own_verdicts`);
* it is load-bearing: restoring the 2026-11-19 reader shape (a value
  constructor reached through a helper body the reader cannot follow) makes the
  field report the dispatch route refused AND the live door answer the existing
  403, in the same test, with the healthy answer pinned first as the control
  (`test_a_reader_that_cannot_explain_the_query_handler_is_reported_and_refused`);
* a non-dispatch route whose declaration contradicts its handler is NAMED, not
  averaged away (`test_a_route_whose_declaration_contradicts_its_handler_is_named`);
* the pre-existing keys keep their names and meanings
  (`test_the_existing_health_keys_keep_their_name_and_meaning`).

The pre-existing /health contracts are pinned next door and run unchanged beside
this file: `tests/unit/test_health_reports_the_gate_export.py`,
`tests/unit/test_health_reports_process_age.py`,
`tests/unit/test_health_source_identity_20261118.py` and
`tests/unit/test_health_answers_while_the_state_pool_is_saturated.py`.
"""
from __future__ import annotations

import api.main as main
from api import state_verdict
from api.state_verdict import (DECLARATION_ATTR, binding_for, route_verdict,
                               state_route_paths)
from core.state_commands import READ_REQUESTS, is_public_read

DISPATCH_PATH = "/api/state/query/{action}"
DISPATCH_URL = "/api/state/query/get_graph"
# The guard's refusal, verbatim (`api/state_http._guard_read_action`).
REFUSAL = "state route declaration does not match its handler"


def _public_actions() -> list:
    """The public actions over the one visibility table the guard consults."""
    return sorted(action for action in READ_REQUESTS if is_public_read(action))


def _dispatch_routes(app) -> dict:
    """The action-dispatch family, identified the way the guard identifies it.

    The guard reads a declaration's KIND before its action (`_router_guard`), so
    a `write`/`director` declaration with action None is judged by its own
    branch, not by `binding_for` on the URL action. A dispatch route is a
    `read` declaration with no action.
    """
    found = {}
    for path, route in state_route_paths(app).items():
        declaration = getattr(getattr(route, "endpoint", None), DECLARATION_ATTR, None)
        if (declaration is not None and declaration[0] == "read"
                and declaration[1] is None):
            found[path] = route
    return found


def _row(field: dict, path: str) -> dict:
    rows = [row for row in field["dispatch"] if row["path"] == path]
    assert len(rows) == 1, (path, field)
    return rows[0]


def _refused_actions(route) -> list:
    """What the guard's own call answers for every public action, derived here."""
    return [action for action in _public_actions()
            if not binding_for(route.endpoint, action, route.path).ok]


def test_the_dispatch_rows_equal_the_guard_s_own_verdicts(client):
    field = client.get("/health").json()["state_door"]
    rows = {row["path"]: row for row in field["dispatch"]}
    routes = _dispatch_routes(main.app)

    # The product must expose the family this field exists for; an empty row set
    # would make every assertion below vacuous.
    assert routes, "the real app must expose the action-dispatch family"
    assert set(rows) == set(routes), (sorted(rows), sorted(routes))

    any_refused = False
    for path, route in routes.items():
        expected = _refused_actions(route)
        assert rows[path]["refused_actions"] == expected, path
        assert rows[path]["verdict"] == ("refused" if expected else "ok"), path
        any_refused = any_refused or bool(expected)

    assert field["verdict"] == ("refused"
                                if any_refused or field["declaration_mismatch"]
                                else "ok")
    assert field["reason"] is None


def test_the_mismatch_list_is_the_routes_route_verdict_refuses(client):
    field = client.get("/health").json()["state_door"]
    dispatch = set(_dispatch_routes(main.app))
    expected = sorted(
        path for path, route in state_route_paths(main.app).items()
        if path not in dispatch
        and route_verdict(route) == "refused-declaration-mismatch")

    assert field["declaration_mismatch"] == expected


def test_a_route_whose_declaration_contradicts_its_handler_is_named(client):
    """The mismatch list is load-bearing: inject the defect it exists for."""
    app = main.app
    injected = "/api/state/health-door-mismatch"

    def dishonest():
        # Declares a public read, delivers no State action at all: the exact
        # shape `binding_for` refuses as a declaration mismatch.
        return {"detail": "nothing a declaration could bind"}

    dishonest._state_route = ("read", "get_graph")
    app.router.add_api_route(injected, dishonest, methods=["GET"])
    try:
        field = client.get("/health").json()["state_door"]
        assert injected in field["declaration_mismatch"], field
        assert field["verdict"] == "refused", field
    finally:
        app.router.routes = [route for route in app.router.routes
                             if getattr(route, "path", None) != injected]

    # And with the defect gone the field is back to judging what is really there.
    healed = client.get("/health").json()["state_door"]
    assert injected not in healed["declaration_mismatch"], healed


def test_a_reader_that_cannot_explain_the_query_handler_is_reported_and_refused(
        client, monkeypatch):
    """The witness: the field moves with the reader, and the door agrees.

    The control is pinned FIRST, so a suite that is already broken cannot make
    the mutated half pass for the wrong reason.
    """
    project_id = "health-door-witness"
    created = client.post("/api/state/commands/create_project",
                          json={"project_id": project_id, "title": project_id})
    assert created.status_code == 200, created.text
    opened = client.post("/api/state/commands/open_project",
                         json={"project_id": project_id})
    assert opened.status_code == 200, opened.text

    healthy = client.get("/health").json()["state_door"]
    row = _row(healthy, DISPATCH_PATH)
    assert row["verdict"] == "ok", healthy
    assert row["refused_actions"] == [], healthy
    served = client.post(DISPATCH_URL, json={"project_id": project_id})
    assert served.status_code == 200, (served.status_code, served.text[:300])

    # The 2026-11-19 shape, restored: a VALUE CONSTRUCTOR reached through a
    # helper body the reader cannot follow. `query` returns
    # `state_write_busy_response(exc)` from an `except` clause, and that
    # helper's body is `JSONResponse(...)`. Taking the response constructors out
    # of the reader's inert set is the defect whose absence turned every public
    # action on this route into "a delivered action is not a literal".
    monkeypatch.setattr(state_verdict, "_INERT_CALLABLES", ())
    # A rule change is invisible to the reader's own cache - unlike a rebound
    # global or a swapped body, which its identity checks catch - so the
    # mutation starts from a fresh read (a deployed rewrite starts from an empty
    # cache for the same reason).
    state_verdict._read_delivery.cache_clear()
    try:
        mutated = client.get("/health").json()["state_door"]
        row = _row(mutated, DISPATCH_PATH)
        assert row["verdict"] == "refused", mutated
        assert row["refused_actions"] == _refused_actions(
            state_route_paths(main.app)[DISPATCH_PATH]), mutated
        assert "get_graph" in row["refused_actions"], mutated
        assert mutated["verdict"] == "refused", mutated

        refused = client.post(DISPATCH_URL, json={"project_id": project_id})
        assert refused.status_code == 403, (refused.status_code, refused.text[:300])
        assert refused.json()["detail"] == REFUSAL
    finally:
        # The reader's cache is process-wide; leave it empty so no later test
        # reads a verdict derived under the mutated rule.
        state_verdict._read_delivery.cache_clear()


def test_the_existing_health_keys_keep_their_name_and_meaning(client):
    direct = main.health_check()
    over_http = client.get("/health").json()

    # The five keys that were there before this field, with the values and the
    # shapes they always had. Their deeper invariants are pinned next door; this
    # test exists so ADDING a field cannot quietly rename or repurpose one.
    for body in (direct, over_http):
        assert body["status"] == "ok"
        assert body["engine"] == "DPE SOTA v3.0"
        assert set(body["gate_harness"]) == {"path", "sha256", "reason"}
        assert set(body["process"]) == {"started_at", "uptime_seconds", "reason"}
        assert set(body["source_identity"]) == {"root", "digest", "captured_at",
                                                "files", "reason"}
        assert set(body["state_door"]) == {"verdict", "dispatch",
                                           "declaration_mismatch", "reason"}

    # The route and a caller with no event loop answer the same field (the
    # payload is plain data: no tuples, no non-JSON values).
    assert over_http["state_door"] == direct["state_door"]
    assert client.get("/health").json()["state_door"] == \
        client.get("/health").json()["state_door"]
