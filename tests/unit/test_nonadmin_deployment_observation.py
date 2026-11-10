"""A non-admin driver measures quiescence; an unreadable instrument names its cause.

Measured 2026-11-10: a platform director on a driver token
(`driver_whoami` -> `is_admin: false`) could never recreate the godot sidecar.
`cli/server.py::_live_runtime_observation` POSTs the runtime observation, and
`api/admin_routers.py` gated it on `local_admin_authority` -> 403 -> measurement
failure -> `core/deployment_quiescence.py` called that "not quiescent" and sent
the operator to audit projects that were not the cause.

Both real paths are driven here: the FastAPI route against a real DriverRegistry,
and the CLI client through that route. Only the HTTP transport, the credential
source, the registry seam and the store are replaced - each at its own edge - so
the authority line itself is what these witnesses measure.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from api import admin_routers, authz, dependencies
from cli import server
from core import deployment_quiescence as dq
from core import driver_credentials, drivers
from core.db_manager import DBManager
from core.state_database import StateDatabase
from skillflow.core import SkillFlow
from skillflow.graph import PipelineGraph, StepNode

PEPPER = "p" * 48
LEGACY = "legacy-admin-token-0123456789abcdef"
ROUTE = "/api/admin/deployment-runtime-observation"
# The sentence an operator must be able to read off a refused measurement.
CREDENTIAL_PHRASE = ("an active off-tunnel driver credential is required; "
                     "the admin role is not required")
CF = {"Cf-Ray": "abc-CDG"}


def _request(headers):
    """The credential the route saw, as the authority functions see it."""
    return type("Request", (), {"headers": Headers(headers), "cookies": {}})()


def _driver(token):
    return {"X-AItelier-Driver-Token": token}


@pytest.fixture
def live(tmp_path, monkeypatch):
    """Real initialized runtime plus the real route; no second runtime is created."""
    db = DBManager(str(tmp_path / "live.db"))
    sf = SkillFlow(str(tmp_path / "live-sf.db"))
    sf.register_graph(PipelineGraph(name="live", begin="work", steps=[StepNode(id="work")]))
    rid = sf.create_run("live", project_id="owned")
    sf.start_run(rid)
    monkeypatch.setattr(dependencies, "_skillflow_instance", sf)
    monkeypatch.setattr(dependencies, "db_instance", db)

    def forbidden():
        raise AssertionError("second composition root getter invoked")

    monkeypatch.setattr(dependencies, "get_skillflow", forbidden)
    monkeypatch.setattr(dependencies, "get_db_manager", forbidden)
    app = FastAPI()
    app.include_router(admin_routers.router)
    client = TestClient(app)
    yield sf, db, rid, client
    sf._conn.close()


@pytest.fixture
def driver_identity(tmp_path, monkeypatch):
    """The documented `authz.driver_registry` seam: a real registry, feature on."""
    registry = drivers.DriverRegistry(StateDatabase(str(tmp_path / "drivers.sqlite")), PEPPER)
    registry.seed(LEGACY)
    tokens = {"codex": registry.register("codex", "Codex", actor="t")["token"]}
    monkeypatch.setattr(authz, "driver_registry", lambda: registry)
    monkeypatch.setattr(authz, "ADMIN_TOKEN", LEGACY)
    return registry, tokens


@pytest.fixture
def wire_client(monkeypatch, live):
    """Replace only the HTTP transport, so the CLI client drives the real route."""
    route_client = live[3]

    class Client:
        def __init__(self, *args, **kwargs):
            self.headers = kwargs["headers"]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def post(self, path):
            assert path == ROUTE
            return route_client.post(path, headers=self.headers)

    monkeypatch.setattr(server.httpx, "Client", Client)
    return route_client


@pytest.fixture
def presented(monkeypatch):
    """Which credential the local CLI presents: the token-file seam, not the route."""

    def present(headers):
        monkeypatch.setattr(driver_credentials, "auth_headers", lambda: dict(headers))

    return present


# ── 1. a refused credential is an instrument failure, not non-quiescence ──────

@pytest.mark.parametrize("case", ["no-token", "unknown-token", "cloudflare-marked"])
def test_a_refused_credential_names_the_instrument_not_the_projects(
        live, driver_identity, wire_client, presented, case, tmp_path, monkeypatch):
    tokens = driver_identity[1]
    headers = {"no-token": {}, "unknown-token": _driver("aitd_unknown")}[case] \
        if case != "cloudflare-marked" else None
    if case == "cloudflare-marked":
        # The token itself is valid; the Cloudflare marker is what refuses it.
        headers = {**_driver(tokens["codex"]), **CF}
    presented(headers)

    with pytest.raises(RuntimeError) as client_refusal:
        server._live_runtime_observation("http://127.0.0.1:4444")
    client_message = str(client_refusal.value)
    assert ROUTE in client_message and "403" in client_message
    assert CREDENTIAL_PHRASE in client_message
    assert "not quiescent" not in client_message

    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    with pytest.raises(RuntimeError) as gate_refusal:
        server._require_deployment_clearance("restart")
    refusal = str(gate_refusal.value)
    assert "quiescence observation is unavailable" in refusal
    assert "measurement_failure remains an authoritative blocker" in refusal
    assert CREDENTIAL_PHRASE in refusal and "403" in refusal
    assert "aborted/unusable" in refusal
    assert "not quiescent" not in refusal
    journal = json.loads(dq.evidence_path().read_text())
    assert journal["latest"]["status"] == "aborted"
    assert journal["latest"]["usable"] is False
    assert journal["latest"]["blockers"]["measurement_failure"]
    assert not any(event["status"] in {"authorized", "overridden"}
                   for event in journal["events"])


# ── 2. an ACTIVE non-admin driver measures: the widening is what let it through ─

def test_active_nonadmin_driver_reads_exactly_the_live_observation(
        live, driver_identity, monkeypatch):
    sf, db, rid, route_client = live
    headers = _driver(driver_identity[1]["codex"])
    # The differential: this very request is NOT admin, and a 403 before the change.
    assert authz.local_admin_authority(_request(headers)) is False
    assert authz.local_driver_authority(_request(headers)) is True
    produced = []
    real = dq.runtime_observation

    def capture(**kwargs):
        value = real(**kwargs)          # the real function over the real singletons
        produced.append(value)
        return value

    monkeypatch.setattr(dq, "runtime_observation", capture)
    response = route_client.post(ROUTE, headers=headers)
    assert response.status_code == 200, response.text
    assert len(produced) == 1
    facts = produced[0]
    assert facts["runs"][0]["run_id"] == rid      # the live singletons, not a new runtime
    assert response.json() == facts
    assert response.json()["digest"] == dq._observation_digest(facts)


# ── 3./4. the credential must be ACTIVE and OFF-tunnel ───────────────────────

def test_suspended_and_unknown_driver_tokens_are_refused(live, driver_identity):
    registry, tokens = driver_identity
    route_client = live[3]
    assert route_client.post(ROUTE, headers=_driver(tokens["codex"])).status_code == 200
    registry.set_status("codex", "suspended", 1, "pause", actor="t")
    refused = route_client.post(ROUTE, headers=_driver(tokens["codex"]))
    assert refused.status_code == 403
    assert "runs" not in refused.json()
    assert route_client.post(ROUTE, headers=_driver("aitd_unknown")).status_code == 403
    assert route_client.post(ROUTE).status_code == 403


def test_a_valid_driver_token_through_cloudflare_is_refused(live, driver_identity):
    route_client = live[3]
    token = driver_identity[1]["codex"]
    for marker in (CF, {"Cf-Access-Jwt-Assertion": "jwt"}):
        refused = route_client.post(ROUTE, headers={**_driver(token), **marker})
        assert refused.status_code == 403
        assert "runs" not in refused.json()


# ── 5. a real active cross-project owner still refuses, and is named ─────────

def _sealed(*, godot_owners=(), errors=(), measurement_failure=None):
    blockers = dq._normalized_owner_blockers(
        runs=[], sidecar_owners=[], external_owners=[],
        registered_external_owners=[], godot_render_owners=list(godot_owners))
    blockers["checkout_leases"] = []
    if measurement_failure is not None:
        blockers["measurement_failure"] = [{"reason": measurement_failure}]
    observation = {
        "schema_version": dq.OBSERVATION_SCHEMA_VERSION,
        "observed_at": dq._now(),
        "projects": [], "runs": [], "resumable_runs": [],
        "sidecar_owners": [], "godot_render_owners": list(godot_owners),
        "external_owners": [], "registered_external_owners": [],
        "blockers": blockers, "errors": list(errors),
    }
    observation["quiescent"] = not errors and not any(blockers.values())
    observation["inventory_digest"] = dq._inventory_digest(observation)
    observation["digest"] = dq._observation_digest(observation)
    assert dq._validate_observation(observation) is None
    return observation


def _decision(observation):
    return {"action": "redeploy", "actor": "driver:director", "reason": "owned witness",
            "ticket": "owned-nonadmin-observation",
            "inventory_digest": observation["digest"],
            "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            "acknowledge_unknown": True, "godot_only": True}


def test_an_active_cross_project_owner_still_refuses_and_is_named(tmp_path):
    owner = {"owner_id": "render-a", "status": "active", "run_id": "run-foreign"}
    observation = _sealed(godot_owners=[owner])
    journal = tmp_path / "journal.json"
    with pytest.raises(dq.DeploymentBlocked) as refused:
        dq.authorize("redeploy", observation, journal=journal,
                     override=_decision(observation), godot_only=True)
    message = str(refused.value)
    assert "cannot bypass authoritative active owners" in message
    assert "render-a" in message                     # the owner, not only the inventory
    assert "godot_render_owners" in message
    assert "not quiescent" in message                # a MEASURED owner keeps that wording
    event = json.loads(journal.read_text())["latest"]
    assert event["status"] == "aborted" and event["usable"] is False
    assert event["blockers"] == observation["blockers"]


# ── 6. an unmeasured host never becomes quiescent ───────────────────────────

def test_an_unmeasurable_observation_can_never_be_authorized(tmp_path):
    observation = dq.failed_observation("owned instrument refused the measurement")
    assert observation["quiescent"] is False
    journal = tmp_path / "journal.json"
    with pytest.raises(dq.DeploymentBlocked) as refused:
        dq.authorize("redeploy", observation, journal=journal,
                     override=_decision(observation), godot_only=True)
    message = str(refused.value)
    assert "quiescence observation is unavailable" in message
    assert "measurement_failure remains an authoritative blocker" in message
    assert "owned instrument refused the measurement" in message
    assert "not quiescent" not in message
    latest = json.loads(journal.read_text())["latest"]
    assert latest["status"] == "aborted" and latest["usable"] is False
    assert latest["blockers"]["measurement_failure"]
    # A forged quiet flag is a structural contradiction, not a clearance.
    forged = {**observation, "quiescent": True}
    forged["digest"] = dq._observation_digest(forged)
    with pytest.raises(dq.DeploymentBlocked, match="observation is malformed"):
        dq.authorize("redeploy", forged, journal=tmp_path / "forged.json",
                     override=_decision(forged), godot_only=True)
    assert json.loads((tmp_path / "forged.json").read_text())["latest"]["status"] == "aborted"
